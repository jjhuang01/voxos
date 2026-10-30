"""状态总线、本地 SSE 服务与微音效。

UI 只是状态的订阅者：核心进程无头运行，胶囊随时可杀可换（DESIGN_SYSTEM.md §1）。
"""

from __future__ import annotations

import hmac
import json
import math
import queue
import secrets
import shutil
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse, urlsplit

__all__ = ["StatusBus", "Event", "UiContext", "serve", "make_token",
           "play", "ensure_earcons", "EARCONS"]

EARCONS: dict[str, tuple[list[float], float]] = {
    "listen": ([880.0, 1320.0], 0.08),
    "think": ([520.0, 528.0], 0.12),
    "done": ([660.0, 990.0], 0.14),
    "warn": ([440.0, 330.0], 0.18),
}


@dataclass
class Event:
    state: str
    label: str = ""
    detail: str = ""
    ts: str = ""
    seq: int = 0
    request: str = ""       # 确认请求 ID：回答必须带上它，否则会串到别的操作


class StatusBus:
    """线程安全的事件总线：落 JSONL + 推送给所有订阅者（SSE/测试）。"""

    MAX_SUBSCRIBERS = 16
    """订阅上限。每个 SSE 订阅占一个服务线程，没有上限就是一条本地 DoS 通道：
    页面开一百个 EventSource 就能把进程的线程数堆上去。超限**拒绝**而不是挤掉老的，
    因为挤掉谁都是静默失效，而拒绝至少能让调用方看见 503。"""

    def __init__(self, path: Path | None = None, history: int = 50) -> None:
        self.path = path
        self._events: deque[Event] = deque(maxlen=history)
        self._subs: list[queue.Queue[Event]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._confirmation: Event | None = None

    def emit(self, state: str, label: str = "", detail: str = "", request: str = "") -> Event:
        with self._lock:
            self._seq += 1
            event = Event(state, label, detail, datetime.now().isoformat(timespec="milliseconds"),
                          self._seq, request)
            self._events.append(event)
            if state == "confirm" and request:
                self._confirmation = event
            elif (state == "confirmation_end" and self._confirmation is not None
                  and self._confirmation.request == request):
                self._confirmation = None
            subscribers = list(self._subs)
        for subscriber in subscribers:
            _offer(subscriber, event)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            except OSError:
                pass
        return event

    def __call__(self, state: str, label: str = "", detail: str = "") -> Event:
        """让总线可以直接当 `on_event` 回调传进内核。"""
        return self.emit(state, label, detail)

    def emit_partial(self, text: str) -> None:
        """边说边出字。**不入 history、不落盘、不推给审计**。

        字幕是过程量：一句话能出十条，而且每条都会被下一条改掉。写进 history
        会把「最近发生了什么」冲干净——用户打开面板想看的是刚才执行了什么，
        不是自己说到一半的草稿。所以只推给此刻在线的订阅者，错过就错过。
        """
        with self._lock:
            event = Event("listening", "聆听中", text, datetime.now().isoformat(timespec="milliseconds"),
                          self._seq)
            subscribers = list(self._subs)
        for subscriber in subscribers:
            _offer(subscriber, event)

    def subscribe(self) -> queue.Queue[Event]:
        """拿一个订阅通道。超过上限时抛 `RuntimeError`，由调用方转成 503。"""
        with self._lock:
            if len(self._subs) >= self.MAX_SUBSCRIBERS:
                raise RuntimeError(f"订阅数已达上限 {self.MAX_SUBSCRIBERS}")
            channel: queue.Queue[Event] = queue.Queue(maxsize=100)
            self._subs.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[Event]) -> None:
        with self._lock:
            if channel in self._subs:
                self._subs.remove(channel)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            last = self._events[-1] if self._events else None
            return {"last": asdict(last) if last else None, "count": self._seq,
                    "confirmation": asdict(self._confirmation) if self._confirmation else None}

    def history(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(event) for event in self._events]


def _offer(channel: queue.Queue[Event], event: Event) -> None:
    """非阻塞投递：一个卡住的界面不能把音频、执行和确认一起拖死。

    队列满时丢最旧的一条再放新的——状态是「最新值有意义」，不是账本。
    """
    try:
        channel.put_nowait(event)
        return
    except queue.Full:
        pass
    try:
        channel.get_nowait()
    except queue.Empty:
        pass
    try:
        channel.put_nowait(event)
    except queue.Full:
        pass


# ------------------------------------------------------------------ 微音效

def ensure_earcons(directory: Path) -> dict[str, Path]:
    """首次运行时合成 4 个 Earcon（不携带二进制资产）。"""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, (freqs, duration) in EARCONS.items():
        path = directory / f"{name}.wav"
        if not path.exists():
            _synth(path, freqs, duration)
        paths[name] = path
    return paths


def _synth(path: Path, freqs: list[float], duration: float, sr: int = 44100) -> None:
    frames = int(sr * duration)
    samples = bytearray()
    for index in range(frames):
        t = index / sr
        envelope = math.exp(-18.0 * t) * (1 - math.exp(-600.0 * t))  # 快起慢落，无爆音
        value = sum(math.sin(2 * math.pi * f * t) for f in freqs) / len(freqs)
        samples += struct.pack("<h", int(max(-1.0, min(1.0, value * envelope * 0.35)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(bytes(samples))


def play(path: Path) -> None:
    """非阻塞播放；找不到播放器就静默跳过（无音频环境下不能影响主流程）。"""
    player = shutil.which("afplay") or shutil.which("aplay") or shutil.which("paplay")
    if not player:
        return
    try:
        subprocess.Popen([player, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ------------------------------------------------------------------ HTTP/SSE

@dataclass
class UiContext:
    """胶囊页面能看到的全部能力。用注入代替 if 分支，加接口不用改 serve()。"""

    bus: StatusBus
    token: str = ""
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    pill_file: Path | None = None
    on_command: Callable[[str], str | dict[str, object]] | None = None
    settings_get: Callable[[], list[dict[str, object]]] | None = None
    settings_set: Callable[[dict[str, object]], dict[str, object]] | None = None
    settings_undo: Callable[[], dict[str, object]] | None = None
    settings_can_undo: Callable[[], bool] | None = None
    status: Callable[[], dict[str, object]] | None = None
    on_enroll: Callable[[float], dict[str, object]] | None = None
    on_enroll_cancel: Callable[[], dict[str, object]] | None = None
    on_confirm: Callable[[bool], None] | None = None
    history: Callable[[], list[dict[str, object]]] | None = None
    on_listen: Callable[[bool], dict[str, object]] | None = None
    on_cue: Callable[[dict[str, object]], dict[str, object]] | None = None
    on_stop: Callable[[], dict[str, object]] | None = None


_PAGE_CACHE: dict[Path, tuple[float, bytes]] = {}
_CACHE_LOCK = threading.Lock()


def make_token() -> str:
    """每次启动一个新的会话令牌：跨站页面拿不到它，也就驱动不了 Agent。"""
    return secrets.token_urlsafe(18)


class _PillHandler(BaseHTTPRequestHandler):
    """无状态 HTTP 处理器：页面 + 状态 + 设置 + 录入 + 确认，全部走 `UiContext`。

    安全模型（REDLINES.md §5.5）：只有页面本身免令牌；其余接口都要求本次运行的令牌，
    且拒绝非本机同源的 `Origin`。浏览器跨站请求拿不到令牌，所以 CSRF 不成立。
    """

    protocol_version = "HTTP/1.1"
    timeout = 30                     # 慢连接不能无限占住一个线程
    MAX_BODY = 1_000_000
    ctx: UiContext
    _payload: dict[str, object] | None = None

    def log_message(self, *args: object) -> None:
        return

    # ---------------------------------------------------------- 基础设施
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        return self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")

    def _body(self) -> dict[str, object] | None:
        """POST 的请求体。在 `do_POST` 里统一读完，这里只负责给出去。

        一定要"统一读"：HTTP/1.1 是长连接，处理函数不读请求体，那几个字节就会留在
        连接里，下一个请求从 `{}` 开始解析，服务器直接回 501。撤销接口不需要参数，
        当初就是因为它没读，才让"撤销之后页面刷不出设置"。
        """
        return self._payload

    def _read_body(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > self.MAX_BODY:
            # 必须把已声明的那段读完再拒绝，否则长连接会被残留字节写花。
            remaining = max(0, min(length, 8_000_000))
            while remaining > 0:
                piece = self.rfile.read(min(65536, remaining))
                if not piece:
                    break
                remaining -= len(piece)
            return None
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _host_name(self) -> str:
        return (urlsplit("//" + self.headers.get("Host", "")).hostname or "").lower()

    def _trusted_host(self) -> bool:
        """Host 是客户端自报的，不能拿它自己证明自己。

        只接受本机名字的 allowlist；否则一个指向 127.0.0.1 的恶意域名
        （DNS rebinding）也能读到注入了令牌的页面。
        """
        return self._host_name() in {name.lower() for name in self.ctx.allowed_hosts}

    def _guard(self) -> bool:
        """可信 Host + 同源 + 令牌。失败时直接回 403，调用方不用再判。"""
        if not self._trusted_host():
            self._json({"ok": False, "error": "不受信任的 Host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or (parsed.hostname or "").lower() != self._host_name():
                self._json({"ok": False, "error": "跨站请求已拒绝"}, 403)
                return False
        token = self.ctx.token
        if not token:
            return True
        supplied = self.headers.get("X-VoxOS-Token", "")
        if not supplied:
            supplied = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        if not hmac.compare_digest(supplied, token):
            self._json({"ok": False, "error": "缺少或错误的令牌"}, 401)
            return False
        return True

    def _page(self) -> None:
        if not self._trusted_host():
            return self._json({"ok": False, "error": "不受信任的 Host"}, 403)
        path = self.ctx.pill_file
        if not path or not path.exists():
            return self._send(200, b"<h1>voxos</h1>", "text/html; charset=utf-8")
        mtime = path.stat().st_mtime
        with _CACHE_LOCK:
            cached = _PAGE_CACHE.get(path)
        if not cached or cached[0] != mtime:
            # 缓存**模板**，不缓存注入了令牌的成品：同一进程换一个 context
            # （嵌入式启动、多实例）时，旧令牌会发给新页面，接口随即 401。
            with _CACHE_LOCK:
                _PAGE_CACHE[path] = (mtime, path.read_bytes())
            cached = _PAGE_CACHE[path]
        body = cached[1].replace(b"__VOXOS_TOKEN__", self.ctx.token.encode())
        return self._send(200, body, "text/html; charset=utf-8")

    # ---------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/settings"):
            return self._page()
        if not self._guard():
            return None
        routes: dict[str, Callable[[], object]] = {
            "/state": self.ctx.bus.snapshot,
            "/history": self.ctx.bus.history,
            "/api/status": self.ctx.status or (lambda: {}),
            "/api/settings": self._read_settings,
            "/api/history": self.ctx.history or list,
        }
        if path == "/events":
            return self._stream()
        route = routes.get(path)
        return self._json(route()) if route else self._json({"ok": False, "error": "没有这个接口"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return None
        self._payload = self._read_body()      # 先读完，见 _body 的说明
        path = urlparse(self.path).path
        routes: dict[str, Callable[[], None]] = {
            "/command": self._command,
            "/api/settings": self._save_settings,
            "/api/settings/undo": self._undo_settings,
            "/api/enroll": self._enroll,
            "/api/enroll/cancel": self._enroll_cancel,
            "/api/confirm": self._confirm,
            "/api/listen": self._listen,
            "/api/cue": self._cue,
            "/api/stop": self._stop,
        }
        route = routes.get(path)
        return route() if route else self._json({"ok": False, "error": "没有这个接口"}, 404)

    # ---------------------------------------------------------- 动作
    def _command(self) -> None:
        body = self._body()
        if body is None or self.ctx.on_command is None:
            return self._json({"ok": False, "error": "请求体需要 JSON"}, 400)
        try:
            reply = self.ctx.on_command(str(body.get("text", "")))
        except Exception as exc:  # 回调异常也要回一个可读响应，不能让胶囊转圈
            return self._json({"ok": False, "error": f"执行异常：{exc}"}, 500)
        return self._json(reply if isinstance(reply, dict) else {"ok": True, "reply": reply})

    def _read_settings(self) -> dict[str, object]:
        """设置 + 还有没有一步可退。只返回数据，发送由路由层统一做一次。

        旧实现在这里直接 `_json(...)`，`do_GET` 又把 None 发了一遍——
        一个请求两个响应，长连接上的下一个请求就会读到错位的响应。
        """
        groups = self.ctx.settings_get() if self.ctx.settings_get else []
        undo = bool(self.ctx.settings_can_undo and self.ctx.settings_can_undo())
        return {"groups": groups, "undo": undo}

    def _save_settings(self) -> None:
        body = self._body()
        if body is None or self.ctx.settings_set is None:
            return self._json({"ok": False, "errors": ["请求体需要 JSON"]}, 400)
        result = self.ctx.settings_set(dict(body.get("values") or {}))
        return self._json(result, 200 if result.get("ok") else 400)

    def _undo_settings(self) -> None:
        if self.ctx.settings_undo is None:
            return self._json({"ok": False, "error": "未启用撤销"}, 400)
        result = self.ctx.settings_undo()
        return self._json(result, 200 if result.get("ok") else 409)

    def _enroll(self) -> None:
        body = self._body() or {}
        if self.ctx.on_enroll is None:
            return self._json({"ok": False, "error": "未启用录入"}, 400)
        try:
            result = self.ctx.on_enroll(float(body.get("seconds", 3.0)))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "秒数不合法"}, 400)
        return self._json(result, 200 if result.get("ok") or result.get("cancelled") else 400)

    def _enroll_cancel(self) -> None:
        if self.ctx.on_enroll_cancel is None:
            return self._json({"ok": False, "error": "未启用录入"}, 400)
        result = self.ctx.on_enroll_cancel()
        return self._json(result, 200 if result.get("ok") else 409)

    def _stop(self) -> None:
        """停止当前回合，不把暂停麦克风误作停止生成或取消后续动作。"""
        if self.ctx.on_stop is None:
            return self._json({"ok": False, "error": "未启用停止控制"}, 400)
        return self._json(self.ctx.on_stop())

    def _listen(self) -> None:
        """开/关持续监听。关掉之后麦克风照常读（否则设备缓冲会溢出），
        但音频不再进入 VAD/识别——就像智能音箱上的静音键。"""
        body = self._body() or {}
        on = body.get("on")
        if not isinstance(on, bool):
            return self._json({"ok": False, "error": "on 必须是布尔值"}, 400)
        if self.ctx.on_listen is None:
            return self._json({"ok": False, "error": "未启用监听开关"}, 400)
        return self._json(self.ctx.on_listen(on))

    def _cue(self) -> None:
        """外部触发器推一条提示：只上屏/出声，不执行任何动作。

        与 `/command` 的区别是刻意分开的：command 会派发、可能改文件；
        cue 只是说话。脚本、日历、CI 都该走这条，而不是拼一句「提醒我…」
        塞进命令通道——那样一次误触发就可能变成一次执行。
        """
        body = self._body()
        if body is None:
            return self._json({"ok": False, "error": "请求体需要 JSON"}, 400)
        if self.ctx.on_cue is None:
            return self._json({"ok": False, "error": "未启用主动提示"}, 400)
        return self._json(self.ctx.on_cue(body))

    def _confirm(self) -> None:
        body = self._body() or {}
        allow = body.get("allow")
        request_id = body.get("id", "")
        if not isinstance(allow, bool):
            return self._json({"ok": False, "error": "allow 必须是布尔值"}, 400)
        if not isinstance(request_id, str):
            return self._json({"ok": False, "error": "确认 ID 不合法"}, 400)
        if self.ctx.on_confirm is not None:
            accepted = self._call_confirm(allow, request_id)
            if accepted is False:
                return self._json({"ok": False, "error": "确认已失效或已被回答"}, 409)
        return self._json({"ok": True})

    def _call_confirm(self, allow: bool, request_id: str) -> object:
        """兼容只接收 allow 的旧回调；新回调返回 False 表示这次回答无效。"""
        assert self.ctx.on_confirm is not None
        try:
            return self.ctx.on_confirm(allow, request_id)  # type: ignore[call-arg]
        except TypeError:
            return self.ctx.on_confirm(allow)

    # ---------------------------------------------------------- SSE
    def _stream(self) -> None:
        try:
            channel = self.ctx.bus.subscribe()
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)}, 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._frame(self.ctx.bus.snapshot())
            while True:
                try:
                    self._frame(asdict(channel.get(timeout=15.0)))
                except queue.Empty:
                    self._frame({"state": "ping"})
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.ctx.bus.unsubscribe(channel)

    def _frame(self, payload: object) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()


def serve(ctx: UiContext, host: str = "127.0.0.1", port: int = 8765,
          block: bool = True) -> ThreadingHTTPServer:
    """起一个极简本地服务：页面、SSE、指令、设置、录入、确认。"""
    if not ctx.token:
        ctx.token = make_token()
    handler = type("VoxosHandler", (_PillHandler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    if block:
        server.serve_forever()
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_forever() -> None:
    """守护进程主循环：Ctrl-C 退出。"""
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[voxos] 已退出")
