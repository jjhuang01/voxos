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

    这里使用「总是拒绝」的声纹替身，只验证拒绝路径，不测真实回声误触发率。
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


def test_separate_bursts_are_not_accumulated_into_one_interruption(cfg):
    """两段 256ms 中间隔 3 秒，不等于连续说了 512ms。"""
    fired = []
    pipeline = _pipeline(cfg, on_barge_in=lambda: fired.append(True))
    pipeline.echo.tts_start()
    samples = np.concatenate([_loud(256), np.zeros(SR * 3), _loud(256)])
    list(pipeline.run(audio.ArraySource(samples)))
    assert not fired


def test_overlap_and_its_continuation_never_reach_stt(cfg):
    """没有 AEC 时宁可要求重说，也不把混音或缺开头的后半句当成指令。"""
    events = []
    pipeline = _pipeline(cfg)
    pipeline.on_event = lambda *args: events.append(args)
    pipeline.echo.tts_start()
    samples = np.concatenate([_loud(1800), np.zeros(SR)])
    assert list(pipeline.run(audio.ArraySource(samples))) == []
    assert any("完整重说" in item[2] for item in events)


def test_full_restatement_after_silence_is_accepted_without_mixed_prefix(cfg):
    """打断后的安静间隔过去后，可接受新完整指令，旧混音不进入识别。"""
    from unittest.mock import Mock

    pipeline = _pipeline(cfg)
    pipeline.stt = Mock()
    pipeline.stt.transcribe.return_value = "现在几点了"
    # 终稿走 streamer.finish 时，换掉 stt 并不会换掉 streamer 里那份旧桩；
    # 这里直接把 streamer 摘掉，才能真正看到「送进识别的最终音频」。
    pipeline.streamer = None
    pipeline.echo.tts_start()
    overlap = np.full(16000, 0.6, dtype=np.float32)
    fresh = _loud(900, amplitude=0.1)
    samples = np.concatenate([overlap, np.zeros(SR * 2), fresh, np.zeros(SR)])
    out = list(pipeline.run(audio.ArraySource(samples)))
    assert len(out) == 1 and out[0].accepted
    final = [c.args[0] for c in pipeline.stt.transcribe.call_args_list if c.kwargs.get("final")]
    assert final and all(np.max(np.abs(x)) < 0.2 for x in final)


def test_restart_silence_uses_pre_agc_level_in_mask_mode(cfg):
    """面罩增益会抬高底噪，恢复收音必须看补偿前电平。"""
    pipeline = _pipeline(cfg.with_overrides(**{"audio.mask_mode": True}))
    pipeline._await_restart = True
    noise = np.ones(SR, dtype=np.float32) * 0.003
    list(pipeline.run(audio.ArraySource(noise)))
    assert not pipeline._await_restart


def test_restart_wait_gives_up_when_the_room_stays_loud(cfg):
    """底噪高于门限时安静间隔永远等不到；不能因此让麦克风永久失聪。"""
    events = []
    pipeline = _pipeline(cfg)
    pipeline.on_event = lambda *args: events.append(args)
    pipeline._await_restart = True
    loud_floor = np.full(SR * 4, 0.05, dtype=np.float32)     # 约 -26 dBFS，持续高于 -42
    list(pipeline.run(audio.ArraySource(loud_floor)))
    assert not pipeline._await_restart and pipeline._restart_wait_ms == 0
    assert any("恢复收音" in item[1] for item in events)


def test_reset_discards_interruption_candidates_and_restart_state(cfg):
    """设备重连不能继承前一段插话候选或重说等待。"""
    pipeline = _pipeline(cfg)
    pipeline._barge, pipeline._barge_ms = [_loud(256)], 256.0
    pipeline._await_restart, pipeline._restart_silence_ms = True, 200.0
    pipeline.reset()
    assert pipeline._barge == [] and pipeline._barge_ms == 0
    assert not pipeline._await_restart and pipeline._restart_silence_ms == 0


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
