"""内核测试：Schema 自省、快慢双通道路由、唤醒词、离线兜底。"""

from __future__ import annotations

import time

from voxos import core


def test_schema_generated_from_annotations():
    schema = core.REGISTRY["write_code"].schema["function"]
    params = schema["parameters"]
    assert params["properties"]["file_path"]["type"] == "string"
    assert params["properties"]["operation"]["enum"] == ["CREATE_OR_OVERWRITE", "APPEND_CODE", "REPLACE_SNIPPET"]
    assert params["required"] == ["file_path", "operation"]          # 有默认值的不算必填
    assert params["properties"]["new_code"]["default"] == ""
    assert params["properties"]["new_code"]["description"]           # docstring Args 被解析
    assert schema["description"].startswith("按操作类型")


def test_every_tool_has_schema_and_risk():
    assert len(core.REGISTRY) >= 10
    for name, meta in core.REGISTRY.items():
        assert meta.schema["function"]["name"] == name
        assert meta.risk.value in {"GREEN", "YELLOW", "RED_CONFIRM", "RED_FORBIDDEN"}


def test_fast_route_decision_latency():
    text = "Siri，我要写日志，今天把登录接口的空值校验补上了"
    wake = core._wake_pattern(core.load())
    core._fast_route(text, wake)  # 预热
    samples = []
    for _ in range(2000):
        started = time.perf_counter()
        core._fast_route(text, wake)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < 5.0, f"快通道 p95={p95:.3f}ms 超过 5ms 预算"


def test_fast_route_matches_three_scenarios():
    wake = core._wake_pattern(core.load())
    assert core._fast_route("Siri，我要写日志，修好了空指针", wake)[0] == "append_structured_log"
    assert core._fast_route("Siri，我要写代码，给登录加验证码", wake)[0] == "dispatch_code_session"
    assert core._fast_route("Siri，检查一下服务器负载", wake)[0] == "inspect_server_ops"
    assert core._fast_route("小助手，撤销刚才的修改", wake)[0] == "rollback_file"


def test_wake_word_optional():
    wake = core._wake_pattern(core.load())
    assert core._fast_route("我要写日志，没有唤醒词也行", wake)[0] == "append_structured_log"
    assert core._fast_route("jarvis 我要写日志，带唤醒词", wake)[0] == "append_structured_log"


def test_dispatch_fast_path_writes_log(cfg, executor):
    result = core.dispatch("Siri，我要写日志，今天把并发登录改成了令牌桶", executor=executor, cfg=cfg)
    assert result.route == "fast"
    assert "日志已记录" in result.reply
    log = cfg.path("workspace.daily_log_dir") / f"{time.strftime('%Y-%m-%d')}.md"
    assert "令牌桶" in log.read_text(encoding="utf-8")


def test_dispatch_ignores_chitchat(cfg, executor):
    assert core.dispatch("今天天气不错啊", executor=executor, cfg=cfg).route == "ignore"
    assert core.dispatch("   ", executor=executor, cfg=cfg).route == "ignore"


def test_offline_multi_intent_plan(cfg, executor):
    result = core.dispatch("记一下日志，然后检查服务器负载", executor=executor, cfg=cfg)
    assert result.route == "offline"
    assert len(result.results) == 2
    assert "负载" in result.reply


def test_multi_intent_beats_fast_path(cfg, executor):
    """快通道抢到第一件事就返回，会把后面那件悄悄吞掉——这条盯的就是它。"""
    result = core.dispatch("记一下今天修了登录 bug，顺便看下服务器负载",
                           executor=executor, cfg=cfg)
    assert len(result.results) == 2, result.results
    assert "日志已记录" in result.reply and "负载" in result.reply


def test_multi_intent_route_label_tells_the_truth(cfg, executor):
    """两段都命中快通道就说 fast；有一段落到启发式就说 offline。别冤枉快通道。"""
    all_fast = core.dispatch("写日志 修完了登录接口，然后检查服务器", executor=executor, cfg=cfg)
    assert all_fast.route == "fast", all_fast.route
    assert len(all_fast.results) == 2
    mixed = core.dispatch("写日志 修完了登录接口，顺便把 auth.py 的空值校验补上",
                          executor=executor, cfg=cfg)
    assert mixed.route == "offline", mixed.route
    assert len(mixed.results) == 2


def test_single_intent_with_inline_connector_is_not_split(cfg, executor):
    """「顺便」出现在句子中间时不能切——切了会把文件名丢掉。"""
    result = core.dispatch("把 auth.py 顺便重构一下", executor=executor, cfg=cfg)
    assert len(result.results) == 1
    assert result.tool == "delegate_to_coding_cli"


def test_heuristic_plan_never_guesses(cfg):
    assert core.heuristic_plan("帮我写一首诗", cfg) == []


def test_chat_reports_unreachable_llm():
    try:
        core.chat(base_url="http://127.0.0.1:9/v1", api_key="x", model="m",
                  messages=[{"role": "user", "content": "hi"}], timeout=0.5)
    except RuntimeError as exc:
        assert "LLM" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛出 RuntimeError")


def test_dispatch_falls_back_when_llm_broken(cfg, executor):
    broken = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "http://127.0.0.1:9/v1"})
    result = core.dispatch("我要写日志，模型挂了也要能记", executor=executor, cfg=broken)
    assert result.route == "fast"          # 快通道不受 LLM 影响
    assert "日志已记录" in result.reply
