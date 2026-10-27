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
}


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
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, spec in MODELS.items():
        target = TARGET / name
        if target.exists() and target.stat().st_size >= spec["min_bytes"] and not args.force:
            print(f"✓ {name} 已存在（{target.stat().st_size / 1e6:.1f}MB）")
            continue
        print(f"↓ {name}：{spec['desc']}")
        ok = download(spec["url"], target, args.proxy) or download(spec["url"], target, None)
        if ok and target.stat().st_size >= spec["min_bytes"]:
            print(f"✓ {name}（{target.stat().st_size / 1e6:.1f}MB）")
        else:
            target.unlink(missing_ok=True)
            failed.append(name)

    if failed:
        print(f"\n⚠️ 下载失败：{', '.join(failed)}")
        print("  可手动下载后放入 .voxos/models/；缺失时系统会自动降级（能量 VAD + 频谱声纹），")
        print("  但声纹精度会下降，FRR/FAR 不再满足 PRD 指标。")
        return 1
    print("\n完成。下一步：voxos enroll --record 3 && voxos run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
