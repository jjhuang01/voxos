"""语义判停测试：mel 特征、补零位置、截断位置、降级、管道接线。"""

from __future__ import annotations

import numpy as np
import pytest

from voxos import audio, dsp, mel, turn


# ------------------------------------------------------------------ 替身

class _StubInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    """ONNX 会话替身：不推理，只记下喂进去的特征，按给定概率回答。"""

    def __init__(self, prob: float = 0.9) -> None:
        self.prob = prob
        self.feeds: list[np.ndarray] = []
        self._inputs = [_StubInput("input_features")]

    def get_inputs(self) -> list[_StubInput]:
        return self._inputs

    def run(self, _outputs: object, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.feeds.append(np.array(next(iter(feed.values()))))
        return [np.array([[self.prob]], dtype=np.float32)]


def _detector(prob: float = 0.9) -> tuple[turn.SmartTurn, _FakeSession]:
    """绕过 `__init__` 造一个判停器。

    不 monkeypatch onnxruntime：CI 只装 `[dev]`，没有 onnxruntime，那样写会让
    这组测试在 CI 上红。要测的是「特征怎么准备、概率怎么用」，不是「ORT 能不能
    加载模型」——后者只在有真模型时有意义，见文件末尾那一条。
    """
    session = _FakeSession(prob)
    detector = object.__new__(turn.SmartTurn)
    detector._session = session          # type: ignore[attr-defined]
    detector._input = session.get_inputs()[0].name   # type: ignore[attr-defined]
    detector.errors = 0                  # type: ignore[attr-defined]
    return detector, session


class _CountingStt:
    partial_equals_final = True

    def __init__(self, text: str = "打开微信") -> None:
        self.text = text
        self.calls: list[tuple[int, bool]] = []

    def transcribe(self, chunk: np.ndarray, final: bool = False) -> str:
        self.calls.append((int(chunk.size), final))
        return self.text


class _PassLock:
    def verify(self, _audio: np.ndarray) -> tuple[bool, float]:
        return True, 1.0


class _FixedTurn:
    """判停替身：永远给同一个概率。"""

    audio_native = True

    def __init__(self, prob: float) -> None:
        self.prob = prob
        self.calls: list[int] = []

    def complete(self, chunk: np.ndarray) -> float:
        self.calls.append(int(chunk.size))
        return self.prob


# ------------------------------------------------------------------ mel 特征

def test_mel_features_have_the_shape_the_model_wants():
    """模型固定吃 `(1, 80, 800)`：80 个 mel × 8 秒 / 10ms 一帧。多一帧少一帧都跑不了。"""
    features = mel.compute(np.zeros(mel.CHUNK_SAMPLES, dtype=np.float32))
    assert features.shape == (80, 800)
    assert features.dtype == np.float32


def test_mel_puts_a_one_khz_tone_in_the_right_bin():
    """滤波器组要是搭错了，形状照样对、范围照样对，只是内容全错。

    拿一个 1kHz 纯音定位：Slaney 刻度下它落在第 26 个 mel bin。差几个 bin 说明
    `_build_mel_filterbank` 抄漏了归一化项，差十几个说明 mel 刻度写错了。
    """
    t = np.arange(dsp.SR * 2) / dsp.SR
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    profile = mel.compute(tone).mean(axis=1)
    assert int(profile.argmax()) == 26


def test_mel_clamps_the_dynamic_range():
    """`max(log, max-8)` 之后动态范围被压到 8 个 log10 单位，除以 4 就是 2。

    这条守的是那行 clamp：删掉它不报错，只是模型看到的特征分布和训练时不一样，
    判停会变成掷硬币。
    """
    rng = np.random.default_rng(0)
    noisy = rng.normal(0, 0.1, mel.CHUNK_SAMPLES).astype(np.float32)
    features = mel.compute(noisy)
    assert float(features.max() - features.min()) <= 2.0 + 1e-6


def test_mel_is_deterministic():
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.1, mel.CHUNK_SAMPLES).astype(np.float32)
    assert np.array_equal(mel.compute(x), mel.compute(x))


# ------------------------------------------------------------------ 补零与截断

def test_short_audio_is_padded_at_the_front():
    """短音频补零补在**前面**：让最后一句贴着窗口末尾。

    补在后面等于给模型看「用户说完之后又沉默了 7 秒」，它会判成「说完了」——
    正好和事实相反。
    """
    detector, session = _detector()
    t = np.arange(dsp.SR) / dsp.SR
    tone = (0.4 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    detector.complete(tone)
    expected = mel.compute(np.pad(tone, (mel.CHUNK_SAMPLES - tone.size, 0)))[None]
    assert np.array_equal(session.feeds[0], expected)


def test_long_audio_keeps_the_last_eight_seconds():
    """12 秒的上限比模型窗口长，超了只留结尾——结尾才是「说完没」的证据。"""
    detector, session = _detector()
    rng = np.random.default_rng(1)
    long = rng.normal(0, 0.1, dsp.SR * 10).astype(np.float32)
    detector.complete(long)
    assert np.array_equal(session.feeds[0], mel.compute(long[-mel.CHUNK_SAMPLES:])[None])


def test_probability_is_passed_through_unchanged():
    """模型输出就是 sigmoid 概率，不要再套一层 softmax 或阈值。"""
    for prob in (0.0, 0.31, 0.5, 0.99):
        detector, _ = _detector(prob)
        assert detector.complete(np.zeros(dsp.SR, dtype=np.float32)) == pytest.approx(prob)


def test_broken_model_returns_minus_one_instead_of_raising():
    """坏模型不能把音频线程带走：返回 -1，让管道退回文本判停。"""
    detector, session = _detector()

    def boom(*_args: object, **_kwargs: object) -> list[np.ndarray]:
        raise RuntimeError("模型坏了")

    session.run = boom                       # type: ignore[method-assign]
    assert detector.complete(np.zeros(dsp.SR, dtype=np.float32)) == -1.0
    assert detector.errors == 1


# ------------------------------------------------------------------ 工厂与降级

def test_make_returns_none_when_the_model_is_missing(cfg):
    """没下模型就退回文本判停，而不是崩在启动路径上。"""
    assert turn.make(cfg) is None
    ok, why = turn.available(cfg)
    assert ok is False and "缺模型" in why


def test_make_returns_none_when_backend_says_text(cfg, tmp_path):
    """配置写死 `text` 时，哪怕模型就在那儿也不用——用户明确关掉的东西不该偷偷生效。"""
    model = cfg.path("audio.turn_model", turn.DEFAULT_MODEL)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"\0" * 16)
    forced = cfg.with_overrides(**{"audio.turn_backend": "text"})
    ok, why = turn.available(forced)
    assert ok is False and "文本判停" in why
    assert turn.make(forced) is None


def test_threshold_falls_back_when_config_is_junk(cfg):
    """配置文件是手写的，`0.5o` 这种手滑不该让整个启动失败。"""
    assert turn.threshold(cfg) == turn.DEFAULT_THRESHOLD
    assert turn.threshold(cfg.with_overrides(**{"audio.turn_threshold": "0.6"})) == 0.6
    assert turn.threshold(cfg.with_overrides(**{"audio.turn_threshold": "0.5o"})) == 0.5


# ------------------------------------------------------------------ 管道接线

def _stream(voice: np.ndarray, silence_ms: float, tail_ms: float = 600) -> np.ndarray:
    return np.concatenate([voice, np.zeros(int(dsp.SR * silence_ms / 1000), dtype=np.float32),
                           np.zeros(int(dsp.SR * tail_ms / 1000), dtype=np.float32)])


def test_audio_turn_detector_finalises_without_a_partial_decode(cfg, bus, make_voice):
    """判停由音频模型给结论时，**一次 partial 解码都不该发生**。

    这是整件事的收益所在：以前为了决定「要不要停」，必须先解一遍文本。现在
    这一步没有了，判停和识别之间不再有依赖——将来换流式识别才不会被它绊住。
    """
    stt = _CountingStt()
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=_PassLock(), on_event=bus,
                                   turn_detector=_FixedTurn(0.9))
    utterances = [u for u in pipeline.run(audio.ArraySource(_stream(make_voice(118, 1.2), 900)))
                  if u.accepted]
    assert len(utterances) == 1
    assert [final for _, final in stt.calls] == [True], "只该有终稿那一次解码"


def test_incomplete_verdict_waits_out_the_extended_window(cfg, bus, make_voice):
    """模型说「没说完」→ 继续等到 `endpoint_extend_ms`，别抢话。

    `endpoint_extend_ms` 是**总时长**，不是「在基础静音之上再加」：`deadline_ms`
    被直接设成它。所以差别是 `extended_ms - silence_ms`（这里 2500 - 800 = 1700ms），
    不是 `extended_ms`。写成加法也能让「继续等」看起来是对的，但会多等 800ms，
    而且只有量出来才发现——所以这条断言测的是**两个配置值的差**，不是绝对值。
    """
    def waited(prob: float) -> float:
        pipeline = audio.AudioPipeline(cfg=cfg, stt=_CountingStt(), lock=_PassLock(),
                                       on_event=bus, turn_detector=_FixedTurn(prob))
        stream = audio.ArraySource(_stream(make_voice(118, 1.2), 4000))
        return [u.audio_ms for u in pipeline.run(stream) if u.accepted][0]

    extended = cfg.get("audio.endpoint_extend_ms", 2500)
    silence = cfg.get("audio.silence_duration_ms", 500)
    assert waited(0.1) - waited(0.9) == pytest.approx(extended - silence, abs=120)


def test_broken_model_falls_back_to_text_mid_stream(cfg, bus, make_voice):
    """模型坏了要能当场退回文本判停，而不是这一整轮都判不出来。"""
    stt = _CountingStt("我要写日志")
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=_PassLock(), on_event=bus,
                                   turn_detector=_FixedTurn(-1.0))
    utterances = [u for u in pipeline.run(audio.ArraySource(_stream(make_voice(118, 1.2), 900)))
                  if u.accepted]
    assert len(utterances) == 1
    assert pipeline.smart_turn is None, "坏模型必须被摘掉，否则下一句还会踩同一个坑"
    assert any(final is False for _, final in stt.calls), "退回文本判停后应该有 partial 解码"


# ------------------------------------------------------------------ 真模型

def test_real_model_speaks_probability_if_it_is_downloaded(cfg):
    """本机下了模型就跑一次真的：输出必须是 0–1 之间的一个数。

    没下就跳过——CI 上永远跳过，这是**故意的**：模型 8.7MB，不该进 CI 缓存。
    """
    path = cfg.path("audio.turn_model", turn.DEFAULT_MODEL)
    if not path.exists():
        pytest.skip("没下 smart-turn 模型（voxos models）")
    detector = turn.SmartTurn(path)
    t = np.arange(dsp.SR) / dsp.SR
    prob = detector.complete((0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
    assert 0.0 <= prob <= 1.0
