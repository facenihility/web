# -*- coding: utf-8 -*-
"""
生成「后端驱动版」前端页面。

做法：读取原始纯前端 HTML，在 </body> 前注入 backend/web/adapter.js。
原文件一个字节都不改动 —— 生成物是一个自包含的单文件页面：

    * 由后端托管打开  → 自动连接同源后端，使用服务端权威仿真
    * 直接双击本地打开 → 探测不到后端时自动回退为内置仿真引擎
    * 页面上可随时点击右下角徽标切换「后端引擎 / 本地引擎」

    python -m backend.build_frontend
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台.html")
DST = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台-后端版.html")
ADAPTER = os.path.join(HERE, "web", "adapter.js")

REQUIRED_MARKERS = [
    ("const clamp=(v,a,b)", "仿真内核"),
    ("let simAI,simBL", "运行状态"),
    ("function frame(now)", "主循环"),
]


def build(src=SRC, dst=DST, adapter=ADAPTER, verbose=True):
    if not os.path.exists(src):
        raise SystemExit("找不到原始页面: %s" % src)
    with open(src, "r", encoding="utf-8") as f:
        html = f.read()
    for marker, name in REQUIRED_MARKERS:
        if marker not in html:
            raise SystemExit("原始页面的「%s」标记丢失（%s），无法安全注入" % (name, marker))
    if "WorkBuddyBackend" in html:
        raise SystemExit("原始页面疑似已被注入过，请检查")
    with open(adapter, "r", encoding="utf-8") as f:
        js = f.read()

    inject = (
        "\n<!-- ===== 后端接入层（由 backend/build_frontend.py 注入，可重复生成） ===== -->\n"
        "<script>\n" + js + "\n</script>\n"
    )
    idx = html.rfind("</body>")
    if idx < 0:
        raise SystemExit("原始页面缺少 </body>")
    out = html[:idx] + inject + html[idx:]
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    if verbose:
        print("已生成后端驱动版页面: %s (%d 字节)" % (dst, len(out.encode("utf-8"))))
    return dst


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
