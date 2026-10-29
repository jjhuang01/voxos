"""Whisper 风格 log-mel 特征（80 维 × 800 帧）。给 smart-turn v3 喂输入。

**这是抄来的，不是想出来的。** 上游是 pipecat 的
`src/pipecat/audio/turn/smart_turn/_whisper_features.py`（BSD-2-Clause），
它自己又是从 HuggingFace `transformers` 的 `WhisperFeatureExtractor` 移植的
（Apache-2.0）。两边都与本项目 Apache-2.0 兼容，保留声明如下：

    Copyright (c) 2024-2026, Daily — SPDX-License-Identifier: BSD-2-Clause
    Portions derived from Hugging Face Transformers (Apache-2.0):
    Copyright 2022-2023 The HuggingFace Inc. team, and the librosa & torchaudio
    authors. Modified by Daily.

为什么不 `pip install transformers`：那一条会拖进 torch（2GB）和一个只用一次的
特征提取器。这里 130 行 numpy，启动 0ms，跟模型本身 8.7MB 一个量级。

**一个数字都不能改。** 模型是在这套特征上训出来的：换 hop、换窗、少做一次
归一化，输出不会报错，只会变成一个随机数生成器。所以下面每个常量都留着
上游的名字和注释。
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_N_FFT = 400
_HOP_LENGTH = 160
_N_MELS = 80
_SAMPLING_RATE = 16000
_MEL_FLOOR = 1e-10
_NORM_VARIANCE_EPS = 1e-7
CHUNK_SAMPLES = _SAMPLING_RATE * 8        # 128000，模型固定吃 8 秒


def _hertz_to_mel_slaney(freq: np.ndarray) -> np.ndarray:
    """Slaney 刻度：1kHz 以下线性，以上对数。和 librosa 的 htk=False 一致。"""
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = 27.0 / np.log(6.4)
    freq = np.atleast_1d(np.asarray(freq, dtype=np.float64))
    mels = 3.0 * freq / 200.0
    log_region = freq >= min_log_hertz
    mels[log_region] = min_log_mel + np.log(freq[log_region] / min_log_hertz) * logstep
    return mels


def _mel_to_hertz_slaney(mels: np.ndarray) -> np.ndarray:
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = np.log(6.4) / 27.0
    mels = np.atleast_1d(np.asarray(mels, dtype=np.float64))
    freq = 200.0 * mels / 3.0
    log_region = mels >= min_log_mel
    freq[log_region] = min_log_hertz * np.exp(logstep * (mels[log_region] - min_log_mel))
    return freq


def _build_mel_filterbank(num_frequency_bins: int, num_mel_filters: int,
                          min_frequency: float, max_frequency: float,
                          sampling_rate: int) -> np.ndarray:
    """Slaney 面积归一化的三角滤波器组，形状 `(频点数, mel 数)`。"""
    mel_min = float(_hertz_to_mel_slaney(np.array([min_frequency]))[0])
    mel_max = float(_hertz_to_mel_slaney(np.array([max_frequency]))[0])
    mel_freqs = np.linspace(mel_min, mel_max, num_mel_filters + 2)
    filter_freqs = _mel_to_hertz_slaney(mel_freqs)
    fft_freqs = np.linspace(0, sampling_rate // 2, num_frequency_bins)

    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    mel_filters = np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))
    enorm = 2.0 / (filter_freqs[2:num_mel_filters + 2] - filter_freqs[:num_mel_filters])
    mel_filters *= np.expand_dims(enorm, 0)
    return mel_filters


def _periodic_hann_window(window_length: int) -> np.ndarray:
    """周期 Hann 窗（等于 `torch.hann_window`）。`np.hanning` 是**对称**窗，差一个点。"""
    return np.hanning(window_length + 1)[:-1]


_HANN_WINDOW = _periodic_hann_window(_N_FFT)
_MEL_FILTERS = _build_mel_filterbank(
    num_frequency_bins=_N_FFT // 2 + 1,
    num_mel_filters=_N_MELS,
    min_frequency=0.0,
    max_frequency=_SAMPLING_RATE / 2.0,
    sampling_rate=_SAMPLING_RATE,
)


def _power_spectrogram(waveform: np.ndarray, window: np.ndarray,
                       frame_length: int, hop_length: int) -> np.ndarray:
    """居中功率谱 `(频点数, 帧数)`，float64——上游也是 float64，别改成 float32。"""
    pad = frame_length // 2
    padded = np.pad(waveform.astype(np.float64), (pad, pad), mode="reflect")
    windows = sliding_window_view(padded, frame_length)[::hop_length]
    spec = np.fft.rfft(windows * window.astype(np.float64), axis=-1)
    return (np.abs(spec) ** 2).T


def compute(audio: np.ndarray, *, do_normalize: bool = True) -> np.ndarray:
    """算出 `(80, 800)` 的 log-mel，取值范围大致 `[-1, 1]`。

    调用方要保证 `audio` 已经是 16kHz 单声道、**恰好 8 秒**（短的补零、长的截尾
    由 `turn.SmartTurn` 做，因为补在哪儿是有讲究的：补在**前面**，让最后一句
    永远贴着窗口末尾）。

    `do_normalize` 做的是「整段波形零均值单位方差」，不是逐帧。位置在谱之前——
    顺序错了不报错，只是模型看到的东西和训练时不一样。
    """
    if audio.ndim != 1:
        raise ValueError(f"要一维音频，拿到的是 {audio.shape}")
    x = np.asarray(audio, dtype=np.float32)
    if x.size < CHUNK_SAMPLES:
        x = np.pad(x, (0, CHUNK_SAMPLES - x.size), mode="constant")
    elif x.size > CHUNK_SAMPLES:
        x = x[:CHUNK_SAMPLES]
    if do_normalize:
        x = (x - x.mean()) / np.sqrt(x.var() + _NORM_VARIANCE_EPS)
    magnitudes = _power_spectrogram(x, _HANN_WINDOW, _N_FFT, _HOP_LENGTH)
    mel_spec = np.maximum(_MEL_FLOOR, _MEL_FILTERS.T @ magnitudes)
    log_spec = np.log10(mel_spec)[:, :-1]      # 丢掉最后一帧，和上游一致
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


__all__ = ["compute", "CHUNK_SAMPLES"]
