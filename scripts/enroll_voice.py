#!/usr/bin/env python3
"""声纹录入（PDF 规范里的 scripts/enroll_voice.py）：读取 3 段语音，取特征均值存 npz。

直接用 CLI 更省事：voxos enroll --wav a.wav --wav b.wav --wav c.wav
本脚本是给"想看清每一步"的人用的显式版本。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voxos import audio, dsp  # noqa: E402
from voxos.config import load  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", action="append", required=True, help="3 段各 3-5 秒的 16k 单声道 wav")
    parser.add_argument("--mode", choices=["normal", "mask"], default="normal", help="normal=正常声线，mask=面罩/低语")
    args = parser.parse_args()

    cfg = load()
    embedder, threshold, kind = audio.make_embedder(cfg)
    samples = [audio.wav_to_array(Path(item).expanduser()) for item in args.wav]
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    path = cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz")
    lock.load(path)
    consistency = lock.enroll(samples, args.mode)
    lock.save(path)
    print(f"后端={kind} 阈值={threshold} 段数={len(samples)} 一致性={consistency:.3f} → {path}")
    if consistency < 0.8:
        print("⚠️ 一致性偏低，建议在安静环境重录（每段说满 3 秒）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
