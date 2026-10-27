"""UI 层测试：状态总线、本地服务（含令牌与跨站防护）、Earcon、胶囊页面。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

from voxos import ui

REPO = Path(__file__).resolve().parents[1]
TOKEN = "test-token"


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


# ------------------------------------------------------------------ HTTP

@pytest.fixture
def server(bus):
    seen: list[str] = []
    ctx = ui.UiContext(
        bus=bus, token=TOKEN, pill_file=REPO / "web" / "pill.html",
        on_command=lambda text: seen.append(text) or f"收到：{text}",
        settings_get=lambda: [{"id": "audio", "label": "声音与声纹", "fields": []}],
        settings_set=lambda values: {"ok": True, "saved": len(values)},
        status=lambda: {"enrolled": False, "version": "0.1.0"},
        history=lambda: [{"text": "写日志", "reply": "已记录"}],
    )
    instance = ui.serve(ctx, "127.0.0.1", 0, block=False)
    yield instance, seen
    instance.shutdown()
    instance.server_close()


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _get(server, path: str, token: str | None = TOKEN) -> tuple[int, bytes]:
    request = urllib.request.Request(_url(server, path))
    if token:
        request.add_header("X-VoxOS-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post(server, path: str, payload: object, token: str | None = TOKEN,
          origin: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(_url(server, path), data=json.dumps(payload).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
    if token:
        request.add_header("X-VoxOS-Token", token)
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_page_is_served_with_token_injected(server):
    instance, _ = server
    status, body = _get(instance, "/", token=None)
    assert status == 200
    html = body.decode("utf-8")
    assert "Ambient Pill" in html and "backdrop-filter" in html
    assert TOKEN in html and "__VOXOS_TOKEN__" not in html


def test_state_requires_token(server):
    instance, _ = server
    assert _get(instance, "/state", token=None)[0] == 401
    status, body = _get(instance, "/state")
    assert status == 200 and json.loads(body)["last"] is None


def test_command_requires_token(server):
    instance, seen = server
    code, payload = _post(instance, "/command", {"text": "写日志"}, token=None)
    assert code == 401 and "令牌" in payload["error"]
    assert seen == []


def test_command_rejects_cross_origin(server):
    instance, seen = server
    code, payload = _post(instance, "/command", {"text": "删库"}, origin="https://evil.example")
    assert code == 403 and "跨站" in payload["error"]
    assert seen == []


def test_command_runs_with_token(server):
    instance, seen = server
    code, payload = _post(instance, "/command", {"text": "写日志"})
    assert code == 200 and payload["reply"] == "收到：写日志"
    assert seen == ["写日志"]


def test_settings_endpoints(server):
    instance, _ = server
    status, body = _get(instance, "/api/settings")
    assert status == 200 and json.loads(body)["groups"][0]["id"] == "audio"
    status, payload = _post(instance, "/api/settings", {"values": {"a": 1, "b": 2}})
    assert status == 200 and payload["saved"] == 2


def test_status_and_history_endpoints(server):
    instance, _ = server
    assert json.loads(_get(instance, "/api/status")[1])["enrolled"] is False
    assert json.loads(_get(instance, "/api/history")[1])[0]["text"] == "写日志"


def test_unknown_route_is_404(server):
    instance, _ = server
    assert _get(instance, "/nope")[0] == 404
    assert _post(instance, "/nope", {})[0] == 404


def test_bad_body_is_400(server):
    instance, _ = server
    request = urllib.request.Request(_url(instance, "/command"), data=b"not json", method="POST")
    request.add_header("X-VoxOS-Token", TOKEN)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    assert code == 400


def test_confirm_and_enroll_are_wired(server):
    instance, _ = server
    ctx = ui.UiContext(bus=ui.StatusBus(None), token=TOKEN,
                       on_enroll=lambda seconds: {"ok": True, "seconds": seconds},
                       on_confirm=lambda allow: None)
    other = ui.serve(ctx, "127.0.0.1", 0, block=False)
    try:
        assert _post(other, "/api/enroll", {"seconds": 3})[1] == {"ok": True, "seconds": 3.0}
        assert _post(other, "/api/confirm", {"allow": True})[1] == {"ok": True}
    finally:
        other.shutdown()
        other.server_close()


# ------------------------------------------------------------------ 资产

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
    for state in ("listening", "executing", "done", "blocked", "confirm", "onboard"):
        assert f'data-state="{state}"' in html or f"{state}:" in html
    assert "prefers-reduced-motion" in html


def test_play_is_silent_when_no_player(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda _: None)
    ui.play(Path("/nonexistent.wav"))  # 不应抛异常
