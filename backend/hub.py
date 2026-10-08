# -*- coding: utf-8 -*-
"""
运行时中枢：双轨仿真（AI 协同调度 / 静态规则基线）推进、参数控制、订阅推送。

- 后台线程按真实时间推进仿真时钟（TSCALE × speed）
- 两个仿真体始终同种子、同任务流并行推进，保证「双轨对照」成立
- 订阅者（SSE）以 10Hz 收到状态快照，参数变更时立即补发
"""

from __future__ import annotations

import json
import queue
import threading
import time

from . import engine, hardware, models

MAX_STEPS_PER_TICK = 12      # 单帧最多推进的子步数（防止卡顿后疯狂追赶）
MAX_CATCHUP_MS = 250.0       # 单帧最多补偿的真实时间


def sim_snapshot(s):
    """把仿真体序列化成前端可直接渲染的快照（字段与前端 JS 对象同构）。"""
    tasks = []
    for tk in s.tasks:
        tasks.append({
            "id": tk.id, "type": tk.type, "state": tk.state, "nodeId": tk.nodeId,
            "devId": tk.devId, "devY": tk.devY, "visT": tk.visT, "visDur": tk.visDur,
            "deadline": tk.deadline, "prio": tk.prio, "bwEff": tk.bwEff,
            "workLeft": tk.workLeft, "bytesLeft": tk.bytesLeft,
            "source": getattr(tk, "source", "sim"), "extId": getattr(tk, "extId", None),
            "compressed": bool(getattr(tk, "compressed", False)),
        })
    nodes = []
    for n in s.nodes:
        nodes.append({
            "id": n.id, "name": n.name, "tag": n.tag, "color": n.color, "cap": n.cap,
            "x": n.x, "y": n.y, "w": n.w, "h": n.h, "lat": n.lat, "reserve": n.reserve,
            "util": n.util, "utilS": n.utilS, "backlog": n.backlog, "pending": n.pending,
            "done": n.done, "latSum": n.latSum, "workSum": n.workSum,
            "rtLoad": n.rtLoad, "loLoad": n.loLoad,
            "hwOverride": bool(getattr(n, "hwOverride", False)),
            "hwSrc": getattr(n, "hwSrc", ""),
            "weight": round(n.prior, 3), "weightDyn": round(n.wDyn, 3),
            "weightQuality": round(n.wQuality, 3), "weightEff": round(n.wEff, 3),
            "pressure": round(n.press, 4), "loadDev": round(n.devS, 4),
            "missEMA": round(n.missEMA, 4), "locked": bool(n.locked),
            "running": [tk.id for tk in n.running],
        })
    links = []
    for l in s.links:
        links.append({
            "id": l.id, "name": l.name, "cap": l.cap, "capS": l.capS, "color": l.color,
            "demand": l.demand, "act": l.act, "load": l.load, "util": l.util, "utilS": l.utilS,
            "hwOverride": bool(getattr(l, "hwOverride", False)),
            "hwSrc": getattr(l, "hwSrc", ""),
        })
    st = s.stat
    return {
        "mode": s.mode,
        "t": s.t,
        "throughput": s.throughput,
        "limitHit": s.limitHit,
        "tasks": tasks,
        "nodes": nodes,
        "links": links,
        "table": s.table[-40:],
        "log": s.log[-40:],
        "typeStat": s.typeStat,
        "stat": {
            "n": st["n"], "latSum": st["latSum"], "miss": st["miss"], "energy": st["energy"],
            "work": st["work"], "bytes": st["bytes"], "cloud": st["cloud"], "edge": st["edge"],
            "local": st["local"], "active": st["active"],
            "compressed": st.get("compressed", 0),
            "compressSavedMB": st.get("compressSavedMB", 0.0),
            "compressExtraTFLOP": st.get("compressExtraTFLOP", 0.0),
            "avgLat": s.avg_lat(), "p95": s.p95(), "missRate": s.miss_rate(),
            "energyPerTask": s.energy_per_task(), "gpu": s.gpu_util(), "peakBw": s.peak_bw(),
        },
        "hist": {
            "lat": s.hist["lat"], "p95": s.hist["p95"],
            "bw": s.hist["bw"], "gpu": s.hist["gpu"],
        },
    }


def kpi(s):
    return {
        "n": s.stat["n"], "t": s.t,
        "avgLat": s.avg_lat(), "p95": s.p95(), "missRate": s.miss_rate(),
        "throughput": s.throughput, "gpu": s.gpu_util(), "peakBw": s.peak_bw(),
        "queue": len(s.tasks),
        "queuedTransfer": len([t for t in s.tasks if t.state == "transfer"]),
        "queuedCompute": len([t for t in s.tasks if t.state == "compute"]),
        "energyPerTask": s.energy_per_task(),
        "carriedRate": (s.stat["edge"] + s.stat["cloud"]) / s.stat["n"] * 100 if s.stat["n"] else 0.0,
        "compressed": s.stat.get("compressed", 0),
        "compressSavedMB": s.stat.get("compressSavedMB", 0.0),
        "compressExtraTFLOP": s.stat.get("compressExtraTFLOP", 0.0),
        "workTotal": s.stat["work"],
        "bytesTotal": s.stat["bytes"],
        "dist": {"local": s.stat["local"], "edge": s.stat["edge"], "cloud": s.stat["cloud"]},
    }


class Hub(object):
    """全局唯一的仿真运行时。线程安全。"""

    def __init__(self, scene="normal", speed=1.5, tick_hz=20, push_hz=10, model_id=None,
                 hw_mode="off"):
        self.lock = threading.RLock()
        self.scene = scene if scene in engine.SCENES else "normal"
        self.params = engine.clone_scene(self.scene)
        self.speed = float(speed)
        self.paused = False
        self.view = "ai"
        self.seed = engine.SEED
        self.model_id = model_id if model_id in models.MODELS else models.DEFAULT
        # 节点权重由算法动态分配；这里的 priors 只是"可选先验偏置"（默认 1.0 = 完全交给算法）
        # 必须在 _make_sims() 之前初始化
        self.node_priors = {n["id"]: 1.0 for n in engine.NODE_DEFS}
        self.node_locks = set()
        self.weight_rev = 0
        self.sims = self._make_sims()
        self.subs = set()
        self.hw = hardware.HardwareGateway(mode=hw_mode)
        self.hw.on_tasks = self.inject_hw_tasks
        self.hw_tasks_injected = 0
        self.tick_hz = tick_hz
        self.push_every = max(1, int(round(tick_hz / float(push_hz))))
        self.seq = 0
        self.ticks = 0
        self.started = time.time()
        self.clients_seen = 0
        self._stop = threading.Event()
        self._thread = None
        self._wake = threading.Event()

    # ---------------- 生命周期 ----------------
    def _make_sims(self):
        m = models.MODELS[self.model_id]
        sims = {"ai": engine.Sim("ai", self.seed, model=m),
                "bl": engine.Sim("baseline", self.seed, model=m)}
        self._apply_weights_locked(sims)
        return sims

    def _apply_weights_locked(self, sims=None):
        """把先验偏置写到 AI 轨（基线是静态规则映射，保持权重无关）。

        注意：真正的权重是算法每步动态算出来的（engine._update_dynamic_weights），
        这里只设置可选的先验偏置 prior 与锁定标记。
        """
        sims = sims or self.sims
        ai = sims["ai"]
        for nid, w in self.node_priors.items():
            nd = ai.nodeById.get(nid)
            if nd is not None:
                nd.prior = float(w)
                nd.locked = nid in self.node_locks
        engine.refresh_weight_state(ai)
        return {n.id: n.prior for n in ai.nodes}

    def set_weights(self, weights=None, locks=None, reset=False):
        """设置**先验偏置**（可选）与锁定；真正的权重由算法动态分配。

        weights: {nodeId: 0.1~4.0} 先验偏置（默认 1.0 = 不干预算法）；
        locks:   需要锁定（权重固定为 1.0、不参与动态分配）的 nodeId 列表；
        reset=True 时把先验恢复为 1.0 并清空锁定。
        改动立即生效，不需要重置仿真。
        """
        with self.lock:
            if reset:
                self.node_priors = {n["id"]: 1.0 for n in engine.NODE_DEFS}
                self.node_locks = set()
            for nid, w in (weights or {}).items():
                if nid not in self.node_priors:
                    raise ValueError("未知节点: %s（可选 %s）"
                                     % (nid, ",".join(self.node_priors)))
                try:
                    v = float(w)
                except (TypeError, ValueError):
                    raise ValueError("先验权重必须是数字: %s=%r" % (nid, w))
                if not (0.1 <= v <= 4.0):
                    raise ValueError("先验权重超出范围 0.1~4.0: %s=%s" % (nid, v))
                self.node_priors[nid] = v
            if locks is not None:
                bad = [x for x in locks if x not in self.node_priors]
                if bad:
                    raise ValueError("未知节点: %s" % ",".join(bad))
                self.node_locks = set(locks)
            self._apply_weights_locked()
            self.weight_rev += 1
        self._publish(force=True)
        return self.node_weight_state()

    def node_weight_state(self):
        with self.lock:
            ai = self.sims["ai"]
            bl = self.sims["bl"]
            rep = engine.node_weight_report(ai)
            return {
                "priors": dict(self.node_priors),
                "weights": dict(self.node_priors),      # 兼容旧字段名（= 先验）
                "locks": sorted(self.node_locks),
                "revision": self.weight_rev,
                "ai": rep,
                "dynamic": {k: v["dynamic"] for k, v in rep.items()},
                "effective": {k: v["effective"] for k, v in rep.items()},
                "baseline": {n.id: round(n.prior, 3) for n in bl.nodes},
                "neutral": all(abs(v - 1.0) < 1e-9 for v in self.node_priors.values()),
                "owner": "algorithm",   # 权重由算法动态分配；priors 只是可选偏置
                "params": {k: v for k, v in (models.MODELS[self.model_id].get("params") or {}).items()
                           if k in ("dynGamma", "dynQueueW", "dynKi", "dynLeak", "dynIMax",
                                    "dynKm", "dynWMin", "dynWMax", "dynTauMs", "learnTauMs",
                                    "learnRate", "qualityMin", "qualityMax")},
                "flags": {k: bool(v) for k, v in models.MODELS[self.model_id]["flags"].items()
                          if k in ("nodeWeight", "weightQuality", "weightIntegral", "weightMiss")},
                "note": "权重由算法每步动态分配（压力比为主 + 质量学习）；prior 是可选先验偏置，"
                        "默认 1.0 表示完全交给算法；锁定后该节点权重固定为 1.0。"
                        "权重只作用于 AI 轨，基线保持静态规则映射。",
            }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sim-hub", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # ---------------- 推进入口 ----------------
    def _loop(self):
        period = 1.0 / self.tick_hz
        last = time.monotonic()
        while not self._stop.is_set():
            self._wake.wait(timeout=period)
            self._wake.clear()
            now = time.monotonic()
            real_ms = (now - last) * 1000.0
            last = now
            if self.paused:
                continue
            real_ms = min(real_ms, MAX_CATCHUP_MS)
            if real_ms <= 0:
                continue
            self.advance(real_ms)

    def advance(self, real_ms):
        """按真实时间推进仿真（等价于前端 frame() 的 TSCALE × speed 逻辑）。"""
        with self.lock:
            sim_ms = real_ms * engine.TSCALE * self.speed
            n = int(round(sim_ms / engine.STEP_MS))
            n = max(1, min(MAX_STEPS_PER_TICK, n))
            h = sim_ms / n
            t0 = self.sims["ai"].t
            for i in range(n):
                tt = t0 + h * (i + 1)
                engine.step(self.sims["ai"], h, self.params, tt)
                engine.step(self.sims["bl"], h, self.params, tt)
            # 硬件在环：每步之后把实测状态写回（仅 hil 模式；离线设备自动失效）
            if self.hw.mode == "hil":
                self.hw.apply_overrides(self.sims)
            self.ticks += 1
            if self.ticks % self.push_every == 0:
                self._publish()

    # ---------------- 硬件在环 ----------------
    def inject_hw_tasks(self, dev, tasks):
        """把硬件上报的真实任务注入**双轨**（保证 AI/基线对照仍然成立）。

        返回派发决策，由网关下发给设备执行。
        """
        out = []
        with self.lock:
            t = self.sims["ai"].t
            for spec in tasks:
                if not isinstance(spec, dict):
                    continue
                ttype = spec.get("type") or spec.get("taskType") or "vision"
                if ttype not in engine.TYPES:
                    continue
                ext = spec.get("id") or spec.get("taskId")
                kw = dict(
                    dev_id=spec.get("device") or dev.node_id or None,
                    bytes_=spec.get("bytes") or spec.get("dataMB"),
                    work_=spec.get("work") or spec.get("workTFLOP"),
                    deadline=spec.get("deadlineMs") or spec.get("deadline"),
                    prio=spec.get("prio") or spec.get("priority"),
                    source="hw", ext_id=ext,
                )
                ai_tk = engine.spawn_task(self.sims["ai"], ttype, t, **kw)
                bl_tk = engine.spawn_task(self.sims["bl"], ttype, t, **kw)
                out.append({
                    "deviceId": dev.id,
                    "payload": {
                        "taskId": ai_tk.id, "extId": ext, "type": ttype,
                        "nodeId": ai_tk.nodeId, "nodeTag": self.sims["ai"].nodeById[ai_tk.nodeId].tag,
                        "deadlineMs": ai_tk.deadline, "bytes": round(ai_tk.bytes, 4),
                        "work": round(ai_tk.work, 6),
                        "estLatMs": round(ai_tk.est["estLat"], 1),
                        "compressed": bool(ai_tk.compressed),
                        "baselineNode": bl_tk.nodeId,
                        "simT": round(t, 1),
                    },
                })
            self.hw_tasks_injected += len(out)
        return out

    def apply_hw(self):
        """手动触发一次实测覆盖（用于影子/在环模式切换后立即生效）。"""
        return self.hw.apply_overrides(self.sims)

    # ---------------- 控制 ----------------
    def control(self, action, value=None):
        with self.lock:
            if action == "scene":
                if value in engine.SCENES:
                    self.scene = value
                    self.params = engine.clone_scene(value)
                    self.reset_locked()
                return self.state()
            if action == "rate":
                self.params["rate"] = float(value)
                return self.state()
            if action == "speed":
                self.speed = max(0.1, min(8.0, float(value)))
                return self.state()
            if action == "pause":
                self.paused = bool(value)
                return self.state()
            if action == "reset":
                self.reset_locked()
                return self.state()
            if action == "view":
                self.view = "bl" if value == "bl" else "ai"
                return self.state()
            if action == "seed":
                self.seed = int(value)
                self.reset_locked()
                return self.state()
            if action == "model":
                if value not in models.MODELS:
                    raise ValueError("未知模型: %s" % value)
                self.model_id = value
                self.reset_locked()
                return self.state()
            if action == "hwMode":
                self.hw.set_mode(value)
                if self.hw.mode != "hil":
                    for s in self.sims.values():
                        for n in s.nodes:
                            n.hwOverride = False
                        for l in s.links:
                            l.hwOverride = False
                else:
                    self.hw.apply_overrides(self.sims)
                return self.state()
            raise ValueError("未知控制指令: %s" % action)

    def reset_locked(self):
        self.sims = self._make_sims()
        self._publish(force=True)

    def reset(self):
        with self.lock:
            self.reset_locked()

    # ---------------- 订阅（SSE） ----------------
    def subscribe(self):
        q = queue.Queue(maxsize=8)
        with self.lock:
            self.subs.add(q)
            self.clients_seen += 1
            q.put(self.snapshot_locked())
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def _publish(self, force=False):
        with self.lock:
            snap = self.snapshot_locked()
        for q in list(self.subs):
            try:
                q.put_nowait(snap)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except Exception:
                    pass

    # ---------------- 快照 ----------------
    def state(self):
        with self.lock:
            return self._state_locked()

    def _state_locked(self):
        m = models.MODELS[self.model_id]
        return {
            "scene": self.scene,
            "sceneLabel": engine.SCENE_LABELS.get(self.scene, self.scene),
            "rate": self.params["rate"],
            "speed": self.speed,
            "paused": self.paused,
            "view": self.view,
            "seed": self.seed,
            "stepMs": engine.STEP_MS,
            "tSim": self.sims["ai"].t,
            "ticks": self.ticks,
            "seq": self.seq,
            "clients": len(self.subs),
            "uptimeSec": time.time() - self.started,
            "model": self.model_id,
            "modelName": m["name"],
            "modelShort": m["short"],
            "hil": self.hw.health(),
            "weights": {"revision": self.weight_rev, "owner": "algorithm",
                        "neutral": all(abs(v - 1.0) < 1e-9 for v in self.node_priors.values()),
                        "priors": dict(self.node_priors), "locks": sorted(self.node_locks),
                        "dynamic": {n.id: round(n.wDyn, 3) for n in self.sims["ai"].nodes},
                        "effective": {n.id: round(n.wEff, 3) for n in self.sims["ai"].nodes}},
        }

    def snapshot(self, force=False):
        with self.lock:
            return self.snapshot_locked()

    def snapshot_locked(self):
        self.seq += 1
        snap = self._state_locked()
        snap["seq"] = self.seq
        snap["ai"] = sim_snapshot(self.sims["ai"])
        snap["bl"] = sim_snapshot(self.sims["bl"])
        return snap

    def metrics(self):
        with self.lock:
            ai, bl = self.sims["ai"], self.sims["bl"]
            a, b = kpi(ai), kpi(bl)
            out = self._state_locked()
            out["ai"] = a
            out["bl"] = b

            def delta(x, y, invert=False):
                if not (y and y > 0) or x <= 0:
                    return None
                d = (x - y) / y * 100.0
                return {"pct": d, "better": (d > 0) if invert else (d < 0)}

            out["delta"] = {
                "avgLat": delta(a["avgLat"], b["avgLat"]),
                "p95": delta(a["p95"], b["p95"]),
                "missRate": delta(a["missRate"], b["missRate"]),
                "throughput": delta(a["throughput"], b["throughput"], invert=True),
                "gpu": delta(a["gpu"], b["gpu"]),
                "peakBw": delta(a["peakBw"], b["peakBw"]),
                "energyPerTask": delta(a["energyPerTask"], b["energyPerTask"]),
                "carriedRate": delta(a["carriedRate"], b["carriedRate"], invert=True),
            }
            return out

    def tasks(self, limit=40, mode="ai"):
        with self.lock:
            s = self.sims["bl"] if mode == "bl" else self.sims["ai"]
            rows = s.table[-limit:]
            return {
                "mode": s.mode, "n": s.stat["n"], "rows": rows,
                "queue": [{"id": t.id, "type": t.type, "state": t.state, "nodeId": t.nodeId,
                           "deadline": t.deadline, "prio": t.prio}
                          for t in s.tasks[-limit:]],
            }

    def logs(self, limit=12, mode="ai"):
        with self.lock:
            s = self.sims["bl"] if mode == "bl" else self.sims["ai"]
            return {"mode": s.mode, "rows": s.log[-limit:]}

    # ---------------- 序列化 ----------------
    @staticmethod
    def dumps(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def catalog():
    """场景 / 任务类型 / 节点 / 链路 / 设备的元数据，供外部客户端使用。"""
    return {
        "scenes": [{"id": k, "label": engine.SCENE_LABELS[k], "rate": v["rate"],
                    "weights": v["w"], "bwScale": v["bw"]} for k, v in engine.SCENES.items()],
        "types": engine.TYPES,
        "typeOrder": engine.TYPE_ORDER,
        "devices": engine.DEVICES,
        "nodes": engine.NODE_DEFS,
        "links": engine.LINK_DEFS,
        "nodeLinks": engine.NODE_LINKS,
        "defaults": {"seed": engine.SEED, "stepMs": engine.STEP_MS, "tScale": engine.TSCALE,
                     "sampleMs": engine.SAMPLE_MS, "rtPrio": engine.RT_PRIO},
        "models": models.catalog(),
        "defaultModel": models.DEFAULT,
        "costModel": {
            "selection": "node* = argmin[ cost + 0.20 × 在途连接数 ]",
            "note": "各模型（v1/v2）的具体代价公式见 models 字段的 formula / cards",
        },
    }
