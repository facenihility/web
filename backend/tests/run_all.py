# -*- coding: utf-8 -*-
"""
一键回归：把本项目全部验证跑一遍并给出汇总表。

    python backend/tests/run_all.py            # 完整回归（约 1 分钟）
    python backend/tests/run_all.py --fast     # 跳过耗时的模型矩阵

覆盖：
  1. 引擎一致性    前端 JS 内核 ↔ Python 引擎（v1，逐位比对）
  2. 前端引擎回归  .test/smoke.js 输出与历史基线逐字节一致
  3. 前端渲染冒烟  .test/uicheck.js 14 项
  4. 后端接口      REST / SSE / 批量实验（test_api.py）
  5. 硬件在环      HTTP/UDP/TCP 遥测、三种模式、指令闭环、看门狗（test_hil.py）
  6. 节点权重      接口 / 意图闸门 / 控制权限 / 自适应定价 / 在线学习（test_weights.py）
  7. 页面集成      无浏览器真跑后端驱动版页面（test_frontend.py）
  8. 模型门禁      默认模型在场景矩阵上不劣于 v1（model_lab serve-ready）
  9. 前端静态校验  标签配平 / id 绑定 / 离线可用性（test_ui_static.py）
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TEST_DIR = os.path.join(ROOT, ".test")
NODE_CANDIDATES = [
    os.environ.get("DSH_NODE"),
    r"C:\Users\h'b'y\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
]
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
TOTAL = 9
_step = [0]


def node_exe():
    for p in NODE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def sha(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def run(cmd, cwd=ROOT, timeout=900):
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=ENV)
    return p.returncode, time.time() - t0, (p.stdout + p.stderr).decode("utf-8", "replace")


def banner(text):
    _step[0] += 1
    print("[%d/%d] %s" % (_step[0], TOTAL, text))


def last_line(out, prefix=""):
    rows = [l for l in out.splitlines() if l.strip() and (not prefix or l.startswith(prefix))]
    return rows[-1].strip() if rows else out.strip()[-90:]


def main():
    fast = "--fast" in sys.argv
    py = sys.executable
    node = node_exe()
    results = []

    print("=" * 78)
    print("工业智算网 · 通算协同调度平台 —— 全量回归")
    print("=" * 78)

    banner("引擎一致性（前端 JS 内核 ↔ Python 引擎，v1 逐位比对）")
    rc, dt, out = run([py, os.path.join(HERE, "test_parity.py")])
    results.append(("引擎一致性 JS↔Python", rc == 0 and "PASS" in out, dt, last_line(out, "PASS")))

    banner("前端引擎回归（.test/smoke.js 输出须与历史基线逐字节一致）")
    ref = os.path.join(TEST_DIR, "report.reference-v1.txt")
    report = os.path.join(TEST_DIR, "report.txt")
    if node and os.path.exists(ref):
        before = sha(ref)
        rc, dt, out = run([node, os.path.join(TEST_DIR, "smoke.js")])
        ok = rc == 0 and before == sha(report)
        results.append(("前端引擎回归（逐字节）", ok, dt, "一致" if ok else "输出与基线不一致"))
    else:
        results.append(("前端引擎回归（逐字节）", False, 0.0, "缺少 node 或基线文件"))
        dt = 0.0

    banner("前端渲染冒烟（.test/uicheck.js，14 项）")
    if node:
        rc, dt, out = run([node, os.path.join(TEST_DIR, "uicheck.js")])
        ui = os.path.join(TEST_DIR, "ui.txt")
        txt = open(ui, "r", encoding="utf-8").read() if os.path.exists(ui) else ""
        ok = rc == 0 and "全部通过" in txt
        results.append(("前端渲染冒烟", ok, dt, "14 项全部通过" if ok else txt.strip()[-80:]))
    else:
        results.append(("前端渲染冒烟", False, 0.0, "缺少 node"))

    banner("后端接口（REST / SSE / 批量实验）")
    rc, dt, out = run([py, os.path.join(HERE, "test_api.py")])
    results.append(("后端接口冒烟", rc == 0 and "失败 0 项" in out, dt, last_line(out, "通过")))

    banner("硬件在环（HTTP/UDP/TCP · 三种模式 · 指令闭环 · 看门狗）")
    rc, dt, out = run([py, os.path.join(HERE, "test_hil.py")])
    results.append(("硬件在环接口", rc == 0 and "失败 0 项" in out, dt, last_line(out, "通过")))

    banner("节点权重与智能分配（接口 / 闸门 / 控制权限 / 定价 / 在线学习）")
    rc, dt, out = run([py, os.path.join(HERE, "test_weights.py")])
    results.append(("节点权重与智能分配", rc == 0 and "失败 0 项" in out, dt, last_line(out, "通过")))

    banner("页面集成（无浏览器真跑后端驱动版页面）")
    rc, dt, out = run([py, os.path.join(HERE, "test_frontend.py")])
    results.append(("页面集成", rc == 0 and '"failed":0' in out, dt,
                    "全部通过" if rc == 0 else out.strip()[-100:]))

    banner("模型门禁（默认模型不劣于 v1）")
    if fast:
        results.append(("模型门禁", True, 0.0, "(--fast 跳过)"))
    else:
        rc, dt, out = run([py, "-m", "backend.model_lab", "serve-ready", "--duration", "60000"])
        ok = rc == 0 and "通过（不劣于 v1）" in out
        results.append(("模型门禁", ok, dt, last_line(out, "默认模型")))

    banner("前端静态校验（标签配平 / id 绑定 / 离线可用性）")
    rc, dt, out = run([py, os.path.join(HERE, "test_ui_static.py")])
    results.append(("前端静态校验", rc == 0, dt, "全部通过" if rc == 0 else out.strip()[-100:]))

    print("\n" + "=" * 78)
    print("%-28s %-6s %8s  %s" % ("测试项", "结果", "耗时", "摘要"))
    print("-" * 78)
    bad = 0
    for name, ok, dt, info in results:
        if not ok:
            bad += 1
        print("%-28s %-6s %7.1fs  %s" % (name, "PASS" if ok else "FAIL", dt, info[:60]))
    print("=" * 78)
    print("总计 %d 项，通过 %d 项，失败 %d 项" % (len(results), len(results) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
