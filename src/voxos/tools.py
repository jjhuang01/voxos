"""业务插件层：每个能力都是一个带 `@tool` 的普通函数。

新增能力只需在此追加一个函数（见 CODING_PHILOSOPHY.md §3「函数即插件」），
不需要改 core.py、不需要手写 JSON Schema、不需要改 Prompt。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import messaging, sandbox, scheduler
from .config import Config, load
from .core import REGISTRY, restore_latest, tool

__all__ = ["RT", "configure", "append_structured_log", "dispatch_code_session",
           "inspect_server_ops", "write_code", "rollback_file", "read_file_head",
           "open_in_ide", "search_notes", "list_tools", "delegate_to_coding_cli",
           "open_app", "tell_time", "run_command", "open_path", "send_message",
           "schedule_task", "list_schedules", "cancel_schedule"]


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
    if not path.is_absolute():
        path = RT.cfg.path("workspace.default_code_dir", "~/Projects") / path
    if not RT.cfg.path_allowed(path):
        raise PermissionError(f"路径不在允许的工作区内：{path}")
    return path


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
    if RT.dry_run:
        return _preview_code_session(inner, session_name)
    if not shutil.which("tmux"):
        return f"未安装 tmux，无法派发。手动执行：{' '.join(inner)}"
    return _send_to_tmux(inner, session_name, project, new_session)


def _preview_code_session(inner: list[str], session_name: str) -> str:
    """演练模式先于依赖检查：它的用途就是「先看看会执行什么」，
    而一台还没装 tmux 的机器恰恰最需要先看。真派发才要求 tmux 在。
    """
    missing = "" if shutil.which("tmux") else "（注意：本机没有 tmux，真派发会失败）"
    return f"（dry-run）将在 {session_name} 会话执行：{' '.join(inner)}{missing}"


def _send_to_tmux(inner: list[str], session_name: str, project: Path, new_session: bool) -> str:
    # tmux 的 send-keys 是把文本送进一个**活着的 shell**，外层 subprocess 用 argv
    # 不等于最终执行安全。这里先按 shell 规则引用每个参数，再以 -l 原样发送；
    # 否则一句带空格的话就会被拆成多个参数，`$(...)` 也会被 shell 展开。
    command = shlex.join(inner)
    exists = subprocess.run(["tmux", "has-session", "-t", session_name],
                            capture_output=True, timeout=5).returncode == 0
    if not exists or new_session:
        created = subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(project)],
                                 check=False, timeout=10)
        if created.returncode != 0:
            return f"派发失败：无法创建 tmux 会话 {session_name}"
    sent = subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", "--", command],
                          check=False, timeout=10)
    if sent.returncode != 0:
        return f"派发失败：无法向 {session_name} 写入指令"
    entered = subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"],
                             check=False, timeout=10)
    if entered.returncode != 0:
        return f"派发失败：指令已写入但没能提交（{session_name}）"
    return f"已派发到编程会话 {session_name}"


# ------------------------------------------------------------------ 只读运维

@tool(fast=r"(?:检查|看看|巡检|查一下)(?:一下)?(?:服务器|系统|线上|磁盘|容器|负载|流量|监控|内存)[，,：:\s]*(.*)$", risk="green")
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
    # 逐行读前 N 行：旧实现先 read_text() 整个文件再切片，读 2GB 日志等于把内存打满。
    limit = max(1, min(int(lines), 500))
    head: list[str] = []
    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            head.append(line.rstrip("\n"))
            if len(head) >= limit:
                break
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
    if root:
        base = _resolve(root)
    else:
        base = RT.cfg.path("workspace.daily_log_dir", "~/Documents/DevLogs")
    if not base.exists():
        return f"目录不存在：{base}"
    hits: list[str] = []
    # 先限量再排序：`sorted(rglob(...))` 会把整棵目录树先读进内存，结果上限
    # 只是输出上限，不是资源上限。
    candidates: list[Path] = []
    for path in base.rglob("*.md"):
        candidates.append(path)
        if len(candidates) >= 2000:
            break
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
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
    session = "voxos_delegate"
    if subprocess.run(["tmux", "has-session", "-t", session],
                      capture_output=True, timeout=5).returncode == 0:
        # 同名会话还在跑时再 new-session 会失败，旧实现却照样说「已委托」。
        return f"已有同名会话 {session} 在运行，未重复委托"
    started = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(workdir),
                              shlex.join(command)], check=False, timeout=10)
    if started.returncode != 0:
        return f"委托失败：无法创建 tmux 会话 {session}"
    return f"已委托 {cli} 在后台执行，会话名 {session}"


# ---------------------------------------------------------------- 日常指令
# 这一组是「天猫精灵式」的基本盘：打开应用、问时间。缺了它们，用户说
# 「打开微信」只会得到「没听懂」——转成文字了，却什么也做不了。

_APP_ALIAS = {
    "微信": "WeChat", "企业微信": "WeCom", "终端": "Terminal", "访达": "Finder",
    "浏览器": "Safari", "备忘录": "Notes", "日历": "Calendar", "计算器": "Calculator",
    "音乐": "Music", "地图": "Maps", "邮件": "Mail", "相机": "Photo Booth",
    "设置": "System Settings", "系统设置": "System Settings", "照片": "Photos",
}
_WEEK = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _app_dirs() -> list[Path]:
    return [Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"]


def _find_app(name: str) -> Path | None:
    """按名字找 .app：完全相同 > 前缀 > 包含，同级取名字最短的。

    为什么不能只做子串匹配：说「微信」会先命中 `wechatwebdevtools`
    （微信开发者工具），因为它在字母序里靠前。用户要的是聊天那个。
    也不做「猜」：找不到就如实说，并列出几个真实存在的应用名。
    """
    wanted = [name.strip().lower()]
    alias = _APP_ALIAS.get(name.strip())
    if alias:
        wanted.append(alias.lower())
    best: tuple[int, int, Path] | None = None
    for folder in _app_dirs():
        if not folder.exists():
            continue
        for path in folder.glob("*.app"):
            stem = path.stem.lower()
            for key in wanted:
                rank = 0 if stem == key else 1 if stem.startswith(key) else 2 if key in stem else None
                if rank is None:
                    continue
                score = (rank, len(stem), path)
                if best is None or score[:2] < best[:2]:
                    best = score
    return best[2] if best else None


@tool(fast=r"^(?:帮我|请|给我)?(?:打开一下|开一下|打开|启动|激活|运行)\s*(.+?)\s*(?:吧|。|！|!)?$",
      risk="green")
def open_app(app: str) -> str:
    """打开本机已安装的应用；名字里带点（如 github.com）则当网址打开。

    Args:
        app: 应用名（微信 / Safari / 终端）或网址（github.com）
    """
    if "." in app and " " not in app and not app.endswith(".app"):
        if RT.dry_run:
            return f"（dry-run）会打开网址 https://{app}"
        webbrowser.open(f"https://{app}")
        return f"已打开 {app}"
    target = _find_app(app)
    if target is None:
        installed = sorted(p.stem for folder in _app_dirs() if folder.exists()
                           for p in folder.glob("*.app"))[:8]
        return f"没找到「{app}」。已安装的有：{'、'.join(installed)}…"
    if RT.dry_run:
        return f"（dry-run）会打开 {target.stem}"
    subprocess.Popen(["open", str(target)])  # noqa: S603
    return f"已打开 {target.stem}"


@tool(fast=r"^(?:现在|今天|请问)?\s*(几点了|几点|什么时间|几号|几月几号|星期几|周几)", risk="green")
def tell_time(what: str = "几点") -> str:
    """回答当前时间 / 日期 / 星期。

    Args:
        what: 用户问的是几点、几号还是星期几
    """
    now = datetime.now()
    if "星期" in what or "周" in what:
        return f"今天是{_WEEK[now.weekday()]}"
    if "号" in what:
        return f"今天是 {now.year} 年 {now.month} 月 {now.day} 日"
    return f"现在是 {now.hour} 点 {now.minute:02d} 分"


# ------------------------------------------------------- 命令 / 文件 / 消息
# 这一组是「能真的替你动手」的部分：跑命令、打开文件、发消息、定时。
# 边界不靠自己写正则判断（shell 是一棵树，绕法无穷），而是交给 macOS 自带的
# seatbelt；发消息这种只能模拟按键的事，照抄社区跑通的做法并如实说清代价。

@tool(fast=r"^(?:执行|运行|跑)(?:一下)?(?:终端)?命令[：:\s]+(.+)$", risk="yellow")
def run_command(command: str, timeout: int = 0) -> str:
    """在本机执行一条 shell 命令（默认关在 macOS 沙箱里，只能写工作区和临时目录）。

    Args:
        command: 要执行的命令，如 `git status`、`ls -la ~/Projects`
        timeout: 超时秒数，0 表示用配置里的 workspace.command_timeout_s
    """
    limit = _timeout(RT.cfg, timeout)
    if sandbox.mode(RT.cfg) == "read-only":
        return "当前是只读沙箱（workspace.sandbox = read-only），一条命令都不执行"
    try:
        done, note = _shell(RT.cfg, command, limit)
    except subprocess.TimeoutExpired:
        return f"{limit} 秒没跑完，已放弃：{command}"
    except OSError as exc:
        return f"命令起不来：{exc}"
    return note + _brief(done)


def _timeout(cfg: Config, raw: int) -> int:
    """超时上限：调用方给的优先，但都要落在 5~600 秒之间。"""
    default = int(cfg.get("workspace.command_timeout_s", 60))
    return max(5, min(int(raw) or default, 600))


def _shell(cfg: Config, command: str, limit: int) -> tuple[subprocess.CompletedProcess, str]:
    """跑一次；沙箱没起来就退回裸跑，并把这件事写进返回文本。

    不静默降级：用户以为有沙箱、实际没有，比一开始就说清楚危险得多。
    """
    argv, profile = sandbox.wrap(cfg, ["/bin/sh", "-lc", command])
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=limit)
    finally:
        if profile is not None:
            profile.unlink(missing_ok=True)
    if done.returncode == 0 or not sandbox.failed_by_sandbox(done.stderr):
        return done, ""
    bare = subprocess.run(["/bin/sh", "-lc", command], capture_output=True, text=True, timeout=limit)
    return bare, "（沙箱没能应用，这次是直接执行的）"


def _brief(done: subprocess.CompletedProcess) -> str:
    """命令结果说人话：成功回正文，失败带退出码与 stderr 首行。"""
    out = (done.stdout or "").strip()
    if len(out) > 2000:
        out = out[:2000] + "\n…（输出太长，已截断）"
    if done.returncode == 0:
        return out or "（命令没有输出）"
    err = (done.stderr or "").strip()[:500]
    return f"退出码 {done.returncode}：{out or '（无输出）'}" + (f"\n{err}" if err else "")


@tool(fast=r"^(?:帮我|请)?(?:打开|显示|定位)(?:一下)?(?:这个)?(?:文件|文件夹|目录|路径)[：:\s]+(.+)$",
      risk="green")
def open_path(path: str, reveal: bool = False) -> str:
    """用系统默认程序打开文件/文件夹，或在访达里定位它（只读，不改任何东西）。

    Args:
        path: 文件或文件夹路径，支持 ~ 展开
        reveal: True 时在访达里选中它，而不是打开
    """
    target = Path(os.path.expanduser(str(path).strip().strip("\"'"))).resolve()
    if not target.exists():
        return f"路径不存在：{target}"
    if RT.dry_run:
        return f"（dry-run）将执行：open {'-R ' if reveal else ''}{target}"
    args = ["open", "-R", str(target)] if reveal else ["open", str(target)]
    if subprocess.run(args, capture_output=True).returncode != 0:
        return f"系统没能打开 {target.name}"
    return f"已{'在访达中定位' if reveal else '打开'} {target.name}"


@tool(risk="red_confirm")
def send_message(contact: str, text: str, app: str = "微信") -> str:
    """在微信 / 企业微信 / 信息里给某个联系人发一条消息（需要辅助功能权限）。

    Args:
        contact: 联系人备注名，如「张三」
        text: 要发出去的内容
        app: 微信 / 企业微信 / 信息
    """
    target = messaging.app_of(app)
    if target is None:
        return f"还不支持在「{app}」里发消息，现在能发的是：微信、企业微信、信息"
    if RT.dry_run:
        return f"（dry-run）将在 {target[0]} 给「{contact}」发送：{text}"
    if not shutil.which("osascript"):
        return "这台机器没有 osascript，没法模拟按键"
    done = subprocess.run(["osascript", "-e", messaging.script(*target, contact, text)],
                          capture_output=True, text=True, timeout=40)
    if done.returncode != 0:
        return f"没能发送：{messaging.permission_hint(done.stderr) or (done.stderr or '').strip()[:160]}"
    return f"已在{app}给「{contact}」发出：{text}"


@tool(risk="red_confirm")
def schedule_task(text: str, at: str, repeat: str = "", label: str = "") -> str:
    """在指定时间重新执行一句话，进程重启也不丢；`repeat=daily` 每天同一时刻。

    Args:
        text: 到点要执行的那句话，如「给张三发消息说我晚点到」
        at: `09:30`（今天或明天）、`2026-10-29 09:00`、或 `+600`（600 秒后）
        repeat: 留空只执行一次；daily 每天同一时刻
        label: 一句话说明，便于以后查看与取消
    """
    when = scheduler.parse_at(at)
    if not when:
        return f"看不懂这个时间：{at}（可以写「09:30」或「2026-10-29 09:00」）"
    if when <= time.time() + 1:
        return f"这个时间已经过去了：{at}"
    # approved=True 是因为**这次调用本身**是 red_confirm：用户刚点过头。
    item = scheduler.add(RT.cfg, text, when, label=label or text,
                         repeat=repeat.strip().lower(), approved=True)
    return f"已排好：{scheduler.stamp(when)} {item['label']}"


@tool(fast=r"^(?:看看|查看|有哪些|列出)?(?:定时任务|计划任务)(?:列表)?[？?]?$", risk="green")
def list_schedules() -> str:
    """列出还没到点的定时任务。"""
    items = scheduler.pending(RT.cfg)
    if not items:
        return "还没有定时任务"
    rows = [f"{scheduler.stamp(float(item['due']))} {item['label']}" for item in items[:10]]
    return f"{len(items)} 条定时任务：" + "；".join(rows)


@tool(fast=r"^(?:取消|删掉|清空)(?:所有|全部)?(?:的)?(?:定时任务|计划任务)$", risk="yellow")
def cancel_schedule(target: str = "") -> str:
    """取消定时任务：给关键词删一条，留空删全部。

    Args:
        target: 任务编号或说明里的关键词；留空表示全部取消
    """
    count = scheduler.clear(RT.cfg, str(target).strip())
    return f"已取消 {count} 条定时任务" if count else "没找到匹配的定时任务"
