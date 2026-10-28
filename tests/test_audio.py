"""音频管道测试：声纹门禁、语义尾词、回声门、自愈重连、WAV 源。"""

from __future__ import annotations

import itertools
import wave
from pathlib import Path

import numpy as np

from voxos import audio, dsp


def build_pipeline(cfg, bus, stub_embedder, transcripts, threshold=0.84, echo=None):
    lock = dsp.SpeakerLock(threshold=threshold, embedder=stub_embedder)
    lock.enroll([np.sin(2 * np.pi * 118 * np.arange(dsp.SR) / dsp.SR).astype(np.float32)])
    return audio.AudioPipeline(
        cfg=cfg, stt=audio.ScriptedStt(transcripts), lock=lock, on_event=bus,
        echo=echo or dsp.EchoGate(decay_ms=300),
    )


def test_pipeline_accepts_owner_and_rejects_stranger(cfg, bus, make_voice, stub_embedder, silence):
    pipeline = build_pipeline(cfg, bus, stub_embedder, ["我要写日志，测试声纹门禁"])
    stream = np.concatenate([make_voice(118, 1.2), silence(1500), make_voice(206, 1.2), silence(1500)])
    utterances = list(pipeline.run(audio.ArraySource(stream)))

    accepted = [u for u in utterances if u.accepted]
    rejected = [u for u in utterances if not u.accepted and u.reason]
    assert len(accepted) == 1
    assert accepted[0].text.startswith("我要写日志")
    assert len(rejected) == 1
    assert "声纹" in rejected[0].reason
    assert pipeline.stats["utterances"] == 1
    assert pipeline.stats["rejected"] == 1


def test_pipeline_merges_unfinished_sentence(cfg, bus, make_voice, stub_embedder, silence):
    pipeline = build_pipeline(cfg, bus, stub_embedder, ["我要写代码"])
    first = make_voice(118, 1.0)
    stream = np.concatenate([first, silence(900), make_voice(118, 1.0, seed=2), silence(1600)])
    utterances = [u for u in pipeline.run(audio.ArraySource(stream)) if u.accepted]
    assert len(utterances) == 1, "半截话不应被切成两条"
    assert utterances[0].audio_ms > 2600, "应当把停顿后的后半句并入同一句"


def test_force_end_flushes_even_when_transcript_is_empty(cfg, bus, make_voice, stub_embedder):
    """硬上限到了必须收尾，哪怕转写是空的。

    空转写曾被当成「还没说完」，于是缓冲不清；下一块音频又把整段重拼重解一次，
    一块比一块慢——实测 12 秒后吞吐掉到 1/8、CPU 一路涨到 95%。
    真机上的触发条件很普通：一声咳嗽、一次 VAD 假阳性。
    """
    short = cfg.with_overrides(**{"audio.max_utterance_ms": 1000,
                                  "audio.min_speech_ms": 100})
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, 1.0)])
    pipeline = audio.AudioPipeline(cfg=short, stt=audio.ScriptedStt([]),
                                   lock=lock, on_event=bus)

    utterances = list(pipeline.run(audio.ArraySource(make_voice(118, 6.0))))

    assert len(utterances) >= 4, f"硬上限没生效，只收尾了 {len(utterances)} 次"
    assert all(not u.accepted for u in utterances)
    assert all("空转写" in u.reason for u in utterances)


def test_pipeline_halts_during_tts(cfg, bus, make_voice, stub_embedder, silence):
    gate = dsp.EchoGate(decay_ms=300, gate_dbfs=-25)
    pipeline = build_pipeline(cfg, bus, stub_embedder, ["这句不该被处理"], echo=gate)
    gate.tts_start()
    quiet = make_voice(118, 1.0, amplitude=0.02)          # 低于门限：被视为自身回声
    assert list(pipeline.run(audio.ArraySource(np.concatenate([quiet, silence(1500)])))) == []
    gate.tts_end()
    gate.tick(400)
    assert list(pipeline.run(audio.ArraySource(np.concatenate([make_voice(118, 1.0), silence(1500)]))))


def test_pipeline_reports_listening_event(cfg, bus, make_voice, stub_embedder, silence):
    pipeline = build_pipeline(cfg, bus, stub_embedder, ["我要写日志，事件检查"])
    list(pipeline.run(audio.ArraySource(np.concatenate([make_voice(118, 1.0), silence(1500)]))))
    states = [event["state"] for event in bus.history()]
    assert "listening" in states


def test_array_source_pads_tail_silence(make_voice):
    source = audio.ArraySource(make_voice(118, 0.2), chunk=512, tail_silence_ms=500)
    chunks = list(source)
    assert all(chunk.size <= 512 for chunk in chunks)
    assert sum(chunk.size for chunk in chunks) >= int(dsp.SR * 0.7)


def test_wav_source_roundtrip(tmp_path, make_voice):
    path = tmp_path / "clip.wav"
    samples = make_voice(118, 0.5)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(dsp.SR)
        handle.writeframes((samples * 32767).astype(np.int16).tobytes())
    loaded = audio.wav_to_array(path)
    assert loaded.size == samples.size
    assert abs(float(np.max(np.abs(loaded))) - float(np.max(np.abs(samples)))) < 0.01


def test_wav_source_rejects_wrong_format(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 100)
    try:
        audio.wav_to_array(path)
    except ValueError as exc:
        assert "单声道" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当拒绝非 16k 单声道")


def test_self_healing_source_reconnects():
    attempts = {"n": 0}
    events: list[tuple[str, str, str]] = []

    def factory():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("device disconnected (-9988)")
        return audio.ArraySource(np.full(2048, 0.1, dtype=np.float32))

    source = audio.SelfHealingSource(factory, backoff_s=0.01, on_event=lambda *a: events.append(a))
    chunks = list(itertools.islice(source, 3))
    assert len(chunks) == 3
    assert attempts["n"] == 3
    assert any("音频设备异常" in event[1] for event in events)


def test_self_healing_gives_up_after_max_retries():
    def factory():
        raise RuntimeError("no mic")

    source = audio.SelfHealingSource(factory, backoff_s=0.01, max_retries=2)
    try:
        next(iter(source))
    except RuntimeError as exc:
        assert "no mic" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("超过重试上限应抛出")


def test_silero_vad_on_real_model():
    """真实模型 + 真实语音：Silero 是语音检测器，不对合成音负责，所以必须有语料才测。"""
    import pytest

    repo = Path(__file__).resolve().parents[1]
    model = repo / ".voxos" / "models" / "silero_vad.onnx"
    clip = repo / ".voxos" / "corpus" / "owner-0.wav"
    if not model.exists() or not clip.exists():
        pytest.skip("缺少 silero_vad.onnx 或验收语料")
    vad = audio.SileroVad(model, threshold=0.5)
    speech = audio.wav_to_array(clip)
    hits = [vad.is_speech(speech[i:i + 512]) for i in range(0, len(speech) - 512, 512)]
    assert sum(hits) > len(hits) * 0.6, f"真实语音检出率过低：{sum(hits)}/{len(hits)}"
    vad.reset()
    assert not any(vad.is_speech(np.zeros(512, dtype=np.float32)) for _ in range(10))


def test_make_vad_falls_back_without_model(cfg):
    vad = audio.make_vad(cfg)
    assert isinstance(vad, dsp.EnergyVad)


def test_make_embedder_falls_back_without_model(cfg):
    embedder, threshold, kind = audio.make_embedder(cfg)
    assert kind == "spectral-fallback"
    assert threshold == cfg.get("audio.voiceprint_threshold_fallback")
    assert embedder(np.zeros(1600, dtype=np.float32)).size == 42


def test_concat_with_silence_lengths(make_voice):
    joined = audio.concat_with_silence([make_voice(118, 0.1), make_voice(118, 0.1)], gap_ms=100)
    assert joined.size == 2 * 1600 + 2 * 1600


def test_missing_sounddevice_hint_matches_how_it_was_installed(monkeypatch):
    """缺 sounddevice 时给的命令必须能直接粘进终端。

    pipx 装出来的包用 `pip install` 补，会补到系统 Python 里，补完还是
    ImportError——用户照着提示做一遍，问题一点没变。这比不给提示更糟：
    他会以为自己操作错了，然后再试一次。
    """
    monkeypatch.setattr(audio.sys, "prefix", "/Users/x/.local/pipx/venvs/voxos")
    assert "pipx inject voxos sounddevice" in str(audio._missing_sounddevice(ImportError()))

    monkeypatch.setattr(audio.sys, "prefix", "/usr/local")
    assert "pip install 'voxos[audio]'" in str(audio._missing_sounddevice(ImportError()))


class _AlwaysOk:
    """声纹永远通过的替身：这一组测的是分段行为，不是身份判定。"""

    def verify(self, audio):
        return True, 1.0


class _CountingStt:
    """记下每次调用的音频长度，用来验证「只解结尾」和「不复用错引擎」。"""

    partial_equals_final = True

    def __init__(self, text: str = "打开微信") -> None:
        self.text = text
        self.calls: list[tuple[int, bool]] = []

    def transcribe(self, audio, final: bool = False) -> str:
        self.calls.append((int(audio.size), final))
        return self.text


class _PlainStt(_CountingStt):
    """不声明 partial == final 的引擎（远端 Whisper、脚本回放都属于这一类）。"""

    partial_equals_final = False


def test_endpoint_check_only_decodes_the_tail(cfg, make_voice, silence):
    """判「说完没」只看结尾，所以不该把整段重新解一遍。

    离线引擎解码耗时随音频线性涨（实测 2s→68ms，12s→283ms）。长句子上每次复查
    都整段重解，是最容易被忽略的 CPU 黑洞——而且它就在识别线程里。
    """
    stt = _CountingStt()
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=_AlwaysOk(), on_event=lambda *a: None)
    stream = np.concatenate([make_voice(118, 4.0, seed=7), silence(600)])
    list(pipeline.run(audio.ArraySource(stream)))

    partial = [n for n, final in stt.calls if not final]
    assert partial, "应该至少复查一次"
    window = int(audio.dsp.SR * float(cfg.get("audio.partial_window_ms", 3000)) / 1000)
    assert max(partial) <= window + 512, f"复查解了 {max(partial)} 采样，超出窗口 {window}"


def test_partial_result_is_reused_only_when_engine_says_so(cfg, make_voice, silence):
    """引擎声明 partial == final 时省掉终稿重解；没声明的引擎必须老实重解。

    写错过一次：条件只判了「文本非空」，于是脚本回放引擎（final 才推进队列）
    的 peek 结果被当成终稿，同一句话被反复派发。
    """
    stream = np.concatenate([make_voice(118, 1.2, seed=8), silence(700)])
    reuse = _CountingStt()
    list(audio.AudioPipeline(cfg=cfg, stt=reuse, lock=_AlwaysOk(),
                             on_event=lambda *a: None).run(audio.ArraySource(stream)))
    assert not [call for call in reuse.calls if call[1]], "声明了 partial==final 就不该再解一次"

    plain = _PlainStt()
    list(audio.AudioPipeline(cfg=cfg, stt=plain, lock=_AlwaysOk(),
                             on_event=lambda *a: None).run(audio.ArraySource(stream)))
    assert [call for call in plain.calls if call[1]], "没声明的引擎必须走 final 重解"


def test_prefix_padding_keeps_the_audio_before_speech_starts(cfg, make_voice, silence):
    """VAD 逐块判，第一个音素常落在「上一块」里——不补就把起手字削掉。"""
    pipeline = audio.AudioPipeline(cfg=cfg, stt=_CountingStt(), lock=_AlwaysOk(),
                                   on_event=lambda *a: None)
    spoken = make_voice(118, 1.0, seed=9)
    stream = np.concatenate([silence(800), spoken, silence(700)])
    got = [u for u in pipeline.run(audio.ArraySource(stream)) if u.accepted]
    assert len(got) == 1
    # 1.0s 人声 + 300ms 前缀 + 尾部静音，必然明显长于 1.0s
    assert got[0].audio_ms > 1000 + float(cfg.get("audio.prefix_padding_ms", 300)) * 0.8


def test_min_speech_gate_counts_voice_not_buffer(cfg, make_voice, silence):
    """加了 prefix padding 之后缓冲一定比人声长，门禁必须按人声时长算。

    否则一声咳嗽 + 300ms 前缀就能凑过 200ms 的门槛，转写出一段空话。
    """
    pipeline = audio.AudioPipeline(cfg=cfg, stt=_CountingStt(), lock=_AlwaysOk(),
                                   on_event=lambda *a: None)
    stream = np.concatenate([silence(600), make_voice(118, 0.08, seed=10), silence(700)])
    got = list(pipeline.run(audio.ArraySource(stream)))
    assert got, "这段必须产生一条记录——否则这条测试就是空过"
    assert all(not u.accepted for u in got), f"80ms 的气音不该被接受：{[(u.accepted, u.reason) for u in got]}"
    assert "太短" in got[0].reason
