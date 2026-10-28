"""工具契约与注册表：`@tool` 装饰器把普通函数变成「LLM 工具 + 正则快通道 + 受控备份」。

自省生成 JSON Schema（CODING_PHILOSOPHY.md §3「零样板扩展」）：改一个函数签名，
Schema、快通道、Prompt 三处自动同步，不存在"两处维护必然不一致"。
"""

from __future__ import annotations

import inspect
import re
import types
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Union, get_args, get_origin, get_type_hints

__all__ = ["RiskLevel", "Tool", "REGISTRY", "FAST_ROUTES", "tool", "build_schema",
           "ensure_tools", "validate_args"]


class RiskLevel(str, Enum):
    """三级风险 + 绝对禁区。见 REDLINES.md §1。"""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED_CONFIRM = "RED_CONFIRM"
    RED_FORBIDDEN = "RED_FORBIDDEN"


@dataclass(frozen=True)
class Tool:
    """一个已注册工具的全部元数据（函数本体、Schema、快通道、风险等级、备份参数）。"""

    name: str
    fn: Callable[..., Any]
    schema: dict[str, Any]
    fast: re.Pattern[str] | None
    risk: RiskLevel
    backup_arg: str | None = None
    first_param: str | None = None


REGISTRY: dict[str, Tool] = {}
FAST_ROUTES: list[tuple[re.Pattern[str], str]] = []


def _unwrap(annotation: Any) -> Any:
    """把 Optional[X] / X | None 还原成 X。"""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if args else str
    return annotation


def _json_type(annotation: Any) -> dict[str, Any]:
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    if origin is Literal:
        return {"type": "string", "enum": [str(a) for a in get_args(annotation)]}
    if origin in (list, tuple, set):
        return {"type": "array", "items": {"type": "string"}}
    return {
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }.get(annotation, {"type": "string"})


def _param_docs(doc: str) -> dict[str, str]:
    """从 docstring 的 `Args:` 段落里取 `name: 描述`（每行一条）。"""
    docs: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("args:", "参数:")):
            in_args = True
            continue
        if in_args and stripped.endswith(":") and " " not in stripped:
            break
        if in_args:
            matched = re.match(r"^(\w+)\s*[:：]\s*(.+)$", stripped)
            if matched:
                docs[matched.group(1)] = matched.group(2).strip()
    return docs


def build_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """由类型注解 + docstring 生成 OpenAI Function Calling Schema（零手写样板）。"""
    hints = get_type_hints(fn)
    docs = _param_docs(inspect.getdoc(fn) or "")
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        prop = _json_type(hints.get(name, str))
        prop["description"] = docs.get(name, name)
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default
        props[name] = prop
    summary = (inspect.getdoc(fn) or fn.__name__).splitlines()[0]
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": summary,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def tool(
    *,
    fast: str | None = None,
    risk: str = "green",
    backup_arg: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个普通函数注册为「LLM 工具 + 可选正则快通道 + 受控备份」。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        params = [p for p in inspect.signature(fn).parameters if p not in ("self", "cls")]
        meta = Tool(
            name=fn.__name__,
            fn=fn,
            schema=build_schema(fn),
            fast=re.compile(fast, re.IGNORECASE) if fast else None,
            risk=RiskLevel(risk.upper()),
            backup_arg=backup_arg,
            first_param=params[0] if params else None,
        )
        REGISTRY[meta.name] = meta
        if meta.fast:
            FAST_ROUTES.append((meta.fast, meta.name))
        return fn

    return decorator


def _type_error(prop: dict[str, Any], value: Any) -> str:
    """单个参数的类型/枚举校验；通过返回空串。"""
    kind = prop.get("type", "string")
    if kind == "string":
        if not isinstance(value, str):
            return "应为字符串"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "应为整数"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "应为数字"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return "应为布尔值"
    elif kind == "array":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return "应为字符串数组"
    allowed = prop.get("enum")
    if allowed and value not in allowed:
        return f"只能取 {' / '.join(str(item) for item in allowed)}"
    return ""


def validate_args(meta: Tool, args: object) -> str:
    """执行侧校验：模型输出是不可信输入，Schema 生成不等于 Schema 强制。

    返回空串表示通过；否则返回可以直接说给用户听的错误。
    """
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    schema = meta.schema["function"]["parameters"]
    props: dict[str, Any] = schema.get("properties", {})
    unknown = [key for key in args if key not in props]
    if unknown:
        return f"未知参数：{'、'.join(sorted(unknown))}"
    missing = [key for key in schema.get("required", []) if key not in args]
    if missing:
        return f"缺少参数：{'、'.join(missing)}"
    for key, value in args.items():
        error = _type_error(props[key], value)
        if error:
            return f"{key} {error}"
    return ""


def ensure_tools() -> None:
    """懒加载工具模块以触发 @tool 注册（打破 core ↔ tools 循环依赖）。"""
    import voxos.tools  # noqa: F401  # 导入即注册


