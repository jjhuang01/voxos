"""业务插件层：每个能力都是一个带 `@tool` 的普通函数，不超过 15 行。

新增能力只需在此追加一个函数（见 CODING_PHILOSOPHY.md §3「函数即插件」），
不需要改 core.py、不需要手写 JSON Schema、不需要改 Prompt。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from .config import Config, load
from .core import REGISTRY, restore_latest, tool

__all__ = ["RT", "configure", "append_structured_log", "dispatch_code_session",
           "inspect_server_ops", "write_code", "rollback_file", "read_file_head",
           "open_in_ide", "search_notes", "list_tools", "delegate_to_coding_cli"]


@dataclass
class Runtime:
    """工具层共享的运行时上下文（配置 + 最近文件），避免全局散落。"""

    cfg: Config = field(default_factory=load)
    recent_files: list[str] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.cfg.flag("workspace.dry_run", True)


RT = Runtime()


def configure(cfg: Config) -> Runtime:
    """守护进程启动时调用一次，注入配置。"""
    global RT
    RT = Runtime(cfg=cfg, recent_files=RT.recent_files)
    return RT


def _resolve(raw: str) -> Path:
    path = Path(os.path.expanduser(raw.strip()))
    if path.is_absolute():
        return path
    return RT.cfg.path("workspace.default_code_dir", "~/Projects") / path


def _remember(path: Path) -> None:
    text = str(path)
    if text in RT.recent_files:
        RT.recent_files.remove(text)
    RT.recent_files.append(text)
    del RT.recent_files[:-10]


# ------------------------------------------------------------------ 写日志

@tool(fast=r"(?:我要|帮我|给我)?(?:写|记)(?:一条|一个|个)?(?:开发)?日志[，,：:\s]*(.+)$", risk="green")
def append_structured_log(
    content: str,
    category: Literal["BUG_FIX", "IDEA", "PROGRESS", "TODO", "DEV"] = "DEV",
    project: str = "General",
) -> str:
    """把口述内容按用户模板追加到今日 Markdown 开发日志。

    Args:
        content: 整理后的日志正文（Markdown 可用）
        category: 日志分类标签
        project: 关联项目或模块名，未提及填 General
    """
    log_dir = RT.cfg.path("workspace.daily_log_dir", "~/Documents/DevLogs")
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    log_file = log_dir / f"{now:%Y-%m-%d}.md"
    if not log_file.exists():
        log_file.write_text(f"# {now:%Y-%m-%d} 开发日志\n", encoding="utf-8")
    template = str(RT.cfg.get("workspace.log_template", "- **[{time}] [{category}]** ({project}) {content}"))
    entry = template.format(time=f"{now:%H:%M:%S}", category=category, project=project, content=content.strip())
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
    return f"日志已记录（{category}）"


# ------------------------------------------------------------------ 编程会话

@tool(fast=r"(?:我要|帮我)?(?:写代码|改代码|继续写|续接)[，,：:\s]*(.+)$", risk="yellow")
def dispatch_code_session(
    instruction: str,
    session_name: str = "main_coding",
    new_session: bool = False,
) -> str:
    """把编程需求派发到本地 tmux 会话中的 Claude Code / Aider（自动续接上下文）。

    Args:
        instruction: 用户的编程需求原文
        session_name: tmux 会话名，默认 main_coding
        new_session: True 时强制新建会话
    """
    project = RT.cfg.path("workspace.default_code_dir", "~/Projects")
    cli = str(RT.cfg.get("workspace.code_cli", "claude"))
    inner = [cli, "-c", "-p", instruction] if cli == "claude" else [cli, "--message", instruction]
    if not shutil.which("tmux"):
        return f"未安装 tmux，无法派发。手动执行：{' '.join(inner)}"
    if RT.dry_run:
        return f"（dry-run）将在 {session_name} 会话执行：{' '.join(inner)}"
    exists = subprocess.run(["tmux", "has-session", "-t", session_name],
                            capture_output=True, timeout=5).returncode == 0
    if not exists or new_session:
        subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(project)],
                       check=False, timeout=10)
    subprocess.run(["tmux", "send-keys", "-t", session_name, " ".join(inner), "Enter"],
                   check=False, timeout=10)
    return f"已派发到编程会话 {session_name}"


# ------------------------------------------------------------------ 只读运维

@tool(fast=r"(?:检查|看看|巡检|查一下)(?:一下)?(?:服务器|系统|线上|磁盘|容器)?[，,：:\s]*(.*)$", risk="green")
def inspect_server_ops(query: str = "") -> str:
    """只读巡检本机/服务器负载、磁盘与容器状态，返回简短语音简报。

    Args:
        query: 用户关注点，如“流量”“错误日志”“磁盘”
    """
    load1, load5, _ = os.getloadavg()
    disk = shutil.disk_usage(Path.home())
    used_pct = disk.used / disk.total * 100
    containers = _docker_count()
    summary = f"负载 {load1:.1f}/{load5:.1f}，磁盘 {used_pct:.0f}%，容器 {containers} 个在跑"
    if any(word in query for word in ("监控", "流量", "看板")):
        url = str(RT.cfg.get("workspace.grafana_url", ""))
        if url and not RT.dry_run:
            webbrowser.open(url)
            summary += "，已打开监控页"
    return summary


def _docker_count() -> int:
    if not shutil.which("docker"):
        return 0
    try:
        out = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=3)
        return len([line for line in out.stdout.splitlines() if line.strip()])
    except (subprocess.SubprocessError, OSError):
        return 0


# ------------------------------------------------------------------ 代码文件

@tool(risk="yellow", backup_arg="file_path")
def write_code(
    file_path: str,
    operation: Literal["CREATE_OR_OVERWRITE", "APPEND_CODE", "REPLACE_SNIPPET"],
    new_code: str = "",
    target_snippet: str = "",
) -> str:
    """按操作类型创建/追加/替换代码文件，写前由安全网关自动快照。

    Args:
        file_path: 目标文件（绝对路径或相对默认代码目录）
        operation: 操作类型
        new_code: 要写入的代码
        target_snippet: REPLACE_SNIPPET 时必填，必须唯一匹配
    """
    target = _resolve(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation == "CREATE_OR_OVERWRITE":
        target.write_text(new_code, encoding="utf-8")
        return f"已写入 {target.name}（{len(new_code.splitlines())} 行）"
    if operation == "APPEND_CODE":
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n" + new_code + "\n")
        return f"已追加到 {target.name}"
    if not target.exists():
        return f"错误：{target.name} 不存在，无法替换片段"
    raw = target.read_text(encoding="utf-8")
    hits = raw.count(target_snippet) if target_snippet else 0
    if hits != 1:
        return f"错误：{target.name} 中匹配到 {hits} 处 target_snippet（必须恰好 1 处），已中止"
    target.write_text(raw.replace(target_snippet, new_code, 1), encoding="utf-8")
    return f"已在 {target.name} 中替换 1 处片段"


@tool(fast=r"(?:撤销|回滚|还原)(?:刚才|上次)?(?:的)?(?:修改|改动|代码)?$", risk="yellow")
def rollback_file(file_path: str = "") -> str:
    """把最近一次被修改的文件恢复到快照版本。

    Args:
        file_path: 要回滚的文件名（留空则回滚最近一次修改）
    """
    backup_dir = RT.cfg.path("workspace.backup_dir", ".voxos_backup")
    name = Path(os.path.expanduser(file_path)).name if file_path.strip() else None
    try:
        restored = restore_latest(backup_dir, name)
    except FileNotFoundError as exc:
        return str(exc)
    return f"已回滚 {restored.name} 到修改前版本"


@tool(risk="green")
def read_file_head(file_path: str, lines: int = 40) -> str:
    """读取文件前若干行（只读，用于“看看这个文件”）。

    Args:
        file_path: 文件路径
        lines: 行数，默认 40
    """
    target = _resolve(file_path)
    if not target.exists():
        return f"文件不存在：{target}"
    _remember(target)
    head = target.read_text(encoding="utf-8", errors="replace").splitlines()[: max(1, int(lines))]
    return f"{target.name}（前 {len(head)} 行）\n" + "\n".join(head)


@tool(risk="green")
def open_in_ide(file_path: str, line: int = 1) -> str:
    """在 VS Code / Cursor 中打开文件并定位到指定行。

    Args:
        file_path: 文件路径
        line: 定位行号
    """
    target = _resolve(file_path)
    ide = str(RT.cfg.get("workspace.ide_command", "code"))
    _remember(target)
    if RT.dry_run:
        return f"（dry-run）将执行：{ide} -g {target}:{line}"
    if not shutil.which(ide):
        return f"未找到命令 {ide}，请安装或修改 workspace.ide_command"
    subprocess.Popen([ide, "-g", f"{target}:{line}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"已在 {ide} 打开 {target.name}:{line}"


# ------------------------------------------------------------------ 笔记与自省

@tool(risk="green")
def search_notes(query: str, root: str = "") -> str:
    """在本地 Markdown 笔记/日志中检索关键词（只读）。

    Args:
        query: 关键词
        root: 检索根目录，留空则用日志目录
    """
    base = Path(os.path.expanduser(root)) if root else RT.cfg.path("workspace.daily_log_dir", "~/Documents/DevLogs")
    if not base.exists():
        return f"目录不存在：{base}"
    hits: list[str] = []
    for path in sorted(base.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if query and query in line:
                hits.append(f"{path.name}:{number} {line.strip()[:60]}")
                break
        if len(hits) >= 3:
            break
    return "未找到相关笔记" if not hits else "；".join(hits)


@tool(risk="green")
def list_tools() -> str:
    """列出当前已注册的工具及其风险等级（自省）。"""
    rows = [f"{name}[{meta.risk.value.lower()}]" for name, meta in sorted(REGISTRY.items())]
    return f"共 {len(rows)} 个工具：" + "、".join(rows)


@tool(risk="red_confirm")
def delegate_to_coding_cli(prompt: str, project_dir: str = "", cli: str = "claude") -> str:
    """把复杂重构委托给本地 Coding CLI（tmux 后台跑，需二次确认）。

    Args:
        prompt: 交给 Coding CLI 的完整需求
        project_dir: 工程目录，留空用默认代码目录
        cli: claude | aider | codex
    """
    workdir = Path(os.path.expanduser(project_dir)) if project_dir else RT.cfg.path("workspace.default_code_dir", "~/Projects")
    command = [cli, "-p", prompt] if cli == "claude" else [cli, "--message", prompt]
    if RT.dry_run:
        return f"（dry-run）将在 {workdir} 执行：{' '.join(command)}"
    if not shutil.which("tmux") or not shutil.which(cli):
        return f"缺少 tmux 或 {cli}，无法委托"
    subprocess.run(["tmux", "new-session", "-d", "-s", "voxos_delegate", "-c", str(workdir),
                    " ".join(command)], check=False, timeout=10)
    return f"已委托 {cli} 在后台执行，会话名 voxos_delegate"
