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
from urllib.parse import parse_qs, urlparse

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


class StatusBus:
    """线程安全的事件总线：落 JSONL + 推送给所有订阅者（SSE/测试）。"""

    def __init__(self, path: Path | None = None, history: int = 50) -> None:
        self.path = path
        self._events: deque[Event] = deque(maxlen=history)
        self._subs: list[queue.Queue[Event]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, state: str, label: str = "", detail: str = "") -> Event:
        with self._lock:
            self._seq += 1
            event = Event(state, label, detail, datetime.now().isoformat(timespec="milliseconds"), self._seq)
            self._events.append(event)
            subscribers = list(self._subs)
        for subscriber in subscribers:
            subscriber.put(event)
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

    def subscribe(self) -> queue.Queue[Event]:
        channel: queue.Queue[Event] = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[Event]) -> None:
        with self._lock:
            if channel in self._subs:
                self._subs.remove(channel)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            last = self._events[-1] if self._events else None
            return {"last": asdict(last) if last else None, "count": self._seq}

    def history(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(event) for event in self._events]


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
    pill_file: Path | None = None
    on_command: Callable[[str], str] | None = None
    settings_get: Callable[[], list[dict[str, object]]] | None = None
    settings_set: Callable[[dict[str, object]], dict[str, object]] | None = None
    settings_undo: Callable[[], dict[str, object]] | None = None
    settings_can_undo: Callable[[], bool] | None = None
    status: Callable[[], dict[str, object]] | None = None
    on_enroll: Callable[[float], dict[str, object]] | None = None
    on_confirm: Callable[[bool], None] | None = None
    history: Callable[[], list[dict[str, object]]] | None = None


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
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _guard(self) -> bool:
        """同源 + 令牌。失败时直接回 403，调用方不用再判。"""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin != f"http://{host}":
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
        path = self.ctx.pill_file
        if not path or not path.exists():
            return self._send(200, b"<h1>voxos</h1>", "text/html; charset=utf-8")
        mtime = path.stat().st_mtime
        with _CACHE_LOCK:
            cached = _PAGE_CACHE.get(path)
        if not cached or cached[0] != mtime:
            html = path.read_bytes().replace(b"__VOXOS_TOKEN__", self.ctx.token.encode())
            with _CACHE_LOCK:
                _PAGE_CACHE[path] = (mtime, html)
            cached = (mtime, html)
        return self._send(200, cached[1], "text/html; charset=utf-8")

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
            "/api/confirm": self._confirm,
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
            reply = f"执行异常：{exc}"
        return self._json({"ok": True, "reply": reply})

    def _read_settings(self) -> None:
        """设置 + 还有没有一步可退。撤销按钮的存在与否由这里说了算，不由前端猜。"""
        groups = self.ctx.settings_get() if self.ctx.settings_get else []
        undo = bool(self.ctx.settings_can_undo and self.ctx.settings_can_undo())
        return self._json({"groups": groups, "undo": undo})

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
        return self._json(result, 200 if result.get("ok") else 400)

    def _confirm(self) -> None:
        body = self._body() or {}
        if self.ctx.on_confirm is not None:
            self.ctx.on_confirm(bool(body.get("allow")))
        return self._json({"ok": True})

    # ---------------------------------------------------------- SSE
    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        channel = self.ctx.bus.subscribe()
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
