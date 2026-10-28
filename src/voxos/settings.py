"""设置页的后端：字段模式、校验、保留注释的 TOML 写回。

三个职责各自独立，互不调用业务逻辑（CODING_PHILOSOPHY.md §2）：
- `describe` 只回答"现在是什么、能改什么"；
- `validate` 只回答"这个值能不能用"；
- `save` 只回答"怎么把值写回文件而不毁掉注释"。

改配置不重启进程：`apply` 写完文件后重新 load 一份 Config 交给调用方替换。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ENV_MAP, Config, load

GROUP_ORDER = ("audio", "stt", "llm", "workspace", "ui")
GROUP_LABEL = {
    "audio": "声音与声纹",
    "stt": "语音识别",
    "llm": "大模型",
    "workspace": "工作区与权限",
    "ui": "界面",
}


@dataclass(frozen=True)
class Field:
    """一个可配置项。`kind` 决定控件，`minimum/maximum` 决定校验。"""

    key: str
    label: str
    kind: str = "str"           # int | float | bool | str | enum
    group: str = "audio"
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    secret: bool = False
    restart: bool = False
    unit: str = ""


SCHEMA: tuple[Field, ...] = (
    Field("audio.voiceprint_threshold", "声纹阈值", "float", "audio",
          "同人相似度超过此值才接受。0.82 为实测标定值，调低更容易通过也更危险。",
          0.50, 0.99, 0.01, restart=False),
    Field("audio.mask_mode", "面罩 / 低语模式", "bool", "audio",
          "开启后加强中高频补偿，适合戴口罩说话。", restart=True),
    Field("audio.vad_threshold", "语音活动灵敏度", "float", "audio",
          "越低越容易触发，嘈杂环境可调高。", 0.10, 0.90, 0.05),
    Field("audio.echo_decay_ms", "回声消退期", "int", "audio",
          "TTS 播报结束后忽略麦克风的时长，防止听到自己。", 0, 2000, 50, unit="ms"),

    Field("stt.backend", "识别后端", "enum", "stt",
          "scripted 用于演示与测试；sense_voice 本地离线；local_whisper / openai 需额外依赖或 Key。",
          options=("scripted", "sense_voice", "local_whisper", "openai"), restart=True),
    Field("stt.model_dir", "本地模型目录", "str", "stt",
          "sense_voice 后端用的模型目录，里面要有 model.int8.onnx 与 tokens.txt。",
          restart=True),
    Field("stt.num_threads", "识别线程数", "int", "stt",
          "sense_voice 推理线程数。2 核就能跑实时，再往上收益递减还抢 CPU。",
          1, 8, 1, restart=True),
    Field("stt.model", "识别模型", "str", "stt", "whisper 的 small / medium / large-v3。", restart=True),
    Field("stt.language", "语言", "str", "stt", "zh / en / auto。", restart=True),
    Field("stt.api_key", "识别 API Key", "str", "stt", "openai 后端才需要。", secret=True),

    Field("llm.enabled", "启用大模型慢通道", "bool", "llm",
          "关闭时只走正则快通道 + 离线启发式，完全离线可用。"),
    Field("llm.base_url", "接口地址", "str", "llm", "任何 OpenAI 兼容端点，如 Ollama / vLLM。"),
    Field("llm.model", "模型名", "str", "llm", ""),
    Field("llm.api_key", "API Key", "str", "llm", "本地模型可留空。", secret=True),

    Field("workspace.role", "当前角色", "enum", "workspace",
          "决定哪些工具可调用；OPS 不能写文件，DOCTOR 输出强制标记草稿。",
          options=("DEVELOPER", "OPS", "DOCTOR")),
    Field("workspace.daily_log_dir", "日志目录", "str", "workspace", "写日志工具的目标目录。"),
    Field("workspace.default_code_dir", "代码目录", "str", "workspace", "续接编程会话的根目录。"),
    Field("workspace.dry_run", "演练模式", "bool", "workspace",
          "开启时不真的派发 tmux / IDE / 浏览器，只打印将执行的命令。"),
    Field("workspace.confirm_timeout_s", "确认超时", "int", "workspace",
          "危险操作等待确认的秒数，超时按拒绝处理。", 3, 60, 1, unit="s"),

    Field("ui.earcons", "提示音", "bool", "ui", "开始与结束时各一声轻响。"),
    Field("ui.speak", "语音播报", "bool", "ui", "完成后用系统 TTS 念一遍结果。"),
    Field("ui.port", "本地端口", "int", "ui", "胶囊与设置页的监听端口。", 1024, 65535, 1,
          restart=True),
)

BY_KEY = {field.key: field for field in SCHEMA}


# ------------------------------------------------------------------ 读取

def describe(cfg: Config) -> list[dict[str, object]]:
    """给前端的分组视图：当前值 + 控件元数据 + 是否被环境变量接管。"""
    groups: dict[str, list[dict[str, object]]] = {name: [] for name in GROUP_ORDER}
    for field in SCHEMA:
        item: dict[str, object] = {
            "key": field.key, "label": field.label, "kind": field.kind, "help": field.help,
            "value": cfg.get(field.key), "unit": field.unit, "restart": field.restart,
            "secret": field.secret, "options": list(field.options),
            "min": field.minimum, "max": field.maximum, "step": field.step,
            "overridden_by": _env_source(field.key),
        }
        if field.secret and item["value"]:
            item["value"] = "••••••"
        groups[field.group].append(item)
    return [{"id": name, "label": GROUP_LABEL[name], "fields": groups[name]}
            for name in GROUP_ORDER if groups[name]]


def _env_source(key: str) -> str:
    for env_key, target in ENV_MAP.items():
        if ".".join(target) == key and os.environ.get(env_key) is not None:
            return env_key
    return ""


# ------------------------------------------------------------------ 校验

def validate(changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把前端来的值收敛成合法值；返回（干净值，错误列表）。"""
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in changes.items():
        field = BY_KEY.get(key)
        if field is None:
            errors.append(f"未知配置项：{key}")
            continue
        value, error = _coerce(field, raw)
        if error:
            errors.append(error)
        else:
            clean[key] = value
    errors.extend(_cross_check(clean))
    return clean, errors


def _coerce(field: Field, raw: Any) -> tuple[Any, str]:
    if field.kind == "bool":
        return (raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")), ""
    if field.kind in ("int", "float"):
        return _coerce_number(field, raw)
    text = "" if raw is None else str(raw).strip()
    if field.kind == "enum" and text not in field.options:
        return None, f"{field.label}：只能取 {' / '.join(field.options)}"
    if len(text) > 500:
        return None, f"{field.label}：太长（上限 500 字符）"
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        # U+007F 这类控制字符能通过旧校验，写进 TOML 后整个文件解析失败。
        return None, f"{field.label}：不能包含控制字符"
    return text, ""


def _coerce_number(field: Field, raw: Any) -> tuple[Any, str]:
    try:
        value = int(raw) if field.kind == "int" else round(float(raw), 4)
    except (TypeError, ValueError, OverflowError):
        return None, f"{field.label}：需要一个数字"
    if not math.isfinite(value):
        return None, f"{field.label}：需要一个有限数字"
    low, high = field.minimum, field.maximum
    if (low is not None and value < low) or (high is not None and value > high):
        return None, f"{field.label}：应在 {low} ~ {high} 之间"
    return value, ""


def _cross_check(clean: dict[str, Any]) -> list[str]:
    if clean.get("llm.enabled") and "llm.base_url" in clean and not clean["llm.base_url"]:
        return ["启用大模型时必须填写接口地址"]
    return []


# ------------------------------------------------------------------ 写回

BACKUP_NAME = "settings.bak"
_LOCK = threading.RLock()


def target_of(cfg: Config) -> Path:
    """这份配置写回哪个文件：`--config` 指的那个，或仓库默认的那份。"""
    return cfg.source or cfg.root / "config" / "settings.toml"


def _render(changes: dict[str, Any], path: Path) -> str:
    """只改等号右边，保留缩进、对齐与行尾注释；返回完整候选文本。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in changes.items():
        section, name = key.split(".", 1)
        lines = _write_value(lines, section, name, _literal(value))
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """同目录临时文件 + fsync + os.replace：中途失败不会留下半个配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(changes: dict[str, Any], path: Path) -> None:
    """就地改值：先渲染并解析成功，再原子替换。"""
    text = _render(changes, path)
    tomllib.loads(text)                      # 语法不成立就不碰原文件
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    _atomic_write(path, text, mode)


def backup_path(target: Path) -> Path:
    """备份放在配置同目录的 `.voxos/` 下，跟着 `--config` 走，不污染仓库根。

    文件名必须带目标身份：同目录放两份配置时，共用一个 settings.bak
    会让「撤销 A」实际恢复 B。
    """
    name = BACKUP_NAME if target.name == "settings.toml" else f"settings.{target.stem}.bak"
    return target.parent / ".voxos" / name


def restore(target: Path) -> Config:
    """回到上一次保存之前。备份用掉就删掉——撤销只退一步，退完这一步就没得再退。"""
    with _LOCK:
        backup = backup_path(target)
        if not backup.exists():
            raise FileNotFoundError("没有可撤销的保存记录")
        text = backup.read_text(encoding="utf-8")
        tomllib.loads(text)                  # 备份坏了也不能写回目标
        mode = (target.stat().st_mode & 0o777) if target.exists() else 0o600
        _atomic_write(target, text, mode)
        backup.unlink()                      # 写成功才消费，写失败备份还在
        return load(target)


def can_undo(cfg: Config) -> bool:
    """有没有一步可退。界面据此决定要不要显示「撤销」。"""
    return backup_path(target_of(cfg)).exists()


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _write_value(lines: list[str], section: str, name: str, literal: str) -> list[str]:
    span = _section_span(lines, section)
    if span is not None:
        for index in range(*span):
            match = re.match(rf"^(\s*){re.escape(name)}(\s*=\s*)", lines[index])
            if not match:
                continue
            value_span = _value_span(lines[index], match.end())
            if value_span is None:
                continue
            head, tail = lines[index][:value_span[0]], lines[index][value_span[1]:]
            lines[index] = head + literal + tail
            return lines
    return _insert_line(lines, section, name, literal)


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """返回 `[section]` 表体所在的行区间；表不存在时返回 None。"""
    header = f"[{section}]"
    for index, line in enumerate(lines):
        if line.strip() == header:
            end = next((i for i in range(index + 1, len(lines)) if lines[i].lstrip().startswith("[")),
                       len(lines))
            return index + 1, end
    return None


def _value_span(line: str, start: int) -> tuple[int, int] | None:
    """从等号右侧找到值的结束位置（跳过引号内的 #）。"""
    index = start
    while index < len(line) and line[index] in " \t":
        index += 1
    end, in_string = index, False
    while end < len(line):
        char = line[end]
        if char == '"' and (end == 0 or line[end - 1] != "\\"):
            in_string = not in_string
        elif char == "#" and not in_string:
            break
        end += 1
    while end > index and line[end - 1] in " \t":
        end -= 1
    return (index, end) if end > index else None


def _insert_line(lines: list[str], section: str, name: str, literal: str) -> list[str]:
    span = _section_span(lines, section)
    if span is None:
        return lines + ["", f"[{section}]", f"{name} = {literal}"]
    start, end = span
    anchor = start
    for index in range(start, end):
        if re.match(r"^\s*[A-Za-z0-9_]+\s*=", lines[index]):
            anchor = index + 1
    return lines[:anchor] + [f"{name} = {literal}"] + lines[anchor:]


def apply(cfg: Config, changes: dict[str, Any], path: Path | None = None) -> tuple[Config, list[str]]:
    """校验 → 备份 → 写盘 → 重新加载。校验失败时一个字节都不写。

    整个序列在同一把锁里；候选文本先解析成功才替换目标，保存与撤销
    不会互相穿插，也不会出现「写成功了但读不回来」。
    """
    clean, errors = validate(changes)
    if errors or not clean:
        return cfg, errors
    target = target_of(cfg) if path is None else path
    with _LOCK:
        mode = (target.stat().st_mode & 0o777) if target.exists() else 0o600
        if target.exists():                   # 留一份上一次的，供「撤销」用
            backup = backup_path(target)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)      # copy2 保权限，不把 0600 变 0644
            os.chmod(backup, mode)
        text = _render(clean, target)
        tomllib.loads(text)
        _atomic_write(target, text, mode)
        return load(target), []
