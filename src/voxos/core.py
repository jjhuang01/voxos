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
from .prompts import DEFAULT_DISPATCH, active as _active_prompt
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
    "llm_ready", "llm_extra", "provider", "history", "reset_history",
    "note_spoken", "note_tts_failure", "tts_failure",
]


# ---------------------------------------------------------------- 快慢双通道

# 出厂那份调度提示词。真正生效的是 `config/prompts/dispatch.md`（可被
# `~/.voxos/prompts/dispatch.md` 覆盖），这里只留同名副本给文档和兜底引用。
SYSTEM_PROMPT = DEFAULT_DISPATCH


@dataclass
class DispatchResult:
    route: str            # fast | llm | offline | ignore | error
    text: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    latency_ms: float = 0.0
    results: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 多轮上下文

# 一句话进来，模型只看得到这一句，等于每轮都失忆。用户说「再来一次」「换成英文」
# 「那个文件」时，模型无从指代——这是「智能」上最便宜的一大步。
#
# 刻意**不记** `fast` / `offline` 两条路的工具输出：那些结果已经进了当轮上下文，
# 再存一份会让下一次调用被上一轮的执行细节带偏（比如上一轮 `df -h` 的整段输出
# 会把这一轮的注意力拉走）。对话要记的是「说了什么」，不是「跑了什么」。
_HISTORY: list[dict[str, str]] = []
_MAX_TURNS = 6                    # 6 轮 ≈ 12 条消息；再多只是把延迟和费用顶上去


def _remember(result: DispatchResult) -> DispatchResult:
    """把这一轮记进上下文，原样返回结果（这样 `dispatch` 一行都不用多）。"""
    if result.route not in ("chat", "llm") or not result.reply.strip():
        return result
    _HISTORY.append({"role": "user", "content": result.text})
    _HISTORY.append({"role": "assistant", "content": result.reply})
    # 先判长度再切：`_HISTORY[:-0]` 是**空列表**，`del` 它会把整段历史清空。
    # 这个坑只在「刚好没超长」的那一刻才炸，靠读代码看不出来。
    if len(_HISTORY) > _MAX_TURNS * 2:
        del _HISTORY[:-_MAX_TURNS * 2]
    return result


def history() -> list[dict[str, str]]:
    """最近几轮对话。给测试和 `/api/status` 用。"""
    return list(_HISTORY)


def reset_history() -> None:
    _HISTORY.clear()


def note_spoken(full: str, spoken: str) -> None:
    """播报结束后校正上下文：只留**用户真听到的那部分**。

    抄 OpenAI `conversation.item.truncate` 的语义——打断时截断音频，**同时**把
    服务端那段文本记录改短，注释原话是「确保上下文中没有用户不知道的文本」。
    不这么做的话，模型会接着一段用户根本没听到的话往下说，用户只觉得
    「它怎么答非所问」，而且完全没法解释。
    """
    if not _HISTORY or _HISTORY[-1]["role"] != "assistant":
        return
    if _HISTORY[-1]["content"] != full:
        return                 # 中间又聊过一轮：别动别人的记录
    if spoken == full:
        return                 # 一句没漏，不用改
    _HISTORY[-1]["content"] = spoken + "……（被打断，后面没念）"


_TTS_FAILURE: dict[str, str] = {}


def note_tts_failure(reason: str) -> None:
    """云端合成失败了。**必须记下来**。

    用户听到的是系统 `say` 的机器腔，但「为什么不是那个好听的声音」得能查到——
    否则下一次只会觉得「这个功能时好时坏」，而且没有任何线索。
    """
    _TTS_FAILURE["last"] = (reason or "").strip()[:200]


def tts_failure() -> str:
    """最近一次云端合成失败的原因（没失败过就是空串）。"""
    return _TTS_FAILURE.get("last", "")


def chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
    timeout: float = 20.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OpenAI 兼容 /chat/completions（DeepSeek / Ollama / Cline 网关通用），零 SDK 依赖。"""
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if extra:
        payload.update(extra)             # 渠道私有字段：thinking / reasoning_effort 等
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
    return _unwrap(body)["choices"][0]["message"]


def _unwrap(body: dict[str, Any]) -> dict[str, Any]:
    """剥掉网关的信封。

    mindax / sub2API 这类网关把标准 OpenAI 响应整个包进 `{"data": …, "success": true}`，
    直接取 `body["choices"]` 会 KeyError——而那个 KeyError 会被上层当成「模型不可用」，
    于是配好了也永远走离线兜底，报错信息还完全指错方向。
    """
    inner = body.get("data")
    if isinstance(inner, dict) and "choices" in inner:
        return inner
    if body.get("error") or body.get("success") is False:
        raise RuntimeError(f"LLM 返回错误：{str(body)[:200]}")
    return body


def llm_ready(cfg: Config) -> tuple[bool, str]:
    """慢通道现在能不能真的发出去；不能就说明缺哪一样。

    `_try_llm` 用它做闸门：缺 Key 就一个请求都不发，省掉一次注定失败的等待。
    但**闸门必须有出口**——状态页、`voxos doctor`、`voxos llm` 都把 `why` 显示出来。
    否则「开了但没配 Key」会悄悄退回离线兜底，用户以为模型在思考，
    其实一个请求都没发出去。
    """
    if not cfg.flag("llm.enabled", False):
        return False, "未启用"
    if not str(cfg.get("llm.base_url", "")).strip():
        return False, "缺接口地址"
    if provider(cfg) not in ("ollama", "local") and not str(cfg.get("llm.api_key", "")).strip():
        return False, "缺 API Key"
    return True, str(cfg.get("llm.model", ""))


def provider(cfg: Config) -> str:
    """渠道名。配置里没写就按模型名前缀认。

    老配置（0.1.18 及以前）没有 `provider` 这一项，而用户自己的
    `~/.voxos/settings.toml` **不会被升级覆盖**——那是他的文件，不该被动。
    所以「这个渠道要不要关思考」不能指望配置文件里有那一行，得由代码兜住：
    `cline-pass/` 开头的模型只可能是 Cline 渠道。
    """
    explicit = str(cfg.get("llm.provider", "")).strip().lower()
    if explicit:
        return explicit
    return "cline" if str(cfg.get("llm.model", "")).strip().lower().startswith("cline-pass/") \
        else "openai"


def _default_thinking(cfg: Config) -> str:
    """没写 `thinking` 时的默认值：Cline 必须关，其它渠道不传这个字段。"""
    return "disabled" if provider(cfg) == "cline" else "auto"


def llm_extra(cfg: Config) -> dict[str, Any]:
    """渠道私有字段。

    Cline 渠道（sub2API）实测：不加 `thinking: {"type": "disabled"}` 时，DeepSeek V4
    会把思考链混进 tool_call 的参数流，结构化调用直接失败；加上之后 3/3 成功。
    交接文档见 yq/memories 的 `cline-deepseek-dsml-tool-call-leak.md`。
    """
    extra: dict[str, Any] = {}
    thinking = str(cfg.get("llm.thinking") or "").strip().lower() or _default_thinking(cfg)
    if thinking in ("disabled", "enabled"):
        extra["thinking"] = {"type": thinking}
    raw = cfg.get("llm.extra_body", "")
    if isinstance(raw, dict):
        extra.update(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            extra.update(json.loads(raw))
        except json.JSONDecodeError:      # 手写的 JSON 有错：忽略，别让整句派发挂掉
            pass
    return extra


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
    mode: str = "assistant",
) -> DispatchResult:
    """一句话进，动作出。顺序：快通道 → LLM 慢通道 → 离线启发式。

    `mode` 只影响慢通道的**说话方式**，不影响工具权限：
    命令该走哪条路还是哪条路，绝不因为「在聊天」就执行不了。
    """
    ensure_tools()
    cfg = cfg or (executor.cfg if executor else load())
    executor = executor or Executor(cfg)
    started = time.perf_counter()
    raw = (text or "").strip()
    if not raw:
        return DispatchResult(route="ignore", text=raw, reply="")

    quick = _quick_route(raw, cfg, executor, started)
    if quick is not None:
        return quick

    if (chat := _small_talk(raw)):
        return _remember(DispatchResult("chat", raw, reply=chat, latency_ms=_ms(started)))

    llm = _try_llm(raw, executor, cfg, dict(context or {}, mode=mode), started)
    if llm is not None:
        return _remember(llm)

    plan = heuristic_plan(raw, cfg)
    if not plan:
        return DispatchResult("ignore", raw, reply=_no_action_reply(cfg), latency_ms=_ms(started))
    results = [executor.execute_safely(name, args, text=raw) for name, args in plan]
    name, args = plan[0]
    return DispatchResult("offline", raw, name, args, "；".join(results), _ms(started), results)


def _quick_route(
    raw: str, cfg: Config, executor: Executor, started: float
) -> DispatchResult | None:
    """多意图切分 + 快通道。命中就返回结果，没命中交回给慢通道。"""
    if _segments(raw, cfg) > 1:
        multi = _multi_intent(raw, cfg, executor, started)
        if multi is not None:
            return multi
    hit = _fast_route(raw, _wake_pattern(cfg))
    if not hit:
        return None
    name, args = hit
    reply = executor.execute_safely(name, args, text=raw)
    return DispatchResult("fast", raw, name, args, reply, _ms(started), [reply])


def _small_talk(text: str) -> str:
    """问候/道谢/问身份：给一句人话，而不是「没听懂」。"""
    for pattern, reply in _CHAT:
        if pattern.match(text):
            return reply
    return ""


def _try_llm(
    text: str, executor: Executor, cfg: Config, context: dict[str, Any], started: float
) -> DispatchResult | None:
    """慢通道。不可用或失败时返回 None，交回给离线规划。

    一旦本轮已经真的执行过工具，失败就**不再**回落离线规划：否则一次派发会因为
    总结阶段的网络抖动被执行第二遍，副作用翻倍。
    """
    if not llm_ready(cfg)[0]:
        return None            # 没开 / 没地址 / 没钥匙：一个请求都别发，直接走离线兜底
    state: dict[str, Any] = {"executed": False, "results": []}
    try:
        return _llm_route(text, executor, cfg, context, started, state)
    except Exception as exc:
        executor.on_event("blocked", "模型不可用", str(exc))
        if not state["executed"]:
            return None
        return DispatchResult("llm", text, reply=f"已执行，但总结失败：{exc}",
                              latency_ms=_ms(started), results=list(state["results"]))


def _multi_intent(
    text: str, cfg: Config, executor: Executor, started: float
) -> DispatchResult | None:
    """一句话里有好几件事：逐段规划，别让快通道抢到第一件就吃掉后面的。

    只有一段能规划出来时返回 None，交回给正常路径（那样路由标签才准）。
    """
    plan, fast_only = _segment_plan(text, cfg)
    if len(plan) < 2:
        return None
    results = [executor.execute_safely(name, args, text=text) for name, args in plan]
    name, args = plan[0]
    return DispatchResult("fast" if fast_only else "offline", text, name, args,
                          "；".join(results), _ms(started), results)


def _segments(text: str, cfg: Config | None = None) -> int:
    """数一句话里有几段（唤醒词先剥掉再数）。"""
    wake = _wake_pattern(cfg)
    return sum(1 for piece in _SPLIT.split(text) if (wake.sub("", piece.strip()) if wake else piece.strip()))


def _segment_plan(
    text: str, cfg: Config | None = None
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """逐段规划：每段先试快通道正则，再落到离线启发式。

    第二个返回值是「每一段都走了快通道」——用来给整句定路由标签，
    否则两段都命中了正则、界面却显示「离线」，等于冤枉了快通道。
    """
    wake = _wake_pattern(cfg)
    plan: list[tuple[str, dict[str, Any]]] = []
    fast_only = True
    for segment in _SPLIT.split(text):
        piece = wake.sub("", segment.strip()) if wake else segment.strip()
        piece = _LEAD.sub("", piece)
        if not piece:
            continue
        hit = _fast_route(piece, wake)
        if hit:
            plan.append(hit)
        else:
            fast_only = False
            plan.extend(heuristic_plan(piece, cfg))
    return plan, fast_only


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


def _prompt_values(cfg: Config, context: dict[str, Any]) -> dict[str, Any]:
    """提示词里能引用的变量。多给一个不影响：模板只替换认得的占位符。"""
    return {
        "role": cfg.get("workspace.role", "DEVELOPER"),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "active_window": context.get("active_window", "unknown"),
        "workspace": context.get("workspace", str(cfg.path("workspace.default_code_dir", "~"))),
        "recent_files": ", ".join(context.get("recent_files", [])[-5:]) or "无",
        "max_rounds": int(cfg.get("llm.max_tool_rounds", 3)),
        "mode": str(context.get("mode", "assistant")),
    }


def _system_message(cfg: Config, context: dict[str, Any]) -> dict[str, Any]:
    return {"role": "system", "content": _active_prompt(cfg, **_prompt_values(cfg, context))}


def _replayable(message: dict[str, Any]) -> dict[str, Any]:
    """回填进对话之前，只留 OpenAI 定义的三件套。

    网关会往 message 里塞 `provider_metadata`（路由、成本、原始 id，几十行）和
    `reasoning_content`（思考链）。把思考链回放给模型，它会把自己的推理当成
    对话的一部分继续往下编——交接文档里那次「复读」就是这么来的。
    """
    return {key: message[key] for key in ("role", "content", "tool_calls") if key in message}


def _execute_calls(
    calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    executor: Executor,
    results: list[str],
    text: str,
) -> tuple[str, dict[str, Any]]:
    """执行一回合内的所有 tool_call，把结果回填到对话里供模型总结。

    解析失败**不执行**：把坏参数换成 `{}` 会调用带默认参数的工具，
    一次格式错误就变成一次真实动作。
    """
    first_name, first_args = "", {}
    for call in calls:
        name = str(call.get("function", {}).get("name", ""))
        raw = call.get("function", {}).get("arguments")
        try:
            args = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            result = "参数不匹配：模型返回的工具参数不是合法 JSON，已拒绝执行"
            results.append(result)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            continue
        if not isinstance(args, dict):
            result = "参数不匹配：模型返回的工具参数必须是 JSON 对象，已拒绝执行"
            results.append(result)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            continue
        first_name, first_args = first_name or name, first_args or args
        result = executor.execute_safely(name, args, text=text)
        results.append(result)
        messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    return first_name, first_args


def _seed_messages(cfg: Config, context: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """系统提示 + 最近几轮 + 这一句。

    历史必须排在 system **之后**：有的网关按消息顺序算前缀缓存，放前面会让每一轮
    都全量重算，白花时间和钱。

    `previous` 单独成一条 system 消息，**不拼进 user 那句**：拼进去模型很可能把
    上一句的指令再执行一遍（「帮我打开微信」+「然后发个消息」会开两次微信）。
    这里明确要求它只用来补全省略的主语/宾语。
    """
    messages = [_system_message(cfg, context), *_HISTORY]
    previous = str(context.get("previous") or "").strip()
    if previous:
        messages.append({"role": "system", "content": (
            f"用户上一句说的是「{previous}」。这一句可能是接着说的，缺主语或宾语时"
            f"参考上一句理解；但**不要重复执行上一句已经做过的事**。")})
    messages.append({"role": "user", "content": text})
    return messages


def _llm_route(
    text: str,
    executor: Executor,
    cfg: Config,
    context: dict[str, Any],
    started: float,
    state: dict[str, Any] | None = None,
) -> DispatchResult:
    messages: list[dict[str, Any]] = _seed_messages(cfg, context, text)
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
            extra=llm_extra(cfg),
        )
        calls = message.get("tool_calls") or []
        if not calls:
            content = (message.get("content") or "").strip()
            if not results:
                return DispatchResult("ignore" if "IGNORE" in content else "llm", text,
                                      reply=content, latency_ms=_ms(started))
            return DispatchResult("llm", text, first_tool, first_args,
                                  content or "；".join(results), _ms(started), results)
        messages.append(_replayable(message))
        if state is not None:
            state["executed"] = True
        name, args = _execute_calls(calls, messages, executor, results, text)
        first_tool, first_args = first_tool or name, first_args or args
        if state is not None:
            state["results"] = list(results)
    return DispatchResult("llm", text, first_tool, first_args, "；".join(results), _ms(started), results)


_SPLIT = re.compile(
    r"(?:然后|接着|再然后"
    r"|[，,;；]\s*(?:然后|顺便|另外|同时|再说|还有)"
    r"|[;；])")

# 闲聊不该回「没听懂」。真机第一次用就撞上了：用户说「你好啊」，
# 界面上连着五条「没听懂，什么也没做」——功能没错，但不像个能对话的东西。
_CHAT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:你好|您好|嗨|哈喽|hello|hi|在吗|在不在|早上好|中午好|下午好|晚上好)"
                r"[啊呀呢嘛吧]*[。！!？?～~，,\s]*$", re.I), "在。"),
    (re.compile(r"^(?:谢谢|多谢|感谢|辛苦了)[啊啦呀您]*[。！!～~]*$"), "不客气。"),
    (re.compile(r"^(?:你是谁|你叫什么|你能做什么|你会什么|你能干嘛|帮助|help)"
                r"[啊呀呢]*[。！!？?]*$", re.I),
     "我是 VoxOS，只听你一个人说话。现在能做：打开应用和文件、跑命令、发消息、"
     "排定时任务、问时间、查服务器负载、记日志。"),
)

# 没匹配到任何能力时的那句话。**只留一份**：终端、接口、历史、界面都说它，
# 曾经三处各写各的（「(无动作)」「没听懂」），用户看到的就是前后矛盾。
NO_ACTION = ("还没学会这句。能做的有：打开应用和文件、跑命令、发消息、"
             "排定时任务、问时间、查服务器负载、记日志。")


def _no_action_reply(cfg: Config) -> str:
    """没匹配到能力时说什么。

    大部分「听不懂」其实是「没配大模型」：正则快通道只认十来种说法，复杂句子
    全靠慢通道。这时候只说「还没学会这句」等于把**系统的配置问题**说成**用户的
    表达问题**——人只会换个说法再试一遍，然后再失败一次。所以两件事一起说：
    现在能做什么，以及复杂句子缺的那一步是什么。
    """
    ready, why = llm_ready(cfg)
    if ready:
        return NO_ACTION
    return f"{NO_ACTION}复杂句子要先接大模型（现在{why}）：voxos llm --set-key"

# 切段后常留下「同时 / 顺便」这类开头词，会让锚定的快通道正则全部失配。
_LEAD = re.compile(r"^(?:同时|顺便|另外|还有|再说|然后|接着|再|也|并)[，,：:\s]*")

# 「把 auth.py 的空值校验补上」——最自然的编程说法。文件名 + 变更动词才认，
# 避免把「把日志改一下」误判成改代码。
_CODE_FILE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|java|rs|rb|php|vue|sql|sh|ya?ml|toml|json|md)\b", re.I)
_CHANGE_VERB = re.compile(r"补|加上|加个|改|删|修|实现|重构|替换|优化")

# 「大改」才交给 Coding CLI（red_confirm，会弹确认卡）。小修小补走 dispatch_code_session，
# 免得每句话都拦一次确认——那样人就会习惯性点允许。
_BIG_CHANGE = re.compile(r"重构|大改|重写|整个项目|整块|全部重写|交给 ?(?:claude|aider|codex|coding)", re.I)

# 写日志要的是**动词**，不是「日志」这个词本身。
# 只按名词匹配会把「看一下日志」「把日志清理一下」「日志在哪」全判成写日志——
# 读和写是相反的动作，用户想查日志，系统却往日记里写了一条。
_WRITE_LOG = re.compile(
    r"(?:写|记)(?:一?[条个下])?(?:开发|今天的)?日志|记录|记一下|记一条|记个")


def heuristic_plan(text: str, cfg: Config | None = None) -> list[tuple[str, dict[str, Any]]]:
    """无模型/断网兜底：只认三大高频场景 + 撤销，命中不了就不做（宁可不做，不能乱做）。"""
    wake = _wake_pattern(cfg)
    plan: list[tuple[str, dict[str, Any]]] = []
    for segment in _SPLIT.split(text):
        piece = wake.sub("", segment.strip()) if wake else segment.strip()
        piece = _LEAD.sub("", piece)
        if not piece:
            continue
        if _WRITE_LOG.search(piece):
            plan.append(("append_structured_log", {"content": piece}))
        elif _BIG_CHANGE.search(piece):
            # 大改交给 Coding CLI：这是 red_confirm，会真的弹确认卡等你点头
            plan.append(("delegate_to_coding_cli", {"prompt": piece}))
        elif _CODE_FILE.search(piece) and _CHANGE_VERB.search(piece):
            plan.append(("dispatch_code_session", {"instruction": piece}))
        elif re.search(r"写代码|改代码|改一下|加个|修复|续接", piece):
            plan.append(("dispatch_code_session", {"instruction": piece}))
        elif re.search(r"服务器|负载|流量|巡检|磁盘|容器|线上|监控", piece):
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
