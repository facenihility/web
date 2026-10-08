# -*- coding: utf-8 -*-
"""
后端接口冒烟测试：自己拉起一个服务实例（随机端口），逐项验证 REST / SSE / 批量实验。

    python backend/tests/test_api.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
# 测试会产生实验记录：把数据目录指到临时目录，避免覆盖交付物 backend/data/report.txt
os.environ.setdefault("DSH_DATA_DIR", os.path.join(tempfile.gettempdir(), "dsh_test_data"))
sys.path.insert(0, ROOT)

from backend import server as S  # noqa: E402

FAILS = []
OKS = [0]


def check(cond, msg):
    if cond:
        OKS[0] += 1
        print("  OK   %s" % msg)
    else:
        FAILS.append(msg)
        print("  FAIL %s" % msg)


def main():
    httpd = S.serve("127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    print("服务: %s" % base)

    def get(path, timeout=10):
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)

    def getj(path, timeout=10):
        st, body, _ = get(path, timeout)
        return st, json.loads(body)

    def postj(path, obj, timeout=60):
        req = urllib.request.Request(
            base + path, data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    try:
        print("\n=== 健康检查与元数据 ===")
        st, h = getj("/api/health")
        check(st == 200 and h.get("ok"), "/api/health 返回 ok")
        check(h["sim"]["scene"] in S.engine.SCENES, "健康检查包含运行状态")
        check(len(h["engine"]["scenes"]) == 6, "6 个场景可选")
        st, cat = getj("/api/catalog")
        check(st == 200 and len(cat["catalog"]["scenes"]) == 6, "/api/catalog 场景清单")
        check(len(cat["catalog"]["nodes"]) == 4 and len(cat["catalog"]["links"]) == 3,
              "/api/catalog 节点/链路定义")

        print("\n=== 静态页面 ===")
        st, body, hdr = get("/")
        check(st == 200 and "工业智算网" in body, "GET / 返回后端驱动版页面")
        check("WorkBuddyBackend" in body, "  └ 已注入后端接入层")
        st, body2, _ = get("/classic")
        check(st == 200 and "WorkBuddyBackend" not in body2, "GET /classic 返回原始页面（未注入）")
        st, body3, _ = get("/static/adapter.js")
        check(st == 200 and "hydrate" in body3, "GET /static/adapter.js 可访问")

        print("\n=== 状态快照 ===")
        st, snap = getj("/api/state")
        s0 = snap["snapshot"]
        check(st == 200 and "ai" in s0 and "bl" in s0, "/api/state 返回双轨快照")
        check(len(s0["ai"]["nodes"]) == 4 and len(s0["ai"]["links"]) == 3, "  └ 节点/链路数量正确")
        check(set(s0["ai"]["hist"].keys()) == {"lat", "p95", "bw", "gpu"}, "  └ 趋势序列齐备")
        check(s0["ai"]["mode"] == "ai" and s0["bl"]["mode"] == "baseline", "  └ 双轨模式正确")

        print("\n=== 仿真推进 ===")
        S.HUB.control("speed", 1.5)
        S.HUB.control("pause", False)
        _, a = getj("/api/state")
        t0 = a["snapshot"]["ai"]["t"]
        time.sleep(1.2)
        _, b = getj("/api/state")
        t1 = b["snapshot"]["ai"]["t"]
        check(t1 > t0 + 50, "仿真时钟推进 (%.1fms → %.1fms)" % (t0, t1))
        check(b["snapshot"]["ai"]["stat"]["n"] >= a["snapshot"]["ai"]["stat"]["n"],
              "完成任务数单调不减")

        print("\n=== 控制指令 ===")
        st, r = postj("/api/control", {"action": "scene", "value": "agv"})
        check(st == 200 and r["state"]["scene"] == "agv", "切换场景 → agv")
        check(abs(r["state"]["rate"] - 26) < 1e-9, "  └ 场景默认强度 26/s")
        st, r = postj("/api/control", {"action": "rate", "value": 15})
        check(r["state"]["rate"] == 15, "设置任务强度 15/s")
        st, r = postj("/api/control", {"action": "speed", "value": 2.0})
        check(abs(r["state"]["speed"] - 2.0) < 1e-9, "设置速度 2.0×")
        st, r = postj("/api/control", {"action": "pause", "value": True})
        check(r["state"]["paused"] is True, "暂停")
        _, p1 = getj("/api/state")
        time.sleep(0.6)
        _, p2 = getj("/api/state")
        check(abs(p2["snapshot"]["ai"]["t"] - p1["snapshot"]["ai"]["t"]) < 1e-6, "  └ 暂停后时钟静止")
        postj("/api/control", {"action": "pause", "value": False})
        st, r = postj("/api/control", {"action": "reset"})
        check(r["state"]["tSim"] == 0, "重置 → 仿真时钟归零")
        st, r = postj("/api/control", {"action": "nope"})
        check(st == 400, "非法指令返回 400")
        st, r = postj("/api/config", {"scene": "normal", "rate": 11, "speed": 1.5})
        check(st == 200 and r["state"]["scene"] == "normal", "批量配置 /api/config")

        print("\n=== 指标 / 任务 / 日志 ===")
        st, m = getj("/api/metrics")
        check(st == 200 and "delta" in m["metrics"] and "avgLat" in m["metrics"]["delta"],
              "/api/metrics 含双轨差值")
        st, tk = getj("/api/tasks?limit=10")
        check(st == 200 and "rows" in tk["data"], "/api/tasks 任务表")
        st, lg = getj("/api/logs?limit=5")
        check(st == 200 and "rows" in lg["data"], "/api/logs 决策日志")

        print("\n=== SSE 实时推流 ===")
        got = []
        req = urllib.request.Request(base + "/api/stream")
        with urllib.request.urlopen(req, timeout=8) as resp:
            check(resp.headers.get("Content-Type", "").startswith("text/event-stream"),
                  "SSE Content-Type 正确")
            buf = b""
            deadline = time.time() + 6
            # 读到「一个完整的 state 事件」为止（快照体积随任务数增长，不能定长读取）
            while time.time() < deadline:
                part = resp.read(2048)
                if not part:
                    break
                buf += part
                text = buf.decode("utf-8", "replace")
                if "event: state\ndata: " in text and "\n\n" in text.split("event: state\ndata: ", 1)[1]:
                    break
            got.append(buf.decode("utf-8", "replace"))
        check("event: hello" in got[0], "  └ 收到 hello 事件")
        check("event: state" in got[0], "  └ 收到 state 事件")
        ev = got[0].split("event: state\ndata: ", 1)[1].split("\n\n", 1)[0]
        ss = json.loads(ev)
        check("ai" in ss and "nodes" in ss["ai"], "  └ state 载荷可解析且结构正确")

        print("\n=== 批量实验与报告 ===")
        t_start = time.time()
        st, r = postj("/api/experiment", {"scenes": ["normal", "agv"], "durationMs": 8000,
                                          "dtMs": 2, "label": "smoke"}, timeout=180)
        check(st == 200 and r.get("ok"), "POST /api/experiment 完成（%.1fs）" % (time.time() - t_start))
        run = r.get("run", {})
        rep = run.get("report", "")
        check("===== normal =====" in rep and "===== agv =====" in rep, "  └ 报告包含两个场景")
        check("DONE" in rep, "  └ 报告以 DONE 结束")
        check(run.get("summary", {}).get("totalScenes") == 2, "  └ 汇总统计生成")
        st, txt, hdr = get("/api/report?format=txt")
        check("===== agv =====" in txt, "/api/report?format=txt 与最近实验一致")
        st, rj = getj("/api/report?format=json")
        check(rj["run"]["id"] == run["id"], "/api/report?format=json 返回同一实验")
        st, rl = getj("/api/runs")
        check(any(x["id"] == run["id"] for x in rl["runs"]), "/api/runs 列出实验记录")
        st, r1 = getj("/api/runs/" + run["id"])
        check(st == 200 and r1["run"]["id"] == run["id"], "/api/runs/{id} 可取回完整结果")

        print("\n=== 错误处理 ===")
        try:
            get("/api/does-not-exist")
            check(False, "未知接口返回 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404, "未知接口返回 404")
    finally:
        httpd.shutdown()
        httpd.server_close()
        S.HUB.stop()

    print("\n通过 %d 项，失败 %d 项" % (OKS[0], len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
