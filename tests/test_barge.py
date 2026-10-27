"""打断词：播报期间喊「停」必须真的停下来，而不是被当成一条新指令。"""

from __future__ import annotations

import numpy as np

from voxos import audio, dsp, main

SR = dsp.SR


class _StopStt:
    """桩 STT：无论听到什么都返回「停」，用来验证打断链路。"""

    def transcribe(self, audio_chunk: np.ndarray, final: bool = False) -> str:
        return "停" if final else ""


def _loud(ms: float = 900, amplitude: float = 0.6) -> np.ndarray:
    t = np.arange(int(SR * ms / 1000)) / SR
    tone = 0.5 * np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 440 * t)
    return (tone * amplitude).astype(np.float32)


def _pipeline(cfg) -> audio.AudioPipeline:
    return audio.AudioPipeline(cfg=cfg, stt=_StopStt(), lock=_AlwaysOn(), vad=dsp.EnergyVad())


class _AlwaysOn:
    """声纹桩：永远认为是本人，把注意力集中在打断逻辑上。"""

    profiles = {"normal": np.ones(4, dtype=np.float32)}

    def verify(self, chunk: np.ndarray) -> tuple[bool, float]:
        return True, 0.99


def test_loud_speech_passes_echo_gate_while_speaking(cfg):
    """回声门不该把喊出来的「停」也一起丢掉。"""
    gate = dsp.EchoGate()
    gate.tts_start()
    assert gate.allow_chunk(-12.0) is True       # 用户喊得够响
    assert gate.allow_chunk(-60.0) is False      # Agent 自己的尾音被丢掉


def test_pipeline_yields_interrupt_utterance_during_tts(cfg):
    pipeline = _pipeline(cfg)
    pipeline.echo.tts_start()
    utterances = list(pipeline.run(audio.ArraySource(_loud())))
    assert utterances, "播报期间的高能量语音应当进入管道"
    assert pipeline.echo.is_interrupt(utterances[0].text)


def test_is_interrupt_matches_common_phrasings():
    gate = dsp.EchoGate()
    for text in ("停", "停一下", "别说了", "算了", "取消", "不用了"):
        assert gate.is_interrupt(text), text
    assert not gate.is_interrupt("我要写日志")


def test_stop_speaking_is_safe_when_idle():
    main._TTS["proc"] = None
    assert main.stop_speaking() is False
