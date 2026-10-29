"""TTS 回归：假 HTTP、假设备与假 say；不联网合成、不出声、不录音。"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from voxos import tts

AUDIO_A = b"\x01\x02\x03\x04"
AUDIO_B = b"\x05\x06\x07\x08"


def _chunk(data: bytes) -> str:
    return json.dumps({"code": 0, "message": "", "data": base64.b64encode(data).decode()})


DONE = json.dumps({"code": tts.DONE_CODE, "message": "ok", "data": None})


class _Handler(BaseHTTPRequestHandler):
    """把请求记下来，然后按「首尾相接的 JSON 串」回音频。"""

    def do_POST(self) -> None:                       # noqa: N802 - 框架要求的方法名
        size = int(self.headers.get("Content-Length", 0))
        self.server.seen = {                          # type: ignore[attr-defined]
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": json.loads(self.rfile.read(size) or b"{}"),
        }
        payload = (self.server.payload or "").encode()   # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        # **故意切成两半**：真实网络下对象会被拆到两个 TCP 段里，
        # 按行切或者按块切都会在这里炸，而线上只表现为「偶尔少一段音频」。
        half = len(payload) // 2
        self.wfile.write(payload[:half])
        self.wfile.flush()
        self.wfile.write(payload[half:])

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.payload = _chunk(AUDIO_A) + _chunk(AUDIO_B) + DONE   # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


@pytest.fixture
def cloud(cfg, server, monkeypatch):
    """把 ENDPOINT 指向假服务器，返回（配置，服务器）。"""
    monkeypatch.setattr(tts, "ENDPOINT",
                        f"http://127.0.0.1:{server.server_address[1]}/api/v3/tts/unidirectional")
    return cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": "sk-test",
                                 "tts.speaker": "zh_female_test", "tts.sample_rate": 24000}), server


def test_audio_chunks_are_decoded_in_order(cloud):
    """音频块是 base64 的 PCM，要按顺序拼起来——顺序错了听出来是噪音。"""
    cfg, _ = cloud
    assert b"".join(tts.synthesize("你好", cfg)) == AUDIO_A + AUDIO_B


def test_a_json_object_split_across_reads_is_not_lost(cloud):
    """响应是一串首尾相接的 JSON，不保证按块对齐。

    上面那个假服务器**故意把一个对象切成两半**——按行切或者按块 `json.loads`
    的实现在这里会少一段音频，而线上只表现为「偶尔缺半句」。
    """
    cfg, _ = cloud
    chunks = list(tts.synthesize("你好", cfg))
    assert len(chunks) == 2


def test_request_matches_the_documented_shape(cloud):
    """字段名是照官方文档抄的：`req_params` / `audio_params` / `format=pcm`。

    写错一个字段名，服务端不会报「字段不认识」，只会用默认值——
    比如把 `pcm` 写成 `wav`，回来的是 mp3，播放端听起来是一片噪音。
    """
    cfg, server = cloud
    list(tts.synthesize("你好", cfg))
    seen = server.seen                                  # type: ignore[attr-defined]
    params = seen["body"]["req_params"]
    assert params["text"] == "你好"
    assert params["speaker"] == "zh_female_test"
    assert params["audio_params"] == {"format": "pcm", "sample_rate": 24000}
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["x-api-resource-id"] == "seed-tts-2.0"


def test_legacy_console_uses_app_id_and_access_key(cfg, server, monkeypatch):
    """旧版控制台没有 `X-Api-Key` 这个东西，硬塞会被拒。"""
    legacy = cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": "",
                                   "tts.app_id": "123456", "tts.access_key": "ak"})
    assert tts.available(legacy) is True
    head = tts.headers(legacy)
    assert head["X-Api-App-Id"] == "123456" and head["X-Api-Access-Key"] == "ak"
    assert "X-Api-Key" not in head


def test_error_code_is_raised_not_swallowed(cloud):
    """出错时服务端返回的是**别的 code**，不是 HTTP 500。

    当成音频继续解 base64，用户听到的就是一阵爆音，而且完全没有报错线索。
    """
    cfg, server = cloud
    server.payload = json.dumps({"code": 45000000,                      # type: ignore[attr-defined]
                                "message": "speaker permission denied"})
    with pytest.raises(RuntimeError, match="speaker permission denied"):
        list(tts.synthesize("你好", cfg))


def test_error_code_55000000_indicates_version_mismatch(cloud):
    """seed-tts-2.0 配 1.0 音色返回 55000000 时，提示音色与 resource_id 版本不匹配。"""
    cfg, server = cloud
    server.payload = json.dumps({"code": 55000000, "message": "resource ID mismatched"})
    with pytest.raises(RuntimeError, match="音色与 resource_id 版本不匹配"):
        list(tts.synthesize("你好", cfg))


def test_default_speaker_is_uranus_2_0(cfg):
    """默认音色必须是 2.0 音色 zh_female_vv_uranus_bigtts。"""
    request_body = tts.body("你好", cfg)
    assert request_body["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"


def test_stop_cuts_the_stream_without_waiting(cloud):
    """被打断时不该把剩下的合成完再停——那是几百毫秒的延迟和一份白花的钱。"""
    cfg, _ = cloud
    assert list(tts.synthesize("你好", cfg, lambda: True)) == []


def test_not_available_without_a_key(cfg):
    """半配着去发请求比不配更糟：用户等 8 秒超时，还以为是自己网络不好。"""
    assert tts.available(cfg.with_overrides(**{"tts.provider": "say"})) is False
    assert tts.available(cfg.with_overrides(**{"tts.provider": "doubao",
                                               "tts.api_key": ""})) is False
    assert tts.available(cfg.with_overrides(**{"tts.provider": "doubao",
                                               "tts.api_key": "sk-x"})) is True


def test_probe_says_which_part_is_missing(cfg):
    """`voxos doctor` 要能回答「为什么还是机器腔」。"""
    ok, why = tts.probe(cfg.with_overrides(**{"tts.provider": "say"}))
    assert ok is False and "say" in why
    ok, why = tts.probe(cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": ""}))
    assert ok is False and "Key" in why
    ok, why = tts.probe(cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": "sk-x"}))
    assert ok is True and "seed-tts-2.0" in why


@pytest.fixture
def device(monkeypatch):
    stream = SimpleNamespace(start=Mock(), stop=Mock(), abort=Mock(),
                             close=Mock(), write=Mock())
    module = SimpleNamespace(RawOutputStream=Mock(return_value=stream))
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module, stream


@pytest.fixture
def session(monkeypatch):
    from voxos import main
    owner = object()
    monkeypatch.setattr(main, "_TTS", {"owner": owner, "proc": None, "stopped": False})
    monkeypatch.setattr(main.core, "note_tts_failure", Mock())
    return main, owner


def _source(parts, closed):
    try:
        yield from parts
    finally:
        closed.append(True)


def _response(monkeypatch, parts):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read1.side_effect = parts
    request = Mock(return_value=response)
    monkeypatch.setattr(tts.urllib.request, "urlopen", request)
    return response, request


def _wav(path, pcm=AUDIO_A, rate=24000, channels=1, width=2):
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        output.writeframes(pcm)


@pytest.fixture
def fake_say(monkeypatch, tmp_path):
    from voxos import main
    original = main.tempfile.TemporaryDirectory
    monkeypatch.setattr(main, "tempfile", SimpleNamespace(
        TemporaryDirectory=lambda **kw: original(dir=tmp_path, **kw)))
    processes = []

    def spawn(args, **kwargs):
        proc = Mock(args=args, stdin=io.BytesIO(), returncode=None)
        proc.poll.side_effect = lambda: proc.returncode
        proc.kwargs = kwargs

        def communicate(input=None, timeout=None):
            proc.text = input
            if "-o" in args:
                _wav(Path(args[args.index("-o") + 1]))
            proc.returncode = 0
            return None, None

        def kill():
            proc.returncode = -9

        proc.communicate.side_effect = communicate
        proc.kill.side_effect = kill
        processes.append(proc)
        return proc

    monkeypatch.setattr(main, "subprocess", SimpleNamespace(
        Popen=spawn, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired))
    return processes


def test_cleanup_oserror_preserves_completed_local_speech(cfg, session, monkeypatch):
    """完成播放后清理失败不应改写 heard；只模拟异常，不涉及真实临时文件。"""
    main, owner = session
    directory = Mock()
    directory.__enter__ = Mock(return_value="/unused-tts-test")
    directory.__exit__ = Mock(side_effect=OSError("cleanup failure"))
    monkeypatch.setattr(main, "tempfile", SimpleNamespace(TemporaryDirectory=Mock(return_value=directory)))
    monkeypatch.setattr(main, "_run_say", Mock(return_value=True))
    monkeypatch.setattr(tts, "play_wav", Mock(return_value=True))
    assert main._say_locally("已经播完", cfg, owner)
    main.core.note_tts_failure.assert_called_once()


def test_play_merges_byte_boundaries_and_references_only_submitted_pcm(cfg, device, monkeypatch):
    module, stream = device
    payload = bytes(range(256)) * 4
    closed, references, times = [], [], iter([1.25, 1.26, 1.27])
    monkeypatch.setattr(tts.time, "monotonic", lambda: next(times))

    def reference(block, rate, stamp):
        assert stream.write.call_args.args == (block,)
        references.append((block, rate, stamp))

    parts = [payload[:1], b"", payload[1:479], payload[479:481], payload[481:]]
    assert tts.play(_source(parts, closed), cfg, reference=reference)
    assert [len(block) for block, _, _ in references] == [480, 480, 64]
    assert b"".join(block for block, _, _ in references) == payload
    assert [rate for _, rate, _ in references] == [24000] * 3
    assert [stamp for _, _, stamp in references] == [1.25, 1.26, 1.27]
    module.RawOutputStream.assert_called_once_with(samplerate=24000, channels=1, dtype="int16")
    stream.stop.assert_called_once_with(ignore_errors=False)
    stream.abort.assert_not_called()
    stream.close.assert_called_once()
    assert closed == [True]


@pytest.mark.parametrize("parts", [[], [b"", b""]])
def test_empty_playback_is_false_and_aborts(cfg, device, parts):
    _, stream = device
    reference, closed = Mock(), []
    assert not tts.play(_source(parts, closed), cfg, reference=reference)
    stream.write.assert_not_called()
    reference.assert_not_called()
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    stream.close.assert_called_once()
    assert closed == [True]


def test_cancelled_before_play_closes_source_without_opening_device(cfg, device):
    module, _ = device
    closed = []
    source = _source([b"", AUDIO_A], closed)
    next(source)
    assert not tts.play(source, cfg, lambda: True)
    module.RawOutputStream.assert_not_called()
    assert closed == [True]


def test_cancel_after_fetch_does_not_write_or_reference(cfg, device):
    _, stream = device
    cancelled, closed, reference = threading.Event(), [], Mock()

    def chunks():
        try:
            cancelled.set()
            yield AUDIO_A * 200
        finally:
            closed.append(True)

    assert not tts.play(chunks(), cfg, cancelled.is_set, reference)
    stream.write.assert_not_called()
    reference.assert_not_called()
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    assert closed == [True]


def test_cancel_during_write_reports_that_block_then_aborts(cfg, device):
    _, stream = device
    cancelled, closed, reference = threading.Event(), [], Mock()
    stream.write.side_effect = lambda _: cancelled.set()
    assert not tts.play(_source([AUDIO_A * 300], closed), cfg, cancelled.is_set, reference)
    stream.write.assert_called_once_with(AUDIO_A * 120)
    assert reference.call_args.args[:2] == (AUDIO_A * 120, 24000)
    reference.assert_called_once()
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    stream.close.assert_called_once()
    assert closed == [True]


@pytest.mark.parametrize("where", ["source", "write", "reference"])
def test_playback_exceptions_abort_close_and_prevent_replay(cfg, device, where):
    _, stream = device
    closed, reference = [], Mock()

    def chunks():
        try:
            yield AUDIO_A * 120
            if where == "source":
                raise RuntimeError("流失败")
            yield AUDIO_B * 120
        finally:
            closed.append(True)

    if where == "write":
        stream.write.side_effect = RuntimeError("写入失败")
    if where == "reference":
        reference.side_effect = RuntimeError("参考失败")
    with pytest.raises(tts.PlaybackStartedError):
        tts.play(chunks(), cfg, reference=reference)
    if where == "write":
        reference.assert_not_called()
    else:
        reference.assert_called_once()
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    stream.close.assert_called_once()
    assert closed == [True]


@pytest.mark.parametrize("pcm", [b"x", AUDIO_A + b"x", AUDIO_A * 120 + b"x"])
def test_odd_tail_never_submits_half_sample(cfg, device, pcm):
    _, stream = device
    with pytest.raises((ValueError, tts.PlaybackStartedError), match="半个"):
        tts.play(iter([pcm]), cfg)
    assert all(len(call.args[0]) % 2 == 0 for call in stream.write.call_args_list)
    assert stream.write.call_count == len(pcm) // 480
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()


def test_synthesize_reads_incrementally_and_done_stops_without_another_read(cfg, monkeypatch):
    response, _ = _response(monkeypatch, [(_chunk(AUDIO_A) + DONE).encode(),
                                          AssertionError("DONE 后不应继续读")])
    assert list(tts.synthesize("你好", cfg)) == [AUDIO_A]
    response.read.assert_not_called()
    response.read1.assert_called_once_with(4096)
    response.__exit__.assert_called_once()


def test_synthesize_yields_audio_before_waiting_for_done(cfg, monkeypatch):
    response, _ = _response(monkeypatch, [_chunk(AUDIO_A).encode(), DONE.encode()])
    source = tts.synthesize("你好", cfg)
    assert next(source) == AUDIO_A
    assert response.read1.call_count == 1
    source.close()
    response.__exit__.assert_called_once()


def test_synthesize_keeps_utf8_characters_split_across_reads(cfg, monkeypatch):
    payload = json.dumps({"code": 42, "message": "语音失败"}, ensure_ascii=False).encode()
    _response(monkeypatch, [bytes([byte]) for byte in payload])
    with pytest.raises(RuntimeError, match="语音失败"):
        list(tts.synthesize("你好", cfg))


@pytest.mark.parametrize("payload", [b"", DONE.encode(), _chunk(AUDIO_A).encode(),
                                     b'{"code":', b'\xe4\xbd',
                                     (_chunk(b"x") + DONE).encode()])
def test_synthesize_rejects_empty_missing_done_or_truncated_stream(cfg, monkeypatch, payload):
    response, _ = _response(monkeypatch, [payload, b""])
    with pytest.raises((RuntimeError, UnicodeDecodeError)):
        list(tts.synthesize("你好", cfg))
    response.__exit__.assert_called_once()


def test_synthesize_does_not_start_network_when_cancelled(cfg, monkeypatch):
    _, request = _response(monkeypatch, [])
    assert list(tts.synthesize("你好", cfg, lambda: True)) == []
    request.assert_not_called()


def test_synthesize_cancels_after_read_without_yielding(cfg, monkeypatch):
    cancelled = threading.Event()
    response, _ = _response(monkeypatch, [])

    def read1(size):
        cancelled.set()
        return (_chunk(AUDIO_A) + DONE).encode()

    response.read1.side_effect = read1
    assert list(tts.synthesize("你好", cfg, cancelled.is_set)) == []
    response.__exit__.assert_called_once()


def test_cloud_and_local_pcm_share_playback(cfg, device, session, fake_say, monkeypatch):
    main, owner = session
    _, stream = device
    play = Mock(wraps=tts.play)
    monkeypatch.setattr(tts, "play", play)
    _response(monkeypatch, [(_chunk(AUDIO_A) + DONE).encode()])
    assert tts.speak("你好", cfg)
    assert main._say_locally("-o 不应成为选项", cfg, owner)
    assert play.call_count == 2
    assert [call.args for call in stream.write.call_args_list] == [(AUDIO_A,), (AUDIO_A,)]
    proc = fake_say[0]
    assert proc.text == "-o 不应成为选项".encode()
    assert proc.args[:3] == ["say", "-r", "220"]
    assert proc.args[3:6] == ["--file-format=WAVE", "--data-format=LEI16@24000", "--channels=1"]
    assert "-o 不应成为选项" not in proc.args
    assert proc.kwargs["stdin"] is subprocess.PIPE
    path = Path(proc.args[-1])
    assert not path.exists() and not path.parent.exists()
    proc.wait.assert_called_once_with(timeout=1)
    proc.kill.assert_not_called()
    assert proc.stdin.closed and main._TTS["proc"] is None


def test_wav_uses_actual_rate_and_reference(cfg, tmp_path, device):
    module, _ = device
    path, references = tmp_path / "actual.wav", []
    _wav(path, AUDIO_A * 81, rate=16000)
    assert tts.play_wav(path, cfg, reference=lambda *args: references.append(args))
    module.RawOutputStream.assert_called_once_with(samplerate=16000, channels=1, dtype="int16")
    assert [len(block) for block, _, _ in references] == [320, 4]
    assert all(rate == 16000 for _, rate, _ in references)
    assert b"".join(block for block, _, _ in references) == AUDIO_A * 81


@pytest.mark.parametrize("channels,width", [(2, 2), (1, 1), (1, 3)])
def test_wav_rejects_non_mono16_before_device(cfg, tmp_path, device, channels, width):
    module, _ = device
    path = tmp_path / "bad.wav"
    _wav(path, b"\x00" * 12, channels=channels, width=width)
    with pytest.raises(ValueError, match="单声道 16bit"):
        tts.play_wav(path, cfg)
    module.RawOutputStream.assert_not_called()


def test_wav_rejects_truncated_pcm(cfg, tmp_path, device):
    _, stream = device
    path = tmp_path / "truncated.wav"
    _wav(path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="截断"):
        tts.play_wav(path, cfg)
    stream.write.assert_not_called()
    stream.abort.assert_called_once()


def test_local_cancelled_before_start_creates_no_process(cfg, session, fake_say):
    main, owner = session
    main.stop_speaking()
    assert not main._say_locally("你好", cfg, owner)
    assert fake_say == []


@pytest.mark.parametrize("failure", ["timeout", "cancel", "exit", "exception"])
def test_local_failure_reaps_process_removes_wav_without_playback(
        cfg, session, fake_say, device, monkeypatch, failure):
    main, owner = session
    module, _ = device
    spawn = main.subprocess.Popen

    def broken(args, **kwargs):
        proc = spawn(args, **kwargs)
        _wav(Path(args[-1]))

        def communicate(**kwargs):
            if failure == "cancel":
                main.stop_speaking()
                raise subprocess.TimeoutExpired(args, 0.05)
            if failure == "exit":
                proc.returncode = 1
                return None, None
            if failure == "exception":
                raise OSError("say 失败")
            raise AssertionError("超时前不应调用 communicate")

        proc.communicate.side_effect = communicate
        return proc

    monkeypatch.setattr(main.subprocess, "Popen", broken)
    if failure == "timeout":
        cfg = cfg.with_overrides(**{"tts.local_timeout_s": 0})
    if failure in {"timeout", "exception"}:
        with pytest.raises((subprocess.TimeoutExpired, OSError)):
            main._say_locally("你好", cfg, owner)
    else:
        assert not main._say_locally("你好", cfg, owner)
    proc = fake_say[0]
    assert not Path(proc.args[-1]).parent.exists()
    proc.wait.assert_called_once_with(timeout=1)
    assert proc.stdin.closed and main._TTS["proc"] is None
    assert proc.kill.call_count == (0 if failure == "exit" else 1)
    module.RawOutputStream.assert_not_called()


def test_local_say_rate_is_configurable(cfg, session, fake_say, device):
    main, owner = session
    assert main._say_locally("你好", cfg.with_overrides(**{"tts.sample_rate": 16000}), owner)
    assert "--data-format=LEI16@16000" in fake_say[0].args


def test_missing_sounddevice_falls_back_to_stdin_say_without_reference(
        cfg, session, fake_say, monkeypatch):
    main, owner = session
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert main._say_locally("-f 不是文件", cfg, owner)
    assert len(fake_say) == 2
    assert "-o" in fake_say[0].args
    assert fake_say[1].args == ["say", "-r", "220"]
    assert fake_say[1].text == "-f 不是文件".encode()
    assert "无 PCM 参考信号" in main.core.note_tts_failure.call_args.args[0]
    assert all(proc.stdin.closed for proc in fake_say)


def test_local_import_error_after_write_never_replays(cfg, session, fake_say, device):
    main, owner = session
    _, stream = device
    stream.write.side_effect = ImportError("写入可能已有声音")
    with pytest.raises(tts.PlaybackStartedError):
        main._say_locally("你好", cfg, owner)
    assert len(fake_say) == 1
    assert not Path(fake_say[0].args[-1]).parent.exists()
    stream.abort.assert_called_once()


def test_cloud_error_after_cancel_never_falls_back(cfg, session, monkeypatch):
    main, owner = session
    local = Mock()

    def fail(*args):
        main.stop_speaking()
        raise OSError("取消后云端才报错")

    monkeypatch.setattr(tts, "speak", fail)
    monkeypatch.setattr(main, "_say_locally", local)
    cloud_cfg = cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": "test"})
    assert not main._say_one("你好", cloud_cfg, owner)
    local.assert_not_called()


@pytest.mark.parametrize("partial", [False, True])
def test_cloud_failure_falls_back_only_before_playback(
        cfg, session, device, monkeypatch, partial):
    main, owner = session
    _, stream = device
    local = Mock(return_value=True)
    monkeypatch.setattr(main, "_say_locally", local)
    payload = _chunk(AUDIO_A * 120).encode() if partial else b'{"code":42}'
    _response(monkeypatch, [payload, b""])
    cloud_cfg = cfg.with_overrides(**{"tts.provider": "doubao", "tts.api_key": "test"})
    assert main._say_one("你好", cloud_cfg, owner) is not partial
    assert local.call_count == (0 if partial else 1)
    assert stream.write.call_count == (1 if partial else 0)
    stream.abort.assert_called_once()


def test_old_speak_finally_cannot_clear_or_kill_new_owner(cfg, session, monkeypatch):
    from voxos import dsp
    main, _ = session
    ready, release, errors = threading.Event(), threading.Event(), []
    echo = SimpleNamespace(tts_start=Mock(), tts_end=Mock())
    new_owner, new_proc = object(), Mock()

    def say(*args):
        ready.set()
        assert release.wait(3), "测试未释放旧 owner"
        return False

    def run():
        try:
            main._speak("旧句。", SimpleNamespace(echo=echo), cfg)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(main.shutil, "which", lambda _: "/fake/say")
    monkeypatch.setattr(dsp, "sentences", lambda _: ["旧句。"])
    monkeypatch.setattr(main, "_say_one", say)
    note = Mock()
    monkeypatch.setattr(main.core, "note_spoken", note)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(3)
        with main._TTS_LOCK:
            main._TTS.update(owner=new_owner, proc=new_proc, stopped=False)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert main._TTS == {"owner": new_owner, "proc": new_proc, "stopped": False}
    new_proc.kill.assert_not_called()
    echo.tts_end.assert_not_called()
    note.assert_not_called()


def test_old_say_cleanup_only_reaps_its_local_process(cfg, session, fake_say, monkeypatch, tmp_path):
    main, owner = session
    path, new_owner, new_proc = tmp_path / "old.wav", object(), Mock()
    spawn = main.subprocess.Popen

    def replaced(args, **kwargs):
        proc = spawn(args, **kwargs)

        def communicate(**kwargs):
            with main._TTS_LOCK:
                main._TTS.update(owner=new_owner, proc=new_proc, stopped=False)
            raise subprocess.TimeoutExpired(args, 0.05)

        proc.communicate.side_effect = communicate
        return proc

    monkeypatch.setattr(main.subprocess, "Popen", replaced)
    assert not main._run_say("旧句", cfg, owner, path)
    fake_say[0].kill.assert_called_once()
    fake_say[0].wait.assert_called_once_with(timeout=1)
    new_proc.kill.assert_not_called()
    assert main._TTS["owner"] is new_owner and main._TTS["proc"] is new_proc


def test_failed_sentence_stops_the_session_and_clears_only_its_state(cfg, session, monkeypatch):
    from voxos import dsp
    main, _ = session
    echo = SimpleNamespace(tts_start=Mock(), tts_end=Mock())
    say, note = Mock(return_value=False), Mock()
    monkeypatch.setattr(main.shutil, "which", lambda _: "/fake/say")
    monkeypatch.setattr(main, "_say_one", say)
    monkeypatch.setattr(main.core, "note_spoken", note)
    monkeypatch.setattr(dsp, "sentences", lambda _: ["第一句。", "第二句。"])
    main._speak("全文", SimpleNamespace(echo=echo), cfg)
    say.assert_called_once()
    echo.tts_start.assert_called_once()
    echo.tts_end.assert_called_once()
    note.assert_called_once_with("全文", "")
    assert main._TTS == {"owner": None, "proc": None, "stopped": False}


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_device_lifecycle_failure_aborts_and_closes_source(cfg, device, operation):
    _, stream = device
    source = Mock(__next__=Mock(side_effect=[AUDIO_A, StopIteration]))
    getattr(stream, operation).side_effect = RuntimeError("设备失败")
    with pytest.raises(RuntimeError, match="设备失败"):
        tts.play(source, cfg)
    source.close.assert_called_once()
    stream.abort.assert_called_once()
    stream.close.assert_called_once()


def test_source_close_failure_aborts_instead_of_draining(cfg, device):
    _, stream = device
    source = Mock(__next__=Mock(side_effect=[AUDIO_A, StopIteration]))
    source.close.side_effect = RuntimeError("关闭源失败")
    with pytest.raises(tts.PlaybackStartedError, match="关闭源失败"):
        tts.play(source, cfg)
    stream.write.assert_called_once_with(AUDIO_A)
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    stream.close.assert_called_once()


def test_local_missing_dependency_after_cancel_does_not_start_direct_say(
        cfg, session, fake_say, monkeypatch):
    main, owner = session

    def unavailable(*args):
        main.stop_speaking()
        raise ImportError("取消时缺少设备依赖")

    monkeypatch.setattr(tts, "play_wav", unavailable)
    assert not main._say_locally("你好", cfg, owner)
    assert len(fake_say) == 1


def test_empty_wav_does_not_succeed(cfg, tmp_path, device):
    _, stream = device
    path = tmp_path / "empty.wav"
    _wav(path, b"")
    assert not tts.play_wav(path, cfg)
    stream.write.assert_not_called()
    stream.abort.assert_called_once()


def test_say_communicate_retry_does_not_resend_text(cfg, session):
    main, owner = session
    proc = Mock(args=["say"], returncode=0)
    proc.communicate.side_effect = [subprocess.TimeoutExpired(["say"], 0.05), (None, None)]
    assert main._wait_say(proc, "-f 只是文本", owner, 15)
    assert [call.kwargs["input"] for call in proc.communicate.call_args_list] == [
        "-f 只是文本".encode(), None]
