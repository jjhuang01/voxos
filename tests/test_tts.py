"""云端语音合成（豆包 2.0）：只用假 HTTP 服务器测。

三条原则：

1. **不发真请求**——没有 Key，也不该为了跑测试去买一个。
2. **不碰扬声器**——CI 上没有音频设备，`play()` 那层只能靠真人验证，
   这里测的是「我们发出去的请求」和「我们怎么理解回来的流」。
3. **按官方文档的形状断言**——字段名写错时，测试红得比线上便宜得多。
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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
