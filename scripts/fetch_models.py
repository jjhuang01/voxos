#!/usr/bin/env python3
"""下载运行所需模型到 .voxos/models/（27MB 声纹 + 0.6MB VAD，不入 Git）。

用法：
    python scripts/fetch_models.py            # 自动尝试直连，失败再走系统代理
    python scripts/fetch_models.py --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".voxos" / "models"

MODELS = {
    "campplus.onnx": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "min_bytes": 20_000_000,
        "desc": "CAM++ 中文声纹嵌入（192 维，CPU 20ms 内完成推理）",
    },
    "silero_vad.onnx": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "min_bytes": 500_000,
        "desc": "Silero VAD（512 定长窗口，实时率约 0.4%）",
    },
    "sense-voice/model.int8.onnx": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
        "min_bytes": 200_000_000,
        "desc": "SenseVoice 离线识别（int8，中英日韩粤，239MB）",
    },
    "sense-voice/tokens.txt": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
        "min_bytes": 200_000,
        "desc": "SenseVoice 词表",
    },
}

# 装不上也不影响核心功能，只是识别会退回脚本回放。
OPTIONAL = {"sense-voice/model.int8.onnx", "sense-voice/tokens.txt"}


def download(url: str, target: Path, proxy: str | None) -> bool:
    if proxy:
        result = subprocess.run(
            ["curl", "-fsSL", "--http1.1", "--max-time", "300", "-x", proxy, "-o", str(target), url],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    opener = urllib.request.build_opener()
    try:
        with opener.open(url, timeout=120) as response, open(target, "wb") as handle:
            while chunk := response.read(1 << 16):
                handle.write(chunk)
        return True
    except Exception as exc:  # 网络问题种类很多，统一报告
        print(f"  直连失败：{exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-asr", action="store_true",
                        help="跳过 239MB 的识别模型（只要声纹与 VAD）")
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)
    failed, skipped = [], []
    for name, spec in MODELS.items():
        if args.no_asr and name in OPTIONAL:
            skipped.append(name)
            continue
        target = TARGET / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size >= spec["min_bytes"] and not args.force:
            print(f"✓ {name} 已存在（{target.stat().st_size / 1e6:.1f}MB）")
            continue
        print(f"↓ {name}：{spec['desc']}")
        ok = download(spec["url"], target, args.proxy) or download(spec["url"], target, None)
        if ok and target.stat().st_size >= spec["min_bytes"]:
            print(f"✓ {name}（{target.stat().st_size / 1e6:.1f}MB）")
        else:
            target.unlink(missing_ok=True)
            (skipped if name in OPTIONAL else failed).append(name)

    if skipped:
        print(f"\n· 可选模型未下载：{', '.join(skipped)}")
        print("  不影响核心功能，但 stt.backend=sense_voice 时会退回脚本回放。")
    if failed:
        print(f"\n⚠️ 下载失败：{', '.join(failed)}")
        print("  可手动下载后放入 .voxos/models/；缺失时系统会自动降级（能量 VAD + 频谱声纹），")
        print("  但声纹精度会下降，FRR/FAR 不再满足 PRD 指标。")
        return 1
    print("\n完成。下一步：voxos enroll --record 3 && voxos run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
