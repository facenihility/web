# -*- coding: utf-8 -*-
"""
硬件在环（HIL）接入网关。

目标：让真实硬件（边缘算力盒子 / 相机 / AGV / 机器人控制柜 / 交换机 / 传感器）
接入进来做半实物仿真测试。设计成**协议无关**的三段式：

    1. 遥测上行（设备 → 后端）
       · HTTP  POST /api/hw/telemetry          单条或批量 JSON
       · UDP   <udp_port>                      每行一个 JSON（低时延高频上报）
       · TCP   <tcp_port>                      每行一个 JSON，可双向（长连接设备）
       三者最终都走 ingest()，同一套校验与状态机。

    2. 决策下行（后端 → 设备）
       · GET /api/hw/commands?deviceId=…&wait=25  长轮询
       · POST /api/hw/ack                         确认/回报执行结果
       指令至少一次投递（未确认会按红投递窗口重发）。

    3. 在环模式
       · off    ：纯仿真，硬件只做登记（不上报也不影响）
       · shadow ：影子模式——硬件照常上报，但**不修改仿真**，只做「实测 vs 仿真」对照。
                  这是接入真实设备时应当先跑的模式，用来验证链路与量纲。
       · hil    ：真在环——实测的 GPU 利用率 / 链路速率 / 时延**覆盖**仿真内部状态，
                  调度器基于真实世界做决策；真实任务上报后进入同一调度管线，
                  决策再下发回设备执行。

安全设计（关键）：
    * 遥测有保鲜期（fresh_ms）：过期数据不再参与覆盖，避免用陈旧值决策。
    * 设备有离线判定（offline_ms）：掉线设备的覆盖自动失效，仿真回落到内部模型，
      即「硬件故障不会让仿真停摆」，不会出现用僵尸数据决策的情况。
    * 覆盖只作用于 util/utilS/lat/load 这些可测量，节点算力 cap、链路容量 cap 等
      物理参数仍由场景定义，避免误配置把模型改坏。
"""

from __future__ import annotations

import json
import socketserver
import sys
import threading
import time
import uuid

from . import engine

MODES = ("off", "shadow", "hil")
MODE_LABELS = {"off": "纯仿真（硬件仅登记）", "shadow": "影子模式（只对照不干预）",
               "hil": "硬件在环（实测覆盖仿真状态）"}

DEFAULT_OFFLINE_MS = 3000      # 超过该时长无遥测 → 判定离线，覆盖失效
DEFAULT_FRESH_MS = 2000        # 遥测保鲜期，过期不参与覆盖
DEFAULT_REDELIVER_MS = 3000    # 指令未确认的重投窗口
MAX_COMMANDS_PER_DEVICE = 200
MAX_INFLIGHT = 100

ROLE_BY_KIND = {
    "edge": "compute", "node": "compute", "server": "compute", "gpu": "compute",
    "switch": "link", "gateway": "link", "router": "link", "tsn": "link",
    "camera": "endpoint", "robot": "endpoint", "agv": "endpoint", "sensor": "endpoint",
}

NODE_IDS = {n["id"] for n in engine.NODE_DEFS}
LINK_IDS = {l["id"] for l in engine.LINK_DEFS}


def _num(v):
    """把上报值安全转成 float（容忍字符串 / 百分号 / 空值）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%")
        if not v:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ratio(v):
    """利用率归一化：容忍 0-1 与 0-100 两种口径，以及 0-1 之外的越界值。"""
    x = _num(v)
    if x is None:
        return None
    if x > 1.5:
        x = x / 100.0
    return max(0.0, min(1.0, x))


class Device(object):
    """一台接入的硬件设备。"""

    def __init__(self, device_id, kind="custom", role=None, name=None, node_id=None,
                 meta=None, token=None):
        self.id = device_id
        self.kind = kind or "custom"
        self.role = role or ROLE_BY_KIND.get(self.kind, "endpoint")
        self.name = name or device_id
        self.node_id = node_id
        self.meta = meta or {}
        self.token = token or uuid.uuid4().hex[:12]
        self.first_seen = time.time()
        self.last_seen = 0.0            # monotonic
        self.last_seen_wall = 0.0
        self.telemetry = 0
        self.seq = None
        self.transport = {}
        self.rate = 0.0                 # 上报频率（EMA，条/秒）
        self._rate_t = time.monotonic()
        self._rate_n = 0
        self.errors = []
        self.warnings = []
        self.real_nodes = {}            # nodeId → 实测记录
        self.real_links = {}            # linkId → 实测记录
        self.tasks_reported = 0
        self.commands = []              # 待投递
        self.inflight = {}              # cmdId → {cmd, sent, tries}
        self.commands_sent = 0
        self.commands_acked = 0
        self.commands_failed = 0
        self.log = []
        self._was_online = False
        self.ever_online = False

    # ------------------------------------------------------------------ 内部
    def note(self, msg):
        self.log.append({"t": time.strftime("%H:%M:%S"), "msg": msg})
        if len(self.log) > 30:
            self.log.pop(0)

    def mark(self):
        now = time.monotonic()
        self._rate_n += 1
        if now - self._rate_t >= 1.0:
            inst = self._rate_n / (now - self._rate_t)
            self.rate = inst if self.rate == 0 else self.rate * 0.6 + inst * 0.4
            self._rate_t = now
            self._rate_n = 0
        self.last_seen = now
        self.last_seen_wall = time.time()
        self.telemetry += 1

    def online(self, now, offline_ms):
        return (now - self.last_seen) * 1000.0 <= offline_ms

    def age_ms(self, now):
        return 0.0 if not self.last_seen else (now - self.last_seen) * 1000.0

    def info(self, now, offline_ms, fresh_ms):
        return {
            "deviceId": self.id, "kind": self.kind, "role": self.role, "name": self.name,
            "nodeId": self.node_id, "token": self.token, "meta": self.meta,
            "online": self.online(now, offline_ms), "everOnline": self.ever_online,
            "lastSeenMs": round(self.age_ms(now), 1) if self.last_seen else None,
            "firstSeen": time.strftime("%H:%M:%S", time.localtime(self.first_seen)),
            "telemetry": self.telemetry, "rate": round(self.rate, 1), "seq": self.seq,
            "transport": dict(self.transport),
            "tasksReported": self.tasks_reported,
            "commands": {"pending": len(self.commands), "inflight": len(self.inflight),
                         "sent": self.commands_sent, "acked": self.commands_acked,
                         "failed": self.commands_failed},
            "real": {
                "nodes": {k: _strip(v, now, fresh_ms) for k, v in self.real_nodes.items()},
                "links": {k: _strip(v, now, fresh_ms) for k, v in self.real_links.items()},
            },
            "errors": self.errors[-5:], "warnings": self.warnings[-5:], "log": self.log[-8:],
        }


def _strip(rec, now, fresh_ms):
    """把实测记录转成可 JSON 化的对照信息（含新鲜度）。"""
    out = {k: v for k, v in rec.items() if k not in ("recv", "raw")}
    out["ageMs"] = round((now - rec["recv"]) * 1000.0, 1)
    out["fresh"] = out["ageMs"] <= fresh_ms
    return out


class HardwareGateway(object):
    """设备注册表 + 遥测摄入 + HIL 覆盖 + 指令队列。线程安全。"""

    def __init__(self, mode="off", offline_ms=DEFAULT_OFFLINE_MS, fresh_ms=DEFAULT_FRESH_MS,
                 redeliver_ms=DEFAULT_REDELIVER_MS, max_devices=64):
        self.lock = threading.RLock()
        self.devices = {}
        self.mode = mode if mode in MODES else "off"
        self.offline_ms = offline_ms
        self.fresh_ms = fresh_ms
        self.redeliver_ms = redeliver_ms
        self.max_devices = max_devices
        self.telemetry_total = 0
        self.telemetry_errors = 0
        self.last_ingest = 0.0
        self.started = time.time()
        self.cmd_seq = 0
        self.events = []
        # 由 hub 注入：真实任务 → 派发决策
        self.on_tasks = None
        self.applied = {"nodes": 0, "links": 0, "lastAt": 0.0}

    # ------------------------------------------------------------ 事件与日志
    def event(self, kind, msg, device=None):
        e = {"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg,
             "deviceId": device.id if device else None}
        self.events.append(e)
        if len(self.events) > 100:
            self.events.pop(0)
        return e

    def _watchdog(self, dev, now):
        on = dev.online(now, self.offline_ms)
        if on and not dev._was_online:
            dev.ever_online = True
            dev.note("上线")
            self.event("online", "%s 上线（%s）" % (dev.name, dev.id), dev)
        elif not on and dev._was_online:
            dev.note("离线：遥测超时，覆盖已失效")
            self.event("offline", "%s 离线，实测覆盖失效，仿真回落到内部模型" % dev.id, dev)
        dev._was_online = on
        return on

    # ---------------------------------------------------------------- 注册
    def register(self, body):
        dev_id = body.get("deviceId") or body.get("device_id")
        if not dev_id:
            raise ValueError("缺少 deviceId")
        with self.lock:
            if dev_id not in self.devices and len(self.devices) >= self.max_devices:
                raise ValueError("设备数已达上限 %d" % self.max_devices)
            dev = self.devices.get(dev_id)
            fresh = dev is None
            if fresh:
                dev = Device(dev_id, kind=body.get("kind", "custom"), role=body.get("role"),
                             name=body.get("name"), node_id=body.get("nodeId"),
                             meta=body.get("meta"))
                self.devices[dev_id] = dev
                self.event("register", "注册设备 %s (%s/%s)" % (dev_id, dev.kind, dev.role), dev)
                dev.note("注册成功")
            else:
                for k, v in (("kind", body.get("kind")), ("role", body.get("role")),
                             ("name", body.get("name")), ("nodeId", body.get("nodeId")),
                             ("meta", body.get("meta"))):
                    if v:
                        setattr(dev, k if k != "nodeId" else "node_id", v)
            dev.mark()
            self._watchdog(dev, time.monotonic())
            return {
                "deviceId": dev.id, "token": dev.token, "role": dev.role, "kind": dev.kind,
                "registered": fresh,
                "config": {
                    "reportIntervalMs": 200, "heartbeatMs": 1000,
                    "offlineMs": self.offline_ms, "freshMs": self.fresh_ms,
                    "mode": self.mode, "schemaVersion": "1.0",
                },
                "endpoints": {
                    "telemetryHttp": "/api/hw/telemetry", "commands": "/api/hw/commands",
                    "ack": "/api/hw/ack", "schema": "/api/hw/schema",
                },
                "validNodeIds": sorted(NODE_IDS), "validLinkIds": sorted(LINK_IDS),
                "serverTimeMs": int(time.time() * 1000),
            }

    # ---------------------------------------------------------------- 摄入
    def ingest(self, body, source="http"):
        """接收一条或多条遥测。返回 {accepted, rejected, tasks, errors}。"""
        items = body if isinstance(body, list) else [body]
        accepted, rejected, errors = 0, 0, []
        decisions = []
        now = time.monotonic()
        with self.lock:
            for item in items:
                if not isinstance(item, dict):
                    rejected += 1
                    errors.append("非对象条目")
                    continue
                try:
                    dec = self._ingest_one(item, source, now)
                    decisions.extend(dec)
                    accepted += 1
                except ValueError as exc:
                    rejected += 1
                    errors.append(str(exc))
            self.telemetry_total += accepted
            self.telemetry_errors += rejected
            if accepted:
                self.last_ingest = now
        return {"accepted": accepted, "rejected": rejected, "errors": errors[:8],
                "dispatched": decisions}

    def _ingest_one(self, item, source, now):
        dev_id = item.get("deviceId") or item.get("device_id")
        if not dev_id:
            raise ValueError("缺少 deviceId")
        dev = self.devices.get(dev_id)
        if dev is None:
            # 允许「先上报后登记」，方便硬件直接接入
            body = dict(item)
            body["deviceId"] = dev_id
            self.register(body)
            dev = self.devices[dev_id]
        dev.mark()
        dev.transport[source] = dev.transport.get(source, 0) + 1
        if item.get("seq") is not None:
            dev.seq = item.get("seq")
        if item.get("heartbeat"):
            self._watchdog(dev, now)
            return []

        # --- 节点遥测 ---
        for n in _as_list(item.get("node")) + _as_list(item.get("nodes")):
            self._ingest_node(dev, n, now)
        # --- 链路遥测 ---
        for l in _as_list(item.get("link")) + _as_list(item.get("links")):
            self._ingest_link(dev, l, now)
        # --- 边缘侧自带聚合指标（可选）---
        if item.get("metrics"):
            dev.meta.setdefault("metrics", {}).update(item["metrics"])

        # --- 真实任务上报 → 交给上层注入同一调度管线 ---
        tasks = _as_list(item.get("tasks")) + _as_list(item.get("task"))
        if tasks:
            dev.tasks_reported += len(tasks)
            if self.on_tasks:
                decisions = self.on_tasks(dev, tasks)
                for d in decisions or []:
                    cmd = self.push_command(d["deviceId"], "dispatch", d["payload"],
                                            ttl_ms=8000)
                    if cmd:
                        d["commandId"] = cmd["id"]
                return decisions or []
        return []

    def _ingest_node(self, dev, payload, now):
        if not isinstance(payload, dict):
            raise ValueError("node 遥测必须是对象")
        nid = payload.get("id") or payload.get("nodeId") or dev.node_id
        if not nid:
            raise ValueError("node 遥测缺少 nodeId（且设备未登记默认节点）")
        if nid not in NODE_IDS:
            msg = "未知 nodeId=%s（可选 %s）" % (nid, ",".join(sorted(NODE_IDS)))
            if msg not in dev.errors:
                dev.errors.append(msg)
            raise ValueError(msg)
        rec = {
            "nodeId": nid,
            "util": _ratio(payload.get("gpuUtil", payload.get("util"))),
            "tflops": _num(payload.get("tflops", payload.get("throughputTflops"))),
            "queueDepth": _num(payload.get("queueDepth", payload.get("queue"))),
            "memPct": _ratio(payload.get("memPct", payload.get("mem"))),
            "powerW": _num(payload.get("powerW", payload.get("power"))),
            "tempC": _num(payload.get("tempC", payload.get("temp"))),
            "latMs": _num(payload.get("latMs", payload.get("rttMs"))),
        }
        # 增量合并：设备可能分频上报不同字段，缺失字段沿用上一次的值，
        # 否则一条只带 gpuUtil 的上报会把此前的实测时延清空。
        old = dev.real_nodes.get(nid) or {}
        merged = dict(old)
        for k, v in rec.items():
            if v is not None:
                merged[k] = v
            elif k not in merged:
                merged[k] = None
        merged.update({"nodeId": nid, "recv": now, "deviceId": dev.id, "wall": time.time()})
        dev.real_nodes[nid] = merged

    def _ingest_link(self, dev, payload, now):
        if not isinstance(payload, dict):
            raise ValueError("links 条目必须是对象")
        lid = payload.get("id") or payload.get("linkId") or dev.node_id
        if lid not in LINK_IDS:
            msg = "未知 linkId=%s（可选 %s）" % (lid, ",".join(sorted(LINK_IDS)))
            if msg not in dev.errors:
                dev.errors.append(msg)
            raise ValueError(msg)
        tx = _num(payload.get("txGbps", payload.get("tx")))
        rx = _num(payload.get("rxGbps", payload.get("rx")))
        util = _ratio(payload.get("util"))
        if util is None and (tx is not None or rx is not None):
            cap = next(l["cap"] for l in engine.LINK_DEFS if l["id"] == lid)
            util = max(0.0, min(1.0, ((tx or 0.0) + (rx or 0.0)) / max(cap, 1e-9)))
        dev.real_links[lid] = _merge_link(dev.real_links.get(lid), {
            "linkId": lid, "util": util, "txGbps": tx, "rxGbps": rx,
            "rttMs": _num(payload.get("rttMs", payload.get("latMs"))),
            "lossPct": _num(payload.get("lossPct", payload.get("loss"))),
            "jitterMs": _num(payload.get("jitterMs")),
            "recv": now, "deviceId": dev.id, "wall": time.time(),
        })

    # ------------------------------------------------------------ HIL 覆盖
    def overrides(self):
        """汇总所有**在线且新鲜**的实测值（多设备取均值，速率取最大）。"""
        now = time.monotonic()
        nodes, links = {}, {}
        with self.lock:
            for dev in self.devices.values():
                self._watchdog(dev, now)
                if not dev.online(now, self.offline_ms):
                    continue
                for nid, rec in dev.real_nodes.items():
                    if (now - rec["recv"]) * 1000.0 > self.fresh_ms:
                        continue
                    agg = nodes.setdefault(nid, {"util": [], "latMs": [], "tflops": [],
                                                 "queueDepth": [], "src": [], "ageMs": 0.0})
                    if rec.get("util") is not None:
                        agg["util"].append(rec["util"])
                    if rec.get("latMs") is not None:
                        agg["latMs"].append(rec["latMs"])
                    if rec.get("tflops") is not None:
                        agg["tflops"].append(rec["tflops"])
                    if rec.get("queueDepth") is not None:
                        agg["queueDepth"].append(rec["queueDepth"])
                    agg["src"].append(dev.id)
                    agg["ageMs"] = max(agg["ageMs"], (now - rec["recv"]) * 1000.0)
                for lid, rec in dev.real_links.items():
                    if (now - rec["recv"]) * 1000.0 > self.fresh_ms:
                        continue
                    agg = links.setdefault(lid, {"util": [], "tx": [], "rx": [], "rttMs": [],
                                                 "lossPct": [], "src": [], "ageMs": 0.0})
                    if rec.get("util") is not None:
                        agg["util"].append(rec["util"])
                    if rec.get("txGbps") is not None:
                        agg["tx"].append(rec["txGbps"])
                    if rec.get("rxGbps") is not None:
                        agg["rx"].append(rec["rxGbps"])
                    if rec.get("rttMs") is not None:
                        agg["rttMs"].append(rec["rttMs"])
                    if rec.get("lossPct") is not None:
                        agg["lossPct"].append(rec["lossPct"])
                    agg["src"].append(dev.id)
                    agg["ageMs"] = max(agg["ageMs"], (now - rec["recv"]) * 1000.0)
        out_n, out_l = {}, {}
        for nid, a in nodes.items():
            out_n[nid] = {
                "util": sum(a["util"]) / len(a["util"]) if a["util"] else None,
                "latMs": sum(a["latMs"]) / len(a["latMs"]) if a["latMs"] else None,
                "tflops": sum(a["tflops"]) if a["tflops"] else None,
                "queueDepth": sum(a["queueDepth"]) if a["queueDepth"] else None,
                "src": a["src"], "ageMs": round(a["ageMs"], 1),
            }
        for lid, a in links.items():
            out_l[lid] = {
                "util": sum(a["util"]) / len(a["util"]) if a["util"] else None,
                "txGbps": max(a["tx"]) if a["tx"] else None,
                "rxGbps": max(a["rx"]) if a["rx"] else None,
                "rttMs": sum(a["rttMs"]) / len(a["rttMs"]) if a["rttMs"] else None,
                "lossPct": max(a["lossPct"]) if a["lossPct"] else None,
                "src": a["src"], "ageMs": round(a["ageMs"], 1),
            }
        return out_n, out_l

    def apply_overrides(self, sims):
        """把实测值写入仿真体（仅 util/utilS/lat/load 等可测量）。"""
        if self.mode != "hil":
            return {"applied": False, "mode": self.mode}
        nodes, links = self.overrides()
        n_ok = l_ok = 0
        for s in (sims.values() if isinstance(sims, dict) else sims):
            for n in s.nodes:
                n.hwOverride = False
                n.hwSrc = ""
            for l in s.links:
                l.hwOverride = False
                l.hwSrc = ""
            for nid, ov in nodes.items():
                nd = s.nodeById.get(nid)
                if nd is None:
                    continue
                if ov["util"] is not None:
                    nd.util = ov["util"]
                    nd.utilS = ov["util"]
                    nd.hwOverride = True
                    nd.hwSrc = ",".join(ov["src"])
                if ov["latMs"] is not None:
                    nd.lat = max(0.0, ov["latMs"])
                n_ok += 1
            for lid, ov in links.items():
                lk = s.linkById.get(lid)
                if lk is None:
                    continue
                if ov["util"] is not None:
                    lk.util = ov["util"]
                    lk.utilS = ov["util"]
                    lk.hwOverride = True
                    lk.hwSrc = ",".join(ov["src"])
                    if ov["txGbps"] is not None or ov["rxGbps"] is not None:
                        lk.load = (ov["txGbps"] or 0.0) + (ov["rxGbps"] or 0.0)
                l_ok += 1
        self.applied = {"nodes": n_ok, "links": l_ok, "nodesUnique": len(nodes),
                        "linksUnique": len(links), "lastAt": time.time()}
        return {"applied": True, "nodes": n_ok, "links": l_ok,
                "nodesUnique": len(nodes), "linksUnique": len(links)}

    # ---------------------------------------------------------------- 指令
    def push_command(self, device_id, ctype, payload=None, ttl_ms=8000):
        with self.lock:
            dev = self.devices.get(device_id)
            if dev is None:
                return None
            self.cmd_seq += 1
            cmd = {"id": "c%d" % self.cmd_seq, "type": ctype, "payload": payload or {},
                   "ts": time.time(), "ttlMs": ttl_ms}
            dev.commands.append(cmd)
            if len(dev.commands) > MAX_COMMANDS_PER_DEVICE:
                dev.commands = dev.commands[-MAX_COMMANDS_PER_DEVICE:]
            return cmd

    def poll_commands(self, device_id, wait_ms=0, limit=20):
        """长轮询取指令；未确认的指令超过重投窗口会重新投递（至少一次语义）。"""
        deadline = time.monotonic() + max(0.0, wait_ms) / 1000.0
        while True:
            with self.lock:
                dev = self.devices.get(device_id)
                if dev is None:
                    return None
                now = time.monotonic()
                dev.mark()
                self._watchdog(dev, now)
                redeliver = []
                for cid, rec in list(dev.inflight.items()):
                    if (now - rec["sent"]) * 1000.0 >= self.redeliver_ms:
                        if rec["tries"] >= 3:
                            dev.commands_failed += 1
                            dev.note("指令 %s 重投 3 次仍未确认，放弃" % cid)
                            del dev.inflight[cid]
                        else:
                            rec["tries"] += 1
                            rec["sent"] = now
                            redeliver.append(rec["cmd"])
                out = redeliver + dev.commands[:limit]
                dev.commands = dev.commands[limit:]
                for c in out:
                    dev.inflight[c["id"]] = {"cmd": c, "sent": now, "tries": 1}
                    dev.commands_sent += 1
                if out:
                    return out
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.05)

    def ack(self, device_id, cmd_id, ok=True, result=None):
        with self.lock:
            dev = self.devices.get(device_id)
            if dev is None:
                raise ValueError("未注册设备: %s" % device_id)
            dev.mark()
            rec = dev.inflight.pop(cmd_id, None)
            if ok:
                dev.commands_acked += 1
            else:
                dev.commands_failed += 1
            if rec:
                cmd = rec["cmd"]
                dev.note("指令 %s(%s) %s" % (cmd_id, cmd.get("type"), "已确认" if ok else "执行失败"))
                if self.events is not None and not ok:
                    self.event("command_failed", "%s 执行 %s 失败: %s" % (
                        device_id, cmd.get("type"), result), dev)
            return {"deviceId": device_id, "commandId": cmd_id, "ok": bool(ok),
                    "known": rec is not None}

    def broadcast(self, ctype, payload=None, role=None):
        """向所有在线设备（可按角色过滤）广播指令。"""
        with self.lock:
            now = time.monotonic()
            ids = [d.id for d in self.devices.values()
                   if d.online(now, self.offline_ms) and (role is None or d.role == role)]
        out = []
        for i in ids:
            c = self.push_command(i, ctype, payload)
            if c:
                out.append({"deviceId": i, "commandId": c["id"]})
        return out

    # ---------------------------------------------------------------- 查询
    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("未知在环模式: %s（可选 %s）" % (mode, "/".join(MODES)))
        with self.lock:
            old = self.mode
            self.mode = mode
            if old != mode:
                self.event("mode", "在环模式 %s → %s" % (old, mode))
        return self.mode

    def health(self):
        now = time.monotonic()
        with self.lock:
            devs = list(self.devices.values())
            for d in devs:
                self._watchdog(d, now)
            online = [d for d in devs if d.online(now, self.offline_ms)]
            return {
                "mode": self.mode, "modeLabel": MODE_LABELS[self.mode],
                "devicesTotal": len(devs), "devicesOnline": len(online),
                "telemetryTotal": self.telemetry_total, "telemetryErrors": self.telemetry_errors,
                "lastIngestAgeMs": (round((now - self.last_ingest) * 1000.0, 1)
                                    if self.last_ingest else None),
                "commandsPending": sum(len(d.commands) + len(d.inflight) for d in devs),
                "commandsAcked": sum(d.commands_acked for d in devs),
                "applied": self.applied,
                "offlineMs": self.offline_ms, "freshMs": self.fresh_ms,
            }

    def snapshot(self, sims=None):
        """给前端的设备清单 + 实测/仿真对照。"""
        now = time.monotonic()
        with self.lock:
            for d in self.devices.values():
                self._watchdog(d, now)
            devs = [d.info(now, self.offline_ms, self.fresh_ms) for d in self.devices.values()]
        nodes, links = self.overrides()
        ai = None
        if sims:
            ai = sims.get("ai") if isinstance(sims, dict) else None
        if ai is not None:
            for d in devs:
                cmp_nodes = {}
                for nid, rec in (d["real"]["nodes"] or {}).items():
                    nd = ai.nodeById.get(nid)
                    cmp_nodes[nid] = {
                        "real": rec,
                        "sim": None if nd is None else {
                            "util": nd.util, "latMs": nd.lat, "queueDepth": len(nd.running),
                            "done": nd.done},
                    }
                cmp_links = {}
                for lid, rec in (d["real"]["links"] or {}).items():
                    lk = ai.linkById.get(lid)
                    cmp_links[lid] = {
                        "real": rec,
                        "sim": None if lk is None else {
                            "util": lk.util, "load": lk.load, "cap": lk.capS},
                    }
                d["compare"] = {"nodes": cmp_nodes, "links": cmp_links}
        devs.sort(key=lambda x: (not x["online"], x["deviceId"]))
        return {
            "mode": self.mode, "modeLabel": MODE_LABELS[self.mode],
            "devices": devs, "overrides": {"nodes": nodes, "links": links},
            "events": self.events[-20:], "health": self.health(),
            "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
        }

    def schema(self):
        """给硬件方的接口契约（字段、单位、示例、模式说明）。"""
        return {
            "version": "1.0",
            "transports": {
                "http": {"path": "/api/hw/telemetry", "method": "POST",
                         "contentType": "application/json",
                         "note": "单条对象或对象数组批量上报"},
                "udp": {"note": "每个数据报一个 JSON 对象，或按 \\n 分隔多个对象；无应答",
                        "defaultPort": 8790},
                "tcp": {"note": "按行分隔的 JSON（\\n 结尾），服务端逐条回一行 JSON 应答，可长连接",
                        "defaultPort": 8791},
            },
            "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
            "registration": {
                "path": "/api/hw/register", "method": "POST",
                "body": {"deviceId": "edge-a-01", "kind": "edge", "name": "边缘智算节点 A（实物）",
                         "nodeId": "edgeA", "role": "compute", "meta": {"vendor": "x", "sw": "1.2"}},
                "response": {"deviceId": "...", "token": "...", "config": {"reportIntervalMs": 200},
                             "validNodeIds": sorted(NODE_IDS), "validLinkIds": sorted(LINK_IDS)},
                "note": "可省略注册直接上报，网关会自动登记；但显式注册能拿到 token 与推荐上报周期",
            },
            "telemetry": {
                "deviceId": "edge-a-01（必填）",
                "seq": "递增序号（可选，便于丢包检测）",
                "ts": "设备侧时间戳 ms（可选）",
                "heartbeat": "true 表示仅心跳（可选）",
                "node": {"id": "edgeA", "gpuUtil": "0.62 或 62（两种口径都支持）",
                         "tflops": 5.1, "queueDepth": 3, "memPct": 0.41, "powerW": 180,
                         "tempC": 61, "latMs": 7.3},
                "links": [{"id": "access", "txGbps": 2.1, "rxGbps": 1.4, "rttMs": 7.3,
                           "lossPct": 0.1, "jitterMs": 0.8}],
                "tasks": [{"id": "cam-1024", "type": "vision", "bytes": 12.2, "work": 0.25,
                           "deadlineMs": 200, "prio": 3}],
                "metrics": {"fps": 30, "tempC": 45},
                "validNodeIds": sorted(NODE_IDS), "validLinkIds": sorted(LINK_IDS),
                "taskTypes": list(engine.TYPES.keys()),
                "units": {"bytes": "MB", "work": "TFLOP", "deadlineMs": "ms", "txGbps": "Gbps",
                          "gpuUtil": "0-1 或 0-100", "rttMs": "ms"},
            },
            "commands": {
                "poll": {"path": "/api/hw/commands?deviceId=edge-a-01&wait=25",
                         "note": "长轮询，最多等 wait 秒；返回数组"},
                "ack": {"path": "/api/hw/ack", "method": "POST",
                        "body": {"deviceId": "edge-a-01", "commandId": "c12", "ok": True,
                                 "result": {"latencyMs": 118}}},
                "types": {
                    "dispatch": {"taskId": 123, "extId": "cam-1024", "type": "vision",
                                 "nodeId": "edgeA", "deadlineMs": 200, "bytes": 12.2,
                                 "work": 0.25, "estLatMs": 141, "compressed": False,
                                 "note": "把任务放到该算力节点执行"},
                    "throttle": {"linkId": "field", "util": 0.83, "capGbps": 1.2,
                                 "tiltFactor": 0.14, "note": "带宽策略建议（拥塞时按优先级轻度倾斜）"},
                    "config": {"reportIntervalMs": 200, "note": "调整上报周期"},
                    "ping": {"note": "连通性探测"},
                },
            },
            "examples": {
                "curl_register": "curl -X POST http://127.0.0.1:8787/api/hw/register "
                                 "-H 'Content-Type: application/json' "
                                 "-d '{\"deviceId\":\"edge-a-01\",\"kind\":\"edge\",\"nodeId\":\"edgeA\"}'",
                "curl_telemetry": "curl -X POST http://127.0.0.1:8787/api/hw/telemetry "
                                  "-H 'Content-Type: application/json' "
                                  "-d '{\"deviceId\":\"edge-a-01\",\"node\":{\"id\":\"edgeA\","
                                  "\"gpuUtil\":0.62,\"tflops\":5.1,\"queueDepth\":3},"
                                  "\"links\":[{\"id\":\"access\",\"txGbps\":2.1,\"rttMs\":7.3}]}'",
                "udp": "echo '{\"deviceId\":\"cam-01\",\"tasks\":[{\"id\":\"t1\","
                       "\"type\":\"vision\",\"bytes\":12.2,\"work\":0.25,\"deadlineMs\":200}]}' "
                       "| nc -u -w1 127.0.0.1 8790",
                "python": "见 backend/tools/hw_device_sim.py（内置多设备模拟器，可直接复用作为接入模板）",
            },
        }


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _merge_link(old, new):
    """链路遥测增量合并：缺失字段沿用上次值，避免分频上报互相清空。"""
    merged = dict(old or {})
    for k, v in new.items():
        if v is not None:
            merged[k] = v
        elif k not in merged:
            merged[k] = None
    return merged


# =========================================================================
# UDP / TCP 传输
# =========================================================================
def _parse_payload(data):
    """解析数据报到 JSON 对象（容忍多行 / 数组 / 尾部噪声）。"""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    data = data.strip()
    if not data:
        return []
    try:
        obj = json.loads(data)
        return obj if isinstance(obj, list) else [obj]
    except ValueError:
        pass
    out = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        out.extend(obj if isinstance(obj, list) else [obj])
    return out


class _UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        gw = self.server.gateway
        for obj in _parse_payload(self.request[0]):
            gw.ingest(obj, source="udp")


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TCPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        gw = self.server.gateway
        for raw in self.rfile:
            for obj in _parse_payload(raw):
                res = gw.ingest(obj, source="tcp")
                self.wfile.write((json.dumps({"ok": res["rejected"] == 0, **res},
                                             ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()


class Transports(object):
    """UDP + TCP 监听器（独立线程，随进程退出）。"""

    def __init__(self, gateway, host="0.0.0.0", udp_port=8790, tcp_port=8791):
        self.gateway = gateway
        self.host = host
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.udp = None
        self.tcp = None
        self.threads = []
        self.errors = {}

    def start(self):
        if self.udp_port:
            try:
                self.udp = _UDPServer((self.host, self.udp_port), _UDPHandler)
                self.udp.gateway = self.gateway
                t = threading.Thread(target=self.udp.serve_forever, kwargs={"poll_interval": 0.2},
                                     name="hw-udp", daemon=True)
                t.start()
                self.threads.append(t)
            except OSError as exc:
                self.udp = None
                self.errors["udp"] = str(exc)
                self.gateway.event("transport", "UDP %d 启动失败: %s" % (self.udp_port, exc))
                # 硬件遥测入口起不来必须显式告警，不能静默降级
                sys.stderr.write("[警告] UDP 遥测端口 %d 绑定失败：%s\n" % (self.udp_port, exc))
        if self.tcp_port:
            try:
                self.tcp = _TCPServer((self.host, self.tcp_port), _TCPHandler)
                self.tcp.gateway = self.gateway
                t = threading.Thread(target=self.tcp.serve_forever, kwargs={"poll_interval": 0.2},
                                     name="hw-tcp", daemon=True)
                t.start()
                self.threads.append(t)
            except OSError as exc:
                self.tcp = None
                self.errors["tcp"] = str(exc)
                self.gateway.event("transport", "TCP %d 启动失败: %s" % (self.tcp_port, exc))
                sys.stderr.write("[警告] TCP 遥测端口 %d 绑定失败：%s\n" % (self.tcp_port, exc))
        return self

    def stop(self):
        for srv in (self.udp, self.tcp):
            if srv is not None:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:      # noqa: BLE001
                    pass
        self.udp = self.tcp = None

    def info(self):
        return {
            "udp": {"port": self.udp_port, "up": self.udp is not None},
            "tcp": {"port": self.tcp_port, "up": self.tcp is not None},
            "errors": dict(self.errors),
        }
