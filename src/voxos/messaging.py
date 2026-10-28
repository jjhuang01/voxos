"""把「给某人发一条消息」翻译成 AppleScript。

**为什么只能靠模拟按键**：微信 Mac 版没有 AppleScript 字典——`sdef
/Applications/WeChat.app` 返回空，拿不到 `send message to buddy` 这种接口。
企业微信同理。能走通的只有 System Events 模拟按键：激活 → 搜索联系人 →
回车进会话 → 输入正文 → 回车发送。这条路不是我想出来的，是社区里
wechat-auto-sender 那类脚本唯一跑通的做法，所以照抄，不发明。

代价也照实说，不假装成功：需要「系统设置 → 隐私与安全性 → 辅助功能」里
勾上运行 VoxOS 的那个程序（终端 / VoxOS.app），并且发送期间窗口会被抢焦点。
两件事都在返回值里说清楚，而不是回一句「已发送」。
"""

from __future__ import annotations

__all__ = ["APPS", "app_of", "script", "quote", "permission_hint"]

# 应用名 → （进程名，搜索快捷键的修饰键）
# 微信/企业微信是 ⌘⇧F（微信自己的「搜索」），iMessage 是 ⌘F。
APPS: dict[str, tuple[str, str]] = {
    "微信": ("WeChat", "command down, shift down"),
    "wechat": ("WeChat", "command down, shift down"),
    "企业微信": ("WeCom", "command down, shift down"),
    "wecom": ("WeCom", "command down, shift down"),
    "信息": ("Messages", "command down"),
    "imessage": ("Messages", "command down"),
    "messages": ("Messages", "command down"),
}

# osascript 权限不足时的特征串 → 该去点哪儿。
# `-10004 权限违例` 是本机实测到的那个：中文系统上 osascript 不会说
# 「not allowed assistive access」，只说「发生权限违例」，所以必须一起认。
_HINTS = (
    ("-1743", "系统设置 → 隐私与安全性 → 辅助功能"),
    ("-10004", "系统设置 → 隐私与安全性 → 辅助功能"),
    ("权限违例", "系统设置 → 隐私与安全性 → 辅助功能"),
    ("-25211", "系统设置 → 隐私与安全性 → 辅助功能"),
    ("not allowed assistive", "系统设置 → 隐私与安全性 → 辅助功能"),
    ("-10827", "系统设置 → 隐私与安全性 → 自动化"),
    ("not authorized", "系统设置 → 隐私与安全性 → 自动化"),
    ("Application isn't running", "目标应用没在运行，先说「打开微信」"),
)


def app_of(name: str) -> tuple[str, str] | None:
    """认得的应用返回（进程名，修饰键），认不得返回 None——不猜。"""
    return APPS.get(str(name or "").strip().lower())


def quote(text: str) -> str:
    """AppleScript 字符串字面量：转义反斜杠与引号，换行拼成 linefeed。

    直接 `f'"{text}"'` 会在内容带引号时把脚本撕成两半，然后 osascript 报一个
    和真正原因无关的语法错。
    """
    parts = str(text).splitlines() or [""]
    escaped = [part.replace("\\", "\\\\").replace('"', '\\"') for part in parts]
    if len(escaped) == 1:
        return f'"{escaped[0]}"'
    return " & linefeed & ".join(f'"{part}"' for part in escaped)


def script(process: str, keys: str, contact: str, text: str, wait: float = 0.6) -> str:
    """生成发送脚本。`wait` 是每步之间的等待：微信搜索是异步的，按太快会丢字。"""
    return "\n".join([
        f'tell application "{process}" to activate',
        f"delay {wait}",
        'tell application "System Events"',
        f'  tell process "{process}"',
        f'    keystroke "f" using {{{keys}}}',
        f"    delay {wait}",
        f"    keystroke {quote(contact)}",
        f"    delay {wait * 2}",
        "    key code 36",
        f"    delay {wait * 1.6}",
        f"    keystroke {quote(text)}",
        f"    delay {wait / 2}",
        "    key code 36",
        "  end tell",
        "end tell",
        "",
    ])


def permission_hint(stderr: str) -> str:
    """从 osascript 的报错里认出「是权限问题」并给出该点哪儿；认不出返回空串。"""
    text = str(stderr or "")
    for marker, hint in _HINTS:
        if marker.lower() in text.lower():
            return hint
    return ""
