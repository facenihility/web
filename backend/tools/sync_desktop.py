# -*- coding: utf-8 -*-
"""
把项目同步到桌面交付包（本项目的工作约定：每次改完都要同步桌面）。

    python backend/tools/sync_desktop.py            # 同步
    python backend/tools/sync_desktop.py --check    # 只校验是否已同步（不改动任何文件）
    python backend/tools/sync_desktop.py --dest "D:\\某处\\交付包"

规则
  · 只复制/覆盖，**从不删除**目标里的文件（多出来的文件会在报告里列出来，人工确认后再处理）；
  · 排除 `__pycache__`、`.test/tuning-archive`；
  · `--check` 时忽略易变产物（`backend/data/runs/**`、`.test/report.txt`、`.test/ui.txt`），
    否则刚跑完测试就会被判定为"不同步"。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "Desktop", "工业智算网-通算协同调度平台")

# 顶层条目：单个文件直接复制；目录递归复制
COPY_FILES = ["工业智算网-通算协同调度仿真平台.html",
              "工业智算网-通算协同调度仿真平台-后端版.html",
              "启动后端.cmd", "使用说明.txt"]
COPY_DIRS = ["backend", ".test"]
EXCLUDE_DIRS = {"__pycache__", "tuning-archive"}
# --check 时忽略的易变文件（相对路径前缀）
VOLATILE = ("backend/data/runs/",)
VOLATILE_FILES = {".test/report.txt", ".test/ui.txt"}
SKIP_EXT = {".pyc", ".pyo"}


def _skip(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if os.path.splitext(rel)[1] in SKIP_EXT:
        return True
    return False


def _is_volatile(rel):
    r = rel.replace("\\", "/")
    return r in VOLATILE_FILES or r.startswith(VOLATILE)


def walk(base):
    """返回 {相对路径: 绝对路径}（已排除缓存/归档）。"""
    out = {}
    for name in COPY_FILES:
        p = os.path.join(base, name)
        if os.path.exists(p):
            out[name] = p
    for d in COPY_DIRS:
        top = os.path.join(base, d)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDE_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base)
                if not _skip(rel):
                    out[rel.replace("\\", "/")] = full
    return out


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def do_sync(src, dst, quiet=False):
    files = walk(src)
    copied = 0
    for rel, full in sorted(files.items()):
        target = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target) and os.path.getsize(target) == os.path.getsize(full):
            if sha(target) == sha(full):
                continue
        shutil.copy2(full, target)
        copied += 1
        if not quiet:
            print("  更新 %s" % rel)
    extra = []
    dst_files = walk(dst) if os.path.isdir(dst) else {}
    for rel in sorted(set(dst_files) - set(files)):
        extra.append(rel)
    return files, copied, extra


def do_check(src, dst):
    src_files = {k: v for k, v in walk(src).items() if not _is_volatile(k)}
    dst_files = {k: v for k, v in walk(dst).items() if not _is_volatile(k)}
    missing = sorted(set(src_files) - set(dst_files))
    extra = sorted(set(dst_files) - set(src_files))
    changed = []
    for rel in sorted(set(src_files) & set(dst_files)):
        if os.path.getsize(src_files[rel]) != os.path.getsize(dst_files[rel]) or \
                sha(src_files[rel]) != sha(dst_files[rel]):
            changed.append(rel)
    return missing, extra, changed


def main(argv=None):
    ap = argparse.ArgumentParser(description="同步项目到桌面交付包")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="目标目录（默认桌面交付包）")
    ap.add_argument("--check", action="store_true", help="只校验，不复制")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    src, dst = os.path.abspath(ROOT), os.path.abspath(a.dest)
    if os.path.normcase(src) == os.path.normcase(dst):
        print("当前运行的就是交付包副本本身：%s" % src)
        print("同步要从**源工作目录**发起，而不是从交付包里：")
        print("    cd <源工作目录>")
        print("    python backend\\tools\\sync_desktop.py")
        return 0
    print("源目录: %s" % src)
    print("目标  : %s" % dst)
    if not os.path.isdir(dst):
        if a.check:
            print("!! 目标目录不存在")
            return 2
        os.makedirs(dst, exist_ok=True)
        print("已创建目标目录")

    if a.check:
        missing, extra, changed = do_check(src, dst)
        if not (missing or extra or changed):
            print("\n✓ 已同步（忽略易变产物：data/runs、.test/report.txt、.test/ui.txt）")
            return 0
        print("\n✗ 未同步：")
        for x in changed:
            print("  内容不同: %s" % x)
        for x in missing:
            print("  目标缺失: %s" % x)
        for x in extra:
            print("  目标多余: %s（不会自动删除）" % x)
        return 1

    files, copied, extra = do_sync(src, dst, quiet=a.quiet)
    print("\n共 %d 个文件，本次更新 %d 个" % (len(files), copied))
    if extra:
        print("目标目录中多出 %d 个文件（未删除，请人工确认）：" % len(extra))
        for x in extra[:12]:
            print("  %s" % x)
    print("提示：建议随后在目标目录跑一次  python backend\\tests\\run_all.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
