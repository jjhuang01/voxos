"""内核：快慢双通道路由与模型调用。

对外只有 `dispatch` 一个入口。工具契约在 `registry.py`，安全边界在 `safety.py`，
本文件只负责"一句话该走哪条路"和"模型怎么被调用"。

`from voxos.core import tool / Executor / RiskLevel ...` 依然可用（此处 re-export），
调用方不必关心内部拆分。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import Config, load
from .registry import (  # noqa: F401  # re-export 给旧调用方
    FAST_ROUTES, REGISTRY, RiskLevel, Tool, build_schema, ensure_tools, tool,
)
from .safety import (  # noqa: F401  # re-export 给旧调用方
    Executor, Guard, Verdict, human_name, restore_latest, snapshot_file,
)

__all__ = [
    "DispatchResult", "dispatch", "chat", "heuristic_plan", "execute_safely", "SYSTEM_PROMPT",
    "RiskLevel", "Tool", "Verdict", "REGISTRY", "FAST_ROUTES", "tool", "build_schema",
    "Guard", "Executor", "ensure_tools", "snapshot_file", "restore_latest", "human_name",
]


# ---------------------------------------------------------------- 快慢双通道

SYSTEM_PROMPT = """你是 VoxOS 的意图调度内核。运行角色：{role}
【环境】时间 {now} | 焦点窗口 {active_window} | 工作区 {workspace} | 最近文件 {recent_files}
【规则】
1. 输入来自语音转写，可能有同音错字，请结合上下文纠正技术术语。
2. 若是闲聊、感叹词、与工作无关的内容，只输出 IGNORE，不要调用任何工具。
3. 一句话含多个意图时，在同一回合发起多个 tool_calls。
4. 用户只说文件名时，优先在【最近文件】与【工作区】内解析，不要臆造路径。
5. 不解释过程、不输出 Markdown；最终回复 ≤ 25 字，将被 TTS 播报。"""


@dataclass
class DispatchResult:
    route: str            # fast | llm | offline | ignore | error
    text: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    latency_ms: float = 0.0
    results: list[str] = field(default_factory=list)


def chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """OpenAI 兼容 /chat/completions（DeepSeek / Ollama / Claude 网关通用），零 SDK 依赖。"""
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key or 'none'}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"LLM 不可达：{exc}") from exc
    return body["choices"][0]["message"]


def _wake_pattern(cfg: Config | None) -> re.Pattern[str] | None:
    words = (cfg.get("audio.wake_words", []) if cfg else []) or []
    if not words:
        return None
    alternation = "|".join(re.escape(w) for w in words)
    # 分隔符要覆盖中文冒号与句点：用户会说「Jarvis：写个日志」
    return re.compile(rf"^(?:{alternation})[，,、：:。.\s]*", re.IGNORECASE)


def dispatch(
    text: str,
    *,
    executor: Executor | None = None,
    cfg: Config | None = None,
    context: dict[str, Any] | None = None,
) -> DispatchResult:
    """一句话进，动作出。顺序：快通道 → LLM 慢通道 → 离线启发式。"""
    ensure_tools()
    cfg = cfg or (executor.cfg if executor else load())
    executor = executor or Executor(cfg)
    started = time.perf_counter()
    raw = (text or "").strip()
    if not raw:
        return DispatchResult(route="ignore", text=raw, reply="")

    hit = _fast_route(raw, _wake_pattern(cfg))
    if hit:
        name, args = hit
        reply = executor.execute_safely(name, args, text=raw)
        return DispatchResult("fast", raw, name, args, reply, _ms(started), [reply])

    if cfg.flag("llm.enabled", False) and cfg.get("llm.base_url"):
        try:
            return _llm_route(raw, executor, cfg, context or {}, started)
        except Exception as exc:
            executor.on_event("blocked", "模型不可用", str(exc))

    plan = heuristic_plan(raw, cfg)
    if not plan:
        return DispatchResult("ignore", raw, reply="", latency_ms=_ms(started))
    results = [executor.execute_safely(name, args, text=raw) for name, args in plan]
    name, args = plan[0]
    return DispatchResult("offline", raw, name, args, "；".join(results), _ms(started), results)


def _fast_route(
    text: str, wake: re.Pattern[str] | None = None
) -> tuple[str, dict[str, Any]] | None:
    """唤醒词在此统一剥离一次，工具正则因此可以写得极简（单一职责）。"""
    normalized = (wake.sub("", text) if wake else text).strip()
    for pattern, name in FAST_ROUTES:
        matched = pattern.match(normalized)
        if not matched:
            continue
        meta = REGISTRY[name]
        if matched.lastindex and meta.first_param:
            return name, {meta.first_param: matched.group(1).strip()}
        return name, {}
    return None


def _system_message(cfg: Config, context: dict[str, Any]) -> dict[str, Any]:
    return {"role": "system", "content": SYSTEM_PROMPT.format(
        role=cfg.get("workspace.role", "DEVELOPER"),
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        active_window=context.get("active_window", "unknown"),
        workspace=context.get("workspace", str(cfg.path("workspace.default_code_dir", "~"))),
        recent_files=", ".join(context.get("recent_files", [])[-5:]) or "无",
    )}


def _execute_calls(
    calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    executor: Executor,
    results: list[str],
    text: str,
) -> tuple[str, dict[str, Any]]:
    """执行一回合内的所有 tool_call，把结果回填到对话里供模型总结。"""
    first_name, first_args = "", {}
    for call in calls:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        first_name, first_args = first_name or name, first_args or args
        result = executor.execute_safely(name, args, text=text)
        results.append(result)
        messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    return first_name, first_args


def _llm_route(
    text: str,
    executor: Executor,
    cfg: Config,
    context: dict[str, Any],
    started: float,
) -> DispatchResult:
    messages: list[dict[str, Any]] = [_system_message(cfg, context), {"role": "user", "content": text}]
    schemas = [meta.schema for meta in REGISTRY.values()]
    results: list[str] = []
    first_tool, first_args = "", {}
    for _ in range(int(cfg.get("llm.max_tool_rounds", 3))):
        message = chat(
            base_url=str(cfg.get("llm.base_url")),
            api_key=str(cfg.get("llm.api_key", "")),
            model=str(cfg.get("llm.model")),
            messages=messages,
            tools=schemas,
            temperature=float(cfg.get("llm.temperature", 0.1)),
            timeout=float(cfg.get("llm.timeout_s", 20)),
        )
        calls = message.get("tool_calls") or []
        if not calls:
            content = (message.get("content") or "").strip()
            if not results:
                return DispatchResult("ignore" if "IGNORE" in content else "llm", text,
                                      reply=content, latency_ms=_ms(started))
            return DispatchResult("llm", text, first_tool, first_args,
                                  content or "；".join(results), _ms(started), results)
        messages.append(message)
        name, args = _execute_calls(calls, messages, executor, results, text)
        first_tool, first_args = first_tool or name, first_args or args
    return DispatchResult("llm", text, first_tool, first_args, "；".join(results), _ms(started), results)


_SPLIT = re.compile(r"(?:然后|接着|再然后|[，,;；]\s*然后|[;；])")

# 「把 auth.py 的空值校验补上」——最自然的编程说法。文件名 + 变更动词才认，
# 避免把「把日志改一下」误判成改代码。
_CODE_FILE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|java|rs|rb|php|vue|sql|sh|ya?ml|toml|json|md)\b", re.I)
_CHANGE_VERB = re.compile(r"补|加上|加个|改|删|修|实现|重构|替换|优化")

# 「大改」才交给 Coding CLI（red_confirm，会弹确认卡）。小修小补走 dispatch_code_session，
# 免得每句话都拦一次确认——那样人就会习惯性点允许。
_BIG_CHANGE = re.compile(r"重构|大改|重写|整个项目|整块|全部重写|交给 ?(?:claude|aider|codex|coding)", re.I)


def heuristic_plan(text: str, cfg: Config | None = None) -> list[tuple[str, dict[str, Any]]]:
    """无模型/断网兜底：只认三大高频场景 + 撤销，命中不了就不做（宁可不做，不能乱做）。"""
    wake = _wake_pattern(cfg)
    plan: list[tuple[str, dict[str, Any]]] = []
    for segment in _SPLIT.split(text):
        piece = wake.sub("", segment.strip()) if wake else segment.strip()
        if not piece:
            continue
        if re.search(r"日志|记录|记一下|记一条|记个", piece):
            plan.append(("append_structured_log", {"content": piece}))
        elif _BIG_CHANGE.search(piece):
            # 大改交给 Coding CLI：这是 red_confirm，会真的弹确认卡等你点头
            plan.append(("delegate_to_coding_cli", {"prompt": piece}))
        elif _CODE_FILE.search(piece) and _CHANGE_VERB.search(piece):
            plan.append(("dispatch_code_session", {"instruction": piece}))
        elif re.search(r"写代码|改代码|改一下|加个|修复|续接", piece):
            plan.append(("dispatch_code_session", {"instruction": piece}))
        elif re.search(r"服务器|负载|流量|巡检|磁盘|容器|线上", piece):
            plan.append(("inspect_server_ops", {"query": piece}))
        elif re.search(r"撤销|回滚|还原", piece):
            plan.append(("rollback_file", {}))
    return plan


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


_DEFAULT: Executor | None = None


def execute_safely(name: str, args: dict[str, Any] | None = None, *, text: str = "") -> str:
    """模块级便捷入口（测试/脚本用），复用同一个默认执行器。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Executor()
    return _DEFAULT.execute_safely(name, args, text=text)
