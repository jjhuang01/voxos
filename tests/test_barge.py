"""播报期间被打断：本人持续说话就该停下来，而不是要求用户先学会一句咒语。"""

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


class _AlwaysOn:
    """声纹桩：永远认为是本人。"""

    profiles = {"normal": np.ones(4, dtype=np.float32)}

    def verify(self, chunk: np.ndarray) -> tuple[bool, float]:
        return True, 0.99


class _NeverMe:
    """声纹桩：永远认为不是本人（模拟我们自己的 TTS 回声）。"""

    profiles = {"normal": np.ones(4, dtype=np.float32)}

    def verify(self, chunk: np.ndarray) -> tuple[bool, float]:
        return False, 0.31


def _pipeline(cfg, lock=None, on_barge_in=None) -> audio.AudioPipeline:
    return audio.AudioPipeline(cfg=cfg, stt=_StopStt(), lock=lock or _AlwaysOn(),
                               vad=dsp.EnergyVad(), on_barge_in=on_barge_in)


def test_loud_audio_is_collected_only_while_muted(cfg):
    """只有「播报中 + 够响」才值得攒起来做插话判断。"""
    gate = dsp.EchoGate()
    assert gate.loud_enough(-12.0) is False      # 没在播报：正常收音，不用攒
    gate.tts_start()
    assert gate.loud_enough(-12.0) is True       # 播报中 + 够响：可能是用户插话
    assert gate.loud_enough(-60.0) is False      # 播报中 + 太轻：自己的尾音


def test_pipeline_fires_barge_in_when_the_owner_speaks_over_tts(cfg):
    """播报期间本人持续说话 → 触发打断回调。

    旧链路要求「放行 → 识别 → 文本含打断词」三步都对，任何一步出错就静默：
    用户说「等一下，我说的是另一个」，被静音门丢掉，感知是「我说话它不理我」。
    新链路是「能量够 + 持续 interrupt_ms + 声纹像本人」，对齐 LiveKit 的
    `min_interruption_duration`（默认 0.5s）——不看用户说了哪个词。
    """
    fired: list[bool] = []
    pipeline = _pipeline(cfg, on_barge_in=lambda: fired.append(True))
    pipeline.echo.tts_start()

    list(pipeline.run(audio.ArraySource(_loud())))

    assert fired, "播报期间本人持续说话应当触发打断"
    assert not pipeline.echo.speaking, "触发打断后回声门应当关闭"


def test_pipeline_ignores_its_own_echo(cfg):
    """能量够但声纹不像本人 → 那是我们自己的回声，继续播报。

    能量分不开两者（实测 TTS 回声 p95 约 -25.8 dBFS，和正常说话只差几 dB），
    所以最终判定必须落在音色上：TTS 是合成音，和本人模板差得远。
    """
    fired: list[bool] = []
    pipeline = _pipeline(cfg, lock=_NeverMe(), on_barge_in=lambda: fired.append(True))
    pipeline.echo.tts_start()

    list(pipeline.run(audio.ArraySource(_loud())))

    assert not fired, "自己的回声不该触发打断"
    assert pipeline.echo.speaking, "回声不该把播报关掉"


def test_short_burst_does_not_interrupt(cfg):
    """只有 200ms 的响声不打断——太短的多半是回声尖峰或关门声。"""
    fired: list[bool] = []
    pipeline = _pipeline(cfg, on_barge_in=lambda: fired.append(True))
    pipeline.echo.tts_start()

    list(pipeline.run(audio.ArraySource(_loud(ms=200))))

    assert not fired


def _fires(cfg, ms: float, lock=None, interrupt_ms: float | None = None) -> bool:
    """喂一段 ms 毫秒的响声，返回是否触发打断。"""
    gate = dsp.EchoGate() if interrupt_ms is None else dsp.EchoGate(interrupt_ms=interrupt_ms)
    fired: list[bool] = []
    pipeline = audio.AudioPipeline(cfg=cfg, stt=_StopStt(), lock=lock or _AlwaysOn(),
                                   vad=dsp.EnergyVad(), echo=gate,
                                   on_barge_in=lambda: fired.append(True))
    gate.tts_start()
    list(pipeline.run(audio.ArraySource(_loud(ms=ms))))
    return bool(fired)


def test_barge_in_waits_for_interrupt_ms_not_a_magic_number(cfg):
    """时长消融：判决点就在 `interrupt_ms` 上，一个 chunk 之内。

    实测（512 样本 = 32ms 一块，默认 500ms）：

        时长   本人    自己的回声
        100ms  False   False
        300ms  False   False
        500ms  True    False
        700ms  True    False
        900ms  True    False

    所以「喊一句特定的话才算打断」那套被换掉了：只要**持续**说够时长就打断。
    卡死在 500 而不是随便一个数，是因为它和 LiveKit 的
    `min_interruption_duration` 默认值一致——短于此的多半是回声尖峰或关门声。
    """
    for ms in (100, 300, 400):
        assert _fires(cfg, ms) is False, f"{ms}ms 不该打断"

    for ms in (512, 700, 900):
        assert _fires(cfg, ms) is True, f"{ms}ms 应当打断"


def test_the_threshold_really_comes_from_the_config(cfg):
    """把 `interrupt_ms` 调到 1200，900ms 就不该再打断——证明它不是在读魔法数。

    没有这条，谁把配置里的 500 改成 100 也不会有测试失败；而 100ms 会把
    「嗯」「啊」这种口头语全判成插话，播报被打断得七零八落。
    """
    assert _fires(cfg, 900) is True
    assert _fires(cfg, 900, interrupt_ms=1200.0) is False


def _barge_then_continue() -> np.ndarray:
    """先说 700ms（打断），停 300ms（混响期），再说 600ms（接着往下说）。"""
    gap = np.zeros(int(SR * 0.3), dtype=np.float32)
    return np.concatenate([_loud(ms=700), gap, _loud(ms=600)])


def _utterance_after_barge(cfg, carry_ttl_ms: float = 2000.0) -> float:
    """跑一遍「打断 → 接着说」，返回最后那句的时长（毫秒）。"""
    pipeline = audio.AudioPipeline(
        cfg=cfg, stt=_StopStt(), lock=_AlwaysOn(), vad=dsp.EnergyVad(),
        echo=dsp.EchoGate(), carry_ttl_ms=carry_ttl_ms)
    pipeline.echo.tts_start()
    spoken = list(pipeline.run(audio.ArraySource(_barge_then_continue())))
    assert spoken, "打断之后接着说，应该产出一句话"
    return spoken[-1].audio_ms


def test_the_interrupt_audio_becomes_the_start_of_the_next_sentence(cfg):
    """判定为插话的那 500ms 不能丢——它就是用户这句话的前半句。

    丢了的话转写出来是「…说的是另一个」，开头没了，用户得整句重说一遍；
    而他刚才明明已经说过了。所以攒下的音频要接回下一句的开头。

    同一段音频，只改 `carry_ttl_ms` 就是一次消融：留着比丢掉多出的那截，
    正是被打断的那段音频（实测 704ms = 判定的 512ms + 混响期继续攒的 192ms）。
    """
    kept = _utterance_after_barge(cfg)
    dropped = _utterance_after_barge(cfg, carry_ttl_ms=0.0)

    extra = kept - dropped
    assert extra >= 400, f"插话那截没接回去（只多出 {extra:.0f}ms）"
    assert 650 <= extra <= 780, f"接回去的量不对：{extra:.0f}ms，应当≈700ms"


def test_carry_expires_so_an_old_phrase_does_not_glue_onto_a_new_one(cfg):
    """只说了半句「等一下」就走开，那半句不该在十秒后粘到另一句话前面。

    没有这个期限，攒下的音频会一直等着——用户过一会儿随口说一句「打开微信」，
    转写出来就是「等一下打开微信」，两个不相干的意图粘在一起。
    """
    pipeline = _pipeline(cfg)
    pipeline._keep_carry(np.ones(512, dtype=np.float32))
    assert pipeline._take_carry(), "刚攒下的时候应该取得到"

    pipeline._keep_carry(np.ones(512, dtype=np.float32))
    pipeline._carry_at -= 3.0                      # 假装过了 3 秒（TTL 是 2 秒）
    assert pipeline._take_carry() == [], "过期的音频要丢掉，不能粘到新句子上"


def test_is_interrupt_matches_common_phrasings():
    gate = dsp.EchoGate()
    for text in ("停", "停一下", "别说了", "算了", "取消", "不用了"):
        assert gate.is_interrupt(text), text
    assert not gate.is_interrupt("我要写日志")


def test_stop_speaking_is_safe_when_idle():
    main._TTS.update(proc=None, owner=None, stopped=False)
    assert main.stop_speaking() is False


def test_stop_speaking_works_between_sentences():
    """句与句之间有几十毫秒的空档，那一刻 `proc` 已经退出。

    旧实现只看 `proc`，于是这段时间里的「停」会被判成「没在播报」→ 返回 False →
    这句「停」被当成一条普通指令派发出去，用户得到一句「还没学会这句」。
    所以判的必须是「有没有人在念」（`owner`），不是「有没有一个进程在跑」。
    """
    main._TTS.update(proc=None, owner=object(), stopped=False)
    try:
        assert main.stop_speaking() is True
        assert main._TTS["stopped"] is True
    finally:
        main._TTS.update(proc=None, owner=None, stopped=False)
