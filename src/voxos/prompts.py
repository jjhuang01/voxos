"""内置提示词系统：随包一份、按角色叠加、改文件即生效。

**为什么提示词不进代码**：调提示词是配置工作，不是发版工作。上一版把它写成
`core.SYSTEM_PROMPT` 里的字符串常量，改一句「回复 ≤ 25 字」都要走一遍构建、
发版、`pipx upgrade`；而真正想改这句话的时刻，永远是「刚说完一句话它回得太长」。

三层来源，后者覆盖前者：

1. 代码里的 `DEFAULT_*`（唯一兜底，保证任何机器上都能起来）
2. 随包资源 `config/prompts/<name>.md`（跟着 wheel 走，`pipx upgrade` 会换）
3. 用户覆盖 `~/.voxos/prompts/<name>.md`（升级不丢，改完下一句就生效）

`<name>` 有两种：`dispatch`（调度内核，必用）和角色名（`developer`/`ops`/`doctor`，
按 `workspace.role` 自动叠加到 dispatch 后面）。角色文件缺失只是少了一段纪律，
不会让系统起不来——这正是「叠加」而不是「替换」的原因。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Config

__all__ = ["DEFAULT_DISPATCH", "NAMES", "active", "render", "role_prompt", "sources"]

DEFAULT_DISPATCH = """你是 VoxOS 的意图调度内核。运行角色：{role}
【环境】时间 {now} | 焦点窗口 {active_window} | 工作区 {workspace} | 最近文件 {recent_files}
【规则】
1. 输入来自语音转写，可能有同音错字，请结合上下文纠正技术术语（如「配置」→ config）。
2. 不是对你说的（屋里闲聊、电视声、听不清的碎片）：只输出 IGNORE，不调工具也不回复。
3. 明确在问你或让你做事：先判断能不能用工具完成；能就调用，不能就直接回答。
4. 一句话含多个意图时，同一回合发起多个 tool_calls；拿到结果后再判断要不要继续，
   最多 {max_rounds} 轮。不要为了凑轮次重复调用同一个工具。
5. 用户只说文件名时，优先在【最近文件】与【工作区】内解析，不要臆造路径。
6. 工具返回「需要确认」或「已拦截」时，把这句话原样告诉用户，不要自己重试。
7. 不解释过程、不输出 Markdown；最终回复 ≤ 25 字，会被 TTS 念出来。"""

DEFAULT_CHAT = """【对话模式】（与上文长度约束冲突时，以本段为准）
- 用户此刻是在聊天或请教，不一定是在派活：先回答，再考虑要不要用工具；
  不要为了「显得能干」而硬调工具。
- 回答可以长一点，但仍然说给人听：口语、短句，不要 Markdown、不要代码块。
- 需要展示代码或多行内容时，说一句「我放在胶囊上了」，正文留给屏幕。
- 不知道就说不知道；需要实时信息又没有合适的工具时，直说需要联网查，不要编。"""

DEFAULT_ROLES: dict[str, str] = {
    "developer": """【DEVELOPER 纪律】
- 改代码前先读文件；没读到内容就不要假装改过。
- 一次只改一处，改完用一句话说清「动了哪个文件、为什么」。
- 不执行 git commit / push / reset；那些由人自己来。""",
    "ops": """【OPS 纪律】
- 只读优先：先看日志、看负载，再谈动作。
- 任何写操作先说影响面（影响哪些服务、能不能回滚），再等人确认。
- 不使用 pkill / kill -9 / reboot 一类命令；重启服务用项目自己的脚本。""",
    "doctor": """【DOCTOR 纪律】
- 所有输出都是草稿，必须能被人一眼看出「这是机器写的」。
- 不下诊断结论、不给用药剂量；涉及身体不适一律建议就医。
- 病历与处方类工具永远只读，不代填、不代签。""",
}

NAMES = ("dispatch", "chat", "developer", "ops", "doctor")

# 只替换**认识的**占位符：用户手写提示词时很可能带 `{"a": 1}` 这种花括号，
# 用 str.format_map 会当场 KeyError，把一句闲聊变成一次异常。
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

# (路径, mtime) -> 文本。按 mtime 失效，所以「存盘 → 下一句生效」不需要重启。
_CACHE: dict[tuple[str, float], str] = {}


def _paths(cfg: Config, name: str) -> list[Path]:
    """按优先级从低到高：随包资源 → 用户覆盖。"""
    return [
        cfg.res_path("llm.prompts_dir", "config/prompts") / f"{name}.md",
        cfg.path("llm.user_prompts_dir", ".voxos/prompts") / f"{name}.md",
    ]


def _read(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_mtime)
    if key not in _CACHE:
        try:
            _CACHE[key] = path.read_text(encoding="utf-8").strip()
        except OSError:                     # 权限/编码问题：当作没有这份文件
            return ""
    return _CACHE[key]


def render(name: str, cfg: Config, **values: Any) -> str:
    """取一段提示词并填占位符。`name` 不存在时返回空串（调用方决定怎么兜底）。"""
    text = ""
    for path in _paths(cfg, name):
        text = _read(path) or text
    if not text:
        return ""
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def role_prompt(cfg: Config, **values: Any) -> str:
    """角色那一段。没有对应文件、或角色是 `*` 时返回空串。"""
    role = str(cfg.get("workspace.role", "DEVELOPER")).strip().lower()
    if role not in DEFAULT_ROLES:
        return ""
    return render(role, cfg, **values)


def active(cfg: Config, **values: Any) -> str:
    """真正发给模型的那段 system：dispatch（+ 对话模式）+ 角色纪律。"""
    base = render("dispatch", cfg, **values) or DEFAULT_DISPATCH
    if str(values.get("mode", "assistant")).strip().lower() == "chat":
        base = f"{base}\n\n{render('chat', cfg, **values) or DEFAULT_CHAT}"
    role = role_prompt(cfg, **values) or DEFAULT_ROLES.get(
        str(cfg.get("workspace.role", "")).strip().lower(), "")
    return f"{base}\n\n{role}" if role else base


def sources(cfg: Config) -> dict[str, str]:
    """每段提示词实际来自哪个文件（`builtin` 表示用的代码里那份）。

    `voxos prompts` 用它回答「我改了文件怎么没反应」——先看它读的是不是那个路径。
    """
    found: dict[str, str] = {}
    for name in NAMES:
        hit = next((p for p in reversed(_paths(cfg, name)) if _read(p)), None)
        found[name] = str(hit) if hit else "builtin"
    return found
