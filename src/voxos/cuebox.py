"""cue 的文件投递箱：让脚本、cron、CI 不用拿到界面令牌也能推一条提示。

为什么不走 HTTP：界面令牌每次启动随机生成、只存在内存里。为了一个提醒把令牌落盘，
等于给本机任何进程一把钥匙；而文件投递箱的权限恰好等于用户自己的文件权限——
本来就能写的东西，不额外开一条越权通道。

这一层只管搬运：写进去（原子）、取出来（顺手扔掉坏的和太旧的）。
「该不该说」在 `cue.py`，「怎么说」在 `main`。混在一起就没法在无声卡机器上测。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .config import Config

__all__ = ["box_dir", "post", "take"]


def box_dir(cfg: Config) -> Path:
    return cfg.data_root / ".voxos" / "cues"


def post(cfg: Config, text: str, *, priority: str = "normal", key: str = "",
         source: str = "cli", now: float | None = None) -> Path:
    """写一条提示。先写临时文件再改名：轮询方永远读不到半个 JSON。"""
    text = text.strip()
    if not text:
        raise ValueError("提示内容是空的")
    stamp = time.time() if now is None else float(now)
    directory = box_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    entry = {"at": stamp, "text": text[:400], "priority": priority,
             "key": key, "source": source}
    path = directory / f"{int(stamp * 1000)}-{secrets.token_hex(3)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def take(cfg: Config, now: float | None = None) -> list[dict[str, object]]:
    """取出该投递的条目，取过就删。

    太旧的直接扔：三小时前的提醒现在说出来只会让人困惑，不如不说。
    坏文件也删——留着它只会让每次轮询都重读一个永远解析不了的东西。
    """
    stamp = time.time() if now is None else float(now)
    directory = box_dir(cfg)
    if not directory.is_dir():
        return []
    stale = _seconds(cfg, "cue.spool_stale_s", 300)
    fresh: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        entry = _read(path)
        path.unlink(missing_ok=True)
        if entry is not None and stamp - _at(entry) <= stale:
            fresh.append(entry)
    return fresh


def _read(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw if str(raw.get("text") or "").strip() else None


def _at(entry: dict[str, object]) -> float:
    """缺时间戳或写了字符串都当「很久以前」——宁可丢一条，不要重复念十条。"""
    try:
        return float(entry.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _seconds(cfg: Config, key: str, fallback: float) -> float:
    try:
        return max(1.0, float(cfg.get(key, fallback)))
    except (TypeError, ValueError):
        return float(fallback)
