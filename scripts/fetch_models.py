#!/usr/bin/env python3
"""下载运行所需模型（27MB 声纹 + 0.6MB VAD，不入 Git）。

真正的实现在 `voxos.models`——因为 `pipx install voxos` 之后 `scripts/` 不在包里，
装好的人必须也能下模型。这个脚本只是给「还没装、只在源码里」的场景留的入口。

用法：
    python scripts/fetch_models.py            # 自动尝试直连，失败再走系统代理
    python scripts/fetch_models.py --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voxos.models import fetch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-asr", action="store_true",
                        help="跳过 239MB 的识别模型（只要声纹与 VAD）")
    parser.add_argument("--dir", default=None, help="模型目录（默认 .voxos/models）")
    args = parser.parse_args()
    return fetch(args.dir, proxy=args.proxy, force=args.force, with_asr=not args.no_asr)


if __name__ == "__main__":
    raise SystemExit(main())
