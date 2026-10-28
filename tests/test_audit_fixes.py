"""审计修复的回归测试：每一条都对应 CODE_AUDIT 里的一个已复现缺陷。

这些测试的价值不在「现在通过」，而在**退回去会红**：
- 确认必须绑定请求 ID；
- 麦克风必须真的 start；
- 坏参数不得执行、不得用默认值兜底；
- 备份正文不能被元数据覆盖；
- 慢订阅者不能阻塞生产者；
- 设置不能先写坏再报错；
- 远端识别必须先过声纹。
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from voxos import audio, core, dsp, main, safety, settings, tools, ui
from voxos.config import Config, load, load_redlines


# ------------------------------------------------------------------ 执行边界

def test_invalid_enum_is_rejected_before_any_write(cfg, executor, tmp_path):
    target = tmp_path / "projects" / "enum.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    result = executor.execute_safely(
        "write_code",
        {"file_path": "enum.py", "operation": "INVALID_ENUM", "target_snippet": "old", "new_code": "new"},
    )
    assert "参数不匹配" in result
    assert target.read_text(encoding="utf-8") == "old\n"


def test_unknown_and_missing_arguments_are_rejected(cfg, executor):
    assert "未知参数" in executor.execute_safely("write_code", {"file_path": "x.py", "operation": "APPEND_CODE",
                                                                "nope": 1})
    assert "缺少参数" in executor.execute_safely("write_code", {"file_path": "x.py"})


def test_non_object_arguments_never_reach_the_tool(cfg, executor):
    assert "JSON 对象" in executor.execute_safely("rollback_file", ["file_path", "x.py"])


def test_malformed_model_arguments_do_not_become_a_default_call(cfg):
    calls: list[tuple[str, object]] = []

    class Spy:
        def execute_safely(self, name, args, **kwargs):
            calls.append((name, args))
            return "executed"

    results: list[str] = []
    messages: list[dict[str, object]] = []
    core._execute_calls([{"id": "1", "function": {"name": "rollback_file", "arguments": "{"}}],
                        messages, Spy(), results, "回滚")
    assert calls == []
    assert "参数不匹配" in results[0]


def test_tool_failure_is_not_recorded_as_success(cfg, bus):
    executor = core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: True)
    executor.execute_safely("rollback_file", {"file_path": "never.py"})
    record = json.loads(cfg.path("audit.path").read_text(encoding="utf-8").splitlines()[-1])
    assert record["ok"] is False


def test_tmux_prompt_keeps_argument_boundaries(cfg, tmp_path):
    live = cfg.with_overrides(**{"workspace.dry_run": False})
    captured: list[list[str]] = []

    def run(argv, **kwargs):
        captured.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(tools, "RT", tools.Runtime(live)), \
            patch.object(tools.shutil, "which", return_value="/mock/tmux"), \
            patch.object(tools.subprocess, "run", side_effect=run):
        reply = tools.dispatch_code_session("多词 prompt with $(echo hi)")
    assert "已派发" in reply
    sent = next(argv for argv in captured if argv[1] == "send-keys" and "-l" in argv)
    # shlex 引用后整段是一个 shell 参数，命令替换不会展开
    assert sent[-1] == "claude -c -p '多词 prompt with $(echo hi)'"


def test_tmux_failure_is_reported_as_failure(cfg):
    live = cfg.with_overrides(**{"workspace.dry_run": False})
    with patch.object(tools, "RT", tools.Runtime(live)), \
            patch.object(tools.shutil, "which", return_value="/mock/tmux"), \
            patch.object(tools.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
        assert "失败" in tools.dispatch_code_session("改代码")


def test_llm_failure_after_a_tool_call_does_not_replay_offline(cfg):
    calls: list[str] = []

    class Spy:
        def __init__(self):
            self.cfg = cfg
            self.on_event = lambda *_: None

        def execute_safely(self, name, args, **kwargs):
            calls.append(name)
            return "已执行"

    message = {"tool_calls": [{"id": "1", "function": {
        "name": "dispatch_code_session",
        "arguments": json.dumps({"instruction": "把 auth.py 的空值校验补上"})}}]}
    llm = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "http://unused.invalid"})
    with patch.object(core, "chat", side_effect=[message, RuntimeError("HTTP 500")]):
        result = core.dispatch("把 auth.py 的空值校验补上", cfg=llm, executor=Spy(), context={})
    assert calls == ["dispatch_code_session"], calls
    assert result.route == "llm" and "总结失败" in result.reply


# ------------------------------------------------------------------ 安全策略

def test_missing_policy_rejects_every_tool():
    guard = safety.Guard(load_redlines("/nonexistent/redlines.toml"), "DEVELOPER")
    verdict = guard.inspect("append_structured_log", {"content": "x"})
    assert verdict.level is core.RiskLevel.RED_FORBIDDEN
    assert "安全策略" in verdict.reason


def test_unknown_role_is_not_wildcard():
    guard = safety.Guard(load_redlines(), "TYPO_ROLE")
    assert guard.inspect("write_code", {"file_path": "a.py"}).level is core.RiskLevel.RED_FORBIDDEN


# ------------------------------------------------------------------ 快照

def test_target_named_meta_json_survives_rollback(tmp_path):
    target = tmp_path / "meta.json"
    target.write_text("user content", encoding="utf-8")
    backup = tmp_path / "backups"
    safety.snapshot_file(target, backup)
    target.write_text("changed", encoding="utf-8")
    safety.restore_latest(backup)
    assert target.read_text(encoding="utf-8") == "user content"


def test_whitespace_path_is_snapshotted(cfg, tmp_path):
    target = tmp_path / "projects" / "safe.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    executor = core.Executor(cfg=cfg, on_event=lambda *_: None, confirm=lambda *_: True)
    executor.execute_safely("write_code", {"file_path": " safe.py ", "operation": "CREATE_OR_OVERWRITE",
                                           "new_code": "new\n"})
    assert list((tmp_path / "backups").glob("*/safe.py"))


def test_new_file_can_be_undone(tmp_path):
    target = tmp_path / "created.py"
    backup = tmp_path / "backups"
    safety.snapshot_file(target, backup)
    target.write_text("new", encoding="utf-8")
    safety.restore_latest(backup)
    assert not target.exists()


def test_rollback_itself_leaves_a_snapshot(tmp_path):
    target = tmp_path / "a.py"
    backup = tmp_path / "backups"
    target.write_text("v1", encoding="utf-8")
    safety.snapshot_file(target, backup)
    target.write_text("v2", encoding="utf-8")
    safety.restore_latest(backup)
    assert target.read_text(encoding="utf-8") == "v1"
    safety.restore_latest(backup)              # 再撤一次 = 撤销刚才的回滚
    assert target.read_text(encoding="utf-8") == "v2"


# ------------------------------------------------------------------ 状态总线 / 确认

def test_full_subscriber_does_not_block_emit():
    bus = ui.StatusBus(None)
    slow = bus.subscribe()
    healthy = bus.subscribe()
    for _ in range(100):
        bus.emit("idle")
        healthy.get_nowait()
    started = time.perf_counter()
    bus.emit("done")
    assert time.perf_counter() - started < 0.2
    assert not healthy.empty()
    slow.get_nowait()


def test_confirm_answer_only_unblocks_its_own_request():
    bus = ui.StatusBus(None)
    gate = main.ConfirmGate(bus, 0.5)
    answers: list[bool] = []
    ready = threading.Event()
    original = bus.emit

    def emit(*args, **kwargs):
        event = original(*args, **kwargs)
        if args[0] == "confirm" and len([e for e in bus.history() if e["state"] == "confirm"]) >= 1:
            ready.set()
        return event

    bus.emit = emit  # type: ignore[method-assign]
    with patch.object(main.sys, "stdin", None):
        first = threading.Thread(target=lambda: answers.append(gate.ask("A", {}, core.Verdict(
            core.RiskLevel.RED_CONFIRM, "A"))))
        first.start()
        assert ready.wait(1)
        request_id = next(e["request"] for e in bus.history() if e["state"] == "confirm")
        assert gate.resolve(True, request_id) is True
        assert gate.resolve(True, request_id) is False      # 首答锁定
        first.join(2)
    assert answers == [True]


def test_stale_confirm_id_cannot_approve_a_later_request():
    gate = main.ConfirmGate(ui.StatusBus(None), 0.01)
    with patch.object(main.sys, "stdin", None):
        assert gate.ask("A", {}, core.Verdict(core.RiskLevel.RED_CONFIRM, "A")) is False
    assert gate.resolve(True, "expired-id") is False


# ------------------------------------------------------------------ 音频

def test_real_mic_source_is_started_inside_self_healing():
    seen = {"started": 0, "read": 0}

    class Stream:
        blocksize = 512

        def __init__(self, **kwargs):
            pass

        def start(self):
            seen["started"] += 1
            self.running = True

        def read(self, size):
            seen["read"] += 1
            if not getattr(self, "running", False):
                raise RuntimeError("Stream is stopped")
            return np.zeros((size, 1), dtype=np.float32), False

        def stop(self):
            self.running = False

        def close(self):
            pass

    with patch.dict(sys.modules, {"sounddevice": SimpleNamespace(InputStream=Stream)}):
        source = audio.SelfHealingSource(lambda: audio.MicSource(), backoff_s=0, max_retries=1)
        chunk = next(iter(source))
    assert chunk.size == 512
    assert seen["started"] == 1


def test_remote_stt_never_sees_a_stranger(cfg, bus, make_voice):
    heard: list[np.ndarray] = []

    class Remote:
        remote = True

        def transcribe(self, samples, final=False):
            heard.append(samples)
            return "不该上传"

    lock = dsp.SpeakerLock(threshold=0.9, embedder=lambda audio: np.array(
        [1.0 if np.max(np.abs(audio)) > 0.4 else 0.0], dtype=np.float32))
    lock.enroll([np.full(3200, 0.5, dtype=np.float32)])
    pipeline = audio.AudioPipeline(cfg=cfg, stt=Remote(), lock=lock, on_event=bus)
    pipeline._ep.should_end = lambda text: True  # type: ignore[method-assign]
    quiet = make_voice(118, 1.0, amplitude=0.05)
    result = pipeline._maybe_finalize([quiet], "candidate", 0)
    assert not result.accepted and "声纹" in result.reason
    assert heard == []


def test_silero_reset_clears_v5_context():
    vad = audio.SileroVad.__new__(audio.SileroVad)
    vad._h = np.ones((2, 1, 64), dtype=np.float32)
    vad._c = np.ones((2, 1, 64), dtype=np.float32)
    vad._state = np.ones((2, 1, 128), dtype=np.float32)
    vad._context = np.ones((1, 64), dtype=np.float32)
    vad._pending = np.ones(10, dtype=np.float32)
    audio.SileroVad.reset(vad)
    assert not vad._context.any()


# ------------------------------------------------------------------ 设置

def test_control_characters_and_nan_are_rejected():
    _, errors = settings.validate({"llm.model": "bad\x7fname"})
    assert errors and "控制字符" in errors[0]
    _, errors = settings.validate({"audio.voiceprint_threshold": float("nan")})
    assert errors and "有限" in errors[0]


def test_invalid_value_never_reaches_the_file(tmp_path):
    """旧实现先写盘再重新解析：校验通过的控制字符能把配置写成不可读。"""
    target = tmp_path / "settings.toml"
    target.write_text('[llm]\nmodel = "old"\n', encoding="utf-8")
    cfg = Config({"llm": {"model": "old"}}, root=tmp_path, source=target)
    _, errors = settings.apply(cfg, {"llm.model": "bad\x7fname"})
    assert errors
    assert load(target).get("llm.model") == "old"


# ------------------------------------------------------------------ HTTP

def _serve(ctx: ui.UiContext) -> tuple[ui.ThreadingHTTPServer, int]:
    server = ui.serve(ctx, "127.0.0.1", 0, block=False)
    return server, server.server_address[1]


def test_settings_get_sends_exactly_one_response(tmp_path: Path):
    page = tmp_path / "pill.html"
    page.write_text('<script>const TOKEN="__VOXOS_TOKEN__";</script>', encoding="utf-8")
    ctx = ui.UiContext(bus=ui.StatusBus(None), token="tok", pill_file=page,
                       settings_get=lambda: [], settings_can_undo=lambda: False)
    server, port = _serve(ctx)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall((f"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          "X-VoxOS-Token: tok\r\nConnection: close\r\n\r\n").encode())
            raw = b""
            while piece := sock.recv(65536):
                raw += piece
        assert raw.count(b"HTTP/1.1 200 OK") == 1
    finally:
        server.shutdown()
        server.server_close()


def test_untrusted_host_cannot_read_the_page(tmp_path: Path):
    page = tmp_path / "pill.html"
    page.write_text("__VOXOS_TOKEN__", encoding="utf-8")
    ctx = ui.UiContext(bus=ui.StatusBus(None), token="secret", pill_file=page)
    server, port = _serve(ctx)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/", headers={"Host": "evil.example"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        assert response.status == 403 and b"secret" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_page_cache_never_reuses_another_context_token(tmp_path: Path):
    page = tmp_path / "pill.html"
    page.write_text("__VOXOS_TOKEN__", encoding="utf-8")
    ui._PAGE_CACHE.clear()
    for token in ("first-token", "second-token"):
        ctx = ui.UiContext(bus=ui.StatusBus(None), token=token, pill_file=page)
        server, port = _serve(ctx)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/")
            body = conn.getresponse().read()
            conn.close()
            assert token.encode() in body
        finally:
            server.shutdown()
            server.server_close()


def test_confirm_requires_a_real_boolean(tmp_path: Path):
    answers: list[bool] = []
    ctx = ui.UiContext(bus=ui.StatusBus(None), token="tok",
                       on_confirm=lambda allow: answers.append(allow))
    server, port = _serve(ctx)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/confirm", body=json.dumps({"allow": "false"}),
                     headers={"X-VoxOS-Token": "tok", "Content-Type": "application/json"})
        assert conn.getresponse().status == 400
        conn.close()
        assert answers == []
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ 运行态

def test_reload_keeps_the_execution_lock(cfg, bus):
    runtime = main.Runtime(cfg, bus)
    old = runtime.executor._lock
    runtime.reload(cfg.with_overrides(**{"audio.voiceprint_threshold": 0.9}))
    assert runtime.executor._lock is old


def test_env_false_is_false_even_without_a_config_key(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXOS_LLM_ENABLED", "false")
    monkeypatch.setenv("VOXOS_DRY_RUN", "false")
    cfg = load(tmp_path / "missing.toml")
    assert cfg.flag("llm.enabled") is False
    assert cfg.flag("workspace.dry_run") is False
