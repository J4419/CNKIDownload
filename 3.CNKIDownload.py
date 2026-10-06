# -*- coding: utf-8 -*-
"""3.CNKIDownload.py —— 知网中文文献批量下载（主程序）

半自动，最后一步永远是人工点击。

程序负责：自己启动调试浏览器、搜索每篇的知网详情页 URL（带缓存）、
          批量把详情页开成标签页、检测已经下载到本地的 PDF、归集重命名、
          算进度、开下一批、出对账报告。
人工负责：在每个标签页点「PDF下载」按钮。

为什么必须人工点：知网的滑块验证码在**自动点击**时必然触发，在**人工点击**时
不触发。所以本程序不模拟点击、不破解验证码、不绕过任何访问控制，只做
「把人本来就要做的点击，按批次整理好」这件事。请只下载你有权访问的文献。

**只依赖 Python 标准库，不需要 pip install 任何东西。**

用法速览：
    python 3.CNKIDownload.py --out "D:\\论文\\中文文献"     首次：开浏览器 + 解析 URL + 开第 1 批
    python 3.CNKIDownload.py --next                          之后：归集 + 开下一批
    python 3.CNKIDownload.py --status                        离线看进度
    python 3.CNKIDownload.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

from CNKIDownload_cdp import (  # noqa: E402
    DEFAULT_PORT,
    MATCH_VERSION,
    LAUNCH_HINT,
    Tab,
    app_dir,
    cdp_status,
    default_profile_dir,
    ensure_browser,
    author_keys,
    filename_matches,
    file_sha256,
    item_identity,
    is_devtools,
    list_pdfs,
    list_tabs,
    load_sibling,
    looks_blocked,
    match_list_by_filename,
    move_file_cross_device,
    new_tab,
    normalize_key,
    pause_if_interactive,
    profile_has_cookies,
    safe_filename,
    setup_console,
    tab_urls,
    user_downloads_dir,
)

setup_console()

# 放清单/状态/PDF 的目录。冻结成 exe 后 __file__ 指向临时解包目录，必须改用它。
SCRIPT_DIR = app_dir()

# 文件名带数字前缀，不是合法模块标识符，只能按路径加载。
make_list = load_sibling(("2.make_list.py", "make_list.py"), "cnki_make_list")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SEARCH_URL_PREFIX = "https://kns.cnki.net/kns8s/defaultresult/index?kw="
JS_LINKS = """JSON.stringify(Array.from(document.querySelectorAll('a.fz14')).map(a => {
    const row = a.closest('tr');
    const text = selector => (row?.querySelector(selector)?.innerText || '').trim();
    const authorCell = row?.querySelector('.author');
    const names = authorCell ? Array.from(authorCell.querySelectorAll('a'))
        .map(n => (n.innerText || n.textContent || '').trim()).filter(Boolean) : [];
    const year = text('.date').match(/(?:19|20)\\d{2}/);
    return {t:(a.innerText||a.textContent||'').trim(), h:a.href,
            author:names.length ? names : text('.author'),
            year:year ? year[0] : '', journal:text('.source')};
}))"""
CNKI_SEARCH_PAGE_RE = re.compile(r"kns\.cnki\.net/kns8s/defaultresult", re.I)
CNKI_ANY_RE = re.compile(r"cnki\.net", re.I)

STATE_FILES = {
    "list": "cnki_list.json",
    "urls": "cnki_detail_urls.json",
    "done": "cnki_dl_state.json",
    "failed": "cnki_failed.json",
    "batch": "cnki_batch_info.json",
    "config": "cnki_config.json",
    "skipped": "cnki_skipped.json",
    "report": "cnki_report.md",
}

# 冻结成 exe 时，命令行里该敲的是 exe 自己；提示文案要跟着变，否则用户照抄会失败。
FROZEN = bool(getattr(sys, "frozen", False))
SELF_EXE = os.path.basename(sys.executable) if FROZEN else ""
SELF_CMD = SELF_EXE if FROZEN else "python 3.CNKIDownload.py"
SELF_MAKE_LIST = (f'{SELF_EXE} --make-list "你的清单.xlsx"'
                  if FROZEN else 'python 2.make_list.py "你的清单.xlsx"')


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=(SELF_EXE or "3.CNKIDownload.py"),
        description="知网中文文献批量下载（半自动；最后一步由人工点击，不破解验证码）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
输出（都在 --out 和 --work 两个目录里）:
  <out>/<编号>_<标题>.pdf        入库后的正文
  <out>/_重复副本/               同一篇被重复点下载时的副本，只挪不删
  <work>/cnki_dl_state.json      已完成编号
  <work>/cnki_detail_urls.json   详情页 URL 缓存（重跑不用重新搜）
  <work>/cnki_failed.json        搜索失败的条目
  <work>/cnki_report.md          --report 生成的对账报告

例:
  {SELF_CMD} --out "D:\\论文\\中文文献" --limit 3
  {SELF_CMD} --next
  {SELF_CMD} --status --report
  {SELF_CMD} --next --manual 9,31,121
""")

    p.add_argument("list_file", nargs="?", help="清单文件（xlsx/csv/txt/json）或工作清单 JSON")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--next", action="store_true", help="续跑：归集 + 重算进度 + 开下一批")
    mode.add_argument("--collect", action="store_true", help="只归集 + 报进度，完全不碰浏览器")
    mode.add_argument("--status", action="store_true", help="离线看进度，完全不碰浏览器")

    p.add_argument("--out", help="PDF 入库目录（会记到状态里，--next 不用重写）")
    p.add_argument("--work", default=os.path.join(SCRIPT_DIR, "cnki-state"),
                   help="状态目录（默认 <工具目录>/cnki-state）")
    p.add_argument("--list", dest="list_opt", help="工作清单 JSON（默认 <work>/cnki_list.json）")
    p.add_argument("--report", nargs="?", const="AUTO", metavar="文件",
                   help="额外写一份 Markdown 进度报告（默认 <work>/cnki_report.md）")

    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP 调试端口（默认 9222）")
    p.add_argument("--batch-size", type=int, default=10, help="每批打开的标签页数（默认 10）")
    p.add_argument("--limit", type=int, default=0, help="本轮只搜索前 N 篇（试跑用）")
    p.add_argument("--manual", default="", help="逗号分隔；这些编号不进自动批次，需人工检索")
    p.add_argument("--name-len", type=int, default=25, help="入库文件名的标题截断长度（默认 25）")
    p.add_argument("--min-kb", type=int, default=20, help="小于该大小的文件视为无效下载（默认 20）")
    p.add_argument("--max-age-h", type=float, default=6.0,
                   help="只归集最近 N 小时内改动过的文件（默认 6）")
    p.add_argument("--any-age", action="store_true", help="归集时不看文件修改时间")

    p.add_argument("--no-open", action="store_true", help="只解析 URL，不开标签页")
    p.add_argument("--force", action="store_true", help="忽略 URL 缓存，重新搜索")
    p.add_argument("--dry-run", action="store_true", help="演练：不移动文件、不开标签页")

    p.add_argument("--no-launch", action="store_true",
                   help="不自己启动浏览器，只连已经开着的调试端口")
    p.add_argument("--relaunch", action="store_true", help="强制重启调试浏览器（要求端口空闲）")
    p.add_argument("--login-wait", type=int, default=0,
                   help="登录/验证页最多等多少秒（首次使用会自动给 180 秒）")
    p.add_argument("--profile", help="调试用浏览器 profile 目录（默认放到用户数据目录）")
    p.add_argument("--browser", help="浏览器可执行文件路径（自动找不到时手动指定）")
    return p


def parse_manual(text: str) -> list[str]:
    return [str(int(x)) for x in re.split(r"[,，\s]+", text or "") if x.strip().isdigit()]


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def countdown(seconds: float, label: str):
    if seconds <= 0:
        return
    tty = False
    try:
        tty = sys.stdout.isatty()
    except Exception:
        pass
    end = time.time() + seconds
    if not tty:
        print(f"  {label}：等待 {int(seconds)} 秒 …")
        time.sleep(seconds)
        return
    while True:
        left = int(end - time.time())
        if left <= 0:
            break
        sys.stdout.write(f"\r  {label} … 剩余 {left:>4d}s ")
        sys.stdout.flush()
        time.sleep(0.5)
    sys.stdout.write("\r" + " " * 64 + "\r")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 归集
# ---------------------------------------------------------------------------

def valid_pdf(path: str, min_kb: int) -> bool:
    try:
        if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) < min_kb * 1024:
            return False
        with open(path, "rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def verified_output(path: str, item: dict, items, cfg) -> bool:
    if not valid_pdf(path, cfg.min_kb):
        return False
    base = os.path.basename(path)
    records = getattr(cfg, "verified_files", {})
    record = records.get(base) if isinstance(records, dict) else None
    if isinstance(record, dict):
        if record.get("identity") != item_identity(item) or record.get("matchVersion") != MATCH_VERSION:
            return False
        try:
            return record.get("sha256") == file_sha256(path)
        except OSError:
            return False
    # 老文件只能凭完整标题核对，编号和截断前缀都不是身份凭据。
    m = re.match(r"^(\d+)_(.+)\.pdf$", base, re.I)
    if not m or int(m.group(1)) != item["no"]:
        return False
    if normalize_key(m.group(2)) != normalize_key(item.get("title")):
        return False
    same_title = [x for x in items if normalize_key(x.get("title")) == normalize_key(item.get("title"))]
    return len(same_title) == 1


def remember_file(path: str, item: dict, cfg, digest: str):
    records = getattr(cfg, "verified_files", None)
    if not isinstance(records, dict):
        records = cfg.verified_files = {}
    records[os.path.basename(path)] = {"identity": item_identity(item), "sha256": digest,
                                     "matchVersion": MATCH_VERSION}
    config_file = getattr(cfg, "config_file", None)
    if config_file:
        config = read_json(config_file, {})
        if not isinstance(config, dict):
            config = {}
        config.update({"out": cfg.out if hasattr(cfg, "out") else os.path.dirname(path),
                       "verifiedFiles": records})
        write_json(config_file, config)


def collect_downloads(items, downloads_dir, out_dir, cfg) -> dict:
    result = {"renamed": [], "duplicates": [], "skipped_old": [], "errors": [],
              "needs_review": [], "unmatched": 0}
    dup_dir = os.path.join(out_dir, "_重复副本")
    age_limit_ms = cfg.max_age_h * 3600 * 1000

    for fp in list_pdfs(downloads_dir):
        if os.path.dirname(os.path.abspath(fp)) == os.path.abspath(out_dir):
            continue  # 入库目录就是下载目录时不要自己搬自己
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if not valid_pdf(fp, cfg.min_kb):
            continue
        base = os.path.basename(fp)

        candidates = filename_matches(os.path.splitext(base)[0], items,
                                      getattr(cfg, "download_authors", None))
        hit = candidates[0] if len(candidates) == 1 else None
        if not hit:
            if candidates:
                result["needs_review"].append(base)
            else:
                result["unmatched"] += 1
            continue

        if not cfg.any_age and age_limit_ms > 0 and (time.time() * 1000 - st.st_mtime * 1000) > age_limit_ms:
            result["skipped_old"].append(base)
            continue

        target = os.path.join(out_dir, f"{hit['no']}_{safe_filename(hit['title'], cfg.name_len)}.pdf")
        if cfg.dry_run:
            result["renamed"].append(f"(dry-run) {base} -> {os.path.basename(target)}")
            continue
        try:
            if os.path.exists(target):
                if not verified_output(target, hit, items, cfg):
                    result["errors"].append(f"{base}: 目标文件尚未核验，保留新下载文件和旧文件供人工检查")
                    continue
                dup_target = os.path.join(dup_dir, base)
                if os.path.exists(dup_target):
                    continue
                os.makedirs(dup_dir, exist_ok=True)
                move_file_cross_device(fp, dup_target)
                result["duplicates"].append(base)
            else:
                digest = file_sha256(fp)
                move_file_cross_device(fp, target)
                remember_file(target, hit, cfg, digest)
                result["renamed"].append(os.path.basename(target))
        except OSError as e:
            result["errors"].append(f"{base}: {e}")
    return result


# ---------------------------------------------------------------------------
# 进度扫描
# ---------------------------------------------------------------------------

def scan_progress(items, out_dir, downloads_dir, cfg) -> dict:
    done, files = set(), {}
    needs_review = []
    by_no = {x["no"]: x for x in items}

    # ① 入库目录：完整题录记录 + 文件校验值，或唯一完整标题。
    if os.path.isdir(out_dir):
        for f in sorted(os.listdir(out_dir)):
            if not f.lower().endswith(".pdf"):
                continue
            m = re.match(r"^(\d+)_", f)
            if m and int(m.group(1)) in by_no:
                no = int(m.group(1))
                if verified_output(os.path.join(out_dir, f), by_no[no], items, cfg):
                    done.add(no)
                    files[no] = f
                else:
                    needs_review.append(f)

    # ② 系统下载目录：完整标题和已知作者必须唯一匹配。
    for fp in list_pdfs(downloads_dir):
        if not valid_pdf(fp, cfg.min_kb):
            continue
        hit = match_list_by_filename(os.path.splitext(os.path.basename(fp))[0], items,
                                    verified_authors=getattr(cfg, "download_authors", None))
        if hit:
            done.add(hit["no"])

    return {"done": sorted(done), "files": files, "needs_review": needs_review}


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def is_detail_url(url) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        return (parsed.scheme in ("http", "https") and not parsed.username and not parsed.password
                and (host == "cnki.net" or host.endswith(".cnki.net"))
                and (path.startswith("/kcms/detail/") or path == "/kcms2/article/abstract"))
    except ValueError:
        return False


def select_detail_link(links, item: dict) -> dict | None:
    title_key = normalize_key(item.get("title"))
    if not title_key or not isinstance(links, list):
        return None
    exact = [x for x in links if isinstance(x, dict) and normalize_key(x.get("t")) == title_key
             and is_detail_url(x.get("h"))]
    if not exact:
        return None
    # 同一 URL 的重复锚点必须提供一致题录，否则不能用筛选掩盖冲突。
    by_url = {}
    for candidate in exact:
        metadata = (tuple(author_keys(candidate.get("author"))), normalize_key(candidate.get("year")),
                    normalize_key(candidate.get("journal")))
        href = candidate["h"]
        if href in by_url and by_url[href][0] != metadata:
            return None
        by_url[href] = (metadata, candidate)
    expected_author = author_keys(item.get("author"))
    expected_year = normalize_key(item.get("year"))
    expected_journal = normalize_key(item.get("journal"))
    matches = []
    multiple = len(by_url) > 1
    for metadata, candidate in by_url.values():
        authors, year, journal = metadata
        if expected_author and authors and expected_author[0] != authors[0]:
            continue
        if expected_year and year and expected_year != year:
            continue
        if expected_journal and journal and expected_journal != journal:
            continue
        if multiple:
            # 有同名题录时，缺失字段不能被当作匹配证据。
            if expected_author and not authors or expected_year and not year or expected_journal and not journal:
                continue
            if not (expected_author and authors or expected_year and year or expected_journal and journal):
                continue
        matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def validated_url_cache(cache, items) -> dict:
    if not isinstance(cache, dict):
        return {}
    result = {}
    for item in items:
        key = str(item["no"])
        entry = cache.get(key)
        if not isinstance(entry, dict) or entry.get("matchVersion") != MATCH_VERSION:
            continue
        if entry.get("identity") != item_identity(item):
            continue
        candidate = entry.get("candidate")
        if (isinstance(candidate, dict) and candidate.get("h") == entry.get("href")
                and select_detail_link([candidate], item) is not None
                and normalize_key(entry.get("hit")) == normalize_key(item.get("title"))):
            result[key] = entry
    return result


def download_authors_from_cache(cache, items) -> dict:
    """补充下载匹配信息，不修改用户清单及其题录标识。"""
    valid = validated_url_cache(cache, items)
    result = {}
    for item in items:
        entry = valid.get(str(item["no"]))
        if entry and not author_keys(item.get("author")):
            authors = entry["candidate"].get("author")
            if author_keys(authors):
                result[item_identity(item)] = authors
    return result


def find_detail_url(tab: Tab, item, cfg) -> dict | None:
    if not isinstance(item, dict):
        item = {"title": str(item)}
    url = SEARCH_URL_PREFIX + urllib.parse.quote(str(item.get("title") or ""), safe="")
    tab.goto(url, settle=cfg.search_wait)

    if looks_blocked(tab.url(), tab.body_text(1500)):
        print()
        print("    [!] 需要人工验证：请到浏览器窗口里拖动滑块完成验证，"
              f"程序等待 {cfg.captcha_wait} 秒")
        countdown(cfg.captcha_wait, "等待人工验证")
        tab.goto(url, settle=cfg.search_wait)

    raw = tab.js(JS_LINKS, timeout=30)
    try:
        links = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        links = []
    return select_detail_link(links, item)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def write_report(cfg, items, progress, remaining, manual_set, url_cache, failed) -> str:
    target = cfg.report
    if not target or target == "AUTO":
        target = os.path.join(cfg.work, STATE_FILES["report"])

    L = []
    L.append("# 知网批量下载 · 进度对账报告")
    L.append("")
    L.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("")
    L.append("## 一、总览")
    L.append("")
    L.append("| 项目 | 数量 |")
    L.append("|---|---|")
    L.append(f"| 清单总数 | {len(items)} |")
    L.append(f"| 已入库 | {len(progress['done'])} |")
    L.append(f"| 剩余 | {len(remaining)} |")
    L.append(f"| 其中需人工检索 | {sum(1 for x in remaining if str(x['no']) in manual_set)} |")
    L.append(f"| 搜索失败 | {len(failed)} |")
    L.append(f"| 入库目录 | `{cfg.out}` |")
    L.append("")

    done_set = set(progress["done"])
    missing = [x for x in items if x["no"] not in done_set]
    if missing:
        L.append("## 二、未入库明细")
        L.append("")
        L.append("| 编号 | 标题 | 状态 |")
        L.append("|---|---|---|")
        for x in missing:
            if str(x["no"]) in manual_set:
                state = "需人工检索（按刊名+年卷期）"
            elif str(x["no"]) in failed:
                state = "尚未唯一核验，需人工检索"
            elif str(x["no"]) in url_cache:
                state = "已定位详情页，等待人工点下载"
            else:
                state = "待下载"
            L.append(f"| {x['no']} | {x['title']} | {state} |")
        L.append("")

    done_items = [x for x in items if x["no"] in done_set]
    if done_items:
        L.append("## 三、已入库明细")
        L.append("")
        L.append("| 编号 | 文件 | 大小(MB) |")
        L.append("|---|---|---|")
        for x in done_items:
            f = progress["files"].get(x["no"])
            size = ""
            if f:
                try:
                    size = f"{os.path.getsize(os.path.join(cfg.out, f)) / 1048576:.2f}"
                except OSError:
                    size = ""
            L.append(f"| {x['no']} | {f or '（仍在系统下载目录，尚未归集）'} | {size} |")
        L.append("")

    L.append("## 四、说明")
    L.append("")
    L.append("- 入库文件名统一为 `编号_标题.pdf`，编号对应清单里的 `no`。")
    L.append("- 同一篇若下载了多份，多余副本会挪到 `_重复副本/`，只挪不删。")
    L.append("- 本报告由 3.CNKIDownload.py 生成，可随时用 `--status --report` 重新生成。")
    L.append("")

    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return target


def print_remaining(remaining, manual_set, url_cache=None, failed=None, limit: int = 40):
    """列剩余条目，并标出每篇卡在哪一步 —— 否则「剩余」是黑盒。"""
    if not remaining:
        print("（没有剩余条目）")
        return
    url_cache = url_cache or {}
    failed = failed or {}
    print()
    print(f"剩余条目（前 {limit} 条）：")
    for x in remaining[:limit]:
        key = str(x["no"])
        if key in manual_set:
            tag = "[需人工检索]"
        elif key in failed:
            tag = "[未唯一核验，需人工检索]"
        elif key in url_cache:
            tag = "[已定位，等人工点下载]"
        else:
            tag = "[还没搜索]"
        print(f"  [{x['no']}] {str(x['title'])[:42]} {tag}")
    if len(remaining) > limit:
        print(f"  ... 其余 {len(remaining) - limit} 条")
    print("  标记含义：「未唯一核验」= 无可信匹配或有同名歧义，需按作者+年份人工找；")
    print("            「已定位」= 详情页已找到，只差你点「PDF下载」；「还没搜索」= 没进入过批次。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    args.work = os.path.abspath(args.work)
    os.makedirs(args.work, exist_ok=True)

    config_file = os.path.join(args.work, STATE_FILES["config"])
    url_cache_file = os.path.join(args.work, STATE_FILES["urls"])
    state_file = os.path.join(args.work, STATE_FILES["done"])
    fail_file = os.path.join(args.work, STATE_FILES["failed"])
    batch_file = os.path.join(args.work, STATE_FILES["batch"])

    prev = read_json(config_file, {}) or {}

    # 目录：命令行 > 上次记住的 > 默认
    out_dir = os.path.abspath(args.out or prev.get("out") or os.path.join(SCRIPT_DIR, "cnki-out"))
    port = args.port if ("--port" in argv) else int(prev.get("port") or DEFAULT_PORT)
    profile_dir = os.path.abspath(args.profile or prev.get("profile") or default_profile_dir())
    manual_set = set(parse_manual(args.manual))
    if not args.manual and prev.get("manual"):
        manual_set = {str(m) for m in prev["manual"]}

    class Cfg:
        pass

    cfg = Cfg()
    cfg.work = args.work
    cfg.out = out_dir
    cfg.port = port
    cfg.profile = profile_dir
    cfg.batch_size = max(1, args.batch_size)
    cfg.limit = max(0, args.limit)
    cfg.name_len = max(8, args.name_len)
    cfg.min_kb = max(1, args.min_kb)
    cfg.max_age_h = max(0.0, args.max_age_h)
    cfg.search_wait = 2.4
    cfg.captcha_wait = 30
    cfg.tab_pause = 0.4
    cfg.force = args.force
    cfg.no_open = args.no_open
    cfg.dry_run = args.dry_run
    cfg.any_age = args.any_age
    cfg.report = args.report
    cfg.config_file = config_file
    records = prev.get("verifiedFiles", {})
    cfg.verified_files = records if isinstance(records, dict) else {}

    # ---- 清单：命令行 > 状态目录 > 自动发现唯一候选 ----
    list_file = os.path.abspath(args.list_opt or args.list_file or os.path.join(args.work, STATE_FILES["list"]))

    if not os.path.exists(list_file):
        if args.list_file or args.list_opt:
            sys.stderr.write(f"[!] 找不到清单文件：{list_file}\n")
            return 1
        # 不用先跑 2.make_list.py：程序旁边只有一个清单文件就自动拿它。
        #
        # 只找「程序所在目录」和状态目录，**故意不翻当前工作目录** ——
        # 否则在别的目录里敲这条命令时，程序会把那儿随便一个 .txt/.csv
        # 当成文献清单（实测踩到过：把一个 63 行的目录清单当成了 63 篇文献）。
        try:
            found = make_list.discover_list_file([SCRIPT_DIR, args.work])
        except RuntimeError as e:
            sys.stderr.write(str(e) + "\n")
            return 1
        if not found:
            sys.stderr.write(
                f"[!] 找不到工作清单：{list_file}\n\n"
                f"程序只在「{SCRIPT_DIR}」（程序所在目录）和状态目录里找清单，不去别处翻。\n"
                "把清单文件放到程序旁边，重跑即可。自动识别只认这些后缀：\n"
                "    .xlsx / .xlsm / .csv / .tsv / .json\n"
                "（**不含 .txt** —— 纯文本清单和普通记事本文件分不出来，请显式传路径）\n"
                "也可以显式指定路径：\n"
                f'    {SELF_MAKE_LIST}\n'
                f'    {SELF_CMD} "你的清单.xlsx" --out "PDF 目录"\n')
            return 1
        print(f"未指定清单；自动发现唯一候选：{found}")
        list_file = found

    # json 直接用；表格（xlsx/csv/txt）先转换，并落一份工作清单方便续跑
    is_json_input = os.path.splitext(list_file)[1].lower() == ".json"
    skipped_rows = []
    if is_json_input:
        items = read_json(list_file, None)
        if not isinstance(items, list) or not items:
            sys.stderr.write(f"[!] 工作清单不是非空数组：{list_file}\n")
            return 1
    else:
        items, skipped_rows, _, _ = make_list.load_list_file(list_file)

    # 一条都没解析出来时必须显式报错。
    # 否则 items 为空 → 剩余为 0 → 后面会打印「全部完成。」，看着像成功、
    # 其实清单根本没读进来 —— 这种"假成功"比报错更坑人。
    if not items:
        sys.stderr.write(
            f"[!] 清单里一条有效条目都没解析出来：{list_file}\n\n"
            "最常见的原因：表里没有「标题」列（认得的表头见 --help），或者标题列全空。\n"
            "先看一眼解析结果：\n"
            f'    {SELF_MAKE_LIST} --dry-run\n')
        return 1

    clean = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("title"):
            continue
        try:
            no = int(str(it.get("no")).strip())
        except (TypeError, ValueError):
            no = i + 1
        it = dict(it)
        it["no"] = no
        it["title"] = str(it["title"]).strip()
        clean.append(it)
    items = clean

    if not items:
        sys.stderr.write(
            f"[!] 清单里的条目全都缺 title 字段：{list_file}\n"
            '每条至少要长这样：{"no": 1, "title": "文献标题"}\n')
        return 1

    seen = set()
    for it in items:
        if it["no"] <= 0 or it["no"] in seen:
            sys.stderr.write(f"[!] 编号必须为不重复的正整数：{it['no']}。未移动任何文件。\n")
            return 1
        seen.add(it["no"])

    work_list = os.path.join(args.work, STATE_FILES["list"])
    if not is_json_input or os.path.normcase(os.path.abspath(list_file)) != os.path.normcase(work_list):
        make_list.write_list(items, work_list)
        print(f"清单已转换并保存到 {work_list}")
        if skipped_rows:
            skip_file = os.path.join(args.work, STATE_FILES["skipped"])
            write_json(skip_file, skipped_rows)
            print(f"其中 {len(skipped_rows)} 条已跳过（另存 {skip_file}）")

    raw_cache = read_json(url_cache_file, {})
    url_cache = {} if cfg.force else validated_url_cache(raw_cache, items)
    cfg.download_authors = download_authors_from_cache(url_cache, items)
    if raw_cache != url_cache:
        write_json(url_cache_file, url_cache)
        print("旧版、题录变化或未通过核验的 URL 缓存已失效，将重新搜索；PDF 不会因此被删除。")
    raw_failed = read_json(fail_file, {})
    failed = {str(x["no"]): x["title"] for x in items
              if isinstance(raw_failed, dict) and raw_failed.get(str(x["no"])) == x["title"]}

    downloads_dir = user_downloads_dir()

    print("=" * 64)
    print("  CNKIDownload · 知网批量下载")
    print(f"  清单: {list_file}（{len(items)} 篇）")
    print(f"  入库: {cfg.out}")
    print(f"  状态: {cfg.work}")
    print(f"  下载: {downloads_dir}")
    print("=" * 64)

    os.makedirs(cfg.out, exist_ok=True)

    # ---- 1. 归集 + 扫描进度 ----
    moved = collect_downloads(items, downloads_dir, cfg.out, cfg)
    progress = scan_progress(items, cfg.out, downloads_dir, cfg)
    done_set = set(progress["done"])
    remaining = [x for x in items if x["no"] not in done_set]
    auto_remaining = [x for x in remaining if str(x["no"]) not in manual_set]

    print()
    if moved["renamed"]:
        print(f"归集新增 {len(moved['renamed'])} 个文件：")
        for m in moved["renamed"]:
            print(f"   + {m}")
    if moved["duplicates"]:
        print(f"重复副本 {len(moved['duplicates'])} 个，已挪到 _重复副本/（没有删除）：")
        for m in moved["duplicates"][:10]:
            print(f"   ~ {m}")
        if len(moved["duplicates"]) > 10:
            print(f"   ... 其余 {len(moved['duplicates']) - 10} 个")
    if moved["skipped_old"]:
        print(f"[!] {len(moved['skipped_old'])} 个匹配到的文件超过 {cfg.max_age_h} 小时没改动，"
              f"未归集（确认需要请加 --any-age）：")
        for m in moved["skipped_old"][:10]:
            print(f"   ? {m}")
    if moved["errors"]:
        print(f"[!] 归集失败 {len(moved['errors'])} 个：")
        for m in moved["errors"][:10]:
            print(f"   x {m}")
    review = moved["needs_review"] + progress["needs_review"]
    if review:
        print(f"[!] {len(review)} 个文件尚未唯一核验，保留原文件供人工核对：")
        for name in review[:10]:
            print(f"   ? {name}")
    if moved["unmatched"]:
        print(f"[提示] {moved['unmatched']} 个下载文件无法按完整题录匹配，未移动。"
              "可能是其他清单的文献、作者信息缺失或标题被截断，请人工核对。")

    print()
    manual_left = len(remaining) - len(auto_remaining)
    tail = f"（其中 {manual_left} 篇需人工检索，不进自动批次）" if manual_left else ""
    print(f"进度：已完成 {len(progress['done'])} / {len(items)}　剩余 {len(remaining)}{tail}")

    write_json(state_file, progress["done"])

    def save_config():
        write_json(config_file, {
            "out": cfg.out,
            "port": cfg.port,
            "profile": cfg.profile,
            "batchSize": cfg.batch_size,
            "nameLen": cfg.name_len,
            "manual": sorted(manual_set, key=int),
            "verifiedFiles": cfg.verified_files,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

    def finish(code: int) -> int:
        save_config()
        if cfg.report:
            path = write_report(cfg, items, progress, remaining, manual_set, url_cache, failed)
            print(f"\n报告已写入: {path}")
        return code

    if args.status or args.collect:
        print_remaining(remaining, manual_set,
                        url_cache, failed)
        return finish(0)

    if not auto_remaining:
        print()
        if remaining:
            print("自动部分已全部完成，剩下的是需要人工检索的条目：")
            print_remaining(remaining, manual_set,
                            url_cache, failed)
        else:
            print("全部完成。")
        return finish(0)

    # ---- 2. 准备调试浏览器 ----
    first_time = not profile_has_cookies(cfg.profile)

    if args.no_launch:
        st = cdp_status(cfg.port)
        if not st["ok"]:
            print()
            if st["reason"] == "not_listening":
                sys.stderr.write("[!] " + LAUNCH_HINT + "\n")
            else:
                sys.stderr.write(
                    f"[!] 端口 {cfg.port} 在监听，但响应不是 DevTools 服务：{st.get('error', '')}\n"
                    f"    常见原因：被系统代理拦了，或这个端口上跑着别的程序。换个 --port 再试。\n")
            return 2
    else:
        if args.relaunch and is_devtools(cfg.port):
            sys.stderr.write(
                f"[!] {cfg.port} 端口上已经有一个调试浏览器在跑，--relaunch 要求独占端口。\n"
                f"    请先手动关掉那个窗口，或换一个 --port。\n")
            return 2
        if not is_devtools(cfg.port):
            print(f"\n正在以调试模式启动浏览器（端口 {cfg.port}）…")
            try:
                ensure_browser(cfg.port, cfg.profile, browser=args.browser,
                               url="https://kns.cnki.net/", relaunch=False)
            except Exception as e:
                sys.stderr.write(f"[!] 启动浏览器失败：{type(e).__name__}: {e}\n")
                return 2
            print(f"浏览器已就绪（profile: {cfg.profile}）")

    st = cdp_status(cfg.port)
    browser_name = (st.get("version") or {}).get("Browser") or "已连接"
    print(f"浏览器: {browser_name}（CDP {cfg.port}）")

    # 首次使用 / 显式要求时，给人工登录留时间
    wait_s = args.login_wait
    if first_time and wait_s <= 0:
        wait_s = 180
    if wait_s > 0:
        print()
        print("-" * 64)
        if first_time:
            print("  首次使用：请在弹出的浏览器窗口里登录知网")
            print("  （机构登录 / 学校 VPN / CARSI 都行）。")
            print("  这一步只需要做一次，登录态会保存到调试 profile 里。")
        else:
            print("  按 --login-wait 要求，暂停等待你在浏览器里完成登录或验证。")
        print("-" * 64)
        countdown(wait_s, "等待登录")

    # ---- 3. 找一个工作页 ----
    tabs = list_tabs(cfg.port)
    work_tab = None
    for t in tabs:
        if CNKI_SEARCH_PAGE_RE.search(str(t.get("url") or "")):
            work_tab = t
            break
    if work_tab is None:
        for t in tabs:
            if CNKI_ANY_RE.search(str(t.get("url") or "")):
                work_tab = t
                break
    if work_tab is None and tabs:
        work_tab = tabs[0]
    if work_tab is None:
        print("没有可用标签页，新开一个…")
        tab_id = new_tab(cfg.port, "https://kns.cnki.net/")
        work_tab = {"id": tab_id, "url": "https://kns.cnki.net/"}

    tab = Tab(cfg.port, work_tab["id"])
    print(f"工作页: {str(work_tab.get('title') or work_tab.get('url') or '')[:60]}")

    # ---- 4. 解析详情页 URL ----
    need = [x for x in auto_remaining if cfg.force or str(x["no"]) not in url_cache]

    if need:
        queue = need[: cfg.limit] if cfg.limit > 0 else need
        print(f"\n搜索详情页 URL（本轮 {len(queue)} / {len(need)} 篇）…")
        for i, item in enumerate(queue, 1):
            sys.stdout.write(f"  [{i}/{len(queue)}] [{item['no']}] {str(item['title'])[:24]} ... ")
            sys.stdout.flush()
            try:
                hit = find_detail_url(tab, item, cfg)
                if hit and hit.get("h"):
                    url_cache[str(item["no"])] = {
                        "href": hit["h"], "title": item["title"], "hit": hit.get("t", ""),
                        "matchVersion": MATCH_VERSION, "identity": item_identity(item), "candidate": hit}
                    failed.pop(str(item["no"]), None)
                    print(f"OK  {str(hit.get('t', ''))[:32]}")
                else:
                    failed[str(item["no"])] = item["title"]
                    print("未唯一核验（无可信匹配或同名歧义，请人工按作者+年份检索）")
            except Exception as e:
                failed[str(item["no"])] = item["title"]
                print(f"异常 {type(e).__name__}: {str(e)[:60]}")
            write_json(url_cache_file, url_cache)
            write_json(fail_file, failed)
            time.sleep(0.6)
        print(f"URL 缓存: 成功 {len(url_cache)} / 失败 {len(failed)}")

        # 搜不到的篇目**不会进任何批次**，会一直挂在「剩余」里。
        # 所以必须当场、醒目地汇总一次，并给出 --manual 排除命令；
        # 否则用户只看到「剩余 5」却不知道这 5 篇是"没下完"还是"根本搜不到"。
        miss = [x for x in queue if str(x["no"]) in failed]
        if miss:
            print()
            print("-" * 64)
            print(f"  [!] 本轮有 {len(miss)} 篇未能唯一核验（已记入 {os.path.basename(fail_file)}）")
            for x in miss[:10]:
                print(f"      [{x['no']}] {str(x['title'])[:42]}")
            if len(miss) > 10:
                print(f"      ... 其余 {len(miss) - 10} 篇")
            print("      这些篇目不进批次，会一直显示为「剩余」。两种处理方式：")
            print("        · 人工上知网按「刊名 + 年卷期」找到后自己下载（推荐）")
            print("        · 或者排除出自动批次，免得每轮白搜一遍：")
            print(f'          {SELF_CMD} --manual "{",".join(str(x["no"]) for x in miss)}"')
            print("-" * 64)
    else:
        print("\n详情页 URL 已全部缓存，跳过搜索。")

    # 本轮新取得作者信息后，立即归集已有下载，避免再等一次或重复开页。
    refreshed_authors = download_authors_from_cache(url_cache, items)
    if refreshed_authors != cfg.download_authors:
        cfg.download_authors = refreshed_authors
        refreshed = collect_downloads(items, downloads_dir, cfg.out, cfg)
        if refreshed["renamed"]:
            print(f"\n使用新核验的作者信息归集 {len(refreshed['renamed'])} 个文件：")
            for name in refreshed["renamed"]:
                print(f"   + {name}")
        for error in refreshed["errors"]:
            print(f"[!] 归集失败：{error}")
        progress = scan_progress(items, cfg.out, downloads_dir, cfg)
        done_set = set(progress["done"])
        remaining = [x for x in items if x["no"] not in done_set]
        auto_remaining = [x for x in remaining if str(x["no"]) not in manual_set]
        write_json(state_file, progress["done"])
        print(f"进度更新：已完成 {len(progress['done'])} / {len(items)}　剩余 {len(remaining)}")
        if not auto_remaining:
            print_remaining(remaining, manual_set, url_cache, failed)
            if not remaining:
                print("全部完成。")
            return finish(0)

    # ---- 5. 开本批标签页 ----
    batch = [x for x in auto_remaining if str(x["no"]) in url_cache][: cfg.batch_size]
    save_config()

    if not batch:
        print()
        print("没有可打开的详情页 URL。可能原因：")
        print(f"  · 搜索全部失败（见 {fail_file}），需要人工检索")
        print("  · 还没解析 URL（先不带 --next/--status/--collect 跑一次）")
        return finish(1)

    if cfg.no_open or cfg.dry_run:
        flag = "--no-open" if cfg.no_open else "--dry-run"
        print(f"\n（{flag}）本批本应打开 {len(batch)} 个标签页：")
        for b in batch:
            info = url_cache[str(b["no"])]
            print(f"   [{b['no']}] {(info.get('hit') or b['title'])[:40]}")
        return finish(0)

    print()
    print("=" * 64)
    print(f"  本批 {len(batch)} 篇，正在打开标签页")
    print("  请在每个标签页点「PDF下载」，下完后回来跑 --next")
    print("=" * 64)
    for b in batch:
        info = url_cache[str(b["no"])]
        print(f"  [{b['no']}] {(info.get('hit') or b['title'])[:38]}")

    existing = tab_urls(cfg.port)
    ok_count, skipped_existing, failed_open = 0, 0, []
    for b in batch:
        href = url_cache[str(b["no"])]["href"]
        if href in existing:
            print(f"  (标签页已存在，跳过) [{b['no']}]")
            skipped_existing += 1
            ok_count += 1
            continue
        try:
            new_tab(cfg.port, href)
            existing.add(href)
            ok_count += 1
        except Exception as e:
            failed_open.append(b["no"])
            print(f"  打开失败 [{b['no']}]: {e}")
        time.sleep(cfg.tab_pause)

    # 复核：别只信脚本自己的日志
    page_count = len(list_tabs(cfg.port))
    print()
    extra = f"（其中 {skipped_existing} 个原本就在）" if skipped_existing else ""
    print(f"已打开 {ok_count}/{len(batch)} 个标签页{extra}")
    print(f"浏览器当前 page 标签页总数: {page_count}（脚本自报以外的独立复核）")
    if failed_open:
        print(f"[!] {len(failed_open)} 个没开出来：{failed_open}。"
              f"若浏览器里确实没有，把 --batch-size 调小到 5 再试。")

    write_json(batch_file, {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "remaining_total": len(remaining),
        "remaining_auto": len(auto_remaining),
        "batch_size": len(batch),
        "batch_nos": [b["no"] for b in batch],
        "opened": ok_count,
        "out": cfg.out,
        "port": cfg.port,
    })

    print()
    print(f'下一批请运行：python 3.CNKIDownload.py --next --out "{cfg.out}"')
    return finish(0)


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
