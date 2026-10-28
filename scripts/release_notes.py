#!/usr/bin/env python3
"""从 CHANGELOG.md 抽出某个版本的段落，供 `gh release create --notes-file` 使用。

用法：python scripts/release_notes.py v0.1.14 > notes.md
      python scripts/release_notes.py 0.1.14

为什么不让 Release 说明手写：手写的东西和 CHANGELOG 会各说一套，而且发版时最容易
被跳过。这里宁可让缺段落直接报错——空白的 Release 页面比一条明确的错误更难发现。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"

FOOTER = """
---

**安装**（Python ≥ 3.11，macOS / Linux）

```bash
curl -fsSL https://raw.githubusercontent.com/jjhuang01/voxos/main/scripts/install.sh | bash
```

从本地包装（和上面等价，只是包自己下）：

```bash
pipx install voxos-{version}-py3-none-any.whl
pipx inject voxos sherpa-onnx sounddevice   # 音频依赖，跳过则降级为脚本回放
voxos doctor
```
"""


def section(text: str, version: str) -> str | None:
    """取 `## [x.y.z]` 到下一个 `## [` 之间的正文。"""
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: release_notes.py <版本，如 v0.1.14>", file=sys.stderr)
        return 2
    version = sys.argv[1].lstrip("v")
    if not CHANGELOG.exists():
        print(f"❌ 没有 {CHANGELOG.name}", file=sys.stderr)
        return 1
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if not body:
        # 不静默返回空：空白的 Release 说明在页面上看起来像"这一版没什么变化"。
        print(f"❌ CHANGELOG.md 里没有 {version} 这一段——先写再发版", file=sys.stderr)
        return 1
    print(body)
    print(FOOTER.format(version=version).rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
