# -*- coding: utf-8 -*-
"""CNKIDownload_cdp —— 浏览器发现 + Chrome DevTools Protocol 客户端。

**纯标准库实现**（含自写的 WebSocket 客户端），不需要 pip install 任何东西，
也不需要 Node.js。这是本工具敢写「装了 Python 就能跑」的底气。

WebSocket / CDP 这一层取自同作者 SCIDownload 项目的成熟实现（MIT），
两个工具因此共享同一套依赖模型。

对外接口：
    find_browser(explicit)                -> 浏览器可执行文件路径
    default_profile_dir()                 -> 默认调试 profile 目录
    port_alive(port)                      -> 端口是否在监听
    is_devtools(port)                     -> 端口是否真的是 DevTools 服务
    ensure_debug_port_available(port)     -> 启动前确认端口空闲
    launch(profile_dir, port, ...)        -> Popen（浏览器独立于本进程存活）
    list_tabs(port)                       -> /json/list 里的 page 列表
    tab_urls(port)                        -> 当前所有 page 的 URL 集合
    new_tab(port, url)                    -> tab_id
    close_tab(port, tab_id)               -> None
    Tab(port, tab_id)                     -> .goto() .js() .front() .close()
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata

# 必须绕开环境里的 HTTP_PROXY / HTTPS_PROXY，否则 127.0.0.1 上的调试端口
# 会被代理劫持，报出「Unexpected status 502 ... does not look like a DevTools server」，
# 看起来像服务异常，其实只是被代理拦了（或者浏览器根本没开）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_SYSTEM = platform.system()

DEFAULT_PORT = 9222


def app_dir() -> str:
    """程序所在目录（放清单、状态、PDF 的那个目录）。

    PyInstaller --onefile 下 `__file__` 指向临时解包目录，程序一退出就没了 ——
    状态文件写在那里等于每次都从头开始。所以冻结后必须改用 exe 自己所在的目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def module_dir() -> str:
    """本模块所在目录。冻结后是 PyInstaller 的解包目录（里面带着同捆的 .py）。"""
    return os.path.dirname(os.path.abspath(__file__))


def load_sibling(candidates, module_name: str):
    """按文件路径加载同目录的兄弟脚本。

    为什么不能直接 import：本项目的脚本名带数字前缀（`2.make_list.py`），
    不是合法的 Python 标识符，`import 2.make_list` 直接 SyntaxError。
    所以一律用 importlib 按路径显式加载。

    搜索顺序：本模块所在目录（冻结时 = 解包目录）→ 程序所在目录。
    """
    import importlib.util

    tried = []
    for base in (module_dir(), app_dir()):
        for name in candidates:
            cand = os.path.join(base, name)
            if cand in tried:
                continue
            tried.append(cand)
            if not os.path.exists(cand):
                continue
            spec = importlib.util.spec_from_file_location(module_name, cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        "找不到 " + " 或 ".join(candidates) + "。它们必须和主程序放在同一目录。\n"
        "已尝试：" + "\n  ".join(tried))


def pause_if_interactive():
    """双击运行时别让窗口一闪就关，让人看得到提示。

    只在「冻结成 exe」或「一个参数都没给」（= 大概率是双击）且输出是终端时才暂停；
    带参数跑（如 --status、--next）不受影响，脚本化调用也不会被卡住。
    """
    frozen = bool(getattr(sys, "frozen", False))
    no_args = len(sys.argv) <= 1
    if not (frozen or no_args):
        return
    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    try:
        input("\n按回车键关闭这个窗口…")
    except (EOFError, KeyboardInterrupt):
        pass


def setup_console():
    """Windows 控制台中文输出：尽量切到 UTF-8，并且**永不因为编码问题崩溃**。

    cp936 控制台遇到不在 GBK 里的字符会直接抛 UnicodeEncodeError 把脚本打断。
    这里先尝试把控制台代码页切到 65001，再把流设成 utf-8 + errors=replace。
    """
    if _SYSTEM == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 浏览器发现
# ---------------------------------------------------------------------------

def browser_candidates() -> list[str]:
    """各平台常见位置里，Chromium 系浏览器的候选路径（按优先级）。"""
    cands: list[str] = []
    if _SYSTEM == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        cands += [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        ]
    elif _SYSTEM == "Darwin":
        cands += [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        for exe in ("microsoft-edge", "microsoft-edge-stable", "google-chrome",
                    "google-chrome-stable", "chromium", "chromium-browser"):
            p = shutil.which(exe)
            if p:
                cands.append(p)
    return cands


def find_browser(explicit: str | None = None) -> str:
    """找一个 Chromium 系浏览器（Edge / Chrome / Chromium）。"""
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise FileNotFoundError(f"--browser 指定的路径不存在: {explicit}")

    cands = browser_candidates()
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "没找到 Edge / Chrome / Chromium。请安装其中之一，或用 --browser <路径> 指定。")


def default_profile_dir() -> str:
    """调试用浏览器 profile 的默认位置（持久保存登录态）。

    登录态、Cookie、缓存都在里面 —— **改名等于让所有用户重新登录一次**，
    所以这个字符串以后不要再动。
    """
    if _SYSTEM == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif _SYSTEM == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "CNKIDownload", "browser-profile")


def profile_has_cookies(profile_dir: str) -> bool:
    """粗判这个 profile 里是否已有登录态（Cookie 文件存在且非空）。"""
    for rel in ("Default/Network/Cookies", "Default/Cookies", "Cookies"):
        p = os.path.join(profile_dir, *rel.split("/"))
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# 端口探测
# ---------------------------------------------------------------------------

def _http(port: int, path: str, method: str = "GET", timeout: float = 20.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with _OPENER.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


def port_alive(port: int, timeout: float = 1.5) -> bool:
    """纯 socket 探测：端口有没有在监听（绕过代理，结果干净）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def is_devtools(port: int) -> bool:
    """端口是否真的是 DevTools 服务（而不只是被别的程序占着）。"""
    try:
        v = _http(port, "/json/version", timeout=3)
        return isinstance(v, dict) and ("Browser" in v or "webSocketDebuggerUrl" in v)
    except Exception:
        return False


def cdp_status(port: int) -> dict:
    """给自检和排错用：把「没监听」和「监听了但不是 DevTools」分开。"""
    if not port_alive(port):
        return {"ok": False, "tcp": False, "reason": "not_listening"}
    try:
        v = _http(port, "/json/version", timeout=4)
        return {"ok": True, "tcp": True, "version": v}
    except Exception as e:
        return {"ok": False, "tcp": True, "reason": "not_devtools", "error": f"{type(e).__name__}: {e}"}


def ensure_debug_port_available(port: int):
    """启动我们自己实例之前，确认端口空闲，避免误连到别人家的浏览器。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if _SYSTEM == "Windows" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    except OSError as e:
        raise RuntimeError(
            f"本机调试端口 {port} 已被占用。请关掉旧的 CNKIDownload 浏览器窗口，"
            f"或改用 --port。原始错误: {e}") from e
    finally:
        probe.close()


def launch(profile_dir: str, port: int = DEFAULT_PORT, browser: str | None = None,
           url: str = "about:blank", wait_s: float = 40.0,
           headless: bool = False) -> subprocess.Popen:
    """以远程调试模式启动浏览器（用独立 profile）。

    关键：Chromium/Edge 136+ 只有在显式传入**非默认** --user-data-dir 时才肯开
    --remote-debugging-port，否则端口永远不监听、怎么重试都没用。
    这个 profile 是持久化的 —— 知网机构登录只需做一次，以后自动复用。
    """
    exe = find_browser(browser)
    ensure_debug_port_available(port)
    os.makedirs(profile_dir, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msImplicitSignin",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)

    # 让浏览器真正独立于本进程存活。只用 DETACHED_PROCESS 不够：
    # 本进程退出时 Chrome/Edge 会被父进程的 job object 一起带走，
    # 于是「开窗口 → 人工登录 → 关掉这个命令」这条路直接断掉。
    # CREATE_BREAKAWAY_FROM_JOB + DETACHED_PROCESS 才能独立运行。
    flags = 0
    if _SYSTEM == "Windows":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        proc = subprocess.Popen(args, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=(_SYSTEM != "Windows"))
    except OSError:
        flags = (0x00000008 | 0x00000200) if _SYSTEM == "Windows" else 0
        proc = subprocess.Popen(args, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=(_SYSTEM != "Windows"))

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_devtools(port):
            return proc
        time.sleep(0.5)
    raise RuntimeError(
        f"浏览器已启动，但 {wait_s:.0f} 秒内调试端口 {port} 没有响应。"
        f"检查是否已有同 profile 的实例在运行，或换一个 --port。")


# ---------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC 6455，只实现 CDP 需要的那部分）
# ---------------------------------------------------------------------------

class _WS:
    def __init__(self, url: str, timeout: float = 60.0):
        u = urllib.parse.urlparse(url)
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 80
        self.path = u.path + (f"?{u.query}" if u.query else "")
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\n"
               f"Host: {self.host}:{self.port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手时连接被关闭")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket 握手失败: {head[:160]!r}")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str):
        payload = text.encode("utf-8")
        n = len(payload)
        header = bytearray([0x81])                      # FIN + text
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            data = self._recv_exact(length) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("WebSocket 已被对端关闭")
            if opcode == 0x9:                           # ping -> pong
                self.sock.sendall(b"\x8a\x80" + secrets.token_bytes(4))
                continue
            if opcode in (0x1, 0x2, 0x0):
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Tab:
    """一个页面标签。CDP 偶发超时不该中断整批流程，所以调用都带重试余地。"""

    def __init__(self, port: int, tab_id: str, timeout: float = 60.0):
        self.port = port
        self.id = tab_id
        self.timeout = timeout
        self._seq = 0

    def _ws_url(self) -> str:
        for t in list_tabs(self.port):
            if t.get("id") == self.id:
                return t["webSocketDebuggerUrl"]
        raise RuntimeError(f"标签页 {self.id} 已不存在")

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None):
        ws = _WS(self._ws_url(), timeout or self.timeout)
        try:
            self._seq += 1
            ws.send(json.dumps({"id": self._seq, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == self._seq:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "CDP 报错"))
                    return msg.get("result", {})
        finally:
            ws.close()

    def front(self):
        """把标签提到前台。不是可选的：后台标签的定时器会被浏览器节流。"""
        try:
            self._call("Page.bringToFront", timeout=15)
        except Exception:
            pass

    def js(self, expression: str, timeout: float | None = None, await_promise: bool = True):
        r = self._call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description"))[:200])
        return r.get("result", {}).get("value")

    def goto(self, url: str, settle: float = 2.5) -> str:
        self.front()
        self._call("Page.navigate", {"url": url})
        deadline = time.time() + max(settle, 1.0) * 3
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                if self.js("document.readyState", timeout=10) == "complete":
                    break
            except Exception:
                pass
        time.sleep(settle)
        return url

    def url(self) -> str:
        try:
            return str(self.js("location.href", timeout=10) or "")
        except Exception:
            return ""

    def body_text(self, limit: int = 4000) -> str:
        try:
            t = self.js("document.body ? document.body.innerText : ''", timeout=15)
            return str(t or "")[:limit]
        except Exception:
            return ""

    def close(self):
        close_tab(self.port, self.id)


# ---------------------------------------------------------------------------
# 标签页
# ---------------------------------------------------------------------------

def list_tabs(port: int, timeout: float = 20.0) -> list[dict]:
    try:
        all_t = _http(port, "/json/list", timeout=timeout)
    except Exception:
        return []
    if not isinstance(all_t, list):
        return []
    return [t for t in all_t if isinstance(t, dict) and t.get("type") == "page"]


def tab_urls(port: int) -> set[str]:
    return {str(t.get("url") or "") for t in list_tabs(port)}


def new_tab(port: int, url: str = "about:blank", timeout: float = 30.0) -> str:
    """新开一个标签页。

    走 HTTP 端点 /json/new，等价于浏览器级 CDP 的 Target.createTarget ——
    这是可靠的。**不要**改用页面里 window.open()：会被浏览器弹窗拦截器拦掉，
    脚本照样以为开成功了，但一个标签页都不会出现。
    """
    enc = urllib.parse.quote(url, safe="")
    for method in ("PUT", "GET"):
        try:
            t = _http(port, f"/json/new?{enc}", method=method, timeout=timeout)
            if isinstance(t, dict) and t.get("id"):
                return t["id"]
        except urllib.error.HTTPError:
            continue
        except Exception:
            break
    raise RuntimeError("无法新建标签页（调试端口未就绪？）")


def close_tab(port: int, tab_id: str):
    try:
        _http(port, f"/json/close/{tab_id}", timeout=10)
    except Exception:
        pass


def ensure_browser(port: int = DEFAULT_PORT, profile_dir: str | None = None,
                   browser: str | None = None, url: str = "https://kns.cnki.net/",
                   relaunch: bool = False, wait_s: float = 40.0):
    """确保有一个可用的调试浏览器。

    已经在跑就直接复用（第二次运行不用重新开窗口、不用重新登录）；
    没有才启动。返回 (是否本次新启动, Popen 或 None)。
    """
    if not relaunch and is_devtools(port):
        return False, None
    if relaunch and is_devtools(port):
        raise RuntimeError(
            f"{port} 端口上已经有一个调试浏览器在跑。--relaunch 要求独占端口，"
            f"请先手动关掉那个窗口，或改用别的 --port。")
    prof = profile_dir or default_profile_dir()
    proc = launch(prof, port, browser=browser, url=url, wait_s=wait_s)
    return True, proc


# 主程序与自检脚本共用的提示文案
LAUNCH_HINT = (
    "浏览器没有以调试模式启动。\n"
    "  正常情况下本程序会自己启动它；如果你用了 --no-launch，\n"
    "  请改用不带 --no-launch 的方式运行，或手动启动：\n"
    "    Windows:  msedge.exe --remote-debugging-port=9222 "
    '--user-data-dir="%LOCALAPPDATA%\\CNKIDownload\\browser-profile"\n'
    "    macOS:    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' "
    "--remote-debugging-port=9222 --user-data-dir=\"$HOME/Library/Application Support/CNKIDownload/browser-profile\""
)

# ---------------------------------------------------------------------------
# 共用工具：匹配、命名、移动、下载目录
# ---------------------------------------------------------------------------

import re as _re

MATCH_VERSION = 2

# 验证码 / 风控判据。**不要**用宽松正则（比如正文里搜「验证」）——
# 知网正文含「验证」字样的地方太多，会导致每篇都误判并白等 30 秒。
BLOCKED_URL_RE = _re.compile(r"(verify|captcha|security-check|checkcode|antibot)", _re.I)
BLOCKED_TEXT_RE = _re.compile(
    r"(拖动下方滑块|请完成安全验证|请进行安全验证|滑动验证|机器人验证|访问验证)")


def looks_blocked(url: str, body_text: str) -> bool:
    return bool(BLOCKED_URL_RE.search(url or "")) or bool(
        BLOCKED_TEXT_RE.search((body_text or "")[:1500]))


def normalize_key(s) -> str:
    """统一宽度和大小写，去掉标点；保留 C++、C# 等标题里的 + 和 #。"""
    text = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return _re.sub(r"[^\w+#]|_", "", text, flags=_re.UNICODE)


def author_keys(value) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else _re.split(r"[;；,，、]", str(value or ""))
    return [key for v in values if (key := normalize_key(v))]


def item_identity(item: dict) -> str:
    """缓存和文件记录绑定完整题录；编号相同不意味着文献相同。"""
    fields = [item.get("no"), normalize_key(item.get("title")),
              author_keys(item.get("author")), normalize_key(item.get("year")),
              normalize_key(item.get("journal"))]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def filename_matches(file_base: str, items: list[dict]) -> list[dict]:
    """只接受完整标题或完整标题加已知作者；返回全部候选以便拒绝歧义。"""
    original = str(file_base or "").strip()
    # 只在已知作者后缀后处理浏览器的 " (1)" 序号，不能删掉标题中的年份。
    stems = {original, _re.sub(r" \([1-9]\d*\)$", "", original)}
    matches = []
    for item in items:
        t = normalize_key(item.get("title", ""))
        if not t:
            continue
        authors = author_keys(item.get("author"))
        matched = normalize_key(original) == t
        if not matched and authors:
            for stem in stems:
                for split in (m.start() for m in _re.finditer("_", stem)):
                    prefix, suffix = stem[:split], stem[split + 1:]
                    suffix_authors = author_keys(suffix)
                    if (normalize_key(prefix) == t and suffix_authors
                            and suffix_authors[0] == authors[0]
                            and all(a in authors for a in suffix_authors)):
                        matched = True
        if matched:
            matches.append(item)
    return matches


def match_list_by_filename(file_base: str, items: list[dict], head_len=None):
    """兼容旧参数，但不再使用前缀长度；必须且只能命中一篇文献。"""
    matches = filename_matches(file_base, items)
    return matches[0] if len(matches) == 1 else None


def safe_filename(s, max_len: int = 25) -> str:
    out = _re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or ""))
    return out.rstrip(". ")[:max_len]


def move_file_cross_device(src: str, dst: str) -> str:
    """跨盘移动。os.rename 从 C:\\Users\\...\\Downloads 到 D:\\... 会抛 EXDEV，
    必须 copy + unlink 兜底，否则文件静默卡在原地
    （表现就是「程序说归集了，可下载目录里还在」）。
    """
    try:
        os.replace(src, dst)
        return "rename"
    except OSError as e:
        if getattr(e, "errno", None) not in (18, 1, 13):  # EXDEV, EPERM, EACCES
            raise
        shutil.copyfile(src, dst)
        try:
            os.remove(src)
        except OSError:
            pass  # 副本已落地；源删不掉就留着，宁可有重复也不能丢文件
        return "copy"


def list_pdfs(directory: str) -> list[str]:
    try:
        return [os.path.join(directory, f) for f in os.listdir(directory)
                if f.lower().endswith(".pdf")]
    except OSError:
        return []


def user_downloads_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


if __name__ == "__main__":
    print(f"Python      : {sys.version.split()[0]}")
    print(f"平台        : {_SYSTEM}")
    print(f"profile     : {default_profile_dir()}")
    try:
        print(f"浏览器      : {find_browser()}")
    except FileNotFoundError as e:
        print(f"浏览器      : 未找到 —— {e}")
    st = cdp_status(DEFAULT_PORT)
    print(f"CDP {DEFAULT_PORT}   : {st}")
