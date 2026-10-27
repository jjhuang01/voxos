#!/usr/bin/env python3
"""构建降级声纹用的背景模型（UBM）：从验收语料统计各维均值/方差。

降级路径（SpectralEmbedder）没有 CAM++ 时使用；有真模型则完全不参与。
产物 `config/ubm.npz` 只有几 KB，可以入库。

用法：python scripts/build_ubm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from voxos import corpus, dsp  # noqa: E402


def main() -> int:
    root = ROOT / ".voxos" / "corpus"
    manifest = corpus.ensure_corpus(root)
    clips = [corpus.load_clip(root, entry) for entry in manifest["owner"]["clips"]]  # type: ignore[index]
    for entry in manifest["impostors"]:  # type: ignore[index]
        clips += [corpus.load_clip(root, clip) for clip in entry["clips"]]
    features = np.stack([dsp.spectral_features(clip) for clip in clips])
    target = ROOT / "config" / "ubm.npz"
    np.savez(target, mu=features.mean(axis=0).astype(np.float32),
             sd=(features.std(axis=0) + 1e-3).astype(np.float32))
    print(f"已写入 {target}（{len(clips)} 段语料，维度 {features.shape[1]}，{target.stat().st_size / 1024:.1f}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
