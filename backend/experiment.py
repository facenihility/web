# -*- coding: utf-8 -*-
"""
批量实验：离线跑完整场景矩阵，产出与 .test/report.txt 同格式的对照报告。

用于答辩/汇报材料的「一键复现」：结果同时落盘到 backend/data/。
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import engine, models
from .hub import kpi

HERE = os.path.dirname(os.path.abspath(__file__))
# 数据目录可用环境变量覆盖：测试跑实验时指向临时目录，
# 避免把交付物（data/report.txt、data/runs）覆盖成测试噪声。
DATA_DIR = os.environ.get("DSH_DATA_DIR") or os.path.join(HERE, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
KEEP_RUNS = 30

_lock = threading.Lock()
_latest = {"id": None}


def _ensure_dirs():
    os.makedirs(RUNS_DIR, exist_ok=True)


def run_batch(scenes=None, duration_ms=120000.0, dt=None, seed=None, label=None, model_id=None):
    """跑一批场景，返回结构化结果 + 文本报告。"""
    dt = float(dt or engine.STEP_MS)
    seed = int(seed if seed is not None else engine.SEED)
    mid = model_id if model_id in models.MODELS else models.DEFAULT
    model = models.MODELS[mid]
    names = list(scenes) if scenes else list(engine.SCENES.keys())
    for n in names:
        if n not in engine.SCENES:
            raise ValueError("未知场景: %s" % n)

    t_start = time.time()
    blocks = []
    detail = []
    for name in names:
        params = engine.clone_scene(name)
        ai = engine.Sim("ai", seed, model=model)
        bl = engine.Sim("baseline", seed, model=model)
        engine.run_steps(ai, params, duration_ms, dt, 0.0)
        engine.run_steps(bl, params, duration_ms, dt, 0.0)
        a, b = kpi(ai), kpi(bl)

        def d(x, y, invert=False):
            if not y or x <= 0:
                return None
            v = (x - y) / y * 100.0
            return {"pct": round(v, 2), "better": bool(v < 0) if not invert else bool(v > 0)}

        detail.append({
            "scene": name,
            "sceneLabel": engine.SCENE_LABELS.get(name, name),
            "rate": params["rate"],
            "ai": a,
            "bl": b,
            "delta": {
                "avgLat": d(a["avgLat"], b["avgLat"]),
                "p95": d(a["p95"], b["p95"]),
                "missRate": d(a["missRate"], b["missRate"]),
                "energyPerTask": d(a["energyPerTask"], b["energyPerTask"]),
                "throughput": d(a["throughput"], b["throughput"], invert=True),
                "gpu": d(a["gpu"], b["gpu"]),
                "peakBw": d(a["peakBw"], b["peakBw"]),
            },
            "nodes": [{"id": n.id, "done": n.done} for n in ai.nodes],
            "links": [{"id": l.id, "util": round(l.util, 4)} for l in ai.links],
            "types": ai.typeStat,
            "compress": {"count": ai.stat.get("compressed", 0),
                         "savedMB": round(ai.stat.get("compressSavedMB", 0.0), 3),
                         "extraTFLOP": round(ai.stat.get("compressExtraTFLOP", 0.0), 3)},
        })
        blocks.append(engine.report_block(name, ai, bl, duration_ms / 1000.0))

    proto = ("# 工业智算网 · 通算协同调度仿真 批量实验结果\n"
             "# 模型: %s (%s)\n"
             "# 协议: 子步长 %.2fms / 每场景 %.0fms 仿真时间 / 固定种子 %d / 双轨同任务流\n"
             "# 说明: 「AI」= AI 通算协同调度, 「基线」= 静态规则映射 + 等权均分带宽\n"
             % (model["name"], mid, dt, duration_ms, seed))
    report = proto + "\n".join(blocks) + "\n\nDONE\n"

    run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(t_start))
    result = {
        "id": run_id,
        "label": label or "场景矩阵",
        "model": mid,
        "modelName": model["name"],
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_start)),
        "protocol": {"dtMs": dt, "durationMs": duration_ms, "seed": seed, "scenes": names,
                     "model": mid},
        "elapsedSec": round(time.time() - t_start, 2),
        "scenes": detail,
        "summary": _summary(detail),
        "report": report,
    }

    with _lock:
        _ensure_dirs()
        _latest["id"] = run_id
        with open(os.path.join(RUNS_DIR, run_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        with open(os.path.join(DATA_DIR, "report.txt"), "w", encoding="utf-8") as f:
            f.write(report)
        _prune()
    return result


def _summary(detail):
    """跨场景汇总：AI 相对基线的平均改善。"""
    keys = ["avgLat", "p95", "missRate", "energyPerTask"]
    agg = {}
    for k in keys:
        vals = [d["delta"][k]["pct"] for d in detail if d["delta"].get(k)]
        agg[k] = round(sum(vals) / len(vals), 2) if vals else None
    wins = {k: 0 for k in keys}
    for d in detail:
        for k in keys:
            v = d["delta"].get(k)
            if v and v["better"]:
                wins[k] += 1
    return {"avgPct": agg, "wins": wins, "totalScenes": len(detail)}


def _prune():
    try:
        files = sorted(f for f in os.listdir(RUNS_DIR) if f.endswith(".json"))
        for f in files[:-KEEP_RUNS]:
            os.remove(os.path.join(RUNS_DIR, f))
    except OSError:
        pass


# =========================================================================
# 模型选型：多模型矩阵对照 + 单开关消融
# =========================================================================
def _score_matrix(ai_rows, bl_rows):
    """AI 相对基线的综合得分（越小越好）：时延/超时/能耗的加权归一化改善。"""
    n = len(ai_rows)
    if not n:
        return 0.0, {}
    d_lat = sum((a["avgLat"] - b["avgLat"]) / b["avgLat"] for a, b in zip(ai_rows, bl_rows)) / n * 100
    d_p95 = sum((a["p95"] - b["p95"]) / b["p95"] for a, b in zip(ai_rows, bl_rows)) / n * 100
    d_miss = sum(a["missRate"] - b["missRate"] for a, b in zip(ai_rows, bl_rows)) / n
    d_eng = sum((a["energyPerTask"] - b["energyPerTask"]) / b["energyPerTask"]
                for a, b in zip(ai_rows, bl_rows)) / n * 100
    info = {"dLatPct": round(d_lat, 2), "dP95Pct": round(d_p95, 2),
            "dMissPt": round(d_miss, 2), "dEnergyPct": round(d_eng, 2)}
    # 权重：平均时延 1、P95 0.5、超时率(百分点) 2、能耗 0.3
    score = d_lat * 1.0 + d_p95 * 0.5 + d_miss * 2.0 + d_eng * 0.3
    return round(score, 3), info


# 关键任务类别（回归红线）
CRITICAL_TYPES = {"robot": "机器人控制", "agv": "AGV 调度", "vision": "机器视觉"}


def apply_weights(sim, weights, locks=None):
    """把节点权重写到仿真体上（只作用于 AI 轨）。"""
    out = {}
    for nid, w in (weights or {}).items():
        nd = sim.nodeById.get(nid)
        if nd is None:
            continue
        nd.weight = max(0.1, min(4.0, float(w)))
        nd.locked = bool(locks and nid in locks)
        out[nid] = nd.weight
    return out


def _type_deltas(scene_rows):
    """逐任务类别的 AI−基线 差异，用于识别「均值变好但某类被牺牲」的伪优化。"""
    out = {}
    for row in scene_rows:
        ai, bl = row["ai"], row["bl"]
        for t, name in CRITICAL_TYPES.items():
            ta = ai["typeStat"].get(t)
            tb = bl["typeStat"].get(t)
            if not ta or not tb or ta["n"] < 20 or tb["n"] < 20:
                continue
            d = out.setdefault(t, {"name": name, "dLatPct": 0.0, "dMissPt": 0.0, "scenes": 0})
            d["dLatPct"] += (ta["lat"] / ta["n"] - tb["lat"] / tb["n"]) / max(tb["lat"] / tb["n"], 1e-9) * 100
            d["dMissPt"] += ta["miss"] / ta["n"] * 100 - tb["miss"] / tb["n"] * 100
            d["scenes"] += 1
    for t, d in out.items():
        if d["scenes"]:
            d["dLatPct"] = round(d["dLatPct"] / d["scenes"], 2)
            d["dMissPt"] = round(d["dMissPt"] / d["scenes"], 2)
    return out


def worst_regressions(rows, lat_tol=5.0, miss_tol=1.0):
    """列出所有违反红线（关键类别明显劣于基线）的模型。"""
    bad = []
    for r in rows:
        for t, d in r.get("typeDeltas", {}).items():
            if d["dMissPt"] > miss_tol or d["dLatPct"] > lat_tol:
                bad.append({"model": r["model"], "type": t, "name": d["name"],
                            "dLatPct": d["dLatPct"], "dMissPt": d["dMissPt"]})
    return bad


def run_matrix(model_ids, scenes=None, duration_ms=120000.0, dt=None, seed=None,
               include_baseline=True, weights=None):
    """对多个模型跑同一场景矩阵，返回对照表（用于模型选型/消融）。

    weights: {nodeId: 权重}，只作用于 AI 轨（基线是静态规则映射，权重无关）。
    """
    dt = float(dt or engine.STEP_MS)
    seed = int(seed if seed is not None else engine.SEED)
    names = list(scenes) if scenes else list(engine.SCENES.keys())
    rows = []
    per_scene = {}
    for mid in model_ids:
        model = models.MODELS[mid]
        ai_rows, bl_rows = [], []
        scene_rows = []
        for name in names:
            params = engine.clone_scene(name)
            ai = engine.Sim("ai", seed, model=model)
            bl = engine.Sim("baseline", seed, model=model)
            if weights:
                apply_weights(ai, weights)
            engine.run_steps(ai, params, duration_ms, dt, 0.0)
            engine.run_steps(bl, params, duration_ms, dt, 0.0)
            a, b = kpi(ai), kpi(bl)
            a["compressed"] = ai.stat.get("compressed", 0)
            a["typeStat"] = ai.typeStat
            a["weights"] = engine.node_weight_report(ai)
            b["typeStat"] = bl.typeStat
            ai_rows.append(a)
            bl_rows.append(b)
            scene_rows.append({"scene": name, "ai": a, "bl": b})
        score, info = _score_matrix(ai_rows, bl_rows)
        rows.append({"model": mid, "name": model["name"], "score": score, **info,
                     "typeDeltas": _type_deltas(scene_rows), "scenes": scene_rows})
        per_scene[mid] = scene_rows
    rows.sort(key=lambda r: r["score"])
    bad = worst_regressions(rows)
    return {"scenes": names, "protocol": {"dtMs": dt, "durationMs": duration_ms, "seed": seed},
            "rows": rows, "regressions": bad,
            "summary": _matrix_text(rows, names, bad)}


def _matrix_text(rows, scenes, bad=None):
    lines = []
    lines.append("模型选型对照（同一场景矩阵、同种子；score 越小越好，负值=优于基线）")
    lines.append("")
    hdr = "%-38s %9s %9s %9s %9s %9s" % ("模型", "score", "Δ均时延%", "ΔP95%", "Δ超时pt", "Δ能耗%")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in rows:
        lines.append("%-38s %9.3f %9.2f %9.2f %9.2f %9.2f" % (
            r["model"][:38], r["score"], r["dLatPct"], r["dP95Pct"], r["dMissPt"], r["dEnergyPct"]))
    lines.append("")
    lines.append("关键任务类别（机器人/AGV/视觉）的 AI−基线 差异 —— 红线检查，正数=劣于基线")
    hdr3 = "%-38s %-10s %10s %10s" % ("模型", "类别", "Δ时延%", "Δ超时pt")
    lines.append(hdr3)
    lines.append("-" * len(hdr3))
    for r in rows:
        td = r.get("typeDeltas") or {}
        if not td:
            continue
        for t in ("robot", "agv", "vision"):
            if t in td:
                d = td[t]
                flag = "  ← 红线" if (d["dMissPt"] > 1.0 or d["dLatPct"] > 5.0) else ""
                lines.append("%-38s %-10s %10.2f %10.2f%s" % (
                    r["model"][:38], d["name"], d["dLatPct"], d["dMissPt"], flag))
    if bad:
        lines.append("")
        lines.append("!! 存在关键类别回归：%s" % "; ".join(
            "%s/%s(Δmiss %+.1fpt, Δlat %+.1f%%)" % (b["model"], b["name"], b["dMissPt"], b["dLatPct"])
            for b in bad))
    else:
        lines.append("")
        lines.append("关键类别红线：全部通过（无类别显著劣于基线）")
    lines.append("")
    lines.append("逐场景 Δ均时延% / Δ超时pt（AI − 基线；负数=更好）")
    hdr2 = "%-24s" % "场景" + "".join("%18s" % r["model"][:17] for r in rows)
    lines.append(hdr2)
    lines.append("-" * len(hdr2))
    for i, sc in enumerate(scenes):
        cells = ""
        for r in rows:
            s = r["scenes"][i]
            dl = (s["ai"]["avgLat"] - s["bl"]["avgLat"]) / max(s["bl"]["avgLat"], 1e-9) * 100
            dm = s["ai"]["missRate"] - s["bl"]["missRate"]
            cells += "%18s" % ("%+.1f%% / %+.1fpt" % (dl, dm))
        lines.append("%-24s%s" % (sc, cells))
    return "\n".join(lines)


def ablation(base_id="v2", scenes=None, duration_ms=120000.0, dt=None, seed=None,
             flags=None, include_v1=True, progress=None, weights=None):
    """单开关敏感性：以基准模型为对照，**逐个翻转**每个开关，看对综合得分的影响。

    翻转（而不是统一关掉）很关键：基准里已经关闭的开关若统一置 False 会得到与基准
    完全相同的行，看不出它的真实影响。翻转后每一行都表示「把这个开关换到相反状态」。
    同时给出 v1 作为参照。结果落盘 data/model-ablation.txt。
    """
    base = models.MODELS[base_id]
    flags = flags or list(base["flags"].keys())
    variants = []
    if include_v1:
        variants.append("v1")
    variants.append(base_id)
    for f in flags:
        v = models.variant(base_id, **{f: not base["flags"][f]})
        models.MODELS[v["id"]] = v
        variants.append(v["id"])
    res = run_matrix(variants, scenes=scenes, duration_ms=duration_ms, dt=dt, seed=seed,
                     weights=weights)
    res["base"] = base_id
    txt = ["# 模型敏感性实验 · 基准 %s" % base_id,
           "# 基准开关: %s" % base["flags"],
           "# 权重意图: %s" % (weights or "全部 1.0（中性）"),
           "# 协议: 子步长 %.1fms / 每场景 %.0fms / 种子 %d / 场景 %s" % (
               dt or engine.STEP_MS, duration_ms, seed or engine.SEED,
               ",".join(scenes) if scenes else "全部"),
           "", res["summary"], "",
           "说明: v2~X 表示把开关 X 翻到相反状态（基准里 X=True 时该行即关闭 X）。",
           "      score 明显变大 = 该开关当前取值有正贡献；与基准相同 = 该开关在本次场景矩阵上无影响。",
           "DONE", ""]
    out = "\n".join(txt)
    with _lock:
        _ensure_dirs()
        with open(os.path.join(DATA_DIR, "model-ablation.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        with open(os.path.join(DATA_DIR, "model-matrix.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    res["text"] = out
    return res


def latest_report(fmt="txt"):
    """最近一次实验报告；若尚未跑过则即时跑一次。"""
    path = os.path.join(DATA_DIR, "report.txt")
    with _lock:
        rid = _latest["id"]
    if rid is None:
        files = []
        if os.path.isdir(RUNS_DIR):
            files = sorted(f for f in os.listdir(RUNS_DIR) if f.endswith(".json"))
        if files:
            rid = files[-1][:-5]
        else:
            res = run_batch()
            return res["report"] if fmt == "txt" else res
    if fmt == "txt":
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""
    with open(os.path.join(RUNS_DIR, rid + ".json"), "r", encoding="utf-8") as f:
        return json.load(f)


def list_runs(limit=30):
    _ensure_dirs()
    out = []
    for fn in sorted(os.listdir(RUNS_DIR), reverse=True)[:limit]:
        if not fn.endswith(".json"):
            continue
        p = os.path.join(RUNS_DIR, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            out.append({"id": d["id"], "label": d.get("label"), "createdAt": d.get("createdAt"),
                        "protocol": d.get("protocol"), "summary": d.get("summary"),
                        "elapsedSec": d.get("elapsedSec")})
        except (OSError, ValueError, KeyError):
            continue
    return out


def get_run(run_id):
    p = os.path.join(RUNS_DIR, os.path.basename(run_id) + ".json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# =========================================================================
# 命令行：python -m backend.experiment [--scenes a,b] [--duration 120000] [--out f.txt]
# =========================================================================
def _main(argv=None):
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)
    scenes, duration, dt, seed, out = None, 120000.0, engine.STEP_MS, engine.SEED, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--scenes", "-s") and i + 1 < len(argv):
            scenes = [x.strip() for x in argv[i + 1].split(",") if x.strip()]; i += 2; continue
        if a in ("--duration", "-d") and i + 1 < len(argv):
            duration = float(argv[i + 1]); i += 2; continue
        if a == "--dt" and i + 1 < len(argv):
            dt = float(argv[i + 1]); i += 2; continue
        if a == "--seed" and i + 1 < len(argv):
            seed = int(argv[i + 1]); i += 2; continue
        if a in ("--out", "-o") and i + 1 < len(argv):
            out = argv[i + 1]; i += 2; continue
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        i += 1
    res = run_batch(scenes=scenes, duration_ms=duration, dt=dt, seed=seed)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(res["report"])
    _sys.stdout.write(res["report"])
    _sys.stdout.write("\n# 用时 %.1fs，明细已保存到 %s\n"
                      % (res["elapsedSec"], os.path.join(RUNS_DIR, res["id"] + ".json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
