# -*- coding: utf-8 -*-
"""
前端静态校验（无浏览器）：
  1. HTML 标签是否配平（自建解析器，容忍 void 元素）
  2. CSS 花括号配平
  3. 脚本里 getElementById / el("...") 引用的每个 id，都能在 HTML 中找到
     （动态创建的 id 走白名单），避免 UI 交互绑定到不存在的元素
  4. 面板关键 id 与 UI 渲染函数齐备（防止改名后静默失效）

    python backend/tests/test_ui_static.py
"""

from __future__ import annotations

import os
import re
import sys
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
HTML = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台.html")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
DYNAMIC_IDS = {"__toast"}          # 运行时创建
REQUIRED_IDS = [
    # 原有
    "scene", "rate", "rateV", "speed", "speedV", "btnAI", "btnPause", "btnReset", "clock",
    "topo", "topoTag", "chLat", "chBw", "chGpu", "tbody", "tblTag", "k1", "k2", "k3", "k4",
    "k5", "k6", "k7", "k8", "kd1", "kd8", "cmp", "nodes", "links", "log", "nodeTag", "linkTag",
    "modeTag",
    # 本轮新增
    "netChip", "netText", "modelChip", "modelText", "hilChip", "hilText", "devChip", "devText",
    "pumpText", "btnRefresh", "btnSchema",
    "sp1", "sp2", "sp3", "sp4", "sp5", "sp6", "sp7", "sp8",
    "hilPane", "hilMode", "hilRefresh", "hilSim", "hilDevices", "hilEvents", "hilTag", "hilHint",
    "expTag", "modelSel", "modelApply", "expDur", "expScope", "expRun", "expHint", "expBody",
    "modelCard", "modelCardTag",
]
REQUIRED_FUNCS = ["drawSpark", "drawSparks", "pushSpark", "reset", "step", "updateUI",
                  "drawTopo", "drawChart", "frame", "sizeTopo", "sizeCharts"]

FAILS = []


def check(cond, msg, info=""):
    print(("  OK   " if cond else "  FAIL ") + msg + (("  [%s]" % info) if info else ""))
    if not cond:
        FAILS.append(msg)


class Balance(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)

    def handle_startendtag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append("多余的结束标签 </%s> 行 %d" % (tag, self.getpos()[0]))
            return
        top, pos = self.stack[-1]
        if top == tag:
            self.stack.pop()
        else:
            self.errors.append("标签不匹配：期望 </%s>（行 %d）却遇到 </%s>（行 %d）"
                               % (top, pos[0], tag, self.getpos()[0]))
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break


def main():
    with open(HTML, "r", encoding="utf-8") as f:
        html = f.read()
    print("校验文件: %s (%d 字节)\n" % (os.path.basename(HTML), len(html.encode("utf-8"))))

    print("=== HTML 标签配平 ===")
    p = Balance()
    p.feed(html)
    check(not p.errors, "标签闭合正确", "; ".join(p.errors[:3]))
    check(not p.stack, "无未闭合标签",
          ",".join("%s@%d" % (t, pos[0]) for t, pos in p.stack[:5]))

    print("\n=== CSS 花括号 ===")
    css = re.search(r"<style>([\s\S]*?)</style>", html)
    check(css is not None, "存在内联样式块")
    if css:
        s = re.sub(r"/\*[\s\S]*?\*/", "", css.group(1))
        check(s.count("{") == s.count("}"), "花括号配平",
              "%d 开 / %d 闭" % (s.count("{"), s.count("}")))

    print("\n=== 脚本引用的 id 均存在 ===")
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    check(len(scripts) == 1, "单一内联脚本（保持单文件零依赖）", "%d 段" % len(scripts))
    src = "\n".join(scripts)
    refs = set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', src))
    refs |= set(re.findall(r'\bel\(\s*"([^"]+)"\s*\)', src))
    missing = sorted(r for r in refs if r not in p.ids and r not in DYNAMIC_IDS)
    check(not missing, "所有引用的 id 都能在 HTML 或动态创建中找到", ",".join(missing[:8]))
    print("       （脚本共引用 %d 个 id，静态标记 %d 个）" % (len(refs), len(p.ids)))

    print("\n=== 关键元素与函数齐备 ===")
    lack = [i for i in REQUIRED_IDS if i not in p.ids]
    check(not lack, "面板关键 id 齐备", ",".join(lack[:8]))
    lackf = [f for f in REQUIRED_FUNCS if not re.search(r"function\s+%s\s*\(" % f, src)]
    check(not lackf, "关键函数齐备", ",".join(lackf))

    print("\n=== 面板渲染函数 ===")
    for name in ("net", "renderHil", "renderExperiment", "renderModels", "expStatus", "toast"):
        check(re.search(r"\b%s\s*[:(]" % name, src) is not None, "UI.%s 已实现" % name)

    print("\n=== 离线可用性 ===")
    guarded = len(re.findall(r"window\.Backend", src))
    check(guarded <= 4 and 'if(window.Backend&&typeof window.Backend[name]==="function")' in src,
          "接入层调用均有守卫（离线时降级为提示，不报错）", "%d 处引用" % guarded)
    check("fetch(" not in src and "XMLHttpRequest" not in src,
          "页面自身不发起网络请求（网络逻辑全部隔离在接入层）")
    check("本地离线引擎" in src or "本地内置" in src, "状态条含离线态文案")

    if FAILS:
        print("\n失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("\n静态校验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
