"""配置加载：TOML + 环境变量覆盖 + 路径解析。

设计约束（见 CODING_PHILOSOPHY.md §1）：
- 只做「读文件 + 展平 + 解析路径」三件事，不含业务策略。
- 环境变量优先级最高，方便演示/CI 不落盘改配置。
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any


def _default_root() -> Path:
    """资源根：源码布局用仓库根；普通安装退到 `<prefix>/share/voxos`。"""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "config").exists():
        return source_root
    installed = Path(sys.prefix) / "share" / "voxos"
    return installed if (installed / "config").exists() else source_root


def _default_data_root() -> Path:
    """用户数据根：模型、声纹、日志、快照、状态。

    为什么要和资源根分开：pipx / pip 装出来的包在 `~/.local/pipx/venvs/voxos/`，
    把 267MB 模型下载到那儿，`pipx upgrade` 重建 venv 时会一起删掉；系统级安装
    还可能根本不可写。所以源码布局照旧（数据就在仓库里），安装布局落到家目录，
    `.voxos/models/...` 自然变成 `~/.voxos/models/...`。

    可用 `VOXOS_HOME` 覆盖（多实例、CI、测试）。它替换的是**家目录**，不是
    `.voxos` 本身：设成 `/data/u` 之后，配置里的 `.voxos/models` 落到
    `/data/u/.voxos/models`。这样配置里的路径写法不必随根目录变化。
    """
    override = os.environ.get("VOXOS_HOME")
    if override:
        return Path(os.path.expanduser(override))
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "config").exists():
        return source_root
    return Path.home()


ROOT = _default_root()
DATA = _default_data_root()

# 环境变量 -> 配置点（演示与 CI 用，避免改文件）
ENV_MAP = {
    "VOXOS_LLM_ENABLED": ("llm", "enabled"),
    "VOXOS_LLM_BASE_URL": ("llm", "base_url"),
    "VOXOS_LLM_MODEL": ("llm", "model"),
    "VOXOS_LLM_API_KEY": ("llm", "api_key"),
    "VOXOS_ROLE": ("workspace", "role"),
    "VOXOS_DRY_RUN": ("workspace", "dry_run"),
    "VOXOS_LOG_DIR": ("workspace", "daily_log_dir"),
    "VOXOS_STT_BACKEND": ("stt", "backend"),
}

_TRUE = {"1", "true", "yes", "on"}
# 这些键即使配置文件里没有，也必须按布尔解析环境变量：
# 否则 `VOXOS_DRY_RUN=false` 会变成非空字符串，flag() 反而返回 True。
_BOOL_KEYS = {("llm", "enabled"), ("workspace", "dry_run")}


class Config:
    """只读配置视图。`cfg.get("audio.vad_threshold", 0.5)` 这样取值。"""

    def __init__(self, data: dict[str, Any], root: Path = ROOT,
                 source: Path | None = None, data_root: Path | None = None) -> None:
        self.data = data
        self.root = root
        self.data_root = data_root if data_root is not None else root
        self.source = source          # 这份配置是从哪个文件读出来的（写回时要用）

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return dict(value) if isinstance(value, dict) else {}

    def path(self, dotted: str, default: str = "") -> Path:
        """取**用户数据**路径并展开 ~ ；相对路径按数据根解析。

        模型、声纹、日志、快照、状态都走这里——它们该落在可写、可备份、
        升级不丢的地方，而不是安装目录。
        """
        return self._join(self.data_root, str(self.get(dotted, default)))

    def res_path(self, dotted: str, default: str = "") -> Path:
        """取**只读安装资源**路径（红线策略、胶囊页面、热词表）。

        相对路径按资源根解析：源码布局是仓库根，安装布局是 `<prefix>/share/voxos`。
        """
        return self._join(self.root, str(self.get(dotted, default)))

    @staticmethod
    def _join(base: Path, raw: str) -> Path:
        expanded = Path(os.path.expanduser(raw))
        return expanded if expanded.is_absolute() else (base / expanded)

    def flag(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted, default)
        if isinstance(value, str):
            return value.strip().lower() in _TRUE
        return bool(value)

    def path_allowed(self, path: str | Path) -> bool:
        """这个路径是否在允许的工作区内。

        默认只允许「代码目录」与「日志目录」及其子目录；绝对路径本身不是越权，
        但一个只听语音的 Agent 不应该因为转写里出现 `/etc/passwd` 就去读它。
        确需访问外部目录时，显式打开 `workspace.allow_outside_roots`。
        """
        if self.flag("workspace.allow_outside_roots", False):
            return True
        target = Path(os.path.expanduser(str(path))).resolve()
        roots = [self.path("workspace.default_code_dir", "~/Projects"),
                 self.path("workspace.daily_log_dir", "~/Documents/DevLogs")]
        for root in roots:
            base = root.expanduser().resolve()
            if target == base or base in target.parents:
                return True
        return False

    def with_overrides(self, **dotted_values: Any) -> "Config":
        """返回副本，用于测试注入。"""
        data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.data.items()}
        for dotted, value in dotted_values.items():
            parts = dotted.split(".")
            node = data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return Config(data, self.root, self.source, self.data_root)


def _apply_env(data: dict[str, Any]) -> None:
    for env_key, (section, key) in ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        node = data.setdefault(section, {})
        if (section, key) in _BOOL_KEYS or isinstance(node.get(key), bool):
            node[key] = raw.strip().lower() in _TRUE
        else:
            node[key] = raw


_SEED_NOTE = (
    "# 这份是你自己的 voxos 配置：从安装目录复制出来的第一份，之后只归你。\n"
    "# 改它、删它都不影响程序本体，`pipx upgrade` 也不会覆盖它。\n"
    "# 出厂默认值跟着安装包走（<prefix>/share/voxos/config/settings.toml）。\n\n"
)


def _settings_path() -> Path:
    """当前生效的配置文件在哪。

    源码布局（数据根就是仓库根）下，仓库里那份 `config/settings.toml` 就是当前配置——
    行为不变。安装布局下不能再用安装目录那份：它只读，而且 `pipx upgrade` 会整份换掉，
    用户在设置页改的东西活不过一次升级。所以改成「数据根优先，首次从安装目录 seed 一份」。
    """
    seed = ROOT / "config" / "settings.toml"
    if DATA == ROOT:
        return seed
    user = DATA / ".voxos" / "settings.toml"
    if user.exists() or not seed.exists():
        return user
    try:
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(_SEED_NOTE + seed.read_text(encoding="utf-8"), encoding="utf-8")
        return user
    except OSError:
        return seed          # 系统级安装、家目录不可写：退回安装目录那份，别在启动时炸


def load(path: str | Path | None = None) -> Config:
    """加载配置；文件缺失时用内置最小默认值（保证任何机器上都能起来）。"""
    target = Path(path) if path else _settings_path()
    data: dict[str, Any] = {}
    if target.exists():
        with open(target, "rb") as handle:
            data = tomllib.load(handle)
    _apply_env(data)
    return Config(data, source=target, data_root=DATA)


def load_redlines(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else ROOT / "config" / "redlines.toml"
    if not target.exists():
        # 缺策略不是「没有规则」，而是「无法证明任何操作安全」：Guard 会据此拒绝一切。
        return {"forbidden": [], "risk": {}, "roles": {}, "missing": True}
    with open(target, "rb") as handle:
        return tomllib.load(handle)
