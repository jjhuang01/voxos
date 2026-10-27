"""状态总线、本地 SSE 服务与微音效。

UI 只是状态的订阅者：核心进程无头运行，胶囊随时可杀可换（DESIGN_SYSTEM.md §1）。
"""

from __future__ import annotations

import json
import math
import queue
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

__all__ = ["StatusBus", "Event", "serve", "play", "ensure_earcons", "EARCONS"]

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

class _PillHandler(BaseHTTPRequestHandler):
    """无状态 HTTP 处理器：/ 胶囊、/state 快照、/history 历史、/events SSE、/command 文本指令。

    上下文（bus / pill_file / on_command）由 `serve()` 通过子类属性注入，避免闭包把
    `serve()` 撑成 80 行的巨型函数。
    """

    protocol_version = "HTTP/1.1"
    bus: StatusBus
    pill_file: Path | None = None
    on_command: Callable[[str], str] | None = None

    def log_message(self, *args: object) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/events"):
            return self._stream()
        if self.path.startswith("/state"):
            return self._json(self.bus.snapshot())
        if self.path.startswith("/history"):
            return self._json(self.bus.history())
        html = self.pill_file.read_bytes() if self.pill_file and self.pill_file.exists() else b"<h1>voxos</h1>"
        return self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/command") or self.on_command is None:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        length = int(self.headers.get("Content-Length", "0"))
        text = self.rfile.read(length).decode("utf-8", "replace")
        try:
            reply = self.on_command(text)
        except Exception as exc:  # 回调异常也要回一个可读响应，不能让胶囊转圈
            reply = f"执行异常：{exc}"
        return self._json({"reply": reply})

    def _json(self, payload: object) -> None:
        return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        channel = self.bus.subscribe()
        try:
            self._frame(self.bus.snapshot())
            while True:
                try:
                    self._frame(asdict(channel.get(timeout=15.0)))
                except queue.Empty:
                    self._frame({"state": "ping"})
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.bus.unsubscribe(channel)

    def _frame(self, payload: object) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()


def serve(
    bus: StatusBus,
    host: str = "127.0.0.1",
    port: int = 8765,
    pill_file: Path | None = None,
    on_command: Callable[[str], str] | None = None,
    block: bool = True,
) -> ThreadingHTTPServer:
    """起一个极简状态服务：/ 胶囊页面、/state 快照、/events SSE、/command 文本指令。"""
    handler = type("VoxosHandler", (_PillHandler,),
                   {"bus": bus, "pill_file": pill_file, "on_command": staticmethod(on_command) if on_command else None})
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
