"""UI 层测试：状态总线、SSE/HTTP 服务、Earcon 合成、胶囊页面。"""

from __future__ import annotations

import json
import urllib.request
import wave
from pathlib import Path

from voxos import ui

REPO = Path(__file__).resolve().parents[1]


def test_bus_emit_and_history(tmp_path):
    bus = ui.StatusBus(tmp_path / "status.jsonl")
    bus("listening", "聆听中", "声纹 0.93")
    bus("done", "完成", "日志已记录")
    history = bus.history()
    assert [event["state"] for event in history] == ["listening", "done"]
    assert bus.snapshot()["count"] == 2
    assert (tmp_path / "status.jsonl").exists()


def test_bus_pushes_to_subscribers(tmp_path):
    bus = ui.StatusBus(tmp_path / "status.jsonl")
    channel = bus.subscribe()
    bus("executing", "写日志…")
    event = channel.get(timeout=1)
    assert event.state == "executing"
    bus.unsubscribe(channel)


def test_http_state_and_command(tmp_path):
    bus = ui.StatusBus(None)
    bus("done", "完成", "日志已记录")
    seen: list[str] = []

    def handler(text: str) -> str:
        seen.append(text)
        return f"收到：{text}"

    server = ui.serve(bus, "127.0.0.1", 0, REPO / "web" / "pill.html", on_command=handler, block=False)
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=3) as response:
            state = json.loads(response.read())
        assert state["last"]["state"] == "done"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/history", timeout=3) as response:
            assert len(json.loads(response.read())) == 1

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "Ambient Pill" in html and "backdrop-filter" in html

        request = urllib.request.Request(f"http://127.0.0.1:{port}/command", data="写日志".encode(), method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.loads(response.read())["reply"] == "收到：写日志"
        assert seen == ["写日志"]
    finally:
        server.shutdown()
        server.server_close()


def test_earcons_are_synthesized(tmp_path):
    paths = ui.ensure_earcons(tmp_path / "sounds")
    assert set(paths) == {"listen", "think", "done", "warn"}
    for path in paths.values():
        with wave.open(str(path)) as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getnframes() > 1000


def test_pill_html_has_four_states():
    html = (REPO / "web" / "pill.html").read_text(encoding="utf-8")
    for state in ("listening", "executing", "done", "blocked", "confirm"):
        assert f'data-state="{state}"' in html or f"{state}:" in html
    assert "prefers-reduced-motion" in html


def test_play_is_silent_when_no_player(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda _: None)
    ui.play(Path("/nonexistent.wav"))  # 不应抛异常
