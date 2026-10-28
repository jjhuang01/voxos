"""测试夹具：临时配置、状态总线、执行器、合成语音与桩声纹。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voxos import core, tools, ui  # noqa: E402
from voxos.config import Config  # noqa: E402

SR = 16000


def base_settings(tmp: Path) -> dict:
    return {
        "audio": {
            "sample_rate": SR, "chunk_size": 512, "vad_threshold": 0.5, "vad_backend": "energy",
            "silero_model": "", "speaker_model": "", "silence_duration_ms": 800, "max_utterance_ms": 12000,
            "min_speech_ms": 200, "voiceprint_threshold": 0.84, "voiceprint_threshold_fallback": 0.45,
            "voiceprint_profiles": ".voxos/voiceprint_profiles.npz", "mask_mode": False,
            "agc_target_dbfs": -3.0, "echo_decay_ms": 300, "hotwords_path": "config/hotwords.txt",
            "hotwords_generated": "config/hotwords.txt.generated", "hotwords_max": 200,
            "wake_words": ["siri", "jarvis", "小助手"],
        },
        "stt": {"backend": "scripted", "language": "zh", "model": "small", "api_base": "", "api_key": ""},
        "llm": {"enabled": False, "base_url": "", "api_key": "", "model": "test-model",
                "temperature": 0.1, "timeout_s": 3, "max_tool_rounds": 2},
        "workspace": {
            "daily_log_dir": str(tmp / "logs"), "default_code_dir": str(tmp / "projects"),
            "ide_command": "code", "code_cli": "claude", "grafana_url": "",
            "backup_dir": str(tmp / "backups"), "dry_run": True, "role": "DEVELOPER",
            "log_template": "- **[{time}] [{category}]** ({project}) {content}", "confirm_timeout_s": 1,
        },
        "ui": {"host": "127.0.0.1", "port": 0, "earcons": False, "pill_file": "web/pill.html"},
        "audit": {"enabled": True, "path": str(tmp / ".voxos" / "audit.jsonl")},
    }


@pytest.fixture(autouse=True)
def _clean_history():
    """每项测试都从「没有上下文」开始。

    多轮历史是**进程级**状态（单用户语音系统就该是这样）。不隔离的话，
    一条测试往历史里塞了一句，下一条测试的模型调用就会多出两条消息——
    这种污染不会报错，只会让断言变得莫名其妙。
    """
    core.reset_history()
    yield
    core.reset_history()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(copy.deepcopy(base_settings(tmp_path)), root=tmp_path,
                    data_root=tmp_path)
    tools.configure(config)
    core.ensure_tools()
    return config


@pytest.fixture
def bus(tmp_path: Path) -> ui.StatusBus:
    return ui.StatusBus(tmp_path / ".voxos" / "status.jsonl")


@pytest.fixture
def executor(cfg: Config, bus: ui.StatusBus) -> core.Executor:
    return core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: True)


@pytest.fixture
def make_voice():
    """合成一个“说话人”：基频 + 谐波 + 音节包络。"""

    def _voice(f0: float = 118.0, seconds: float = 1.6, seed: int = 0, amplitude: float = 0.5) -> np.ndarray:
        rng = np.random.default_rng(seed)
        t = np.arange(int(SR * seconds)) / SR
        signal = np.zeros_like(t)
        for k in range(1, 16):
            if f0 * k > SR / 2 - 400:
                break
            signal += (0.9 ** k) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6.28))
        signal *= 1 + 0.3 * np.sin(2 * np.pi * 3 * t)
        signal += rng.normal(0, 0.005, t.size)
        return (signal / np.max(np.abs(signal)) * amplitude).astype(np.float32)

    return _voice


@pytest.fixture
def stub_embedder():
    """确定性桩声纹：按频谱峰值分桶，保证“不同音高 = 不同人”，不依赖模型质量。"""

    def _embed(audio: np.ndarray) -> np.ndarray:
        vector = np.zeros(512, dtype=np.float32)
        if audio.size < 800:
            return vector
        centered = audio - audio.mean()
        corr = np.correlate(centered, centered, mode="full")[centered.size - 1:]
        low, high = int(SR / 400), int(SR / 70)
        if high >= corr.size:
            return vector
        peak = int(np.argmax(corr[low:high])) + low
        vector[min(int(round(SR / peak)), 511)] = 1.0
        return vector

    return _embed


@pytest.fixture
def silence():
    def _silence(ms: float) -> np.ndarray:
        return np.zeros(int(SR * ms / 1000), dtype=np.float32)

    return _silence
