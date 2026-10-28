"""真实识别：SenseVoice 接线、标签清理，以及"缺模型时给出人话错误"。

需要模型的用例在没下载时自动跳过——跳过就是跳过，不拿合成音假装通过。
模型下载：`python scripts/fetch_models.py`
"""

from __future__ import annotations

import importlib.util
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from voxos import audio, corpus
from voxos.config import Config, load

REPO = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO / ".voxos" / "models" / "sense-voice"
HAS_MODEL = (MODEL_DIR / "model.int8.onnx").exists() and (MODEL_DIR / "tokens.txt").exists()
HAS_SAY = bool(shutil.which("say") and shutil.which("afconvert"))
HAS_SHERPA = importlib.util.find_spec("sherpa_onnx") is not None
needs_model = pytest.mark.skipif(not (HAS_MODEL and HAS_SAY), reason="缺 SenseVoice 模型或 say")


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def test_missing_model_files_fail_loudly(tmp_path: Path):
    """模型不在时要立刻说清缺哪个文件，而不是让识别默默返回空串。

    这一条测的是「依赖在、文件不在」。没装 sherpa-onnx 时它根本走不到文件检查，
    那是下一条的事——跳过，不拿别的失败冒充这一条通过。
    """
    if not HAS_SHERPA:
        pytest.skip("缺 sherpa-onnx：这条测的是「有依赖但缺模型文件」")
    with pytest.raises(FileNotFoundError, match="model.int8.onnx"):
        audio.SenseVoiceStt(tmp_path)


def test_missing_sherpa_says_what_to_install(tmp_path: Path):
    """缺依赖时的报错必须带上安装命令，不能只丢一个裸 ImportError。"""
    if HAS_SHERPA:
        pytest.skip("已装 sherpa-onnx，这条测不到缺依赖分支")
    with pytest.raises(RuntimeError, match=r"voxos\[models\]"):
        audio.SenseVoiceStt(tmp_path)


def test_backend_sense_voice_is_wired(tmp_path: Path):
    """配置里写 sense_voice 就得真的走到这个后端（用空目录触发它的报错来证明）。"""
    cfg = load().with_overrides(**{"stt.backend": "sense_voice", "stt.model_dir": str(tmp_path)})
    # 两种「拒绝」都算接线成功：缺依赖报 RuntimeError，缺文件报 FileNotFoundError。
    with pytest.raises(FileNotFoundError if HAS_SHERPA else RuntimeError):
        audio.make_stt(cfg)


def test_hotwords_only_apply_to_backends_that_support_them():
    """热词表对 SenseVoice 不生效——启动横幅和 doctor 不能报一个假的「热词=N 条」。

    sherpa-onnx 的 SenseVoice 没有热词偏置接口，扫几千个文件名出来也喂不进去。
    与其装作生效，不如提前不扫（`refresh_hotwords` 的 rglob 不便宜）。
    """
    cfg = load()
    assert audio.supports_hotwords(cfg.with_overrides(**{"stt.backend": "openai"}))
    assert audio.supports_hotwords(cfg.with_overrides(**{"stt.backend": "local_whisper"}))
    assert not audio.supports_hotwords(cfg.with_overrides(**{"stt.backend": "sense_voice"}))
    assert not audio.supports_hotwords(cfg.with_overrides(**{"stt.backend": "scripted"}))


def test_relative_model_dir_resolves_against_data_root(tmp_path: Path, monkeypatch):
    """相对 model_dir 要按**数据根**解析，不能按当前工作目录。

    这条是装完之后真跑才发现的：`pipx install` 后模型在 `~/.voxos/models`，
    而 `cfg.get("stt.model_dir")` 原样返回 `.voxos/models/sense-voice`——
    只有恰好在 `$HOME` 下启动才找得到，换个目录就报「缺 model.int8.onnx」。
    """
    seen: list[str] = []

    class Recorder:
        def __init__(self, model_dir, num_threads=2):  # noqa: ANN001
            seen.append(str(model_dir))

    monkeypatch.setattr(audio, "SenseVoiceStt", Recorder)
    data_root = tmp_path / "home"
    cfg = Config({"stt": {"backend": "sense_voice", "model_dir": ".voxos/models/sense-voice"}},
                 root=tmp_path, data_root=data_root)
    audio.make_stt(cfg)
    assert seen == [str(data_root / ".voxos" / "models" / "sense-voice")]


def test_special_tags_are_stripped():
    """SenseVoice 会在文本里塞 <|zh|><|NEUTRAL|> 这类标记，必须清掉再进路由。"""
    raw = "<|zh|><|NEUTRAL|><|Speech|><|withitn|>把登录接口的空值校验补上"
    assert audio.SenseVoiceStt._TAG.sub("", raw) == "把登录接口的空值校验补上"


@needs_model
def test_transcribes_real_speech(tmp_path: Path):
    """真实音色说一句产品最常听的话，识别结果要能直接拿去路由。"""
    line = "把登录接口的空值校验补上"
    wav = tmp_path / "clip.wav"
    assert corpus._say(line, corpus.OWNER_VOICE, wav), "say 合成失败"

    engine = audio.SenseVoiceStt(MODEL_DIR)
    heard = engine.transcribe(_read_wav(wav))
    assert "登录" in heard, heard
    assert "空值" in heard or "校验" in heard, heard


@needs_model
def test_realtime_factor_is_fast(tmp_path: Path):
    """离线识别必须明显快于实时，否则实时字幕/端点复查都无从谈起。"""
    import time

    wav = tmp_path / "clip.wav"
    assert corpus._say("检查一下服务器现在的负载和磁盘使用情况", corpus.OWNER_VOICE, wav)
    samples = _read_wav(wav)
    engine = audio.SenseVoiceStt(MODEL_DIR)

    started = time.perf_counter()
    engine.transcribe(samples)
    elapsed = time.perf_counter() - started
    audio_seconds = samples.size / 16000
    assert elapsed / audio_seconds < 0.6, f"实时率 {elapsed / audio_seconds:.2f}"
