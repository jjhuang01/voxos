"""命令沙箱：用 macOS 自带的 seatbelt（`sandbox-exec`）划边界，不靠字符串校验。

**为什么不自己写「命令里有没有 rm -rf」**：shell 是一棵树，绕过字符串匹配的
方式无穷——`$IFS`、变量拼接、`sh -c`、管道、子 shell、`python -c`。codex 和
Claude Code 都不用正则做沙箱；macOS 上它们用 seatbelt，就是 `sandbox-exec`
吃的那套 profile 语言。所以这里直接抄，不发明。

三档，语义对齐 codex 的 `sandbox_mode`：

| 档位 | 跑命令 | 文件写入 | 联网 |
| :--- | :--- | :--- | :--- |
| `read-only` | 不允许 | 不允许 | 不允许 |
| `workspace-write` | 允许 | **只允许工作区 + 临时目录** | 看 `workspace.network` |
| `danger-full-access` | 允许 | 不限制 | 不限制 |

`workspace-write` 下如果沙箱**应用不起来**（内核拒绝、二进制缺失），
`run_command` 会降级成「红线段 + 路径校验」并把这件事写进返回文本——
不静默，但也不因此让工具变砖。沙箱是边界，不是许可证。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .config import Config

__all__ = ["MODES", "mode", "profile", "wrap", "writes_roots", "SANDBOX_EXEC"]

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MODES = ("read-only", "workspace-write", "danger-full-access")

# 沙箱自己起不来时内核给的话。用它判断「是不是沙箱的问题」，
# 而不是把命令本身的失败也当成沙箱失败。
#
# 特意**不认**裸的 `Operation not permitted`：命令自己失败时也会这么说，
# 认了就会把同一条命令再跑一遍——那是一次多余的副作用，比「没降级」更糟。
_APPLY_FAILED = ("sandbox_apply", "sandbox-exec: ")


def mode(cfg: Config) -> str:
    """当前档位。写错了按最严处理——配置拼错不该变成「全开」。"""
    raw = str(cfg.get("workspace.sandbox", "workspace-write")).strip().lower()
    return raw if raw in MODES else "read-only"


def writes_roots(cfg: Config) -> list[Path]:
    """允许写入的目录：工作区两个根 + 临时目录。

    临时目录必须给：`git`、`npm`、编译器的中间文件都在 `$TMPDIR` 里，
    不放行的话连 `git status` 都会失败——那样的沙箱没人会用，等于逼用户关掉它。
    """
    roots = [cfg.path("workspace.default_code_dir", "~/Projects"),
             cfg.path("workspace.daily_log_dir", "~/Documents/DevLogs"),
             Path("/tmp"), Path("/private/tmp"), Path("/dev"),
             Path(tempfile.gettempdir())]
    return [path.expanduser().resolve() for path in roots]


def profile(cfg: Config) -> str:
    """生成 seatbelt profile 文本（SBPL）。"""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    allow = " ".join(f'(subpath "{root}")' for root in writes_roots(cfg))
    lines.append(f"(allow file-write* {allow})")
    if not cfg.flag("workspace.network", False):
        # 断网：语音助手的命令不该在你不知道的时候往外发东西。
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def wrap(cfg: Config, argv: list[str]) -> tuple[list[str], Path | None]:
    """把 argv 包进沙箱。返回（真正执行的 argv，profile 临时文件）。

    `danger-full-access` 原样返回；`read-only` 由调用方拒绝，不走到这里。
    """
    if mode(cfg) == "danger-full-access" or not Path(SANDBOX_EXEC).exists():
        return argv, None
    handle = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False, encoding="utf-8")
    handle.write(profile(cfg))
    handle.close()
    path = Path(handle.name)
    return [SANDBOX_EXEC, "-f", str(path), *argv], path


def failed_by_sandbox(stderr: str) -> bool:
    """这次失败是沙箱没起来，还是命令本身出错？

    区分很重要：命令出错要如实报错，沙箱没起来要降级并**说出来**。
    混在一起的结果是用户以为命令有问题，其实是权限。
    """
    return any(marker in stderr for marker in _APPLY_FAILED)


def available() -> bool:
    return Path(SANDBOX_EXEC).exists() and shutil.which("sandbox-exec") is not None
