# -*- coding: utf-8 -*-
"""
模型实验台：在同一场景矩阵上对比 / 消融 / 调参调度模型，用数据决定模型选型。

    python -m backend.model_lab matrix  --models v1,v2 --duration 60000
    python -m backend.model_lab ablate  --base v2 --duration 60000
    python -m backend.model_lab params  --base v2 --sweep compressRatio=0.2,0.35,0.5
    python -m backend.model_lab serve-ready   # 校验默认模型确实不劣于 v1

所有子命令都支持 --scenes/--duration/--dt/--seed，默认全场景矩阵 120s / 2ms。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import engine, experiment, models  # noqa: E402


def _parse(argv):
    opts = {"scenes": None, "duration": 120000.0, "dt": engine.STEP_MS, "seed": engine.SEED,
            "models": None, "base": models.DEFAULT, "sweep": [], "val_seeds": [20261006, 77777],
            "cmd": argv[0] if argv else "matrix"}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--val-seeds" and i + 1 < len(argv):
            opts["val_seeds"] = [int(x) for x in argv[i + 1].split(",") if x.strip()]; i += 2; continue
        if a in ("--weights", "-w") and i + 1 < len(argv):
            w = {}
            for pair in argv[i + 1].split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    w[k.strip()] = float(v)
            opts["weights"] = w or None
            i += 2; continue
        if a in ("--scenes", "-s") and i + 1 < len(argv):
            opts["scenes"] = [x for x in argv[i + 1].split(",") if x]; i += 2; continue
        if a in ("--duration", "-d") and i + 1 < len(argv):
            opts["duration"] = float(argv[i + 1]); i += 2; continue
        if a == "--dt" and i + 1 < len(argv):
            opts["dt"] = float(argv[i + 1]); i += 2; continue
        if a == "--seed" and i + 1 < len(argv):
            opts["seed"] = int(argv[i + 1]); i += 2; continue
        if a in ("--models", "-m") and i + 1 < len(argv):
            opts["models"] = [x for x in argv[i + 1].split(",") if x]; i += 2; continue
        if a == "--base" and i + 1 < len(argv):
            opts["base"] = argv[i + 1]; i += 2; continue
        if a == "--sweep" and i + 1 < len(argv):
            opts["sweep"].append(argv[i + 1]); i += 2; continue
        i += 1
    return opts


def cmd_matrix(o):
    ids = o["models"] or [m["id"] for m in models.catalog()]
    res = experiment.run_matrix(ids, scenes=o["scenes"], duration_ms=o["duration"],
                                dt=o["dt"], seed=o["seed"], weights=o.get("weights"))
    print(res["summary"])
    return res


def cmd_ablate(o):
    res = experiment.ablation(base_id=o["base"], scenes=o["scenes"], duration_ms=o["duration"],
                              dt=o["dt"], seed=o["seed"], weights=o.get("weights"))
    print(res["summary"])
    print("\n已写入 backend/data/model-ablation.txt")
    return res


def cmd_params(o):
    """参数扫描：对每个 key=v1,v2,... 生成派生模型并对比。"""
    base = models.MODELS[o["base"]]
    variants = [o["base"]]
    for spec in o["sweep"]:
        key, _, vals = spec.partition("=")
        for v in [x for x in vals.split(",") if x]:
            vv = float(v) if "." in v or "e" in v.lower() else int(v)
            m = dict(base)
            m = {"id": "%s@%s=%s" % (o["base"], key, v), "name": "%s %s=%s" % (base["short"], key, v),
                 "short": "%s=%s" % (key, v), "desc": "参数扫描", "formula": base["formula"],
                 "flags": dict(base["flags"]), "params": dict(base["params"]), "cards": base["cards"],
                 "default": False}
            m["params"][key] = vv
            models.MODELS[m["id"]] = m
            variants.append(m["id"])
    res = experiment.run_matrix(variants, scenes=o["scenes"], duration_ms=o["duration"],
                                dt=o["dt"], seed=o["seed"], weights=o.get("weights"))
    print(res["summary"])
    return res


def cmd_serve_ready(o):
    """冒烟校验：默认模型在场景矩阵上不劣于 v1（用于改动模型后的回归）。"""
    ids = ["v1", models.DEFAULT]
    res = experiment.run_matrix(ids, scenes=o["scenes"], duration_ms=o["duration"],
                                dt=o["dt"], seed=o["seed"])
    print(res["summary"])
    cur = {r["model"]: r for r in res["rows"]}
    d = models.DEFAULT
    if d not in cur or "v1" not in cur:
        print("!! 缺少对照模型"); return res
    ok = cur[d]["score"] <= cur["v1"]["score"] + 1e-9
    print("\n默认模型(%s) score=%.3f  vs  v1 score=%.3f  →  %s"
          % (d, cur[d]["score"], cur["v1"]["score"], "通过（不劣于 v1）" if ok else "不通过！"))
    return res


# 参与组合搜索的开关（其余开关固定为默认值：compress 为本次核心能力，始终开启）
SEARCH_FLAGS = ["wfsShare", "leastHarm", "normOccupancy", "slackEnergy", "allocShare",
                "liveUrgency"]


def cmd_search(o):
    """开关组合穷举 + 留出集验证。

    第一阶段（选型）：在 60s 矩阵上穷举 2^k 个开关组合，按 score 排序。
    第二阶段（验证）：把前若干名放到**不同时长 + 不同随机种子**的留出集上复测，
    只有同时战胜 v1 才算真正可用 —— 避免在单一矩阵上过拟合。
    """
    base_id = o["base"]
    flags = SEARCH_FLAGS
    ids = []
    for mask in range(1 << len(flags)):
        ov = {f: bool((mask >> i) & 1) for i, f in enumerate(flags)}
        v = models.variant(base_id, **ov)
        models.MODELS[v["id"]] = v
        ids.append(v["id"])
    print("组合数 %d，选型矩阵 %.0fms …" % (len(ids), o["duration"] / 2))
    sel = experiment.run_matrix(ids, scenes=o["scenes"], duration_ms=o["duration"] / 2,
                                dt=o["dt"], seed=o["seed"])
    top = [r["model"] for r in sel["rows"][:4]]
    print("\n[选型] 前 6 名（%.0fms / seed %d）" % (o["duration"] / 2, o["seed"]))
    for r in sel["rows"][:6]:
        print("  %-58s score=%.3f  Δlat=%.2f%%  Δmiss=%.2fpt" % (
            r["model"][:58], r["score"], r["dLatPct"], r["dMissPt"]))

    print("\n[验证] 留出集：%.0fms × seeds %s" % (o["duration"], o["val_seeds"]))
    cand = top + ["v1"]
    all_rows = {}
    for sd in o["val_seeds"]:
        v = experiment.run_matrix(cand, scenes=o["scenes"], duration_ms=o["duration"],
                                  dt=o["dt"], seed=sd)
        print("\n  seed=%d" % sd)
        print(v["summary"].split("关键任务类别")[0].strip()[:1600])
        for r in v["rows"]:
            all_rows.setdefault(r["model"], []).append((sd, r["score"], r["dLatPct"], r["dMissPt"]))
    print("\n[结论] 各候选在留出集上的表现（每行：种子/score/Δlat/Δmiss）")
    best = None
    for mid, rows in sorted(all_rows.items(), key=lambda kv: sum(x[1] for x in kv[1])):
        v1s = dict((s, sc) for s, sc, _, _ in all_rows.get("v1", []))
        win = all(sc < v1s.get(s, 1e9) for s, sc, _, _ in rows)
        print("  %-58s %s  %s" % (mid[:58],
              "  ".join("%d:%.1f/%+.1f%%/%+.1fpt" % x for x in rows),
              "✓ 全面优于 v1" if win else "✗ 未全面胜出"))
        if win and (best is None or sum(x[1] for x in rows) < best[1]):
            best = (mid, sum(x[1] for x in rows))
    if best:
        print("\n>>> 建议采用: %s" % best[0])
        print("    开关: %s" % models.MODELS[best[0]]["flags"])
    else:
        print("\n>>> 没有候选在留出集上全面胜出，保持现默认：%s" % models.DEFAULT)
    return sel


CMDS = {"matrix": cmd_matrix, "ablate": cmd_ablate, "params": cmd_params,
        "serve-ready": cmd_serve_ready, "search": cmd_search}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    o = _parse(argv)
    fn = CMDS.get(o["cmd"])
    if not fn:
        print("未知子命令: %s（可选 %s）" % (o["cmd"], ",".join(CMDS)))
        return 2
    t0 = time.time()
    fn(o)
    print("\n用时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

