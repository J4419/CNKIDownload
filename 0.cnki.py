# -*- coding: utf-8 -*-
"""0.cnki.py —— 单一入口。

打包成 exe 用的就是这个文件：把四个脚本收进一个入口，
双击 exe 就等于 `python 3.CNKIDownload.py`（不给参数的那个默认流程）。

    python 0.cnki.py                 主流程（= 双击 exe 的行为）
    python 0.cnki.py --check-env     环境自检
    python 0.cnki.py --make-list F   只转换清单，不下载
    python 0.cnki.py --clear-cache   清理调试 profile

四个带数字前缀的脚本仍是「源码用户」的入口（阅读顺序 1→2→3→4），
本文件只是给它们加一层路由，不改变任何行为。
"""
from __future__ import annotations

import os
import sys

from CNKIDownload_cdp import app_dir, load_sibling, pause_if_interactive, setup_console

setup_console()

SCRIPT_DIR = app_dir()

HELP = """CNKIDownload —— 知网中文文献批量下载（半自动）

用法:
  CNKIDownload.exe [选项]              主流程：开浏览器 + 搜索 + 开本批标签页
  CNKIDownload.exe --check-env         环境自检（不知道哪里出问题就先跑这个）
  CNKIDownload.exe --make-list <清单>  只转换清单，不下载
  CNKIDownload.exe --clear-cache       清理调试 profile（默认只清缓存，保留登录态）
  CNKIDownload.exe --help              看主流程的完整选项

日常两步:
  1) 双击 exe（或跑主流程）→ 浏览器里挨个点「PDF下载」→ 关掉标签页
  2) 再双击一次 → 自动归集 + 开下一批
  重复这两步直到完成。

清单:
  把清单文件（.xlsx / .csv / .txt / .json）放到 exe 旁边，第一次运行会自动识别并转换。
  有多个清单文件时会列出来要求你指定，不会替你猜。

边界:
  本工具不破解验证码、不绕过付费墙。它只把你本来就点得动的那些「PDF下载」
  按批次整理好 —— 最后那一下永远由你点。
"""


def main(argv: list[str]) -> int:
    first = argv[0] if argv else ""

    if first in ("--check-env", "check-env"):
        mod = load_sibling(("1.check_env.py", "check_env.py"), "cnki_check_env")
        return mod.main(argv[1:])

    if first in ("--make-list", "make-list"):
        mod = load_sibling(("2.make_list.py", "make_list.py"), "cnki_make_list")
        return mod.main(argv[1:])

    if first in ("--clear-cache", "clear-cache"):
        mod = load_sibling(("4.ClearProfile.py", "clear_profile.py"), "cnki_clear_profile")
        return mod.main(argv[1:])

    if first in ("-h", "--help") and len(argv) == 1:
        sys.stdout.write(HELP)
        # 再打出主流程的完整选项表。
        # （之前只打上面那段简版就走了，用户照提示敲 --help 也看不到完整参数，等于死循环。）
        try:
            mod = load_sibling(("3.CNKIDownload.py", "cnki_main.py"), "cnki_main")
            print()
            print("=" * 68)
            print("  主流程的完整选项")
            print("=" * 68)
            mod.build_parser().print_help()
        except Exception as exc:  # noqa: BLE001
            print(f"\n（完整选项表读取失败：{type(exc).__name__}: {exc}）")
        return 0

    mod = load_sibling(("3.CNKIDownload.py", "cnki_main.py"), "cnki_main")
    return mod.main(argv)


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\n已中断。已归集的不会丢，原样重跑即可续跑。")
        code = 130
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"\n[错误] {type(exc).__name__}: {exc}\n")
        code = 1
    pause_if_interactive()
    sys.exit(code)
