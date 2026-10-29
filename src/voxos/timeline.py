"""会话时间线：把「我说了什么 → 它想干什么 → 干了什么 → 结果」串成一条。

## 为什么现有的两份日志答不了这个问题

用户问的是「我说了十句，为什么一句都没执行」。手上有两份日志，都答不了：

- `audit.jsonl` 只有**工具调用**。被丢弃的句子根本没调工具，所以一条都没有。
  它能回答「做过什么」，不能回答「没做什么、为什么」。
- `status.jsonl` 只有**状态变化**。`executing` 那条带着文本，但它和审计里的
  工具调用之间没有 ID 关联，只能靠时间戳猜；而被丢弃的句子连状态都没有。

所以「没反应」的时候，两份日志加起来仍然是空白。这不是日志不够多，是**少了
一个以「一次对话回合」为单位的记录**。

## 一次回合，一条记录

`Turn` 覆盖四种结局，缺一种就会出现「查不到」的黑洞：

| outcome | 含义 | 用户看到什么 |
| :--- | :--- | :--- |
| `dropped` | 音频层就丢了（声纹/太短/空转写/回声门） | 现在：**什么都没有** |
| `missed` | 进了派发，但没匹配上任何工具 | 「没听懂」 |
| `acted` | 调了工具，成功 | 「完成」+ 结果 |
| `failed` | 调了工具，报错 | 「失败」+ 原因 |
| `chatting` | 闲聊 / LLM 直接回话 | 「回话」 |

`dropped` 这一行是重点：**它以前完全不存在**。`main.py` 里一句
`if not utterance.accepted: continue` 就把原因（`Utterance.reason`）扔掉了，
用户只看到胶囊上那句字幕，然后什么都没有——最容易被误判成「软件坏了」。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Turn", "record", "recent", "path_of", "OUTCOMES"]

OUTCOMES = ("dropped", "missed", "acted", "failed", "chatting")

_LOCK = threading.Lock()
"""同一进程内串行写。跨进程（守护进程 + CLI）靠 `O_APPEND` 的单行原子性——
一行 JSON 远小于 `PIPE_BUF`（4096），追加不会互相截断。"""


@dataclass
class Turn:
    """一次对话回合。字段顺序就是「人读它的时候想知道什么的顺序」。"""

    ts: str = ""
    outcome: str = "acted"
    text: str = ""                 # 用户说的。丢弃时是已经识别出来的半句
    score: float = 0.0             # 声纹相似度
    audio_ms: float = 0.0
    route: str = ""                # fast | llm | offline | chat | ignore
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    risk: str = ""
    ok: bool = True
    reply: str = ""
    reason: str = ""               # 丢弃 / 失败的原因，人话
    ms: float = 0.0

    def line(self) -> str:
        """给终端看的一行。胶囊用 JSON，人看这个。

        声纹和音频长度只在**有音频**时才显示：打字入口没有这两项，硬报一个
        `声纹0.00 / 0ms` 只会让人以为「声纹坏了」。
        """
        head = {"dropped": "丢弃", "missed": "没懂", "acted": "执行",
                "failed": "失败", "chatting": "回话"}.get(self.outcome, self.outcome)
        what = self.tool or self.route or "-"
        detail = self.reason or self.reply or ""
        voice = f" 声纹{self.score:.2f}" if self.score > 0 else ""
        span = f"{self.audio_ms:>5.0f}ms" if self.audio_ms > 0 else "     -"
        return f"{self.ts[11:19]} {head:<3} {span}{voice:<9} {what:<18} {detail[:58]}"


def path_of(cfg: Any) -> Path:
    """时间线落在哪。和审计同目录——它们是同一次操作的两面。"""
    return cfg.path("audit.timeline_path", ".voxos/timeline.jsonl")


def record(turn: Turn, path: Path | None = None, cfg: Any = None) -> Turn:
    """追加一条。**写失败不能影响主链路**——记日志不该让一次执行失败。"""
    if not turn.ts:
        turn.ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    target = path or (path_of(cfg) if cfg is not None else None)
    if target is None:
        return turn
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(turn), ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[voxos] 时间线写入失败（忽略）：{exc}")
    return turn


def recent(path: Path | None = None, cfg: Any = None, limit: int = 50) -> list[dict[str, Any]]:
    """最近的回合，**新的在前**。读坏了就返回已经读到的部分，不抛。"""
    target = path or (path_of(cfg) if cfg is not None else None)
    if target is None or not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        # 从文件尾读：时间线可能有几万行，全读进内存只为看最后 50 条不划算。
        with open(target, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, 64 * 1024 * max(1, limit // 10))
            handle.seek(size - block)
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in chunk.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                   # 半行（进程被杀）跳过，不影响其余的
    return list(reversed(rows))
