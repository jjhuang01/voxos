"""纯函数信号处理层：AGC、高架滤波、能量 VAD、声纹嵌入、端点判定、回声门。

本模块**不做任何 I/O**（不碰麦克风、不读文件、不打日志），因此可以完全离线单测。
音频一律用 `numpy.float32` 表示，取值 [-1, 1]，采样率 16kHz。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

SR = 16000
_EPS = 1e-10

# 语义未完结尾词（PROMPTS_AND_ROUTING.md §6）
UNFINISHED_TAILS = (
    "我要", "我要写", "写代码", "改代码", "加个", "做个", "修复", "重构", "然后", "接着",
    "把", "把那个", "在", "因为", "和", "的", "如果", "帮我", "再", "就是", "那个", "所以",
    "但是", "而且", "并且", "或者", "，", ",", "、",
)


def as_float(x: np.ndarray | list[float]) -> np.ndarray:
    """统一成 float32 一维数组。"""
    arr = np.asarray(x, dtype=np.float32)
    return arr.reshape(-1)


def dbfs(x: np.ndarray) -> float:
    """RMS 分贝值；静音返回 -120。"""
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * float(np.log10(rms)) if rms > _EPS else -120.0


def agc(x: np.ndarray, target_dbfs: float = -3.0, max_gain_db: float = 24.0, floor_dbfs: float = -55.0) -> np.ndarray:
    """动态增益归一化：把耳语拉到目标电平，但不放大底噪。"""
    level = dbfs(x)
    if level <= floor_dbfs:
        return x
    gain = min(max_gain_db, target_dbfs - level)
    return np.clip(x * np.float32(10 ** (gain / 20.0)), -1.0, 1.0).astype(np.float32)


def high_shelf(x: np.ndarray, sr: int = SR, f0: float = 3000.0, gain_db: float = 6.0, q: float = 0.707) -> np.ndarray:
    """RBJ 高架滤波器：补偿静音面罩造成的高频衰减（2k–7kHz）。"""
    amplitude = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2 * q)
    cos_w = np.cos(w0)
    sqrt_a = np.sqrt(amplitude)
    b0 = amplitude * ((amplitude + 1) + (amplitude - 1) * cos_w + 2 * sqrt_a * alpha)
    b1 = -2 * amplitude * ((amplitude - 1) + (amplitude + 1) * cos_w)
    b2 = amplitude * ((amplitude + 1) + (amplitude - 1) * cos_w - 2 * sqrt_a * alpha)
    a0 = (amplitude + 1) - (amplitude - 1) * cos_w + 2 * sqrt_a * alpha
    a1 = 2 * ((amplitude - 1) - (amplitude + 1) * cos_w)
    a2 = (amplitude + 1) - (amplitude - 1) * cos_w - 2 * sqrt_a * alpha
    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    return _biquad(x, b, a)


def _biquad(x: np.ndarray, b: np.ndarray, a: np.ndarray) -> np.ndarray:
    """直接 I 型 IIR（无 scipy 依赖）；1 秒音频约 3ms。"""
    out = np.empty_like(x, dtype=np.float32)
    x1 = x2 = y1 = y2 = 0.0
    for i, sample in enumerate(x):
        value = float(sample)
        y0 = b[0] * value + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        out[i] = y0
        x2, x1 = x1, value
        y2, y1 = y1, y0
    return out


@dataclass
class EnergyVad:
    """零依赖能量 VAD（Silero 缺失时的降级路径）。带自适应底噪跟踪。"""

    threshold_dbfs: float = -42.0
    floor_dbfs: float = -70.0
    adapt: float = 0.02
    _floor: float = field(default=-70.0, repr=False)

    def is_speech(self, chunk: np.ndarray) -> bool:
        level = dbfs(chunk)
        if level < self._floor + 6.0:  # 安静时缓慢学习底噪
            self._floor = (1 - self.adapt) * self._floor + self.adapt * level
        return level > max(self.threshold_dbfs, self._floor + 9.0)

    def reset(self) -> None:
        self._floor = self.floor_dbfs


@dataclass
class EndpointDetector:
    """端点判定：基础静音窗 + 语义尾词延长 + 硬上限。"""

    sr: int = SR
    silence_ms: float = 800.0
    extended_ms: float = 2500.0
    max_ms: float = 12000.0
    min_speech_ms: float = 200.0
    speech_ms: float = 0.0
    silence_ms_now: float = 0.0
    total_ms: float = 0.0
    deadline_ms: float = 800.0
    extended: bool = False

    def reset(self) -> None:
        self.speech_ms = self.silence_ms_now = self.total_ms = 0.0
        self.deadline_ms, self.extended = self.silence_ms, False

    def push(self, is_speech: bool, n_samples: int) -> str:
        """喂入一帧，返回 idle | speech | candidate | force_end。"""
        span = n_samples / self.sr * 1000.0
        self.total_ms += span
        if self.total_ms >= self.max_ms:
            return "force_end"
        if is_speech:
            self.speech_ms += span
            self.silence_ms_now = 0.0
            return "speech"
        if self.speech_ms == 0.0:
            return "idle"
        self.silence_ms_now += span
        return "candidate" if self.silence_ms_now >= self.deadline_ms else "speech"

    def should_end(self, text: str) -> bool:
        """候选端点处用转写文本二次判断：半截话继续等，说完才切。"""
        if self.total_ms >= self.max_ms:
            return True
        if self.speech_ms < self.min_speech_ms:
            return False
        if not self.extended and text.strip().endswith(UNFINISHED_TAILS):
            self.deadline_ms, self.extended = self.extended_ms, True
            return False
        return self.silence_ms_now >= self.deadline_ms


@dataclass
class EchoGate:
    """半双工回声门：TTS 期间及混响期内只放行「打断词 + 高能量」音频。"""

    decay_ms: float = 300.0
    gate_dbfs: float = -25.0
    interrupt_words: tuple[str, ...] = ("停", "停一下", "算了", "取消", "别说了", "不用了")
    speaking: bool = False
    _decay_left: float = 0.0

    def tts_start(self) -> None:
        self.speaking, self._decay_left = True, 0.0

    def tts_end(self) -> None:
        self.speaking, self._decay_left = False, self.decay_ms

    def tick(self, ms: float) -> None:
        if not self.speaking:
            self._decay_left = max(0.0, self._decay_left - ms)

    @property
    def muted(self) -> bool:
        return self.speaking or self._decay_left > 0.0

    def allow_chunk(self, level_dbfs: float) -> bool:
        """未静音时全放行；静音期间只放行足够响的（用户喊“停”）。"""
        return not self.muted or level_dbfs > self.gate_dbfs

    def allow_text(self, text: str) -> bool:
        return not self.muted or any(word in text for word in self.interrupt_words)

    def is_interrupt(self, text: str) -> bool:
        return any(word in text for word in self.interrupt_words)


def spectral_features(x: np.ndarray, sr: int = SR, bands: int = 40) -> np.ndarray:
    """原始频谱统计特征（未归一化）：对数带能量均值 + 音高统计。

    降级声纹用；生产路径请用 `audio.SherpaEmbedder`（CAM++/ECAPA）。
    """
    signal = as_float(x)
    if signal.size < sr // 10:
        return np.zeros(bands + 2, dtype=np.float32)
    frame = int(0.04 * sr)
    hop = int(0.02 * sr)
    edges = np.geomspace(80.0, sr / 2 - 200, bands + 1)
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    window = np.hanning(frame).astype(np.float32)
    energies, pitches = [], []
    for start in range(0, signal.size - frame, hop):
        frame_data = signal[start:start + frame]
        if float(np.sqrt(np.mean(frame_data ** 2))) < 0.005:
            continue
        spec = np.abs(np.fft.rfft(frame_data * window)) ** 2
        energies.append([spec[(freqs >= lo) & (freqs < hi)].sum() for lo, hi in zip(edges[:-1], edges[1:])])
        pitches.append(_pitch(frame_data, sr))
    if not energies:
        return np.zeros(bands + 2, dtype=np.float32)
    log_bands = np.log(np.mean(energies, axis=0) + _EPS)
    voiced = np.array([p for p in pitches if p > 0], dtype=np.float32)
    pitch_stats = [voiced.mean() / 100 if voiced.size else 0.0,
                   voiced.std() / 10 if voiced.size else 0.0,
                   float(np.percentile(voiced, 20)) / 100 if voiced.size else 0.0,
                   float(np.percentile(voiced, 80)) / 100 if voiced.size else 0.0]
    return np.concatenate([log_bands, pitch_stats]).astype(np.float32)


def spectral_embedding(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """零依赖声纹嵌入：特征去均值 + L2 归一化（无背景模型时使用）。"""
    vector = spectral_features(x, sr)
    if not np.any(vector):
        return vector
    vector = vector - vector.mean()
    return (vector / (np.linalg.norm(vector) + _EPS)).astype(np.float32)


def _pitch(frame: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 400.0) -> float:
    """自相关基频估计，用于区分说话人音高。"""
    centered = frame - frame.mean()
    corr = np.correlate(centered, centered, mode="full")[len(centered) - 1:]
    lo, hi = int(sr / fmax), int(sr / fmin)
    if hi >= corr.size or lo < 1:
        return 0.0
    window = corr[lo:hi]
    peak = int(np.argmax(window)) + lo
    return sr / peak if corr[peak] > 0.3 * corr[0] else 0.0


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator > _EPS else 0.0


@dataclass
class SpeakerLock:
    """双模声纹锁：normal / mask 两组嵌入，校验取最大相似度。

    embedder 可插拔：生产用 CAM++（192 维），无模型时降级频谱嵌入。
    """

    threshold: float = 0.76
    embedder: Callable[[np.ndarray], np.ndarray] = spectral_embedding
    profiles: dict[str, np.ndarray] = field(default_factory=dict)

    def enroll(self, samples: list[np.ndarray], mode: str = "normal") -> float:
        """录入若干段音频，取嵌入均值；返回各段之间的一致性（越高越可靠）。"""
        vectors = [self.embedder(s) for s in samples if as_float(s).size > 1600]
        if not vectors:
            raise ValueError("有效音频不足（每段至少 0.1 秒）")
        stacked = np.stack([v / (np.linalg.norm(v) + _EPS) for v in vectors])
        mean = stacked.mean(axis=0)
        mean = (mean / (np.linalg.norm(mean) + _EPS)).astype(np.float32)
        self.profiles[mode] = mean
        if len(vectors) > 1:
            return float(np.mean([cosine(v, mean) for v in stacked]))
        return 1.0

    def verify(self, audio: np.ndarray) -> tuple[bool, float]:
        """返回 (是否本人, 最高相似度)。未录入则 fail-closed。"""
        if not self.profiles:
            return False, 0.0
        if as_float(audio).size < SR // 10:
            return False, 0.0
        current = self.embedder(audio)
        best = max(cosine(current, profile) for profile in self.profiles.values())
        return best >= self.threshold, round(best, 4)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, **{k: v for k, v in self.profiles.items()})

    def load(self, path: Path) -> "SpeakerLock":
        if path.exists():
            with np.load(path) as data:
                self.profiles = {k: data[k].astype(np.float32) for k in data.files}
        return self
