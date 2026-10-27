"""验收语料：优先用 macOS `say` 合成真实音色，否则退化为合成音。

真实语料用于三件事：声纹 FRR/FAR 测量、端到端 demo、CI 回归。
产物全部落在 `.voxos/corpus/`（已 gitignore），不入库。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import dsp

OWNER_VOICE = "Tingting"

# 冒充者：中文音色说中文、英文音色说英文（跨语言说会合成失败）。
# 含两位中文女声（同性别困难样本），让 FAR 不被高估。
IMPOSTOR_VOICES: tuple[tuple[str, str], ...] = (
    ("Meijia", "zh"), ("Samantha", "en"), ("Daniel", "en"), ("Karen", "en"), ("Moira", "en"),
)

# 主人说的四句话：前两句用于声纹录入，后两句用于端到端触发
OWNER_LINES = (
    "把刚才登录模块的报错原因记到今天的开发日志里",
    "帮我在认证文件里加上空值校验",
    "Siri，我要写日志，今天把登录接口的空值校验补上了",
    "Siri，检查一下服务器现在的负载和磁盘使用情况",
)
ZH_LINES = (
    "麻烦把今天下午的会议纪要发我一份",
    "这个接口的返回格式我需要再确认一下",
    "明天上午十点我们开个同步会",
    "请把测试环境的日志清理干净",
)
EN_LINES = (
    "please remember the login timeout bug we fixed this morning",
    "add a null check in the auth module and run the tests",
    "check the server load and write it to my dev log",
    "deploy the staging build and tell me the result",
)


def ensure_corpus(root: Path, force: bool = False) -> dict[str, object]:
    """生成或复用语料，返回 manifest（含每段音频的路径与转写）。"""
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    if manifest_path.exists() and not force:
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    if shutil.which("say") and shutil.which("afconvert"):
        manifest = _build_tts(root)
        if manifest:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return manifest
    manifest = _build_synthetic(root)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _build_tts(root: Path) -> dict[str, object] | None:
    owner_clips = []
    for index, text in enumerate(OWNER_LINES):
        path = root / f"owner-{index}.wav"
        if not _say(text, OWNER_VOICE, path):
            return None
        owner_clips.append({"path": path.name, "text": text})

    impostors = []
    for voice, language in IMPOSTOR_VOICES:
        clips = []
        for index, text in enumerate(ZH_LINES if language == "zh" else EN_LINES):
            path = root / f"{voice.lower()}-{index}.wav"
            if not _say(text, voice, path):
                break
            clips.append({"path": path.name, "text": text})
        if len(clips) == 4:                     # 2 段录入 + 2 段测试
            impostors.append({"voice": voice, "clips": clips})
    if not impostors:
        return None
    return {"kind": "tts", "owner": {"voice": OWNER_VOICE, "clips": owner_clips}, "impostors": impostors}


def _say(text: str, voice: str, out: Path) -> bool:
    aiff = out.with_suffix(".aiff")
    try:
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(out)],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        aiff.unlink(missing_ok=True)
    return out.exists() and out.stat().st_size > 8000


def _build_synthetic(root: Path) -> dict[str, object]:
    """无 `say` 的机器：用不同基频的谐波堆模拟不同说话人（仅保证链路可跑）。"""
    def voice(f0: float, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        t = np.arange(int(dsp.SR * 3.0)) / dsp.SR
        signal = np.zeros_like(t)
        for k in range(1, 16):
            if f0 * k > dsp.SR / 2 - 400:
                break
            signal += (0.9 ** k) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6.28))
        signal *= 1 + 0.3 * np.sin(2 * np.pi * 3 * t)
        signal += rng.normal(0, 0.01, t.size)
        return (signal / np.max(np.abs(signal)) * 0.6).astype(np.float32)

    def write(path: Path, samples: np.ndarray) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(dsp.SR)
            handle.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())

    owner_clips = []
    for index, text in enumerate(OWNER_LINES):
        path = root / f"owner-{index}.wav"
        write(path, voice(118.0, index))
        owner_clips.append({"path": path.name, "text": text})
    impostors = []
    for index, (f0, voice_name) in enumerate(((206.0, "synthetic-A"), (172.0, "synthetic-B"))):
        clips = []
        for clip_index in range(4):
            path = root / f"synthetic-{index}-{clip_index}.wav"
            write(path, voice(f0, 10 + index * 4 + clip_index))
            clips.append({"path": path.name, "text": EN_LINES[clip_index]})
        impostors.append({"voice": voice_name, "clips": clips})
    return {"kind": "synthetic", "owner": {"voice": "synthetic", "clips": owner_clips}, "impostors": impostors}


def load_clip(root: Path, entry: dict[str, str]) -> np.ndarray:
    """读语料音频（16-bit 单声道 16k）。"""
    with wave.open(str(root / entry["path"]), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
