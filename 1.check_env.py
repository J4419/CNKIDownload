# -*- coding: utf-8 -*-
"""1.check_env.py —— 环境自检（只读，不修改任何文件）

    python 1.check_env.py
    python 1.check_env.py --port 9222

检查：Python 版本 / 浏览器可执行文件 / 调试 profile / CDP 端口 / 清单与目录。
默认不联系任何第三方服务，不走代理，不读取 Cookie 内容。

退出码：0 = 通过，1 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys

from CNKIDownload_cdp import (
    DEFAULT_PORT,
    app_dir,
    browser_candidates,
    cdp_status,
    default_profile_dir,
    find_browser,
    list_tabs,
    profile_has_cookies,
    setup_console,
    user_downloads_dir,
)

setup_console()

SCRIPT_DIR = app_dir()
DEFAULT_WORK_DIR = os.path.join(SCRIPT_DIR, "cnki-state")
STATE_LIST = "cnki_list.json"

OK, BAD, WARN = "  [OK]  ", "  [FAIL]", "  [WARN]"
failures = 0
warnings = 0

# 冻结成 exe 时，命令行里该敲的是 exe 自己，而不是 python 脚本。
FROZEN = bool(getattr(sys, "frozen", False))
SELF = os.path.basename(sys.executable) if FROZEN else "python 3.CNKIDownload.py"
SELF_MAKE_LIST = (f'{os.path.basename(sys.executable)} --make-list "你的清单文件"'
                  if FROZEN else "python 2.make_list.py <你的清单文件>")


def line(tag: str, msg: str):
    global failures, warnings
    if tag == BAD:
        failures += 1
    elif tag == WARN:
        warnings += 1
    print(f"{tag} {msg}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="CNKIDownload 环境自检")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP 调试端口（默认 9222）")
    args = ap.parse_args(argv)

    print("=" * 64)
    print("  CNKIDownload 环境自检")
    print(f"  工具目录: {SCRIPT_DIR}")
    print("=" * 64)

    # --- 1. Python ---
    print("\n[1] Python")
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        where = f"内置于 {os.path.basename(sys.executable)}" if FROZEN else sys.executable
        line(OK, f"Python {platform.python_version()}  ({where})")
    else:
        line(BAD, f"Python {platform.python_version()} 版本过低，需要 3.10 或更高")
    line(OK, "零第三方依赖：本工具只用标准库，不需要 pip install 任何东西")

    # --- 2. 浏览器 ---
    print("\n[2] 浏览器可执行文件")
    try:
        exe = find_browser()
        line(OK, f"已找到: {exe}")
    except FileNotFoundError:
        line(BAD, "没有在常见位置找到 Edge / Chrome / Chromium")
        print("         候选位置：")
        for c in browser_candidates():
            print(f"           - {c}")

    # --- 3. 调试 profile ---
    profile = default_profile_dir()
    print("\n[3] 调试用浏览器 profile")
    print(f"         默认路径: {profile}")
    if os.path.isdir(profile):
        if profile_has_cookies(profile):
            line(OK, "已存在，且已有登录态（Cookie 在）")
        else:
            line(WARN, "已存在，但没看到 Cookie —— 可能还没在调试窗口里登录过知网")
    else:
        line(WARN, "还不存在。首次运行主程序时会自动创建，并留出时间让你登录知网")

    # --- 4. CDP 端口 ---
    print(f"\n[4] CDP 调试端口 {args.port}")
    st = cdp_status(args.port)
    if st["ok"]:
        ver = st.get("version") or {}
        line(OK, f"端口在监听，且确实是 DevTools 服务：{ver.get('Browser', '未知浏览器')}")
        print(f"         当前 page 标签页数: {len(list_tabs(args.port))}")
        ws = str(ver.get("webSocketDebuggerUrl") or "")
        if "127.0.0.1" in ws or "localhost" in ws:
            line(OK, "调试端点绑定在回环地址（安全）")
        else:
            line(WARN, "调试端点看起来不在回环地址上，请检查启动参数")
    elif st.get("reason") == "not_listening":
        line(WARN, f"端口 {args.port} 没有监听 —— 调试浏览器还没开")
        print("         这不算问题：3.CNKIDownload.py 会自己把它启动起来")
    else:
        line(BAD, f"端口 {args.port} 在监听，但响应不是 DevTools 服务：{st.get('error', '')}")
        print("         通常是被系统代理拦了，或那个端口上跑着别的程序。换个 --port 再试。")

    # --- 5. 清单与目录 ---
    print("\n[5] 清单与目录")
    list_path = os.path.join(DEFAULT_WORK_DIR, STATE_LIST)
    if os.path.exists(list_path):
        try:
            with open(list_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            line(OK, f"工作清单: {list_path}（{len(items)} 篇）")
        except Exception as e:
            line(BAD, f"工作清单读不出来: {e}")
    else:
        cands = [f for f in sorted(os.listdir(SCRIPT_DIR))
                 if f.lower().endswith((".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".json"))
                 and not f.lower().startswith(("package", "requirements", "readme"))]
        if cands:
            line(WARN, f"还没生成工作清单。工具目录下的候选清单: {', '.join(cands[:6])}")
            print(f"         下一步：{SELF_MAKE_LIST}")
            print(f"         （其实直接跑 {SELF} 就行，只有一个清单文件时会自动识别并转换）")
        else:
            line(WARN, f"工具目录下没有找到清单文件（{SCRIPT_DIR}）")
    print(f"         状态目录: {DEFAULT_WORK_DIR}"
          f"{'（已存在）' if os.path.isdir(DEFAULT_WORK_DIR) else '（尚未创建）'}")
    dl = user_downloads_dir()
    print(f"         系统下载目录: {dl}{'' if os.path.isdir(dl) else '（不存在）'}")

    print("\n" + "=" * 64)
    if failures == 0:
        print(f"  自检通过（{warnings} 条提示）。可以继续了。")
    else:
        print(f"  有 {failures} 项失败、{warnings} 条提示，请先按上面的提示处理。")
    print("=" * 64)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
