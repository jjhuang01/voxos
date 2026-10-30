"""DSP 纯函数测试：AGC、高架滤波、VAD、端点、回声门、声纹锁。"""

from __future__ import annotations

import numpy as np
import pytest

from voxos import dsp


def band_energy(signal: np.ndarray, low: float, high: float, sr: int = dsp.SR) -> float:
    spectrum = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(signal.size, 1 / sr)
    return float(spectrum[(freqs >= low) & (freqs < high)].sum())


def test_agc_lifts_whisper_without_blowing_up():
    rng = np.random.default_rng(0)
    whisper = (rng.normal(0, 0.004, dsp.SR) * np.hanning(dsp.SR)).astype(np.float32)
    boosted = dsp.agc(whisper)
    assert dsp.dbfs(boosted) > dsp.dbfs(whisper) + 10
    assert np.max(np.abs(boosted)) <= 1.0


def test_agc_leaves_noise_floor_alone():
    noise = (np.random.default_rng(1).normal(0, 1e-4, dsp.SR)).astype(np.float32)
    assert np.allclose(dsp.agc(noise), noise, atol=1e-6)


def test_high_shelf_boosts_presence_band():
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 0.1, dsp.SR).astype(np.float32)
    boosted = dsp.high_shelf(noise, gain_db=6.0)
    ratio_before = band_energy(noise, 3000, 7000) / band_energy(noise, 100, 3000)
    ratio_after = band_energy(boosted, 3000, 7000) / band_energy(boosted, 100, 3000)
    assert ratio_after > ratio_before * 2


def test_energy_vad_separates_speech_from_silence(make_voice, silence):
    vad = dsp.EnergyVad()
    assert vad.is_speech(make_voice(118, seconds=0.5)) is True
    for _ in range(20):
        vad.is_speech(silence(32))
    assert vad.is_speech(silence(32)) is False


def test_endpoint_waits_for_unfinished_sentence():
    detector = dsp.EndpointDetector()
    for _ in range(30):
        detector.push(True, 512)
    state = "speech"
    for _ in range(30):                      # ~960ms 静音 > 800ms 基础窗
        state = detector.push(False, 512)
    assert state == "candidate"
    assert detector.should_end("我要写代码") is False      # 半截话：延长等待
    assert detector.deadline_ms == detector.extended_ms
    for _ in range(70):                      # 继续静音超过 2.5s
        state = detector.push(False, 512)
    assert detector.should_end("我要写代码") is True


def test_endpoint_ends_complete_sentence():
    detector = dsp.EndpointDetector()
    for _ in range(30):
        detector.push(True, 512)
    for _ in range(30):
        detector.push(False, 512)
    assert detector.should_end("把登录超时改成五秒") is True


def test_endpoint_ignores_too_short_speech():
    detector = dsp.EndpointDetector(min_speech_ms=200)
    detector.push(True, 512)                 # 32ms 人声
    for _ in range(30):
        detector.push(False, 512)
    assert detector.should_end("嗯") is False


def test_endpoint_force_end_at_hard_limit():
    detector = dsp.EndpointDetector(max_ms=1000)
    state = "speech"
    for _ in range(40):
        state = detector.push(True, 512)
    assert state == "force_end"


def test_echo_gate_blocks_during_tts_but_allows_shouting():
    gate = dsp.EchoGate(decay_ms=300, gate_dbfs=-25)
    gate.tts_start()
    assert gate.allow_chunk(-40) is False        # 自己的回声/呼吸
    assert gate.allow_chunk(-10) is True         # 用户喊“停”
    assert gate.allow_text("已为您打开文件") is False
    assert gate.allow_text("停一下") is True
    assert gate.is_interrupt("算了") is True


def test_bare_interrupt_is_a_whole_sentence_match():
    """整句匹配，不是「句子里含这几个字」。

    「我要把服务器停下来」里也有「停」，那是一条指令；按子串匹配会把它当成
    「闭嘴」直接吞掉，用户下的命令就没了。标点先去掉，所以「算了。」也算。
    """
    for text in ("停", "停一下", "算了", "取消", "别说了", "不用了", "算了。", " 停！"):
        assert dsp.bare_interrupt(text), text

    for text in ("我要把服务器停下来", "取消明天的会", "别说了半天我还没说完重点", ""):
        assert not dsp.bare_interrupt(text), text


def test_echo_gate_releases_after_decay():
    gate = dsp.EchoGate(decay_ms=300)
    gate.tts_start()
    gate.tts_end()
    assert gate.muted is True
    gate.tick(320)
    assert gate.muted is False
    assert gate.allow_chunk(-40) is True


def test_speaker_lock_mechanics(tmp_path, make_voice, stub_embedder):
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    assert lock.verify(make_voice())[0] is False          # 未录入 = fail closed
    consistency = lock.enroll([make_voice(118, seed=1), make_voice(118, seed=2)])
    assert consistency > 0.99
    ok, score = lock.verify(make_voice(118, seed=3))
    assert ok and score > 0.99
    ok, score = lock.verify(make_voice(206, seed=4))
    assert not ok and score < 0.84
    path = tmp_path / "vp.npz"
    lock.save(path)
    reloaded = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder).load(path)
    assert reloaded.verify(make_voice(118, seed=5))[0] is True


def test_dual_mode_profiles_take_best_match(tmp_path, make_voice, stub_embedder):
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, seed=1)], mode="normal")
    lock.enroll([make_voice(240, seed=2)], mode="mask")
    assert set(lock.profiles) == {"normal", "mask"}
    assert lock.verify(make_voice(240, seed=3))[0] is True


def test_spectral_features_differ_by_pitch(make_voice):
    low = dsp.spectral_features(make_voice(118, seconds=2.0))
    high = dsp.spectral_features(make_voice(220, seconds=2.0))
    assert low.shape == high.shape
    assert dsp.cosine(low, high) < 0.99


@pytest.mark.parametrize("size", [0, 10, 100])
def test_spectral_features_handle_short_audio(size):
    vector = dsp.spectral_features(np.zeros(size, dtype=np.float32))
    assert vector.size == 42
    assert not np.any(np.isnan(vector))


def test_cosine_edge_cases():
    assert dsp.cosine(np.zeros(4), np.zeros(4)) == 0.0
    assert dsp.cosine(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(1.0)
    assert dsp.cosine(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(0.0)


def test_silence_never_produces_an_utterance():
    """纯静音跑 60 秒不能产出任何一段「语音」——否则没人说话时它每 12 秒醒一次。"""
    ep = dsp.EndpointDetector()
    states = [ep.push(False, 512) for _ in range(1875)]
    assert set(states) == {"idle"}


def test_long_speech_still_hits_the_hard_limit():
    ep = dsp.EndpointDetector(max_ms=1000.0)
    states = [ep.push(True, 512) for _ in range(60)]
    assert "force_end" in states


# ---------------------------------------------------------------- 播报切分

def test_sentences_split_on_punctuation_not_mid_sentence():
    """以前是 `say text[:60]`：第 60 个字落在一句话中间，用户听到半截。

    按句切之后每一段都是完整的一句话，`say` 念完一句再念下一句。
    """
    assert dsp.sentences("好的，已经打开了微信。接下来需要我做什么？") == [
        "好的，", "已经打开了微信。", "接下来需要我做什么？"]


def test_sentences_handle_text_without_punctuation():
    """模型经常回一句没有句号的短话——不能因为没标点就整段丢掉。"""
    assert dsp.sentences("没有任何标点的一句话") == ["没有任何标点的一句话"]
    assert dsp.sentences("") == []
    assert dsp.sentences("   ") == []


def test_sentences_respect_the_speak_budget():
    """念一段两千字的答案没人受得了；超了要停在**句子边界**上，不是半个词上。"""
    chunks = dsp.sentences("这是一句话。" * 100, limit=40)
    # 软预算：不切句子，所以最多超出一句（这里一句 6 个字）
    assert 40 <= sum(len(c) for c in chunks) <= 46
    assert all(c.endswith("。") for c in chunks)


def test_sentences_break_long_sentences_on_commas():
    """一口气念不完的长句要在逗号处断开，而不是硬切在字数上限上。"""
    chunks = dsp.sentences("前面还有很长一段铺垫，" + "然后才是重点" * 8, max_chars=20)
    assert chunks[0].endswith("，")
    assert all(len(c) <= 20 for c in chunks)


def _voice_by_first_sample(audio):
    """按第一个采样值决定音色方向：<0.5 是本人，>=0.5 是别人。

    这样能把「一句话里混了两个人的声音」构造得完全可控，不依赖真实音频。
    """
    vector = np.zeros(8, dtype=np.float32)
    vector[0 if float(audio[0]) < 0.5 else 1] = 1.0
    return vector


def _owner_profile():
    vector = np.zeros(8, dtype=np.float32)
    vector[0] = 1.0
    return {"normal": vector}


def _mixed(owner_secs: float, other_secs: float, total: float = 4.0):
    """一段「本人说前半、别人说后半」的音频。"""
    own = np.full(int(dsp.SR * owner_secs), 0.1, dtype=np.float32)
    other = np.full(int(dsp.SR * other_secs), 0.9, dtype=np.float32)
    return np.concatenate([own, other])


def test_segmented_verification_tolerates_one_other_speaker():
    """旁边的人插一句，不该让整句话被拒——那正是「屋里多个人」最常见的形态。

    整段一个嵌入会把两个人的音色平均成一个谁也不像的东西，本人被拒、指令丢掉。
    分段看占比：多数窗口像本人就通过。
    """
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_voice_by_first_sample,
                           profiles=_owner_profile())

    ok, best, ratio = lock.verify_segments(_mixed(owner_secs=3.0, other_secs=1.0))

    assert ok is True, "本人占多数时应当通过"
    assert ratio >= 0.5


def test_segmented_verification_rejects_when_others_dominate():
    """别人说大半 → 拒。否则就是「屋里有人说话，Agent 却照做」。"""
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_voice_by_first_sample,
                           profiles=_owner_profile())

    ok, best, ratio = lock.verify_segments(_mixed(owner_secs=1.0, other_secs=3.0))

    assert ok is False
    assert ratio < 0.5, "占比要能报出来，时间线才能说清是「别人在说」"


def test_segmented_verification_boundary_follows_min_ratio():
    """判决边界落在 `min_ratio` 上，不是一个魔法分数。

    实测（1.5s 窗口、4s 音频、阈值 0.75）：别人占 0/25/38/50% → 通过
    （本人窗口占 67%~100%），62/75/100% → 拒绝（33%~0%）。同一条 4s 音频，
    把 `min_ratio` 从 0.5 提到 0.8 就该翻成拒绝——这证明卡的是
    「多数窗口像本人」这条可解释的规则，而不是某个调出来的数字。
    """
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_voice_by_first_sample,
                           profiles=_owner_profile())
    clip = _mixed(owner_secs=2.5, other_secs=1.5)      # 本人 2/3 的窗口

    ok, _, ratio = lock.verify_segments(clip, min_ratio=0.5)
    assert ok is True
    assert ratio == 0.667, "占比要如实报出来，时间线才能说清是谁在说"

    assert lock.verify_segments(clip, min_ratio=0.8)[0] is False


def test_segmented_verification_falls_back_for_short_audio():
    """短句切不出两个窗口时退回整段验证，行为和 verify 一致。"""
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_voice_by_first_sample,
                           profiles=_owner_profile())

    ok, best, ratio = lock.verify_segments(np.full(int(dsp.SR * 0.8), 0.1, dtype=np.float32))

    assert ok is True
    assert ratio == 1.0


def _simulate_aec_signals():
    sr = dsp.SR
    duration = 2.0
    n = int(sr * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    np.random.seed(42)
    tts = (0.4 * np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 440 * t)
           + 0.1 * np.random.randn(n)).astype(np.float32)
    delay = 800
    rir = np.zeros(64, dtype=np.float32)
    rir[0], rir[16], rir[40] = 0.6, 0.2, 0.1
    echo = np.zeros_like(tts)
    filtered = np.convolve(tts, rir)[:n - delay]
    echo[delay:delay + len(filtered)] = filtered
    voice = np.zeros(n, dtype=np.float32)
    v_start, v_end = int(0.8 * sr), int(1.8 * sr)
    vt = t[v_start:v_end]
    voice[v_start:v_end] = (0.35 * np.sin(2 * np.pi * 180 * vt) + 0.2 * np.sin(2 * np.pi * 360 * vt))
    return tts, echo, voice, delay, v_start, v_end


def _run_blocks(filter_inst: dsp.AECFilter, mic: np.ndarray, tts: np.ndarray, delay: int) -> np.ndarray:
    block_size = 320
    cleaned = []
    for i in range(0, len(mic), block_size):
        ref_idx = i - delay
        ref_b = tts[ref_idx:ref_idx + block_size] if ref_idx >= 0 else np.zeros(block_size, dtype=np.float32)
        if len(ref_b) < block_size:
            ref_b = np.pad(ref_b, (0, block_size - len(ref_b)))
        cleaned.append(filter_inst.filter_block(mic[i:i + block_size], ref_b))
    return np.concatenate(cleaned)


def test_aec_nlms_cancels_echo_and_restores_user_voice():
    """合成信号验证 AEC：只有 TTS 时残余能量显著下降，双讲时与本人语音相关性显著上升。"""
    tts, echo, voice, delay, v_start, v_end = _simulate_aec_signals()
    mic_double, mic_single = echo + voice, echo.copy()

    # 延迟估计验证
    est_delay = dsp.estimate_delay(echo[delay:delay + 480], tts[:delay + 480])
    assert abs(est_delay - delay) <= 1

    # 单讲：残余能量显著下降
    clean_s = _run_blocks(dsp.AECFilter(taps=64), mic_single, tts, delay)
    p_orig = float(np.mean(mic_single[v_start:v_end] ** 2))
    p_clean = float(np.mean(clean_s[v_start:v_end] ** 2))
    erle = 10.0 * np.log10(p_orig / max(p_clean, 1e-12))
    assert erle > 30.0

    # 双讲：与本人语音相关性显著高于消除前
    clean_d = _run_blocks(dsp.AECFilter(taps=64), mic_double, tts, delay)
    c_before = float(np.corrcoef(mic_double[v_start:v_end], voice[v_start:v_end])[0, 1])
    c_after = float(np.corrcoef(clean_d[v_start:v_end], voice[v_start:v_end])[0, 1])
    assert c_before < 0.82
    assert c_after > 0.85
    assert c_after > c_before + 0.04

