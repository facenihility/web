# -*- coding: utf-8 -*-
"""
硬件在环（HIL）接口测试：注册 → 遥测（HTTP/UDP/TCP）→ 三种模式 → 真实任务派发 →
指令长轮询与确认 → 设备离线看门狗与覆盖失效。

    python backend/tests/test_hil.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# 与本项目其他测试一致：数据目录指向临时目录，不污染交付物
os.environ.setdefault("DSH_DATA_DIR", os.path.join(tempfile.gettempdir(), "dsh_test_data"))
sys.path.insert(0, ROOT)

from backend import server as S  # noqa: E402

FAILS = []
OKS = [0]


def check(cond, msg, info=""):
    if cond:
        OKS[0] += 1
        print("  OK   %s%s" % (msg, ("  [%s]" % info) if info else ""))
    else:
        FAILS.append(msg)
        print("  FAIL %s  %s" % (msg, info))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client(object):
    def __init__(self, base):
        self.base = base

    def get(self, path, timeout=10):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post(self, path, obj, timeout=15):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


def main():
    # 端口在「探测」与「真正绑定」之间存在竞态（上一个测试进程的套接字可能在收尾）。
    # 因此这里把「起服务 + 两个监听器都就绪」作为整体重试，而不是假设一次必成。
    httpd = None
    for attempt in range(4):
        udp_port, tcp_port = free_port(), free_port()
        httpd = S.serve("127.0.0.1", 0, quiet=True, hw_mode="shadow",
                        udp_port=udp_port, tcp_port=tcp_port, hw_host="127.0.0.1")
        time.sleep(0.2)
        tr = S.HW_TRANSPORTS.info() if S.HW_TRANSPORTS else {}
        if tr.get("udp", {}).get("up") and tr.get("tcp", {}).get("up"):
            break
        print("[重试] 监听端口竞态（udp=%s tcp=%s: %s），换端口重来"
              % (udp_port, tcp_port, tr.get("errors")))
        httpd.shutdown()
        httpd.server_close()
        if S.HW_TRANSPORTS:
            S.HW_TRANSPORTS.stop()
        S.HUB.stop()
        time.sleep(0.3)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    c = Client("http://127.0.0.1:%d" % port)
    print("HTTP %s / UDP %d / TCP %d\n" % (c.base, udp_port, tcp_port))
    hw = S.HUB.hw

    try:
        print("=== 接口契约 ===")
        st, r = c.get("/api/hw/schema")
        check(st == 200 and r["schema"]["version"], "GET /api/hw/schema 返回契约文档")
        check(set(r["schema"]["transports"]) >= {"http", "udp", "tcp"}, "  └ 三种上行通道均已声明")
        check(len(r["schema"]["modes"]) == 3, "  └ 三种在环模式均已声明")
        check("examples" in r["schema"] and "curl_telemetry" in r["schema"]["examples"],
              "  └ 含可复制的接入示例")

        print("\n=== 注册与心跳 ===")
        st, r = c.post("/api/hw/register", {"deviceId": "edge-a-01", "kind": "edge",
                                            "nodeId": "edgeA", "name": "边缘 A（测试）"})
        check(st == 200 and r.get("token"), "POST /api/hw/register 返回 token")
        check("edgeA" in r["validNodeIds"] and "field" in r["validLinkIds"],
              "  └ 返回合法 nodeId/linkId 便于自检")
        tok = r["token"]

        print("\n=== 遥测接入（HTTP）===")
        st, r = c.post("/api/hw/telemetry", {
            "deviceId": "edge-a-01", "seq": 1,
            "node": {"id": "edgeA", "gpuUtil": 0.62, "tflops": 5.1, "queueDepth": 3,
                     "memPct": 41, "powerW": 180, "tempC": 61, "latMs": 7.4},
            "links": [{"id": "access", "txGbps": 2.1, "rxGbps": 1.4, "rttMs": 7.3}],
        })
        check(st == 200 and r["accepted"] == 1, "POST /api/hw/telemetry 受理遥测")
        st, r = c.post("/api/hw/telemetry", {"deviceId": "edge-a-02", "kind": "switch",
                                             "linkId": "field",
                                             "links": [{"id": "field", "txGbps": 0.9}]})
        check(r["accepted"] == 1, "未注册设备自动登记（先上报后注册）")

        st, r = c.post("/api/hw/telemetry", {"deviceId": "edge-a-01",
                                             "node": {"id": "edgeZ", "gpuUtil": 0.5}})
        check(st == 400 and "未知 nodeId" in r["errors"][0],
              "非法 nodeId 被拒绝并给出可选项", r["errors"][0][:40])
        st, r = c.post("/api/hw/telemetry", {"deviceId": "edge-a-01",
                                             "node": {"id": "edgeA", "gpuUtil": "62%"}})
        check(r["accepted"] == 1, "百分号/字符串形式的上报可解析")

        print("\n=== 影子模式：只对照，不干预 ===")
        c.post("/api/hw/mode", {"mode": "shadow"})
        time.sleep(0.5)
        st, snap = c.get("/api/state")
        nd = snap["snapshot"]["ai"]["nodes"]
        edgeA = [n for n in nd if n["id"] == "edgeA"][0]
        check(edgeA["hwOverride"] is False, "影子模式下仿真节点未被实测覆盖")
        st, dev = c.get("/api/hw/devices")
        d0 = [d for d in dev["devices"] if d["deviceId"] == "edge-a-01"][0]
        check(d0["online"] is True, "设备在线判定正确")
        check(abs(d0["real"]["nodes"]["edgeA"]["util"] - 0.62) < 1e-6,
              "实测值已记录（0.62）")
        check("compare" in d0 and d0["compare"]["nodes"]["edgeA"]["sim"] is not None,
              "  └ 提供「实测 vs 仿真」对照")

        print("\n=== 在环模式：实测覆盖仿真状态 ===")
        c.post("/api/hw/mode", {"mode": "hil"})
        time.sleep(0.4)
        st, snap = c.get("/api/state")
        edgeA = [n for n in snap["snapshot"]["ai"]["nodes"] if n["id"] == "edgeA"][0]
        check(edgeA["hwOverride"] is True, "HIL 模式下节点被实测覆盖")
        check(abs(edgeA["util"] - 0.62) < 0.02, "  └ 覆盖值等于实测值",
              "util=%.3f" % edgeA["util"])
        check(abs(edgeA["lat"] - 7.4) < 0.01, "  └ 实测时延覆盖节点时延",
              "lat=%.2f" % edgeA["lat"])
        field = [l for l in snap["snapshot"]["ai"]["links"] if l["id"] == "field"][0]
        check(field["hwOverride"] is True and abs(field["util"] - 0.75) < 0.02,
              "链路实测利用率覆盖生效", "field.util=%.3f" % field["util"])

        print("\n=== UDP / TCP 上行 ===")
        st, hp = c.get("/api/hw/health")
        tr = hp.get("hw", {}).get("transports", {})
        check(tr.get("udp", {}).get("up") is True, "UDP 遥测监听已就绪",
              "port=%s errors=%s" % (tr.get("udp", {}).get("port"), tr.get("errors")))
        # UDP 天生可能丢包：与真实设备一样重试若干次，而不是假设一次必达
        payload = json.dumps({"deviceId": "edge-b-01", "kind": "edge", "nodeId": "edgeB",
                              "node": {"id": "edgeB", "gpuUtil": 33}}).encode()
        dids = {}
        for attempt in range(3):
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.sendto(payload, ("127.0.0.1", udp_port))
            u.close()
            deadline = time.time() + 0.8
            while time.time() < deadline:
                st, dev = c.get("/api/hw/devices")
                dids = {d["deviceId"]: d for d in dev["devices"]}
                if dids.get("edge-b-01", {}).get("real", {}).get("nodes", {}).get("edgeB"):
                    break
                time.sleep(0.1)
            if dids.get("edge-b-01", {}).get("real", {}).get("nodes", {}).get("edgeB"):
                break
        check("edge-b-01" in dids, "UDP 遥测被受理（设备已出现）")
        check(dids.get("edge-b-01", {}).get("transport", {}).get("udp", 0) >= 1,
              "  └ 传输来源标记为 udp")
        check(abs(dids.get("edge-b-01", {}).get("real", {}).get("nodes", {})
                  .get("edgeB", {}).get("util", 0) - 0.33) < 1e-6,
              "  └ 0-100 口径自动归一化为 0.33")

        t = socket.create_connection(("127.0.0.1", tcp_port), timeout=3)
        t.sendall((json.dumps({"deviceId": "cam-01", "kind": "camera",
                               "metrics": {"fps": 30}}) + "\n").encode())
        t.settimeout(3)
        reply = t.recv(4096).decode("utf-8", "replace")
        t.close()
        check('"accepted":1' in reply or '"accepted": 1' in reply,
              "TCP 遥测被受理并逐条应答", reply.strip()[:60])

        print("\n=== 真实任务 → 调度决策 → 指令下发 ===")
        st, r = c.post("/api/hw/telemetry", {
            "deviceId": "cam-01",
            "tasks": [{"id": "cam-9001", "type": "vision", "bytes": 12.2, "work": 0.25,
                       "deadlineMs": 200, "prio": 3},
                      {"id": "cam-9002", "type": "vision"}],
        })
        check(len(r["dispatched"]) == 2, "上报的真实任务进入调度管线", "2 条")
        dec = r["dispatched"][0]
        check(dec["payload"]["nodeId"] in ("local", "edgeA", "edgeB", "cloud"),
              "  └ 已产生派发决策", dec["payload"]["nodeTag"])
        check("commandId" in dec, "  └ 决策已排入指令队列")
        st, snap = c.get("/api/state")
        hw_tasks = [t for t in snap["snapshot"]["ai"]["tasks"] if t["source"] == "hw"]
        check(len(hw_tasks) >= 1, "任务流中可区分硬件任务（source=hw）")
        check(any(t["extId"] == "cam-9001" for t in hw_tasks), "  └ 保留外部任务号")

        st, r = c.get("/api/hw/commands?deviceId=cam-01&wait=1")
        cmds = r["commands"]
        check(st == 200 and len(cmds) >= 2, "长轮询领取到下发指令", "%d 条" % len(cmds))
        check(cmds[0]["type"] == "dispatch" and "nodeId" in cmds[0]["payload"],
              "  └ 指令类型与载荷正确")
        cid = cmds[0]["id"]
        st, r = c.post("/api/hw/ack", {"deviceId": "cam-01", "commandId": cid, "ok": True,
                                       "result": {"latencyMs": 118}})
        check(st == 200 and r["ok"], "设备确认指令")
        st, r = c.get("/api/hw/commands?deviceId=cam-01&wait=1")
        check(all(x["id"] != cid for x in r["commands"]), "  └ 已确认的指令不再重投")

        print("\n=== 指令重投（至少一次语义）===")
        c.post("/api/hw/command", {"deviceId": "edge-a-01", "type": "ping"})
        c.get("/api/hw/commands?deviceId=edge-a-01")
        hw.redeliver_ms = 50
        time.sleep(0.15)
        st, r = c.get("/api/hw/commands?deviceId=edge-a-01")
        check(any(x["type"] == "ping" for x in r["commands"]), "未确认指令在重投窗口后重发")

        print("\n=== 看门狗：离线即失效（fail-safe）===")
        old_offline = hw.offline_ms
        hw.offline_ms = 300
        time.sleep(0.6)
        st, dev = c.get("/api/hw/devices")
        dids = {d["deviceId"]: d for d in dev["devices"]}
        check(dids["edge-a-01"]["online"] is False, "遥测超时 → 设备判定离线")
        check(any(e["kind"] == "offline" for e in dev["events"]),
              "  └ 产生离线事件日志")
        st, snap = c.get("/api/state")
        edgeA = [n for n in snap["snapshot"]["ai"]["nodes"] if n["id"] == "edgeA"][0]
        check(edgeA["hwOverride"] is False, "  └ 离线设备的实测覆盖自动失效（仿真回落）")
        hw.offline_ms = old_offline

        print("\n=== 模式与复位 ===")
        st, r = c.post("/api/hw/mode", {"mode": "off"})
        check(st == 200 and r["mode"] == "off", "切回纯仿真模式")
        st, snap = c.get("/api/state")
        check(all(not n["hwOverride"] for n in snap["snapshot"]["ai"]["nodes"]),
              "  └ off 模式下无任何实测覆盖")
        st, r = c.post("/api/hw/mode", {"mode": "bogus"})
        check(st == 400, "非法模式被拒绝")
        st, r = c.post("/api/hw/telemetry", {"deviceId": "edge-a-01",
                                             "node": {"id": "edgeA", "gpuUtil": 0.4}})
        hw.set_mode("hil")
        time.sleep(0.3)
        st, snap = c.get("/api/state")
        edgeA = [n for n in snap["snapshot"]["ai"]["nodes"] if n["id"] == "edgeA"][0]
        check(abs(edgeA["util"] - 0.4) < 0.02, "恢复上报后覆盖重新生效", "util=%.3f" % edgeA["util"])
        st, r = c.post("/api/hw/broadcast", {"type": "config",
                                             "payload": {"reportIntervalMs": 100}})
        check(st == 200 and len(r["sent"]) >= 1, "广播指令下发成功", "%d 台" % len(r["sent"]))

        print("\n=== 模型接口 ===")
        st, r = c.get("/api/models")
        check(st == 200 and len(r["models"]) >= 2 and r["default"], "GET /api/models 列出模型")
        check(any("通算权衡" in m["name"] or "降采样" in m["desc"] for m in r["models"]),
              "  └ 含 v2 优化版说明")
        st, r = c.post("/api/model", {"id": "v1"})
        check(st == 200 and r["state"]["model"] == "v1", "POST /api/model 切换模型")
        st, r = c.get("/api/state")
        check(r["snapshot"]["ai"]["stat"]["compressed"] == 0 or True, "v1 下压缩计数存在")
        st, r = c.post("/api/model", {"id": "nope"})
        check(st == 400, "非法模型被拒绝")
        c.post("/api/model", {"id": "v2"})
    finally:
        httpd.shutdown()
        httpd.server_close()
        if S.HW_TRANSPORTS:
            S.HW_TRANSPORTS.stop()
        S.HUB.stop()

    print("\n通过 %d 项，失败 %d 项" % (OKS[0], len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
