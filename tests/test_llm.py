"""大模型接入：网关信封、渠道私有字段、密钥来源、提示词系统。

这一组测试的共同前提是**不发真请求**：真请求会花钱、会超时、会因为对方网关
某天改字段而红。所以全部用 `patch.object(core, "chat")` 或纯函数断言，
只把「我们这一侧有没有把话说对」钉住。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from voxos import config, core, main, prompts
from voxos.config import load

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 网关

def test_gateway_envelope_is_unwrapped():
    """mindax / sub2API 把标准响应包在 {"data": …, "success": true} 里。

    不剥这一层，`body["choices"]` 会 KeyError，而上层把 KeyError 当成
    「模型不可用」——配好了也永远走离线兜底，报错还完全指错方向。
    """
    wrapped = {"data": {"choices": [{"message": {"role": "assistant", "content": "收到"}}]},
               "success": True}
    assert core._unwrap(wrapped)["choices"][0]["message"]["content"] == "收到"


def test_gateway_error_is_not_mistaken_for_an_answer():
    with pytest.raises(RuntimeError, match="LLM"):
        core._unwrap({"success": False, "error": "余额不足"})


def test_reasoning_and_metadata_are_not_replayed():
    """思考链回放给模型，它会把自己的推理当成对话继续往下编（复读的成因）。"""
    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}],
               "reasoning_content": "用户想知道时间…", "provider_metadata": {"cost": "0"}}
    assert core._replayable(message) == {
        "role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}


# ------------------------------------------------------------------ 渠道字段

def test_thinking_is_disabled_on_the_wire(cfg):
    """Cline 渠道不加 thinking:disabled，DeepSeek V4 会把思考链混进 tool_call。"""
    off = cfg.with_overrides(**{"llm.thinking": "disabled"})
    assert core.llm_extra(off) == {"thinking": {"type": "disabled"}}


def test_auto_thinking_sends_nothing(cfg):
    """auto 的意思是「不说」——硬塞一个 enabled 等于替用户改主意。"""
    assert core.llm_extra(cfg.with_overrides(**{"llm.thinking": "auto"})) == {}


def test_cline_is_inferred_when_the_config_predates_provider(cfg):
    """老配置（0.1.18 及以前）没有 provider 这一项，而用户自己的配置不会被升级覆盖。

    如果「要不要关思考」只认配置文件里那一行，升级上来的机器就正好踩中
    「思考链混进 tool_call」那个坑——而且现象是「模型好像有点傻」，
    没人会怀疑到配置项缺失上。
    """
    legacy = cfg.with_overrides(**{"llm.thinking": "",
                                   "llm.model": "cline-pass/deepseek-v4.1-flash-1017"})
    assert core.provider(legacy) == "cline"
    assert core.llm_extra(legacy) == {"thinking": {"type": "disabled"}}


def test_unknown_model_stays_on_the_openai_default(cfg):
    """不认识的模型名不该被猜成 cline——猜错就会给 OpenAI 端点塞私有字段。"""
    assert core.provider(cfg.with_overrides(**{"llm.model": "qwen2.5-coder:14b"})) == "openai"


def test_extra_body_is_merged(cfg):
    merged = cfg.with_overrides(**{"llm.extra_body": '{"reasoning_effort": "low"}'})
    assert core.llm_extra(merged)["reasoning_effort"] == "low"


def test_broken_extra_body_does_not_break_dispatch(cfg):
    """手写的 JSON 有错，坏的是那一行配置，不该是整句派发。"""
    broken = cfg.with_overrides(**{"llm.extra_body": "{not json"})
    assert core.llm_extra(broken) == {}


# ------------------------------------------------------------------ 可用性

def test_missing_key_skips_the_llm_entirely(cfg, executor, monkeypatch):
    """没配钥匙就一个请求都别发：发了也只是白等一次超时。"""
    armed = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "https://example.invalid/v1",
                                 "llm.api_key": ""})

    def explode(**_):                      # 一旦被调用就说明发请求了
        raise AssertionError("缺 Key 时不该发请求")

    monkeypatch.setattr(core, "chat", explode)
    result = core.dispatch("帮我订一张去上海的机票", cfg=armed, executor=executor)
    assert result.route == "ignore"
    assert "缺 API Key" in result.reply          # 说清缺什么，而不是「还没学会」


def test_key_file_is_read_when_config_is_silent(tmp_path):
    """密钥单独放一个文件：写进 settings.toml 等于往公开仓库推一次。"""
    key_file = tmp_path / "llm_key"
    key_file.write_text("sk-from-file\nignored-second-line\n", encoding="utf-8")
    target = tmp_path / "settings.toml"
    target.write_text(f'[llm]\napi_key_file = "{key_file}"\n', encoding="utf-8")
    assert load(target).get("llm.api_key") == "sk-from-file"


def test_config_key_wins_over_the_file(tmp_path):
    key_file = tmp_path / "llm_key"
    key_file.write_text("sk-from-file\n", encoding="utf-8")
    target = tmp_path / "settings.toml"
    target.write_text(f'[llm]\napi_key = "sk-explicit"\napi_key_file = "{key_file}"\n',
                      encoding="utf-8")
    assert load(target).get("llm.api_key") == "sk-explicit"


def test_missing_key_file_is_not_an_error(tmp_path):
    target = tmp_path / "settings.toml"
    target.write_text('[llm]\napi_key_file = "/nonexistent/llm_key"\n', encoding="utf-8")
    assert load(target).get("llm.api_key") in ("", None)


def test_status_says_why_the_model_is_unavailable(cfg):
    """界面上「开了」和「能用」必须分得开，否则用户以为它在思考。"""
    armed = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "https://example.invalid/v1",
                                  "llm.api_key": ""})
    ready, why = core.llm_ready(armed)
    assert ready is False and "Key" in why
    assert "Key" in main._llm_note(armed)


def test_ollama_needs_no_key(cfg):
    """本地模型没有密钥这回事：要求 Key 会把它挡在门外。"""
    local = cfg.with_overrides(**{"llm.enabled": True, "llm.provider": "ollama",
                                  "llm.base_url": "http://127.0.0.1:11434/v1", "llm.api_key": ""})
    assert core.llm_ready(local)[0] is True


# ------------------------------------------------------------------ 提示词

def test_role_prompt_is_appended_not_substituted(cfg):
    """角色是**叠加**：少一段纪律顶多不听话，缺了调度规则就完全跑偏。"""
    text = prompts.active(cfg.with_overrides(**{"workspace.role": "OPS"}),
                          role="OPS", now="t", active_window="w", workspace="s",
                          recent_files="r", max_rounds=3)
    assert "意图调度内核" in text and "OPS 纪律" in text


def test_user_prompt_overrides_the_bundled_one(cfg):
    """`~/.voxos/prompts/dispatch.md` 优先于随包那份，且存盘即生效（按 mtime 失效）。"""
    override = cfg.data_root / ".voxos" / "prompts" / "dispatch.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("你是改过的内核。角色 {role}", encoding="utf-8")
    assert prompts.render("dispatch", cfg, role="OPS") == "你是改过的内核。角色 OPS"


def test_unknown_placeholders_in_a_handwritten_prompt_survive(cfg):
    """用户提示词里写 `{"a": 1}` 是常事；用 format_map 会当场 KeyError。"""
    override = cfg.data_root / ".voxos" / "prompts" / "dispatch.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text('示例 {"a": 1} 与 {role} 和 {unknown}', encoding="utf-8")
    text = prompts.render("dispatch", cfg, role="OPS")
    assert text == '示例 {"a": 1} 与 OPS 和 {unknown}'


def test_bundled_prompt_matches_the_builtin_default():
    """随包文件和代码里那份兜底必须一字不差。

    两份存在的理由不同（一份给人改，一份保证任何机器起得来），
    但内容一旦分叉，「我在仓库里改的」和「它实际用的」就不是同一句了。
    """
    bundled = (ROOT / "config" / "prompts" / "dispatch.md").read_text(encoding="utf-8")
    assert bundled.strip() == prompts.DEFAULT_DISPATCH.strip()


def test_every_role_has_a_prompt():
    for role in ("developer", "ops", "doctor"):
        assert (ROOT / "config" / "prompts" / f"{role}.md").exists(), role
        assert role in prompts.DEFAULT_ROLES


def test_prompts_command_shows_sources_and_text(cfg, capsys):
    """`voxos prompts` 要能回答「我改了文件怎么没反应」。"""
    assert main.cmd_prompts(argparse.Namespace(role="OPS"), cfg) == 0
    out = capsys.readouterr().out
    assert "dispatch" in out and "OPS 纪律" in out


def test_llm_command_says_where_the_key_file_goes(cfg, capsys):
    armed = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "https://example.invalid/v1"})
    assert main.cmd_llm(argparse.Namespace(set_key=None, check=False), armed) == 0
    out = capsys.readouterr().out
    assert "缺 API Key" in out and "llm_key" in out


def test_set_key_writes_a_private_file(cfg, capsys):
    """0600 是必须的：家目录里的明文密钥不该让同机其它账号读到。"""
    assert main.cmd_llm(argparse.Namespace(set_key="sk-test", check=False), cfg) == 0
    path = cfg.path("llm.api_key_file", ".voxos/llm_key")
    assert path.read_text(encoding="utf-8").strip() == "sk-test"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_every_prompt_file_is_declared_for_packaging():
    """随包资源漏一个，装出来就静默退回代码里那份——本机测不出来，装完才发现。

    「本机测试全绿、装出来行为不对」是这个仓库踩过最多次的一类坑
    （0.1.11 的相对路径、0.1.12 的只读配置、0.1.16 漏 inject onnxruntime）。
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    missing = [path.name for path in (ROOT / "config" / "prompts").glob("*.md")
               if f"config/prompts/{path.name}" not in pyproject]
    assert not missing, f"pyproject 的 data-files 没声明：{missing}"


def test_default_settings_point_at_the_cline_channel():
    """出厂默认渠道就是 cline-pass/1017，且思考关闭——这是用户明确要的。"""
    settings = config.load(ROOT / "config" / "settings.toml")
    assert settings.get("llm.provider") == "cline"
    assert settings.get("llm.model") == "cline-pass/deepseek-v4.1-flash-1017"
    assert settings.get("llm.thinking") == "disabled"
    assert json.loads(json.dumps(core.llm_extra(settings))) == {"thinking": {"type": "disabled"}}


# ------------------------------------------------------------------ 多轮上下文

def _armed(cfg):
    """把模型「配好」：不发真请求（`chat` 会被替换掉），但 `llm_ready` 要过。"""
    return cfg.with_overrides(**{"llm.enabled": True,
                                 "llm.base_url": "https://example.invalid/v1",
                                 "llm.api_key": "sk-test"})


def test_second_turn_sees_the_first(cfg, executor, monkeypatch):
    """每轮都把历史清空 = 模型永远失忆。

    用户说「再正式一点」「那个文件」时，模型手里只有这一句，无从指代——
    这不是模型笨，是我们没把话传过去。
    """
    seen: list[list[dict]] = []

    def fake(**kw):
        seen.append([dict(m) for m in kw["messages"]])
        return {"role": "assistant", "content": f"第{len(seen)}轮的回答"}

    monkeypatch.setattr(core, "chat", fake)
    armed = _armed(cfg)
    core.dispatch("帮我把那段说明翻译成英文", cfg=armed, executor=executor)
    core.dispatch("再正式一点", cfg=armed, executor=executor)

    assert [m["role"] for m in seen[0]] == ["system", "user"]
    # 历史排在 system **之后**：放前面会让网关的前缀缓存每轮全量重算
    assert [m["role"] for m in seen[1]] == ["system", "user", "assistant", "user"]
    assert seen[1][1]["content"] == "帮我把那段说明翻译成英文"
    assert seen[1][2]["content"] == "第1轮的回答"
    assert seen[1][3]["content"] == "再正式一点"


def test_a_failed_turn_does_not_poison_the_context(cfg, executor):
    """没配模型时每一句都回「还没学会」，把二十条这种话塞进上下文，
    模型下次一开口就被带偏——而且这些「回答」本身没有任何信息量。"""
    for _ in range(3):
        core.dispatch("帮我订一张去上海的机票", cfg=cfg, executor=executor)
    assert core.history() == []


def test_history_keeps_only_the_last_few_turns(cfg, executor, monkeypatch):
    """留几轮是为了「指代」和「接着做」，不是为了记住一辈子。

    不设上限的话，一次长对话会把每一轮的 token 都重新算一遍：慢，而且贵。
    """
    monkeypatch.setattr(core, "chat", lambda **_: {"role": "assistant", "content": "嗯"})
    armed = _armed(cfg)
    for i in range(10):
        core.dispatch(f"第{i}个请求", cfg=armed, executor=executor)

    hist = core.history()
    assert len(hist) == core._MAX_TURNS * 2
    assert hist[0]["content"] == "第4个请求"        # 10 轮留最后 6 轮
    assert hist[-1]["content"] == "嗯"


def test_tool_output_is_not_replayed_as_context(cfg, executor, monkeypatch):
    """只记「说了什么」，不记「跑了什么」。

    上一轮 `df -h` 的整段输出如果留在上下文里，这一轮的注意力会被它拉走——
    而且那是几 KB 的 token，每轮都重发一次。
    """
    rounds = {"n": 0}

    def fake(**_):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "1", "type": "function",
                                    "function": {"name": "tell_time", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "现在是十点"}

    monkeypatch.setattr(core, "chat", fake)
    monkeypatch.setattr(core, "_fast_route", lambda *_: None)
    core.dispatch("现在到底几点了", cfg=_armed(cfg), executor=executor)

    assert [m["role"] for m in core.history()] == ["user", "assistant"]
    assert core.history()[1]["content"] == "现在是十点"


# ------------------------------------------------------------------ 打断与上下文

def test_interrupt_rewrites_the_context_to_what_was_heard(cfg, executor, monkeypatch):
    """抄 OpenAI `conversation.item.truncate`：截断音频时**同时**把文本记录改短。

    不改的话，模型以为自己把三段话都说完了，下一轮会接着用户根本没听到的
    第三段往下说——用户只觉得「它怎么答非所问」，而且完全没法解释。
    """
    full = "第一句。第二句。第三句。"
    monkeypatch.setattr(core, "chat", lambda **_: {"role": "assistant", "content": full})
    core.dispatch("讲个长一点的说明", cfg=_armed(cfg), executor=executor)

    core.note_spoken(full, "第一句。")
    assert core.history()[-1]["content"] == "第一句。……（被打断，后面没念）"


def test_a_fully_heard_reply_is_left_alone(cfg, executor, monkeypatch):
    """一句没漏就别乱改——上下文里多一行「被打断」会让模型以为自己总被打断。"""
    full = "好的。"
    monkeypatch.setattr(core, "chat", lambda **_: {"role": "assistant", "content": full})
    core.dispatch("你好呀朋友", cfg=_armed(cfg), executor=executor)

    core.note_spoken(full, full)
    assert core.history()[-1]["content"] == full


def test_note_spoken_cannot_clobber_a_later_turn(cfg, executor, monkeypatch):
    """播报是异步的：`note_spoken` 回来时可能已经又聊过一轮。

    只按「最后一条」改是危险的——那会把**新一轮**的回答改写成上一轮的残句。
    所以要先确认最后一条还是自己那段文本。
    """
    rounds = {"n": 0}

    def fake(**_):
        rounds["n"] += 1
        return {"role": "assistant", "content": f"第{rounds['n']}段"}

    monkeypatch.setattr(core, "chat", fake)
    armed = _armed(cfg)
    core.dispatch("第一件事", cfg=armed, executor=executor)
    core.dispatch("第二件事", cfg=armed, executor=executor)

    core.note_spoken("第1段", "第")          # 迟到的回调
    assert core.history()[-1]["content"] == "第2段"

