# -*- coding: utf-8 -*-
"""
启动入口：

    python -m backend.run                 # http://127.0.0.1:8787
    python -m backend.run --port 9000
    python -m backend.run --scene stress --speed 2.0

等价于 `python backend/server.py`。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import server  # noqa: E402

if __name__ == "__main__":
    sys.exit(server.main())
