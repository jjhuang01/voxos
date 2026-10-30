"""云端语音合成（豆包 2.0）与云端/本地共用的 PCM 播放。

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

没有配 Key 时 `available()` 返回 False，由 main 用系统 `say` 合成临时 WAV，
交给公共 PCM 播放；只有缺少 sounddevice 时才退回无参考信号的 say 直播。
"""

from __future__ import annotations

import base64
import codecs
import json
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path
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
            "speaker": str(cfg.get("tts.speaker", "zh_female_vv_uranus_bigtts")),
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
        if not isinstance(obj, dict):
            raise RuntimeError("语音合成响应必须为 JSON 对象")
        out.append(obj)
        buffer = buffer[end:]


def _stopped(should_stop: Callable[[], bool] | None) -> bool:
    return should_stop is not None and should_stop()


def _response_objects(response: object, should_stop: Callable[[], bool] | None
                      ) -> Iterator[dict]:
    """增量读取相接的 JSON；UTF-8 字符和对象都允许跨网络块。"""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    while not _stopped(should_stop):
        chunk = response.read1(4096)
        if _stopped(should_stop):
            return
        buffer += decoder.decode(chunk, final=not chunk)
        objects, buffer = _objects(buffer)
        yield from objects
        if not chunk:
            if buffer.strip():
                raise RuntimeError("语音合成响应截断或格式无效")
            return


def synthesize(text: str, cfg: Config, should_stop: Callable[[], bool] | None = None
               ) -> Iterator[bytes]:
    """文本 → 小端 int16 单声道 PCM；仅有非空音频和 DONE 才算合成完成。"""
    if not text.strip() or _stopped(should_stop):
        return
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body(text, cfg)).encode("utf-8"),
        headers=headers(cfg), method="POST")
    timeout = float(cfg.get("tts.timeout_s", 8))
    if _stopped(should_stop):
        return
    with urllib.request.urlopen(request, timeout=timeout) as response:
        size = 0
        for obj in _response_objects(response, should_stop):
            if _stopped(should_stop):
                return
            if int(obj.get("code", 0) or 0) == DONE_CODE:
                if not size or size % 2:
                    raise RuntimeError("语音合成音频为空或采样截断")
                return
            for audio in _audio(obj):
                size += len(audio)
                yield audio
        if not _stopped(should_stop):
            raise RuntimeError("语音合成未收到 DONE")


def _audio(obj: dict) -> Iterator[bytes]:
    """一个响应对象 → 音频字节（可能一个字节都没有）。"""
    code = int(obj.get("code", 0) or 0)
    if code == DONE_CODE:
        return
    if code != 0:
        if code == 55000000:
            msg = obj.get("message") or "未知"
            raise RuntimeError(f"语音合成失败（{code}）：音色与 resource_id 版本不匹配（{msg}）")
        raise RuntimeError(f"语音合成失败（{code}）：{obj.get('message') or '未知'}")
    data = obj.get("data")
    if data:
        yield base64.b64decode(data, validate=True)


class PlaybackStartedError(RuntimeError):
    """写入已开始，可能已有内容出声；调用方不得整句降级重播。"""


def _pcm_blocks(chunks: Iterator[bytes], rate: int,
                should_stop: Callable[[], bool] | None) -> Iterator[bytes]:
    """跨任意字节边界合并为约 10ms 块；尾块不补零，不提交半个采样。"""
    if rate <= 0:
        raise ValueError("播放采样率必须为正数")
    size = max(1, rate // 100) * 2
    pending = bytearray()
    while not _stopped(should_stop):
        piece = next(chunks, None)
        if _stopped(should_stop):
            return
        if piece is None:
            break
        pending.extend(piece)
        while len(pending) >= size:
            if _stopped(should_stop):
                return
            block = bytes(pending[:size])
            del pending[:size]
            yield block
    if _stopped(should_stop):
        return
    if len(pending) % 2:
        raise ValueError("PCM 尾部存在半个 int16 采样")
    if pending:
        yield bytes(pending)


def _close_chunks(chunks: Iterator[bytes]) -> None:
    close = getattr(chunks, "close", None)
    if close is not None:
        close()


def _close_playback(stream: object, chunks: Iterator[bytes], complete: bool,
                    should_stop: Callable[[], bool] | None) -> bool:
    finished = False
    try:
        _close_chunks(chunks)
        if complete and not _stopped(should_stop):
            stream.stop(ignore_errors=False)
            finished = not _stopped(should_stop)
        return finished
    finally:
        if stream is not None:
            try:
                if not finished:
                    stream.abort()
            finally:
                stream.close()


def play(chunks: Iterator[bytes], cfg: Config,
         should_stop: Callable[[], bool] | None = None,
         reference: Callable[[bytes, int, float], None] | None = None) -> bool:
    """播放 int16 单声道 PCM，空流或取消返回 False。

    可选 reference 仅报告 write 成功的约 10ms 块（末块可更短）、采样率和
    提交后的单调时间；不是 DAC 时间，不表示声学对齐，也不是 AEC。
    """
    stream, started, complete = None, False, False
    try:
        try:
            if _stopped(should_stop):
                return False
            import sounddevice as sd

            rate = int(cfg.get("tts.sample_rate", 24000))
            stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16")
            stream.start()
            for piece in _pcm_blocks(chunks, rate, should_stop):
                if _stopped(should_stop):
                    return False
                started = True  # write 抛错也可能已部分提交，禁止降级重播。
                stream.write(piece)
                if reference is not None:
                    reference(piece, rate, time.monotonic())
                if _stopped(should_stop):
                    return False
            complete = started and not _stopped(should_stop)
        finally:
            finished = _close_playback(stream, chunks, complete, should_stop)
        return finished
    except Exception as exc:
        if started:
            raise PlaybackStartedError(str(exc)) from exc
        raise


def _wav_chunks(source: wave.Wave_read) -> Iterator[bytes]:
    remaining = source.getnframes()
    while remaining:
        frames = min(remaining, 4096)
        chunk = source.readframes(frames)
        if len(chunk) != frames * 2:
            raise ValueError("本地 WAV 音频截断")
        remaining -= frames
        yield chunk


def play_wav(path: Path, cfg: Config,
             should_stop: Callable[[], bool] | None = None,
             reference: Callable[[bytes, int, float], None] | None = None) -> bool:
    """校验本地 WAV，以文件实际采样率走公共 PCM 播放路径。"""
    if _stopped(should_stop):
        return False
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels() != 1 or source.getsampwidth() != 2
                or source.getcomptype() != "NONE" or source.getframerate() <= 0):
            raise ValueError("本地 WAV 必须为单声道 16bit PCM")
        actual = cfg.with_overrides(**{"tts.sample_rate": source.getframerate()})
        return play(_wav_chunks(source), actual, should_stop, reference)


def speak(text: str, cfg: Config, should_stop: Callable[[], bool] | None = None,
          reference: Callable[[bytes, int, float], None] | None = None) -> bool:
    """一句到底：合成 + 播放。失败会抛异常，由调用方决定退回哪条路。"""
    return play(synthesize(text, cfg, should_stop), cfg, should_stop, reference=reference)


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
