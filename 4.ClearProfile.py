# -*- coding: utf-8 -*-
"""4.ClearProfile.py —— 清理调试用浏览器 profile

    python 4.ClearProfile.py --dry-run   先看会删什么（建议每次都先跑这个）
    python 4.ClearProfile.py             真清（只清缓存，保留登录态）
    python 4.ClearProfile.py --all       连登录态一起清掉，下次要重新登录

默认目标是本工具自己的调试 profile（用户数据目录下的 CNKIDownload/browser-profile）。
profile 里存着 Cookie 和机构登录态，所以：
  · 只清缓存时，Cookies / Login Data / Local State 一律不动
  · --all 会删掉整个 profile 目录，不可恢复
  · 自定义 --profile 想整目录删除，必须再加 --confirm-custom-profile
  · 拒绝在磁盘根目录、用户主目录、系统个人文件夹和过浅路径上执行
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import sys

from CNKIDownload_cdp import app_dir, default_profile_dir, profile_has_cookies, setup_console

setup_console()

SCRIPT_DIR = app_dir()

# 只清这些（相对 profile 根）：都是可再生的缓存
CACHE_ENTRIES = [
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/ShaderCache",
    "Default/GrShaderCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "Default/optimization_guide_hint_cache_store",
    "Default/optimization_guide_model_metadata_store",
    "Default/BudgetDatabase",
    "Default/Extension Rules",
    "Default/Site Characteristics Database",
    "Default/Sync Data/LevelDB",
    "Default/Media Cache",
    "Default/Network/TransportSecurity",
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "ShaderCache",
    "GrShaderCache",
    "BrowserMetrics",
    "Crashpad/reports",
    "component_crx_cache",
    "extensions_crx_cache",
    "GraphiteDawnCache",
    "segmentation_platform",
]

# 这些永远不要碰
NEVER_DELETE = {"Cookies", "Cookies-journal", "Login Data", "Login Data-journal",
                "Local State", "Preferences", "Web Data"}


def dir_size(path: str) -> int:
    if os.path.isfile(path) and not os.path.islink(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n: int) -> str:
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def assert_no_links(path: str):
    """拒绝目标及其父路径中的符号链接、Windows 目录联接和其他重解析点。"""
    current = os.path.abspath(path)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError(f"路径包含链接或重解析点：{current}")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def assert_safe(target: str):
    """拒绝在危险路径上执行。宁可误拒，也不删错。"""
    resolved = canonical_path(target)
    reasons = []
    if os.path.dirname(resolved) == resolved:
        reasons.append("这是磁盘/文件系统根目录")
    for protected, label in ((os.path.expanduser("~"), "用户主目录"),
                             (SCRIPT_DIR, "工具目录")):
        try:
            if os.path.commonpath((resolved, canonical_path(protected))) == resolved:
                reasons.append(f"这是{label}或包含它的上级目录")
        except ValueError:
            pass  # 位于不同磁盘，目标不可能包含该受保护目录。
    personal_names = {"desktop", "documents", "downloads", "music", "pictures",
                      "videos", "桌面", "文档", "下载"}
    if any(os.path.basename(p).casefold() in personal_names
           for p in (os.path.abspath(target), resolved)):
        reasons.append("这是系统个人文件夹")
    try:
        assert_no_links(target)
    except (OSError, ValueError) as exc:
        reasons.append(str(exc))
    depth = len([p for p in resolved.split(os.sep) if p])
    min_depth = 3 if platform.system() == "Windows" else 2
    if depth < min_depth:
        reasons.append(f"路径太浅（{depth} 层），拒绝操作")

    if reasons:
        sys.stderr.write(f"[!] 拒绝在 {resolved} 上执行：\n"
                         + "\n".join("    - " + r for r in reasons) + "\n")
        raise SystemExit(1)


def remove_checked(path: str):
    """只移除无链接的既定目标，失败或有残留时不得报告成功。"""
    assert_no_links(path)
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    if os.path.lexists(path):
        raise OSError(f"删除后目标仍存在：{path}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="4.ClearProfile.py",
        description="清理调试用浏览器 profile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例:
  python 4.ClearProfile.py --dry-run
  python 4.ClearProfile.py
  python 4.ClearProfile.py --all
""")
    ap.add_argument("--dry-run", action="store_true", help="只列出会删什么，不真删")
    ap.add_argument("--all", action="store_true", help="删掉整个 profile（含登录态，不可恢复）")
    ap.add_argument("--profile", help="自定义 profile 目录（默认用本工具的调试 profile）")
    ap.add_argument("--confirm-custom-profile", action="store_true",
                    help="对自定义 --profile 执行 --all 时必须额外加上")
    args = ap.parse_args(argv)

    profile = os.path.abspath(args.profile or default_profile_dir())
    assert_safe(profile)

    if args.all and args.profile and not args.confirm_custom_profile:
        sys.stderr.write(
            "[!] --all 会删掉整个自定义 profile，必须同时加 --confirm-custom-profile 才执行。\n"
            f"    目标：{profile}\n")
        return 1

    print("=" * 64)
    print("  CNKIDownload profile 清理")
    print(f"  目标: {profile}")
    mode = "--all（整目录删除，含登录态）" if args.all else "--cache-only（保留登录态）"
    print(f"  模式: {mode}{'  [dry-run]' if args.dry_run else ''}")
    print("=" * 64)
    print()

    if not os.path.lexists(profile):
        print("目标不存在，无需清理。")
        return 0
    if not os.path.isdir(profile):
        sys.stderr.write("[!] profile 必须是普通目录。\n")
        return 1

    # ---- --all ----
    if args.all:
        size = dir_size(profile)
        print(f"将删除：{profile}")
        print(f"                    {human(size)}")
        if args.dry_run:
            print("\n--dry-run：未删除任何内容。")
            return 0
        try:
            remove_checked(profile)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[!] 无法完整删除 profile：{exc}\n"
                             "    可能仍有登录数据残留，请关闭浏览器后重试。\n")
            return 1
        print(f"\n已删除整个 profile，释放约 {human(size)}。下次启动需要重新登录知网。")
        return 0

    # ---- 只清缓存 ----
    planned = []
    for rel in CACHE_ENTRIES:
        if os.path.basename(rel) in NEVER_DELETE:
            continue
        full = os.path.join(profile, *rel.split("/"))
        if not os.path.lexists(full):
            continue
        try:
            assert_no_links(full)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[!] 拒绝清理 {rel}：{exc}\n")
            return 1
        size = dir_size(full)
        planned.append((rel, size))

    if not planned:
        print("没有找到可清理的缓存目录。")
        return 0

    for rel, size in planned:
        print(f"  {'会删' if args.dry_run else '待删'}  {rel:<46} {human(size)}")
    print(f"\n共 {len(planned)} 项，合计约 {human(sum(s for _, s in planned))}。")

    if args.dry_run:
        print("\n--dry-run：未删除任何内容。确认无误后去掉 --dry-run 重跑。")
        return 0

    removed = 0
    freed = 0
    for rel, size in planned:
        full = os.path.join(profile, *rel.split("/"))
        try:
            remove_checked(full)
            removed += 1
            freed += size
            print(f"  已删  {rel:<46} {human(size)}")
        except (OSError, ValueError) as e:
            print(f"  [!] 删不掉 {rel}: {e}")

    print(f"\n完成：删除 {removed}/{len(planned)} 项，释放约 {human(freed)}。")
    if profile_has_cookies(profile):
        print("Cookie 文件仍在；本次只清缓存，未删除登录数据。")
    else:
        print("没看到 Cookie 文件 —— 可能会需要重新登录。")
    return 0 if removed == len(planned) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
