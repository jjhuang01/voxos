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

**为什么是「先探测」而不是「先试一次、失败再降级」**：macOS 不允许
**嵌套收紧**。进程已经在另一个沙箱里时，`sandbox_apply` 对任何比现状更严的
profile 都返回 EPERM——本机实测（macOS 27.2）：`(allow default)` 能过，
`(deny file-write*)`、`(deny network*)`、甚至只写一句 `(deny sysctl-write)`
全部 `sandbox_apply: Operation not permitted`。所以「试一下」的代价是每条命令
多起一个进程、而且可能把同一条命令跑两遍。这里改成启动后探一次、缓存结论，
`run_command` 直接按结论走，并在结果里说清是哪一种不可用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Config

__all__ = ["MODES", "mode", "profile", "wrap", "writes_roots", "SANDBOX_EXEC",
           "status", "failed_by_sandbox", "available"]

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MODES = ("read-only", "workspace-write", "danger-full-access")

# 只放行这几个设备节点的**写**：`/dev/null` 每天被重定向几万次，
# `/dev/fd` 是进程替换（`diff <(a) <(b)`）的地基。
_WRITE_NODES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom",
                "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/dtracehelper")
_DEV_FD = "/dev/fd"

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
    """允许写入的目录：工作区两个根 + 启动目录 + 额外配置 + 临时目录。

    临时目录必须给：`git`、`npm`、编译器的中间文件都在 `$TMPDIR` 里，
    不放行的话连 `git status` 都会失败——那样的沙箱没人会用，等于逼用户关掉它。

    **启动目录也必须给**（codex 的 `workspace-write` 就是 cwd + `$TMPDIR` + `~/.codex`）：
    本机实测过，不给的话用户在自己项目里说「跑一下测试」，pytest 连
    `.pytest_cache` 都写不进去——命令没错，是边界画错了。但 `/` 和整个家目录
    不能算：App 由 LaunchServices 拉起时 cwd 就是 `/`，放进去等于不设边界。
    """
    roots = [cfg.path("workspace.default_code_dir", "~/Projects"),
             cfg.path("workspace.daily_log_dir", "~/Documents/DevLogs"),
             Path("/tmp"), Path("/private/tmp"), Path(tempfile.gettempdir())]
    roots.extend(Path(os.path.expanduser(str(one)))
                 for one in _as_list(cfg.get("workspace.write_roots", [])))
    start = _start_dir()
    if start is not None:
        roots.append(start)
    return _dedup(roots)


def _dedup(roots: list[Path]) -> list[Path]:
    """去重并保持顺序。`/tmp` 是 `/private/tmp` 的符号链接，`resolve()` 之后
    两个就重了；重复的 `(subpath …)` 不会报错，只是白白把 profile 撑长。"""
    seen: dict[Path, None] = {}
    for path in roots:
        seen.setdefault(path.expanduser().resolve(), None)
    return list(seen)


def _as_list(raw: object) -> list[str]:
    """配置里写成一个字符串也认——手写 TOML 时很容易漏掉方括号。"""
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return [str(one) for one in raw] if isinstance(raw, (list, tuple)) else []


def _start_dir() -> Path | None:
    """从哪个目录启动的。`/`、家目录本身、以及**家目录的祖先**返回 None。

    为什么要排祖先而不只是家目录：`cd /Users` 再跑，cwd 是 `/Users`，
    一条 `(subpath "/Users")` 就等于放行整个家目录——边界看着还在，其实没了。

    反过来，**不能要求 cwd 必须在家目录下**：项目放在外置盘（`/Volumes/Work/…`）
    或 `/opt` 是常见做法，排掉的话用户在自己的项目里跑测试会被莫名拒绝，
    而报错和「命令有 bug」长得一样。那些位置本来就受 Unix 权限和 SIP 管，
    seatbelt 放开也不会凭空多出写权限。
    """
    try:
        start = Path.cwd()
    except OSError:                     # cwd 被删了
        return None
    home = Path.home()
    return None if start == home or start in home.parents else start


def profile(cfg: Config) -> str:
    """生成 seatbelt profile 文本（SBPL）。"""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    allowed = [f'(subpath "{root}")' for root in writes_roots(cfg)]
    # 只给 /dev 底下真正需要写的那几个节点，而不是整段 `/dev`：
    # `(subpath "/dev")` 等于允许 `dd of=/dev/disk2`（要 root 才生效，但没必要开）。
    allowed += [f'(literal "{node}")' for node in _WRITE_NODES]
    allowed.append(f'(subpath "{_DEV_FD}")')
    lines.append(f"(allow file-write* {' '.join(allowed)})")
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


# ---------------------------------------------------------------- 可用性探测

# 探测用最小 profile：只写一句「拒绝一切写」。它比现状更严——正好是我们要测的
# 那件事。用真实 profile 去探也行，但那样探测本身就会建临时文件、依赖配置。
_PROBE = "(version 1)(allow default)(deny file-write*)"
_NESTED = "当前进程已在另一个沙箱里，macOS 不允许嵌套收紧"
_CACHE: tuple[bool, str] | None = None


def status() -> tuple[bool, str]:
    """这个进程能不能真的套上沙箱。返回（可用，不可用的原因）。**每个进程探一次**。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _probe()
    return _CACHE


def _probe() -> tuple[bool, str]:
    if not available():
        return False, f"没有 {SANDBOX_EXEC}"
    try:
        done = subprocess.run([SANDBOX_EXEC, "-p", _PROBE, "/usr/bin/true"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"探测失败：{exc}"
    if done.returncode == 0:
        return True, ""
    err = (done.stderr or "").strip()
    # EPERM 就是「已在沙箱里」：内核只允许把沙箱换成不更严的那一个。
    if "Operation not permitted" in err:
        return False, _NESTED
    return False, err[:120] or "sandbox-exec 拒绝了这个 profile"


def reset() -> None:
    """忘掉探测结果（测试用；真实运行里进程的沙箱状态不会变）。"""
    global _CACHE
    _CACHE = None
