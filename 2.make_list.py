# -*- coding: utf-8 -*-
"""2.make_list.py —— 把各种清单文件转成 CNKIDownload 的工作清单 cnki_list.json

    python 2.make_list.py 我的文献清单.xlsx
    python 2.make_list.py list.csv --out cnki-state/cnki_list.json
    python 2.make_list.py --help

支持 .xlsx / .xlsm（zipfile + xml.etree，纯标准库，不需要 openpyxl / pandas）
     .csv / .tsv / .txt / .json

输出的 JSON 是数组，每条形如：
    {"no": 1, "title": "文献标题", "author": "作者", "journal": "期刊", "year": "2018"}
其中 no / title 是必需的；3.CNKIDownload.py 只依赖这两个字段。

本文件同时以模块方式被 3.CNKIDownload.py 引用（导出 load_list_file /
discover_list_file），所以两者要放在同一目录。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from CNKIDownload_cdp import setup_console

setup_console()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORK_DIR = os.path.join(SCRIPT_DIR, "cnki-state")

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"

SUPPORTED_EXT = (".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".json")


# ===========================================================================
# xlsx：zipfile + xml.etree，纯标准库
# ===========================================================================

def _col_index(ref: str) -> int:
    """'C12' -> 2（0 起算）"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _node_text(el) -> str:
    """取单元格/共享字符串里的全部文本（兼容富文本的多个 <t> 段）。"""
    return "".join(t.text or "" for t in el.iter(f"{_NS}t"))


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    """按 workbook.xml 的首个 sheet 找到对应 worksheet 路径。"""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = wb.find(f"{_NS}sheets")
        rid = None
        if sheets is not None:
            for sh in sheets:
                rid = sh.get(f"{_NS_R}id") or sh.get("id")
                break
        if rid:
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                if rel.get("Id") == rid:
                    target = (rel.get("Target") or "").lstrip("/")
                    if target.startswith("xl/"):
                        cand = target
                    else:
                        cand = "xl/" + target
                    if cand in zf.namelist():
                        return cand
    except Exception:
        pass
    for name in zf.namelist():
        if re.match(r"^xl/worksheets/sheet\d+\.xml$", name):
            return name
    raise ValueError("这个 xlsx 里没有找到工作表")


def read_xlsx_rows(path: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [_node_text(si) for si in sst]
        sheet = ET.fromstring(zf.read(_first_sheet_path(zf)))

    rows: list[list[str]] = []
    for row in sheet.iter(f"{_NS}row"):
        cells: list[str] = []
        for c in row.iter(f"{_NS}c"):
            idx = _col_index(c.get("r") or "")
            if idx < 0:
                idx = len(cells)
            ctype = c.get("t") or "n"
            if ctype == "inlineStr":
                is_el = c.find(f"{_NS}is")
                val = _node_text(is_el) if is_el is not None else _node_text(c)
            else:
                v = c.find(f"{_NS}v")
                raw = (v.text or "") if v is not None else ""
                if ctype == "s":
                    try:
                        val = shared[int(raw)]
                    except (ValueError, IndexError):
                        val = ""
                elif ctype == "b":
                    val = "TRUE" if raw == "1" else "FALSE"
                else:
                    val = raw
            while len(cells) < idx:
                cells.append("")
            if len(cells) == idx:
                cells.append(str(val).strip())
            else:
                cells[idx] = str(val).strip()
        rows.append(cells)
    return rows


# ===========================================================================
# csv / tsv / txt
# ===========================================================================

def _sniff_delim(text: str) -> str | None:
    head = text.split("\n", 1)[0]
    counts = {
        "\t": head.count("\t"),
        ",": head.count(","),
        ";": head.count(";"),
    }
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else None


def read_delimited(text: str, delim: str) -> list[list[str]]:
    text = text.lstrip("\ufeff")
    sniffer = csv.reader(io.StringIO(text), delimiter=delim)
    return [[str(c) for c in row] for row in sniffer]


def read_txt_plain(text: str) -> list[list[str]]:
    """每行一条标题；行首的 '12.' / '12、' / '12）' 顺手当编号。"""
    out: list[list[str]] = []
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*[.、,，)）:：]?\s*(.+)$", line)
        out.append([m.group(1), m.group(2)] if m else ["", line])
    return out


# ===========================================================================
# 列名识别
# ===========================================================================

RULES: list[tuple[str, re.Pattern]] = [
    ("no",      re.compile(r"^(序号|编号|序|no\.?|num(ber)?|index|id)$", re.I)),
    ("title",   re.compile(r"^(中文标题|标题|题名|中文题名|篇名|title)$", re.I)),
    ("author",  re.compile(r"^(作者|第一作者|author|authors)$", re.I)),
    ("journal", re.compile(r"^(期刊|刊名|来源|期刊名称|journal|source)$", re.I)),
    ("year",    re.compile(r"^(年份|年|发表年|year)$", re.I)),
    ("skip",    re.compile(r"^(已下文件|已下载|已获取|本地文件|fulltext|local_?file)$", re.I)),
]


def detect_columns(header: list[str]) -> dict:
    found: dict = {}
    for i, raw in enumerate(header):
        cell = re.sub(r"\s+", "", str(raw or ""))
        if not cell:
            continue
        for key, pat in RULES:
            if key not in found and pat.match(cell):
                found[key] = i
    return found


def rows_to_items(rows: list[list[str]], keep_skipped: bool = False):
    items, skipped = [], []
    if not rows:
        return items, skipped, None

    header = [str(c or "") for c in rows[0]]
    cols = detect_columns(header)
    header_looks_real = "title" in cols or "no" in cols
    data_rows = rows[1:] if header_looks_real else rows

    def get(row: list[str], key: str, fallback: int | None = None) -> str:
        idx = cols.get(key, fallback)
        if idx is None or idx < 0 or idx >= len(row):
            return ""
        return str(row[idx] or "").strip()

    auto_no = 1
    for row in data_rows:
        title = get(row, "title", None if header_looks_real else 1)
        if not title:
            continue
        if re.fullmatch(r"(未找到|待定|无)", title):
            skipped.append({"reason": "标题为占位符", "title": title})
            continue
        skip_cell = get(row, "skip", None)
        if skip_cell and not keep_skipped:
            skipped.append({"reason": "清单显示已有全文", "title": title, "detail": skip_cell})
            continue

        no_raw = get(row, "no", None if header_looks_real else 0)
        no = int(no_raw) if re.fullmatch(r"\d+", no_raw) else auto_no
        auto_no = max(auto_no, no) + 1

        items.append({
            "no": no,
            "title": title,
            "author": get(row, "author", None),
            "journal": get(row, "journal", None),
            "year": get(row, "year", None),
        })
    return items, skipped, header


# ===========================================================================
# 统一入口
# ===========================================================================

def _normalize_json_items(data) -> list[dict]:
    arr = data if isinstance(data, list) else (data.get("items") or data.get("list") or [])
    items, auto = [], 1
    for raw in arr:
        if isinstance(raw, str):
            t = raw.strip()
            if t:
                items.append({"no": auto, "title": t, "author": "", "journal": "", "year": ""})
                auto += 1
            continue
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("中文标题") or raw.get("标题") or "").strip()
        if not title:
            continue
        no_raw = raw.get("no", raw.get("序号", raw.get("num")))
        try:
            no = int(str(no_raw).strip())
        except (TypeError, ValueError):
            no = auto
        auto = max(auto, no) + 1
        items.append({
            "no": no,
            "title": title,
            "author": str(raw.get("author") or raw.get("作者") or "").strip(),
            "journal": str(raw.get("journal") or raw.get("期刊") or "").strip(),
            "year": str(raw.get("year") or raw.get("年") or "").strip(),
        })
    return items


def load_list_file(path: str, keep_skipped: bool = False):
    """读任意受支持的清单文件，返回 (items, skipped, header, source)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        rows = read_xlsx_rows(path)
    elif ext in (".csv", ".tsv"):
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        rows = read_delimited(text, "\t" if ext == ".tsv" else (_sniff_delim(text) or ","))
    elif ext == ".txt":
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        delim = _sniff_delim(text)
        rows = read_delimited(text, delim) if delim else read_txt_plain(text)
    elif ext == ".json":
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            data = json.load(f)
        return _normalize_json_items(data), [], None, path
    else:
        raise ValueError(f"不支持的清单格式：{ext or '(无扩展名)'}。"
                         f"支持 {'/'.join(SUPPORTED_EXT)}")

    items, skipped, header = rows_to_items(rows, keep_skipped=keep_skipped)
    return items, skipped, header, path


def discover_list_file(dirs: list[str] | None = None, include_txt: bool = False) -> str | None:
    """在给定目录里找唯一候选清单。

    规则：1 个 → 用它；0 个 → 返回 None；多个 → 抛错（**不替你猜**）。

    默认**不认 .txt**：纯标题的 .txt 和普通人随手写的记事本文件长得一模一样，
    自动发现时太容易误抓 —— 实测把一个 63 行的目录清单当成了 63 篇文献。
    真要用 .txt，把路径显式传给程序即可。
    """
    dirs = dirs or [os.getcwd(), SCRIPT_DIR, DEFAULT_WORK_DIR]
    exts = SUPPORTED_EXT if include_txt else tuple(e for e in SUPPORTED_EXT if e != ".txt")
    seen, cands = set(), []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in sorted(names):
            if not f.lower().endswith(exts):
                continue
            if re.match(r"^(package|package-lock|requirements|readme)", f, re.I):
                continue
            full = os.path.join(d, f)
            if full in seen:
                continue
            seen.add(full)
            cands.append(full)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    raise RuntimeError("找到多个候选清单文件，程序不会替你猜是哪一个。请显式指定：\n"
                       + "\n".join("  - " + c for c in cands))


def write_list(items: list[dict], out_file: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    return out_file


# ===========================================================================
# CLI
# ===========================================================================

HELP = """2.make_list.py —— 生成 CNKIDownload 工作清单

用法:
  python 2.make_list.py <清单文件> [选项]

选项:
  --out <文件>        输出路径（默认 cnki-state/cnki_list.json）
  --keep-skipped      不清除"已下文件"非空的行（默认清除）
  --dry-run           只打印识别结果，不写文件
  -h, --help          显示本帮助

支持的输入:
  .xlsx / .xlsm       zipfile + xml.etree 解析，不需要 openpyxl
  .csv / .tsv / .txt  自动识别分隔符；纯标题的 txt 每行一条即可
  .json               直接是数组，或 {"items": [...]}

自动识别的列名（中英文都认）:
  编号: 序号 / 编号 / no / num
  标题: 中文标题 / 标题 / 题名 / title      <- 必需
  作者: 作者 / 第一作者 / author
  期刊: 期刊 / 刊名 / 来源 / journal
  年份: 年份 / 年 / year
  跳过: 已下文件 / 已下载 / fulltext       <- 非空则不进清单（--keep-skipped 可保留）
"""


def main(argv: list[str]) -> int:
    if not argv or "-h" in argv or "--help" in argv:
        sys.stdout.write(HELP)
        return 0 if argv else 1

    def opt_val(name, default=None):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return default

    consumed = {"--out"}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            if a in consumed:
                i += 1
        else:
            positional.append(a)
        i += 1

    dry_run = "--dry-run" in argv
    input_file = positional[0] if positional else None

    if not input_file:
        try:
            input_file = discover_list_file(include_txt=True)
        except RuntimeError as e:
            sys.stderr.write(str(e) + "\n")
            return 1
        if not input_file:
            sys.stderr.write("当前目录下没有找到清单文件。请显式指定：python 2.make_list.py <清单文件>\n")
            return 1
        print(f"未指定清单文件；自动发现唯一候选：{input_file}")

    if not os.path.exists(input_file):
        sys.stderr.write(f"找不到文件：{input_file}\n")
        return 1

    out = os.path.abspath(opt_val("--out") or os.path.join(DEFAULT_WORK_DIR, "cnki_list.json"))
    items, skipped, header, source = load_list_file(input_file, keep_skipped="--keep-skipped" in argv)

    if not items:
        sys.stderr.write("没有解析出任何条目。请检查清单表头是否含「标题」列。\n")
        if header:
            sys.stderr.write("识别到的表头：" + " | ".join(header) + "\n")
        return 1

    nos = [it["no"] for it in items]
    dups = sorted({n for n in nos if nos.count(n) > 1})

    print(f"来源: {source}")
    if header:
        print("表头: " + " | ".join(header))
    extra = f"，{len(skipped)} 条已跳过" if skipped else ""
    print(f"条目: {len(items)} 条待下载{extra}")
    if dups:
        print("[!] 有重复编号：" + ", ".join(str(d) for d in dups))

    print("\n前 5 条预览:")
    for it in items[:5]:
        tail = f"  -- {it['author']}" if it.get("author") else ""
        print(f"  [{it['no']}] {it['title'][:46]}{tail}")
    if len(items) > 5:
        print(f"  ... 其余 {len(items) - 5} 条")

    if skipped:
        print("\n跳过的条目（前 10 条）:")
        for s in skipped[:10]:
            print(f"  - {s['reason']}: {str(s['title'])[:40]}")

    if dry_run:
        print("\n--dry-run：未写入任何文件。")
        return 0

    write_list(items, out)
    print(f"\n已写入: {out}")
    if skipped:
        skip_file = os.path.join(os.path.dirname(out), "cnki_skipped.json")
        with open(skip_file, "w", encoding="utf-8") as f:
            json.dump(skipped, f, ensure_ascii=False, indent=1)
        print(f"跳过的条目另存: {skip_file}")
    print("\n下一步: python 3.CNKIDownload.py --status")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
