#!/usr/bin/env python3
"""生成 App 图标（1024×1024 PNG），只用标准库 + numpy。

为什么自己画而不放一个 png 进仓库：图标只有 4 根竖条，用有符号距离场渲染
比维护一堆二进制素材更小、更能改。设计取自 DESIGN_SYSTEM.md 的语义色：
深底 + `--ok #30D158`（聆听状态的颜色）。

用法：python scripts/make_icon.py build/icon.png
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

SIZE = 1024
BG = (0x1A, 0x1C, 0x20)          # 胶囊页面的深底
BAR = (0x30, 0xD1, 0x58)         # --ok：聆听 / 通过
EDGE = (0x2C, 0x30, 0x36)        # 一点点描边，深色桌面上不会糊成一团


def _rounded_box(px: np.ndarray, py: np.ndarray, cx: float, cy: float,
                 half_w: float, half_h: float, radius: float) -> np.ndarray:
    """有符号距离场（像素单位）：<0 在内部。"""
    qx = np.abs(px - cx) - (half_w - radius)
    qy = np.abs(py - cy) - (half_h - radius)
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    inside = np.minimum(np.maximum(qx, qy), 0)
    return outside + inside - radius


def _coverage(distance: np.ndarray) -> np.ndarray:
    """距离以**像素**为单位时，0.5px 的过渡带才等于一条软边。

    第一版在 0..1 的归一化坐标里做这件事，0.5 变成了半个画布——
    于是整张图糊成一团灰。单位错了，形状再对也看不出来。
    """
    return np.clip(0.5 - distance, 0.0, 1.0)


def _bars(px: np.ndarray, py: np.ndarray, half: float) -> np.ndarray:
    """四根竖条，高度像一段正在收听的波形（比例相对底板，换尺寸不变形）。"""
    heights = (0.30, 0.56, 0.78, 0.46)
    half_w = 0.052 * half
    gap = 0.042 * half
    span = len(heights) * 2 * half_w + (len(heights) - 1) * gap
    left = SIZE / 2 - span / 2 + half_w
    mask = np.zeros_like(px)
    for index, height in enumerate(heights):
        center_x = left + index * (2 * half_w + gap)
        mask = np.maximum(mask, _coverage(_rounded_box(
            px, py, center_x, SIZE / 2, half_w, height * half, half_w)))
    return mask


def render() -> np.ndarray:
    axis = np.arange(SIZE, dtype=np.float32) + 0.5
    px, py = np.meshgrid(axis, axis)          # 像素坐标
    canvas = np.zeros((SIZE, SIZE, 4), dtype=np.float32)
    half = 0.410 * SIZE

    plate = _coverage(_rounded_box(px, py, SIZE / 2, SIZE / 2, half, half, 0.185 * SIZE))
    rim = plate - _coverage(_rounded_box(px, py, SIZE / 2, SIZE / 2,
                                         half - 6, half - 6, 0.185 * SIZE - 6))
    bars = _bars(px, py, half)

    for channel, value in enumerate(BG):
        canvas[..., channel] = value / 255.0
    for channel, value in enumerate(EDGE):
        canvas[..., channel] = canvas[..., channel] * (1 - rim) + (value / 255.0) * rim
    for channel, value in enumerate(BAR):
        canvas[..., channel] = canvas[..., channel] * (1 - bars) + (value / 255.0) * bars
    canvas[..., 3] = plate
    return (np.clip(canvas, 0, 1) * 255).astype(np.uint8)


def write_png(path: Path, image: np.ndarray) -> None:
    """最小 PNG 编码：8bit RGBA、无滤波。够用，也就不需要 Pillow。"""
    height, width, _ = image.shape
    raw = b"".join(b"\x00" + image[row].tobytes() for row in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon.png")
    write_png(target, render())
    print(f"图标已生成：{target}（{SIZE}×{SIZE}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
