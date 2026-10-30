import numpy as np


def _pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()

from voxos import audio, dsp


def _stream(delay: int, gain: float = 0.5):
    rng = np.random.default_rng(1)
    ref = rng.standard_normal(dsp.SR * 3).astype(np.float32) * 0.3
    mic = np.zeros_like(ref)
    mic[delay:] = ref[:len(ref) - delay] * gain
    ec = audio.EchoCanceller()
    out = []
    for i in range(0, len(ref), 512):
        ec.feed_reference(_pcm(ref[i:i + 512]), dsp.SR)
        out.append(ec.process(mic[i:i + 512]))
    return ec, mic, np.concatenate(out)


def test_streaming_locks_true_delay_and_reduces_echo():
    for delay in (800, 2400, 4800):
        ec, mic, out = _stream(delay)
        assert ec.delay_locked and abs(ec.delay + 16 - delay) <= 32
        tail = slice(dsp.SR * 2, None)
        assert np.dot(out[tail], out[tail]) < np.dot(mic[tail], mic[tail]) * 0.5


def test_never_louder_than_input():
    _, mic, out = _stream(2400)
    for i in range(0, len(mic), 512):
        assert np.dot(out[i:i + 512], out[i:i + 512]) <= np.dot(mic[i:i + 512], mic[i:i + 512]) * 1.05 + 1e-6


def test_silence_reference_does_not_lock():
    ec = audio.EchoCanceller()
    rng = np.random.default_rng(2)
    for _ in range(60):
        ec.feed_reference(_pcm(rng.standard_normal(512) * 0.3), dsp.SR)
        ec.process(rng.standard_normal(512).astype(np.float32) * 0.3)
    assert not ec.delay_locked
