"""主动提示（cue）的策略内核：什么时候可以主动出声。

Cue 不等于定时任务。定时任务是到点**执行一句话**（可能有副作用，走安全网关）；
cue 是「到点或遇到条件时只提醒」：说一句、胶囊上显示一行，不执行动作。
两者必须分开——执行错了会改文件、发消息；cue 说错了只是烦人。所以 cue 需要的
是**降噪策略**（静音时段、频率上限、去重），而不是二次确认。

这一层只裁决「该不该说」，不负责投递。投递统一走 `main._speak_async`，
这样主动提示和被动回话共享同一套回声门、打断与静音语义，不会出现
「回话能被打断、提醒不能」这种两套行为。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .config import Config

__all__ = ["Cue", "CueGuard", "in_quiet_hours", "quiet_window", "PRIORITIES"]

PRIORITIES = ("low", "normal", "high")


@dataclass(frozen=True)
class Cue:
    """一条待投递的提示。`key` 相同的提示在去重窗口内只出现一次。"""

    text: str
    priority: str = "normal"
    key: str = ""
    source: str = "manual"


def quiet_window(cfg: Config) -> tuple[int, int]:
    """把 `"23:00-07:00"` 解析成分钟数。写错就当没有静音时段（宁可多说）。"""
    raw = str(cfg.get("cue.quiet_hours", "")).strip()
    if "-" not in raw:
        return -1, -1
    left, _, right = raw.partition("-")
    start, end = _minutes(left), _minutes(right)
    if start < 0 or end < 0 or start == end:
        return -1, -1
    return start, end


def _minutes(text: str) -> int:
    hour, _, minute = text.strip().partition(":")
    if not hour.isdigit() or (minute and not minute.isdigit()):
        return -1
    h, m = int(hour), int(minute or 0)
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else -1


def in_quiet_hours(cfg: Config, when: datetime) -> bool:
    """跨午夜的窗口也要对：23:00-07:00 是一段，不是「起点大于终点就无效」。"""
    start, end = quiet_window(cfg)
    if start < 0:
        return False
    now = when.hour * 60 + when.minute
    return start <= now < end if start < end else (now >= start or now < end)


class CueGuard:
    """频率与去重。状态只在内存：进程重启就重新开始，这是有意的。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._sent: deque[float] = deque()
        self._keys: dict[str, float] = {}

    def allow(self, cue: Cue, now: float | None = None) -> tuple[bool, str]:
        """返回（能不能说，不能说的原因）。原因要能直接上屏，别写英文错误码。"""
        stamp = time.time() if now is None else float(now)
        text = cue.text.strip()
        if not text:
            return False, "空提示"
        high = cue.priority == "high"
        if not high and in_quiet_hours(self.cfg, datetime.fromtimestamp(stamp)):
            return False, "静音时段"
        if self._dedupe(cue, stamp):
            return False, "重复提示"
        if self._within_hour(stamp) >= self._limit("cue.max_per_hour", 6):
            return False, "本小时提示次数已达上限"
        if not high and self._sent and \
                stamp - self._sent[-1] < self._limit("cue.min_interval_s", 90):
            return False, "距上一条太近"
        self._sent.append(stamp)
        if cue.key:
            self._keys[cue.key] = stamp
        return True, ""

    def _dedupe(self, cue: Cue, stamp: float) -> bool:
        if not cue.key:
            return False
        last = self._keys.get(cue.key)
        return last is not None and stamp - last < self._limit("cue.dedupe_s", 21600)

    def _within_hour(self, stamp: float) -> int:
        while self._sent and stamp - self._sent[0] >= 3600:
            self._sent.popleft()
        return len(self._sent)

    def _limit(self, key: str, fallback: float) -> float:
        try:
            return float(self.cfg.get(key, fallback))
        except (TypeError, ValueError):
            return float(fallback)
