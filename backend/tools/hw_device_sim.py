# -*- coding: utf-8 -*-
"""
硬件设备模拟器 —— 没有真实硬件时用它验证全链路，也可直接当作**接入模板**。

    # 起后端（另开一个终端）
    python -m backend.run --hw-mode hil

    # 模拟 1 台边缘盒子 + 1 台交换机 + 2 台相机 + 1 台 AGV/机器人控制器
    python -m backend.tools.hw_device_sim --base http://127.0.0.1:8787 --duration 30

    # 只用 UDP 上报
    python -m backend.tools.hw_device_sim --transport udp --udp-port 8790 --duration 20

    # 只用 TCP 上报（长连接 + 逐条应答）
    python -m backend.tools.hw_device_sim --transport tcp --tcp-port 8791 --duration 20

做的事：注册设备 → 周期上报遥测（含真实任务）→ 长轮询领取调度指令并确认。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import engine  # noqa: E402


def http_json(url, body=None, method=None, timeout=6.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


class UdpSender(object):
    def __init__(self, host, port):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, obj):
        self.sock.sendto((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"), self.addr)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TcpSender(object):
    """按行 JSON 的长连接；服务端每条回一行 JSON 应答。"""

    def __init__(self, host, port, retry=3):
        self.addr = (host, port)
        self.sock = None
        self.buf = b""
        self.lock = threading.Lock()
        self.retry = retry
        self._connect()

    def _connect(self):
        for _ in range(self.retry):
            try:
                s = socket.create_connection(self.addr, timeout=3.0)
                s.settimeout(3.0)
                self.sock = s
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("TCP 连接失败: %s:%s" % self.addr)

    def send(self, obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.sock.sendall(line)
                self.buf += self.sock.recv(65536)
                while b"\n" in self.buf:
                    one, self.buf = self.buf.split(b"\n", 1)
                    if one.strip():
                        return json.loads(one.decode("utf-8", "replace"))
            except OSError:
                try:
                    self._connect()
                except RuntimeError:
                    return None
        return None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


class Reporter(object):
    """一台模拟设备：周期上报 + 长轮询指令。"""

    def __init__(self, base, spec, sender, opts, stats):
        self.base = base
        self.spec = spec
        self.device_id = spec["deviceId"]
        self.sender = sender
        self.opts = opts
        self.stats = stats
        self.stop = threading.Event()
        self.threads = []
        self.seq = 0
        self.rng = random.Random(hash(self.device_id) & 0xFFFF)
        self.phase = self.rng.random() * 6.28
        self.token = None

    # ------------------------------------------------------------ 上报相关
    def register(self):
        body = {"deviceId": self.spec["deviceId"], "kind": self.spec.get("kind", "custom"),
                "name": self.spec.get("name"), "nodeId": self.spec.get("nodeId"),
                "role": self.spec.get("role"), "meta": {"simulator": True}}
        r = http_json(self.base + "/api/hw/register", body)
        self.token = r.get("token")
        if self.opts.get("verbose"):
            print("  [注册] %s → token=%s mode=%s" % (self.device_id, self.token,
                                                      r.get("config", {}).get("mode")))
        return r

    def payload(self, t):
        self.seq += 1
        spec = self.spec
        kind = spec.get("kind")
        body = {"deviceId": self.device_id, "seq": self.seq, "ts": int(time.time() * 1000)}
        load = 0.5 + 0.45 * math.sin(t * spec.get("period", 0.35) + self.phase)
        load = max(0.02, min(1.0, load + self.rng.uniform(-0.05, 0.05)))
        if kind in ("edge", "node", "server", "gpu"):
            cap = spec.get("cap", 6.0)
            body["node"] = {
                "id": spec["nodeId"], "gpuUtil": round(load, 4),
                "tflops": round(cap * load, 3),
                "queueDepth": int(load * 6), "memPct": round(load * 0.8, 3),
                "powerW": round(120 + 180 * load, 1), "tempC": round(38 + 26 * load, 1),
                "latMs": round(spec.get("latMs", 6.5) * (0.8 + 0.6 * load), 2),
            }
        elif kind in ("switch", "link", "tsn", "gateway"):
            lid = spec["linkId"]
            cap = next((l["cap"] for l in engine.LINK_DEFS if l["id"] == lid), 1.0)
            body["links"] = [{
                "id": lid, "txGbps": round(cap * load * 0.9, 3), "rxGbps": round(cap * load * 0.4, 3),
                "rttMs": round(1.5 + 6 * load, 2), "lossPct": round(max(0.0, (load - 0.9) * 100), 3),
                "jitterMs": round(0.2 + load, 2),
            }]
        elif kind in ("camera", "agv", "robot", "sensor"):
            tasks = []
            n = self.opts.get("tasks_per_period", 1)
            for i in range(n):
                if self.rng.random() > spec.get("taskProb", 0.7):
                    continue
                ttype = spec.get("taskType", "vision")
                tp = engine.TYPES[ttype]
                tasks.append({
                    "id": "%s-%d" % (self.device_id, self.seq * 10 + i),
                    "type": ttype,
                    "bytes": round((tp["dataIn"] + tp["dataOut"]) * self.rng.uniform(0.8, 1.3), 3),
                    "work": round(tp["work"] * self.rng.uniform(0.8, 1.25), 5),
                    "deadlineMs": tp["deadline"], "prio": tp["prio"],
                })
            if tasks:
                body["tasks"] = tasks
            body["metrics"] = {"fps": spec.get("fps", 30), "tempC": round(35 + 15 * load, 1),
                               "battery": round(100 - 30 * load, 1)}
        if self.opts.get("verbose") and self.seq % 10 == 0:
            print("  [上报] %s #%d load=%.2f" % (self.device_id, self.seq, load))
        return body

    def report_loop(self):
        interval = self.opts.get("interval_ms", 200) / 1000.0
        t0 = time.time()
        while not self.stop.is_set():
            payload = self.payload(time.time() - t0)
            try:
                if self.opts["transport"] == "http":
                    r = http_json(self.base + "/api/hw/telemetry", payload)
                    if r.get("dispatched"):
                        self.stats["dispatched"] += len(r["dispatched"])
                else:
                    self.sender.send(payload)
                    self.stats["sent_%s" % self.opts["transport"]] += 1
                self.stats["telemetry"] += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.stats["errors"] += 1
                if self.opts.get("verbose"):
                    print("  [错误] %s 上报失败: %s" % (self.device_id, exc))
            self.stop.wait(interval)

    def command_loop(self):
        """长轮询领取下发指令并确认（真实设备在这里对接执行器）。"""
        while not self.stop.is_set():
            try:
                r = http_json("%s/api/hw/commands?deviceId=%s&wait=%d"
                              % (self.base, self.device_id, self.opts.get("poll_wait", 2)))
            except (urllib.error.URLError, OSError, ValueError):
                self.stop.wait(1.0)
                continue
            for c in r.get("commands", []):
                self.stats["commands"] += 1
                if c["type"] == "dispatch":
                    p = c["payload"]
                    self.stats["dispatch_cmds"] += 1
                    if self.opts.get("verbose"):
                        print("  [指令] %s ← 任务 %s(%s) 派发到 %s 预估 %.0fms%s"
                              % (self.device_id, p.get("extId") or p.get("taskId"), p.get("type"),
                                 p.get("nodeId"), p.get("estLatMs", 0),
                                 "（降采样）" if p.get("compressed") else ""))
                elif c["type"] == "ping":
                    pass
                # 回报执行结果（真实设备应回填实测时延）
                try:
                    http_json(self.base + "/api/hw/ack", {
                        "deviceId": self.device_id, "commandId": c["id"], "ok": True,
                        "result": {"executedAt": int(time.time() * 1000)}})
                    self.stats["acked"] += 1
                except (urllib.error.URLError, OSError, ValueError):
                    self.stats["errors"] += 1

    def start(self):
        self.register()
        th = threading.Thread(target=self.report_loop, name="rep-" + self.device_id, daemon=True)
        th.start()
        self.threads.append(th)
        if self.opts.get("consume_commands", True):
            th2 = threading.Thread(target=self.command_loop, name="cmd-" + self.device_id,
                                   daemon=True)
            th2.start()
            self.threads.append(th2)

    def stop_all(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=3.0)


def default_devices():
    return [
        {"deviceId": "edge-a-01", "kind": "edge", "name": "边缘智算节点 A（实物）",
         "nodeId": "edgeA", "cap": 6.0, "latMs": 6.5, "period": 0.31},
        {"deviceId": "edge-b-01", "kind": "edge", "name": "边缘智算节点 B（实物）",
         "nodeId": "edgeB", "cap": 6.0, "latMs": 6.5, "period": 0.37},
        {"deviceId": "sw-field-01", "kind": "switch", "name": "现场 TSN 交换机",
         "linkId": "field", "period": 0.29},
        {"deviceId": "sw-access-01", "kind": "switch", "name": "边缘接入交换机",
         "linkId": "access", "period": 0.33},
        {"deviceId": "cam-01", "kind": "camera", "name": "视觉相机阵列 #1",
         "taskType": "vision", "taskProb": 0.8, "fps": 30, "period": 0.41},
        {"deviceId": "agv-01", "kind": "agv", "name": "AGV 车队控制器",
         "taskType": "agv", "taskProb": 0.5, "period": 0.43},
        {"deviceId": "robot-01", "kind": "robot", "name": "机器人控制柜",
         "taskType": "robot", "taskProb": 0.6, "period": 0.39},
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="硬件设备模拟器（同时可作为接入模板）")
    ap.add_argument("--base", default="http://127.0.0.1:8787", help="后端地址")
    ap.add_argument("--transport", choices=["http", "udp", "tcp"], default="http")
    ap.add_argument("--udp-host", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=8790)
    ap.add_argument("--tcp-host", default="127.0.0.1")
    ap.add_argument("--tcp-port", type=int, default=8791)
    ap.add_argument("--duration", type=float, default=20.0, help="运行秒数")
    ap.add_argument("--interval-ms", type=int, default=200, help="上报周期")
    ap.add_argument("--poll-wait", type=int, default=2, help="指令长轮询等待秒数")
    ap.add_argument("--tasks-per-period", type=int, default=1)
    ap.add_argument("--mode", default=None, choices=["off", "shadow", "hil"],
                    help="先把后端切到该在环模式")
    ap.add_argument("--no-commands", action="store_true", help="不领取下发指令")
    ap.add_argument("--verbose", action="store_true")
    opts = vars(ap.parse_args(argv))
    opts["consume_commands"] = not opts.pop("no_commands")

    base = opts["base"].rstrip("/")
    try:
        health = http_json(base + "/api/health")
    except (urllib.error.URLError, OSError) as exc:
        print("无法连接后端 %s: %s" % (base, exc))
        print("请先启动：python -m backend.run")
        return 2
    print("后端: %s  模型=%s 场景=%s" % (base, health["sim"].get("modelShort"),
                                        health["sim"]["scene"]))
    if opts["mode"]:
        http_json(base + "/api/hw/mode", {"mode": opts["mode"]})
        print("已在环模式 → %s" % opts["mode"])
    else:
        cur = health["sim"].get("hil", {}).get("mode", "off")
        print("当前在环模式: %s（可用 --mode hil 切换）" % cur)

    sender = None
    if opts["transport"] == "udp":
        sender = UdpSender(opts["udp_host"], opts["udp_port"])
    elif opts["transport"] == "tcp":
        sender = TcpSender(opts["tcp_host"], opts["tcp_port"])
    print("上报通道: %s" % opts["transport"].upper())

    stats = {"telemetry": 0, "errors": 0, "commands": 0, "dispatch_cmds": 0, "acked": 0,
             "dispatched": 0, "sent_udp": 0, "sent_tcp": 0}
    reps = [Reporter(base, spec, sender, opts, stats) for spec in default_devices()]
    print("启动 %d 台模拟设备，运行 %.0fs …" % (len(reps), opts["duration"]))
    for r in reps:
        r.start()
    try:
        end = time.time() + opts["duration"]
        while time.time() < end:
            time.sleep(1.0)
            try:
                dev = http_json(base + "/api/hw/devices")
                on = dev["health"]["devicesOnline"]
                print("  t=%.0fs 在线 %d/%d 遥测 %d 指令 %d 确认 %d 覆盖 节点%d/链路%d"
                      % (opts["duration"] - (end - time.time()), on,
                         dev["health"]["devicesTotal"], stats["telemetry"], stats["commands"],
                         stats["acked"], dev["health"]["applied"]["nodes"],
                         dev["health"]["applied"]["links"]))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print("  查询失败: %s" % exc)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        try:
            final = http_json(base + "/api/hw/devices")
        except (urllib.error.URLError, OSError, ValueError):
            final = None
        for r in reps:
            r.stop_all()
        if sender:
            sender.close()

    print("\n汇总: 遥测 %d 条（错误 %d）｜领取指令 %d 条（其中派发 %d）｜确认 %d 条"
          % (stats["telemetry"], stats["errors"], stats["commands"], stats["dispatch_cmds"],
             stats["acked"]))
    if final:
        print("设备状态: 在线 %d/%d，实测覆盖 节点 %d / 链路 %d"
              % (final["health"]["devicesOnline"], final["health"]["devicesTotal"],
                 final["health"]["applied"]["nodes"], final["health"]["applied"]["links"]))
        for e in final.get("events", [])[-6:]:
            print("  [%s] %s %s" % (e["t"], e["kind"], e["msg"]))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
