# -*- coding: utf-8 -*-
"""
前端集成验证：拉起后端，用 Node + DOM 垫片真跑一遍「后端驱动版」页面。

    python backend/tests/test_frontend.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
# 页面集成测试会触发一次实验：数据目录指到临时目录，避免覆盖交付物 data/report.txt
os.environ.setdefault("DSH_DATA_DIR", os.path.join(tempfile.gettempdir(), "dsh_test_data"))
sys.path.insert(0, ROOT)

from backend import build_frontend, server as S  # noqa: E402

NODE_CANDIDATES = [
    os.environ.get("DSH_NODE"),
    shutil.which("node"),
    r"C:\Users\h'b'y\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
]


def find_node():
    for p in NODE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def main():
    node = find_node()
    if not node:
        print("SKIP: 未找到 node")
        return 0

    build_frontend.build(verbose=True)
    page = build_frontend.DST

    httpd = S.serve("127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    print("服务: %s" % base)
    print("页面: %s\n" % page)

    try:
        proc = subprocess.run([node, os.path.join(HERE, "page_harness.js"), base, page],
                              capture_output=True, cwd=ROOT, timeout=180)
    finally:
        httpd.shutdown()
        httpd.server_close()
        S.HUB.stop()

    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    print(out.strip())
    if err.strip():
        print("--- stderr ---")
        print(err.strip())
    ok = proc.returncode == 0
    print("\n前端集成验证: %s" % ("PASS" if ok else "FAIL (exit %d)" % proc.returncode))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
