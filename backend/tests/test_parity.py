# -*- coding: utf-8 -*-
"""
一致性验证：Python 移植引擎 vs 原始前端 JS 内核。

用 Node 直接执行从 HTML 中抽取的 JS 内核（backend/tests/parity_harness.js），
在相同种子、相同子步长、相同场景下与 Python 引擎逐项比对。
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
sys.path.insert(0, ROOT)

from backend import engine  # noqa: E402

HTML = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台.html")
HARNESS = os.path.join(HERE, "parity_harness.js")
NODE_CANDIDATES = [
    os.environ.get("DSH_NODE"),
    shutil.which("node"),
    r"C:\Users\h'b'y\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
]

DT = 2.0
DUR = 60000.0     # 比对时长 60s 仿真时间（约 30000 子步）


def find_node():
    for p in NODE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def js_run(node, dt=DT, dur=DUR):
    out = subprocess.run([node, HARNESS, HTML, str(dt), str(dur)],
                         capture_output=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError("harness failed: " + out.stderr.decode("utf-8", "replace"))
    return json.loads(out.stdout.decode("utf-8"))


def py_run(dt=DT, dur=DUR):
    """一致性回归固定使用 v1（与前端内嵌引擎同源）。"""
    v1 = engine.models.MODELS["v1"]
    res = {}
    for scene in engine.SCENES:
        p = engine.clone_scene(scene)
        ai = engine.Sim("ai", engine.SEED, model=v1)
        bl = engine.Sim("baseline", engine.SEED, model=v1)
        engine.run_steps(ai, p, dur, dt, 0.0)
        engine.run_steps(bl, p, dur, dt, 0.0)
        res[scene] = {"ai": _summ(ai), "bl": _summ(bl)}
    return res


def _summ(s):
    return {
        "n": s.stat["n"], "t": s.t, "latSum": s.stat["latSum"], "p95": s.p95(),
        "miss": s.stat["miss"], "energy": s.stat["energy"], "throughput": s.throughput,
        "backlog": len(s.tasks), "arrivals": s.nextId - 1, "backlogTrace": s.backlogTrace,
        "nodes": [{"id": x.id, "done": x.done, "util": x.util, "utilS": x.utilS,
                   "backlog": x.backlog, "pending": x.pending} for x in s.nodes],
        "links": [{"id": x.id, "util": x.util, "utilS": x.utilS, "load": x.load} for x in s.links],
        "dist": _dist(s),
        "stat": {"local": s.stat["local"], "edge": s.stat["edge"], "cloud": s.stat["cloud"]},
        "typeStat": s.typeStat,
        "hist": {k: len(v) for k, v in s.hist.items()},
    }


def _dist(s):
    d = {}
    for k in s.typeStat:
        for n, c in s.typeStat[k]["node"].items():
            d[n] = d.get(n, 0) + c
    return d


FAIL = []


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def cmp_float(path, a, b):
    if not close(a, b, 1e-9):
        FAIL.append("%s: python=%r js=%r" % (path, a, b))


def cmp_int(path, a, b):
    if int(a) != int(b):
        FAIL.append("%s: python=%r js=%r" % (path, a, b))


def compare(py, js):
    for scene in js:
        p, j = py[scene], js[scene]
        for mode in ("ai", "bl"):
            pm, jm = p[mode], j[mode]
            for k in ("n", "miss", "arrivals", "backlog"):
                cmp_int("%s.%s.%s" % (scene, mode, k), pm[k], jm[k])
            for k in ("latSum", "p95", "energy", "throughput"):
                cmp_float("%s.%s.%s" % (scene, mode, k), pm[k], jm[k])
            for k in ("local", "edge", "cloud"):
                cmp_int("%s.%s.stat.%s" % (scene, mode, k), pm["stat"][k], jm["stat"][k])
            for i, nd in enumerate(jm["nodes"]):
                pn = pm["nodes"][i]
                cmp_int("%s.%s.node[%s].done" % (scene, mode, nd["id"]), pn["done"], nd["done"])
                cmp_float("%s.%s.node[%s].util" % (scene, mode, nd["id"]), pn["util"], nd["util"])
                cmp_float("%s.%s.node[%s].utilS" % (scene, mode, nd["id"]), pn["utilS"], nd["utilS"])
            for i, lk in enumerate(jm["links"]):
                pl = pm["links"][i]
                cmp_float("%s.%s.link[%s].util" % (scene, mode, lk["id"]), pl["util"], lk["util"])
                cmp_float("%s.%s.link[%s].load" % (scene, mode, lk["id"]), pl["load"], lk["load"])
            for t in jm["typeStat"]:
                jt, pt = jm["typeStat"][t], pm["typeStat"][t]
                cmp_int("%s.%s.type[%s].n" % (scene, mode, t), pt["n"], jt["n"])
                cmp_int("%s.%s.type[%s].miss" % (scene, mode, t), pt["miss"], jt["miss"])
                cmp_float("%s.%s.type[%s].lat" % (scene, mode, t), pt["lat"], jt["lat"])
                for nd in jt["node"]:
                    cmp_int("%s.%s.type[%s].node[%s]" % (scene, mode, t, nd),
                            pt["node"].get(nd, 0), jt["node"][nd])
            cmp_int("%s.%s.hist.lat" % (scene, mode), pm["hist"]["lat"], jm["hist"]["lat"])


def main():
    node = find_node()
    if not node:
        print("SKIP: 未找到 node，无法执行 JS 交叉验证")
        return 0
    print("node =", node)
    import time
    t0 = time.time()
    py = py_run()
    t_py = time.time() - t0
    print("python 引擎 %.1fs（%s 场景 × 2 轨 × %.0fms 仿真）" % (t_py, len(py), DUR))
    t0 = time.time()
    js = js_run(node)
    print("node   内核 %.1fs（同一协议）" % (time.time() - t0))
    compare(py, js)
    if FAIL:
        print("\n不一致 %d 处：" % len(FAIL))
        for f in FAIL[:40]:
            print("  -", f)
        return 1
    total = sum(js[s][m]["n"] for s in js for m in ("ai", "bl"))
    print("PASS: JS / Python 内核在 %d 个场景(双轨)上逐项一致（累计完成 %d 个任务）" % (len(js), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
