#!/usr/bin/env python3
"""声纹录入兼容入口，与 voxos enroll 共用逐句质量、最差留一与保存门禁。

也可直接运行：voxos enroll --wav a.wav --wav b.wav --wav c.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voxos.config import load  # noqa: E402
from voxos.main import cmd_enroll  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", action="append", required=True, help="3 段各 3-5 秒的 16k 单声道 wav")
    parser.add_argument("--mode", choices=["normal", "mask"], default="normal", help="normal=正常声线，mask=面罩/低语")
    args = parser.parse_args()

    args.script = False
    args.check = False
    return cmd_enroll(args, load())


if __name__ == "__main__":
    raise SystemExit(main())
