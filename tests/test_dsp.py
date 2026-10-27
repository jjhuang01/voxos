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
