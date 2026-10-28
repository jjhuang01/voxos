"""云端语音合成（豆包语音合成 2.0）：一次输入、流式出音频。

这一层只做「文本 → PCM 字节流 → 扬声器」，**不碰分段、不碰上下文**：
分段在 `dsp.sentences`，播报编排在 `main._speak`。分开的理由很实际——
这样这一层能拿一个假 HTTP 服务器完整测出来，不需要麦克风、也不需要真 Key。

接口形状全部来自官方文档《HTTP Chunked/SSE 单向流式-V3》，不是猜的：

    POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
    Header  X-Api-Key（新版控制台）  或  X-Api-App-Id + X-Api-Access-Key（旧版）
            X-Api-Resource-Id: seed-tts-2.0
    Body    {"user": {"uid": ...},
             "req_params": {"text": ..., "speaker": ...,
                            "audio_params": {"format": "pcm", "sample_rate": 24000}}}
    响应    一串独立 JSON 对象：音频块 {"code":0,"data":"<base64>"}，
            结束 {"code":20000000,"message":"ok"}，出错是别的 code

**为什么用「单向流式 HTTP」而不是「双向流式 WebSocket」**：双向那条是为
「边想边说」设计的（LLM 还在生成就往里灌字），我们现在的链路是
「一整句已经拿到手 → 送合成」，单向就够，而且少一个长连接要维护。
等真要做「边生成边播」，再换 WebSocket 那条。

没有配 Key 时 `available()` 返回 False，一切照旧走系统 `say`——
**没买接口的机器行为一个字都不变**。
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.request
import uuid
from typing import Callable, Iterator

from .config import Config

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DONE_CODE = 20000000          # 合成结束成功；0 是「这一块是音频」
_DECODER = json.JSONDecoder()


def available(cfg: Config) -> bool:
    """能不能用云端合成。少一样就当没配——**别半配着去发请求**。"""
    if str(cfg.get("tts.provider", "say")) != "doubao":
        return False
    has_key = bool(str(cfg.get("tts.api_key", "")).strip())
    has_legacy = bool(str(cfg.get("tts.app_id", "")).strip()
                      and str(cfg.get("tts.access_key", "")).strip())
    return has_key or has_legacy


def headers(cfg: Config) -> dict[str, str]:
    """鉴权头。新旧两版控制台并存，两套都给火山认。"""
    head = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": str(cfg.get("tts.resource_id", "seed-tts-2.0")),
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    key = str(cfg.get("tts.api_key", "")).strip()
    if key:
        head["X-Api-Key"] = key
    else:
        head["X-Api-App-Id"] = str(cfg.get("tts.app_id", "")).strip()
        head["X-Api-Access-Key"] = str(cfg.get("tts.access_key", "")).strip()
    return head


def body(text: str, cfg: Config) -> dict:
    """请求体。`pcm` 而不是默认的 `mp3`：流式播放要的就是裸采样，不想再解一层码。"""
    return {
        "user": {"uid": str(cfg.get("tts.uid", "voxos"))},
        "req_params": {
            "text": text,
            "speaker": str(cfg.get("tts.speaker", "zh_female_shuangkuaisisi_moon_bigtts")),
            "audio_params": {
                "format": "pcm",
                "sample_rate": int(cfg.get("tts.sample_rate", 24000)),
            },
        },
    }


def _objects(buffer: str) -> tuple[list[dict], str]:
    """从流里切出完整的 JSON 对象，剩下的半个留到下一块。

    响应是**一串首尾相接的 JSON**，不保证按行、也不保证按块对齐——按行切会在
    某一块刚好断在对象中间时炸掉，而那种情况只在网络抖动时出现，测不出来。
    """
    out: list[dict] = []
    while True:
        buffer = buffer.lstrip()
        if not buffer:
            return out, ""
        try:
            obj, end = _DECODER.raw_decode(buffer)
        except ValueError:
            return out, buffer       # 半个对象，等下一块
        if isinstance(obj, dict):
            out.append(obj)
        buffer = buffer[end:]


def synthesize(text: str, cfg: Config, should_stop: Callable[[], bool] | None = None
               ) -> Iterator[bytes]:
    """文本 → PCM 字节流（16 bit 小端单声道）。

    一块一块 yield，调用方边收边播——**首字节到了就能出声**，不用等整句合成完。
    `timeout` 同时管连接和读：首字节迟迟不来会抛 `socket.timeout`，
    上层据此退回系统 `say`，用户不会干等。
    """
    if not text.strip():
        return
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body(text, cfg)).encode("utf-8"),
        headers=headers(cfg), method="POST")
    timeout = float(cfg.get("tts.timeout_s", 8))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buffer = ""
        while True:
            if should_stop is not None and should_stop():
                return
            chunk = response.read(4096)
            if not chunk:
                return
            buffer += chunk.decode("utf-8", "ignore")
            objects, buffer = _objects(buffer)
            for obj in objects:
                yield from _audio(obj)


def _audio(obj: dict) -> Iterator[bytes]:
    """一个响应对象 → 音频字节（可能一个字节都没有）。"""
    code = int(obj.get("code", 0) or 0)
    if code == DONE_CODE:
        return
    if code != 0:
        raise RuntimeError(f"语音合成失败（{code}）：{obj.get('message') or '未知'}")
    data = obj.get("data")
    if data:
        yield base64.b64decode(data)


def play(chunks: Iterator[bytes], cfg: Config,
         should_stop: Callable[[], bool] | None = None) -> bool:
    """把 PCM 流放出来。返回是否**放完**（被打断就是 False）。

    用 `RawOutputStream` 而不是 `OutputStream`：数据本来就是 int16 PCM，
    再让 sounddevice 转一次是白花 CPU，还会在流边界上引入一个缓冲延迟。
    """
    import sounddevice as sd           # 只有真要出声时才导入

    rate = int(cfg.get("tts.sample_rate", 24000))
    with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as stream:
        for piece in chunks:
            if should_stop is not None and should_stop():
                return False
            stream.write(piece)
    return True


def speak(text: str, cfg: Config, should_stop: Callable[[], bool] | None = None) -> bool:
    """一句到底：合成 + 播放。失败会抛异常，由调用方决定退回哪条路。"""
    return play(synthesize(text, cfg, should_stop), cfg, should_stop)


def probe(cfg: Config) -> tuple[bool, str]:
    """`voxos doctor` 用：一句话说清云端合成到底能不能用。"""
    if str(cfg.get("tts.provider", "say")) != "doubao":
        return False, "系统 say（未启用云端合成）"
    if not available(cfg):
        return False, "云端合成缺 Key（provider=doubao 但没配 api_key / app_id+access_key）"
    return True, f"豆包 {cfg.get('tts.resource_id', 'seed-tts-2.0')} / {cfg.get('tts.speaker', '')}"


def stop_event() -> threading.Event:
    """给不方便传闭调用的地方用（测试与脚本）。"""
    return threading.Event()
