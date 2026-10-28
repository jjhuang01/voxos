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


ROOT = _default_root()

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
                 source: Path | None = None) -> None:
        self.data = data
        self.root = root
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
        """取路径类配置并展开 ~ ；相对路径按仓库根目录解析。"""
        raw = str(self.get(dotted, default))
        expanded = Path(os.path.expanduser(raw))
        return expanded if expanded.is_absolute() else (self.root / expanded)

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
        return Config(data, self.root, self.source)


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


def load(path: str | Path | None = None) -> Config:
    """加载配置；文件缺失时用内置最小默认值（保证任何机器上都能起来）。"""
    target = Path(path) if path else ROOT / "config" / "settings.toml"
    data: dict[str, Any] = {}
    if target.exists():
        with open(target, "rb") as handle:
            data = tomllib.load(handle)
    _apply_env(data)
    return Config(data, source=target)


def load_redlines(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else ROOT / "config" / "redlines.toml"
    if not target.exists():
        # 缺策略不是「没有规则」，而是「无法证明任何操作安全」：Guard 会据此拒绝一切。
        return {"forbidden": [], "risk": {}, "roles": {}, "missing": True}
    with open(target, "rb") as handle:
        return tomllib.load(handle)
