"""UI 层测试：状态总线、本地服务（含令牌与跨站防护）、Earcon、胶囊页面。"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

from voxos import audio, main, ui
from voxos.config import load

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


def test_settings_undo_endpoint():
    """撤销：有备份时 GET 说 true，POST 退一步，退完就变 false。"""
    state = {"undo": True, "restored": 0}

    def undo() -> dict:
        if not state["undo"]:
            return {"ok": False, "error": "没有可撤销的保存记录"}
        state["undo"] = False
        state["restored"] += 1
        return {"ok": True, "groups": []}

    ctx = ui.UiContext(bus=ui.StatusBus(None), token=TOKEN,
                       settings_get=lambda: [], settings_undo=undo,
                       settings_can_undo=lambda: state["undo"])
    instance = ui.serve(ctx, "127.0.0.1", 0, block=False)
    try:
        assert json.loads(_get(instance, "/api/settings")[1])["undo"] is True
        code, payload = _post(instance, "/api/settings/undo", {})
        assert code == 200 and payload["ok"] is True and state["restored"] == 1
        assert json.loads(_get(instance, "/api/settings")[1])["undo"] is False
        code, payload = _post(instance, "/api/settings/undo", {})
        assert code == 409 and "没有可撤销" in payload["error"]
        assert state["restored"] == 1
    finally:
        instance.shutdown()
        instance.server_close()


def test_settings_undo_requires_token(server):
    instance, _ = server                      # 这个上下文没接撤销，先验令牌门禁
    code, payload = _post(instance, "/api/settings/undo", {}, token=None)
    assert code == 401 and "令牌" in payload["error"]


def test_post_does_not_poison_the_keepalive_connection():
    """POST 完紧接着 GET，必须都正常。

    HTTP/1.1 长连接上，处理函数不读请求体，那几个字节会留在连接里，下一个请求
    从 `{}` 开始解析——服务器直接回 501。撤销接口不需要参数，当初就漏了这一步，
    表现是"点完撤销，设置页刷不出来"。
    """
    ctx = ui.UiContext(bus=ui.StatusBus(None), token=TOKEN,
                       settings_get=lambda: [{"id": "audio", "label": "声音与声纹", "fields": []}],
                       settings_undo=lambda: {"ok": True, "groups": []})
    instance = ui.serve(ctx, "127.0.0.1", 0, block=False)
    port = instance.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/settings/undo", body="{}",
                     headers={"X-VoxOS-Token": TOKEN, "Content-Type": "application/json"})
        first = conn.getresponse()
        assert first.status == 200, first.status
        first.read()
        conn.request("GET", "/api/settings", headers={"X-VoxOS-Token": TOKEN})
        second = conn.getresponse()
        body = json.loads(second.read())
        assert second.status == 200, (second.status, body)
        assert body["groups"][0]["id"] == "audio"
        conn.close()
    finally:
        instance.shutdown()
        instance.server_close()


def test_status_and_history_endpoints(server):
    instance, _ = server
    assert json.loads(_get(instance, "/api/status")[1])["enrolled"] is False
    assert json.loads(_get(instance, "/api/history")[1])[0]["text"] == "写日志"


def test_subscriber_count_is_capped():
    """每个 SSE 订阅占一个服务线程，没有上限就是一条本地 DoS 通道。

    选择「拒绝」而不是「挤掉最老的」：挤掉谁都是静默失效（页面还连着、却再也收不到
    状态），而拒绝至少让调用方看见 503。
    """
    bus = ui.StatusBus(None)
    channels = [bus.subscribe() for _ in range(ui.StatusBus.MAX_SUBSCRIBERS)]
    with pytest.raises(RuntimeError):
        bus.subscribe()
    bus.unsubscribe(channels[0])
    bus.subscribe()          # 释放一个就能再要一个，上限不是一次性的
    assert len(bus._subs) == ui.StatusBus.MAX_SUBSCRIBERS


def test_stream_returns_503_when_subscribers_are_exhausted():
    bus = ui.StatusBus(None)
    for _ in range(ui.StatusBus.MAX_SUBSCRIBERS):
        bus.subscribe()
    ctx = ui.UiContext(bus=bus, token=TOKEN, pill_file=REPO / "web" / "pill.html")
    instance = ui.serve(ctx, "127.0.0.1", 0, block=False)
    try:
        status, body = _get(instance, "/events")
        assert status == 503, f"订阅满了还给了 {status}"
        assert "上限" in json.loads(body)["error"]
    finally:
        instance.shutdown()
        instance.server_close()


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


def test_page_is_read_from_disk_once_until_it_changes(server, monkeypatch):
    """#9：每次请求都读盘返回 HTML 曾经是热路径上的浪费。

    断言的是缓存**按 mtime 失效**——只测「第二次没读盘」的话，一个永远不失效的
    缓存也能通过，而那意味着改完页面得重启才生效。
    """
    instance, _ = server
    ui._PAGE_CACHE.clear()
    reads: list[Path] = []
    real = Path.read_bytes

    def counting_read(self):
        reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    _get(instance, "/")
    _get(instance, "/")
    assert len(reads) == 1, f"两次请求读了 {len(reads)} 次盘，缓存没生效"

    page = REPO / "web" / "pill.html"
    os.utime(page, (page.stat().st_atime, page.stat().st_mtime + 10))
    try:
        _get(instance, "/")
        assert len(reads) == 2, "文件改过了还在用旧缓存"
    finally:
        os.utime(page, (page.stat().st_atime, page.stat().st_mtime - 10))
        ui._PAGE_CACHE.clear()


def test_every_stt_backend_has_a_human_label():
    """#24：状态卡上曾经直接显示 `scripted` 这种配置标识符。

    这条守的是漂移：新增一个后端却忘了起中文名，这里先红，而不是等用户
    在状态卡上读到一个英文枚举值。
    """
    source = Path(audio.__file__).read_text(encoding="utf-8")
    backends = set(re.findall(r'backend == "(\w+)"', source))
    assert backends, "没解析到 make_stt 的分支，说明它的结构变了，改这条测试"
    missing = backends - set(main.STT_LABELS)
    assert not missing, f"这些后端在界面上没有中文名：{sorted(missing)}"
    for key, label in main.STT_LABELS.items():
        assert label != key, f"{key} 的中文名就是它自己"

    class FakeRuntime:
        cfg = load().with_overrides(**{"stt.backend": "scripted"})
        started = time.time()

    assert main._status(FakeRuntime(), None)["stt"] == "脚本回放（演示）"


def test_play_is_silent_when_no_player(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda _: None)
    ui.play(Path("/nonexistent.wav"))  # 不应抛异常
