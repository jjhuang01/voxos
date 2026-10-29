"""对话层：这一句要不要出声、慢动作要不要先应一声、念出来该多长。

以前没有这一层：派发完把 reply 直接丢给 TTS。于是三件事混在调用点里——
写代码要等模型好几秒、期间一句反馈都没有（用户以为它没听见）；
回复里带 Markdown 和代码块会被逐字念出来；「关掉对话」只能关 `ui.speak`，
连「已停止」也一起哑掉。

这一层**只做策略，不做 I/O**：输入配置与文本，输出「说什么 / 说多长」。
真正出声仍归 `main._speak` 与 `tts.play`，所以这些规则在没有声卡的机器上可测。
"""

from __future__ import annotations

import re

from .config import Config

__all__ = ["MODES", "mode", "ack_after_s", "ack", "speech_limit", "shape", "is_chat"]

MODES = ("off", "assistant", "chat")

# 慢动作的确认语按意图分。全都说「收到」的话，用户听不出它是听懂了还是在敷衍；
# 而确认语本身很短，说得具体一点几乎不增加延迟。
_ACKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:写|改|实现|重构|补|修|加).{0,12}(?:代码|函数|方法|接口|文件|bug|测试)"
                r"|\.(?:py|ts|tsx|js|go|rs|java)\b", re.I), "收到，这就开始写"),
    (re.compile(r"(?:跑|执行|检查|查|看|扫).{0,10}(?:负载|磁盘|服务|日志|进程|端口|容器|状态)"),
     "收到，去看一下"),
    (re.compile(r"打开|启动|切换到|切到"), "好，打开"),
    (re.compile(r"(?:发|回)(?:个|条)?(?:消息|微信|邮件)|告诉|通知|转告"), "好，我来发"),
    (re.compile(r"提醒|定时|每天|每隔|备忘"), "好，记下了"),
    (re.compile(r"(?:查|搜|找)一下|帮我查|搜索"), "好，我去查"),
)

_DEFAULT_ACK = "收到，开始处理"

# 语音里念不出来的东西：代码块、链接、Markdown 记号。念出来既难听又浪费时间。
_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`([^`\n]*)`")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARK = re.compile(r"(?m)^\s*(?:#{1,6}|[-*+]|\d+[.)])\s+")
_BOLD = re.compile(r"[*_]{1,3}([^*_\n]+)[*_]{1,3}")
_SPACE = re.compile(r"\s+")
_SENTENCE_END = "。！？!?；;…"


def mode(cfg: Config) -> str:
    """对话模式：off（不出声）| assistant（短回执）| chat（可以聊）。

    未知值退回 `assistant` 而不是 `off`：配置写错不该让系统突然变成哑巴，
    那会被当成「坏了」，而且没有任何地方能看出是拼写问题。
    """
    value = str(cfg.get("dialogue.mode", "assistant")).strip().lower()
    return value if value in MODES else "assistant"


def is_chat(cfg: Config) -> bool:
    return mode(cfg) == "chat"


def ack_after_s(cfg: Config) -> float:
    """等多久还没结果就先应一声；0 表示关闭确认。"""
    try:
        ms = float(cfg.get("dialogue.ack_after_ms", 600))
    except (TypeError, ValueError):
        return 0.6
    return max(0.0, ms) / 1000.0


def ack(text: str, cfg: Config) -> str:
    """慢动作的第一句话。配置了固定文案就照说，否则按意图挑一句。"""
    fixed = str(cfg.get("dialogue.ack_text", "")).strip()
    if fixed:
        return fixed
    raw = (text or "").strip()
    for pattern, reply in _ACKS:
        if pattern.search(raw):
            return reply
    return _DEFAULT_ACK


def speech_limit(cfg: Config, mode_name: str | None = None) -> int:
    """念出来的字数上限。聊天模式允许更长——但也不是把整篇文章念完。"""
    key = "dialogue.chat_speech_chars" if (mode_name or mode(cfg)) == "chat" \
        else "dialogue.speech_chars"
    try:
        value = int(cfg.get(key, 240 if "chat" in key else 60))
    except (TypeError, ValueError):
        value = 60
    return max(16, value)


def _strip_markup(text: str) -> str:
    """把「看得懂但念不出」的记号换成人话。"""
    cleaned = _FENCE.sub("代码已显示在胶囊上。", text)
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _URL.sub("链接", cleaned)
    cleaned = _INLINE.sub(r"\1", cleaned)
    cleaned = _MARK.sub("", cleaned)
    cleaned = _BOLD.sub(r"\1", cleaned)
    return _SPACE.sub(" ", cleaned).strip()


def _cut(text: str, limit: int) -> str:
    """在句末截断；找不到句末就退到逗号，再不行才硬切。"""
    window = text[:limit]
    for marks in (_SENTENCE_END, "，,"):
        cut = max((window.rfind(mark) for mark in marks), default=-1)
        if cut >= max(8, limit // 3):
            return window[: cut + 1]
    return window


def shape(text: str, cfg: Config, mode_name: str | None = None) -> str:
    """回复 → 适合念出来的一段话。空回复返回空串（调用方据此不出声）。"""
    cleaned = _strip_markup(text or "")
    if not cleaned:
        return ""
    limit = speech_limit(cfg, mode_name)
    if len(cleaned) <= limit:
        return cleaned
    return _cut(cleaned, limit).rstrip("，,；;") + "……"
