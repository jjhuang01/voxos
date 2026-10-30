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


def test_previous_utterance_reaches_the_llm_as_its_own_system_note(cfg):
    """上一句要作为**独立的 system 说明**传下去，不能拼进这一句里。

    端点判定会在停顿处切句（实测：停 600ms 且结尾不是未完词就切），切完第二句
    就孤立了——「然后发个消息」没有上文，快通道匹配不上，LLM 也不知道发给谁。

    但也不能把两句拼成一句派发：那样模型会把上一句的指令再执行一遍，
    「帮我打开微信」+「然后发个消息」会开两次微信。所以这里必须是一条
    明确写着「不要重复执行」的说明。
    """
    from voxos import core

    messages = core._seed_messages(cfg, {"previous": "帮我打开微信"}, "然后发个消息")

    notes = [m for m in messages if m["role"] == "system" and "帮我打开微信" in m["content"]]
    assert len(notes) == 1, "上一句应当出现在一条 system 消息里"
    assert "不要重复执行" in notes[0]["content"]
    assert messages[-1] == {"role": "user", "content": "然后发个消息"}


def test_no_previous_means_no_extra_message(cfg):
    """没有上一句就别硬塞一条空说明——那会让模型每次都多读一段废话。"""
    from voxos import core

    plain = core._seed_messages(cfg, {}, "帮我打开微信")
    with_previous = core._seed_messages(cfg, {"previous": "上一句"}, "帮我打开微信")

    assert len(with_previous) == len(plain) + 1


def test_runtime_carries_the_previous_sentence_into_the_next_dispatch(cfg):
    """端到端：第一句记下来，第二句派发时它真的跟着进去了。

    上一条只证明「给了 previous 就会用」。真正会坏的是**没人给**：
    `Runtime.dispatch` 每句都是独立一次调用，没有任何地方记着上一句，
    于是「我断断续续说」的第二句永远是孤立的。这里走
    `record()` → `dispatch()` 两个真实入口，把接线钉住——
    把 `_last_text` 那两行删掉，这条就会红。
    """
    from unittest.mock import patch

    from voxos import core, ui
    from voxos.main import Runtime

    runtime = Runtime(cfg, ui.StatusBus())
    seen: list[dict[str, object]] = []

    def spy(text, **kwargs):
        seen.append(dict(kwargs.get("context") or {}))
        return core.DispatchResult(route="chat", text=text, reply="好的")

    runtime.record("帮我打开微信", core.DispatchResult(route="fast", text="帮我打开微信",
                                                     reply="已打开"))
    with patch.object(core, "dispatch", spy):
        runtime.dispatch("然后发个消息", context={})

    assert seen[0].get("previous") == "帮我打开微信", "第二句派发时上一句必须跟着走"


def test_withdrawal_gets_a_short_answer_not_a_capability_lecture(cfg):
    """答案念完之后说「算了」，应该得到一声「好」，不是一篇功能介绍。

    实测：`dispatch("算了")` 走的是 ignore 分支，回复是
    「还没学会这句。能做的有：打开应用和文件、跑命令、发消息……」
    ——用户想收回，收到的是一段他刚刚才听过的东西。
    """
    from voxos import ui
    from voxos.main import Runtime

    runtime = Runtime(cfg, ui.StatusBus())
    result = runtime.dispatch("算了", context={})

    assert result.reply == "好"
    assert result.route == "chat", "回话要朗读；ignore 那一支是不朗读的"
    assert "还没学会" not in result.reply


def test_withdrawal_denies_a_pending_confirmation(cfg):
    """确认框还挂着的时候说「不用了」，必须真的把它拒掉。

    以前这句连确认框都碰不到：它被当成普通指令派发，确认框只能等超时，
    用户说完「不用了」还得再点一下拒绝。语音能拒不能批——失败关闭。
    """
    import threading

    from voxos import ui
    from voxos.main import Runtime

    runtime = Runtime(cfg, ui.StatusBus())
    runtime.gate._waiters["req-1"] = threading.Event()
    runtime.gate._current = "req-1"

    result = runtime.dispatch("不用了", context={})

    assert result.reply == "好，不做了"
    assert runtime.gate.resolve(True, "req-1") is False, "已经拒过，就不能再批"


def test_a_very_old_utterance_is_skipped_instead_of_executed(cfg):
    """排队排过一整个回合的话，不要再执行——它已经是「过去的意思」了。

    派发是单线程顺序的：一句在跑（实测一次 LLM 回合 4.8 秒），后面说的话只能
    排队。十秒前说的「打开微信」现在才执行，用户早忘了自己说过；而且他刚才
    可能已经改主意了。跳过并说明原因，比默默执行安全。
    """
    import queue
    import threading
    import types
    from unittest.mock import patch

    from voxos import ui
    from voxos.main import _dispatch_loop
    from voxos.main import Runtime

    runtime = Runtime(cfg, ui.StatusBus())
    shown: list[tuple[str, str, str]] = []
    runtime.bus.emit = lambda state, label, detail="": shown.append((state, label, detail))
    executed: list[str] = []
    runtime.dispatch = lambda text, context=None, mode="assistant": executed.append(text)

    pipeline = types.SimpleNamespace(echo=types.SimpleNamespace(is_interrupt=lambda t: False))
    box: queue.Queue = queue.Queue()
    # 时钟钉死：`time.monotonic()` 在容器里可能只有 1 秒，直接减 30 会得到负数
    box.put(types.SimpleNamespace(text="打开微信", score=0.9, audio_ms=1200,
                                  accepted=True, at=970.0))
    box.put(None)

    with patch("voxos.main.time.monotonic", return_value=1000.0):
        assert _dispatch_loop(runtime, pipeline, box, {}, threading.Event()) == 0
    assert executed == [], "过期的话不该被执行"
    assert any("秒前说的" in detail for _, _, detail in shown), "要说明为什么跳过"


def test_a_fresh_utterance_still_runs(cfg):
    """刚说完的话当然要执行——过期判断不能把正常的一句也吞掉。"""
    import queue
    import threading
    import types
    from unittest.mock import patch

    from voxos import ui
    from voxos.main import Runtime, _dispatch_loop

    runtime = Runtime(cfg, ui.StatusBus())
    runtime.bus.emit = lambda state, label, detail="": None
    executed: list[tuple[str, str]] = []

    def fake_dispatch(text, context=None, mode="assistant", on_sentence=None, should_stop=None):
        """替身遵循真实派发签名，内部 TypeError 不触发第二次副作用。"""
        executed.append((text, mode))
        return core.DispatchResult(route="fast", text=text, reply="日志已记录")

    runtime.dispatch = fake_dispatch
    runtime.cfg.data["ui"] = {"earcons": False, "speak": False}

    pipeline = types.SimpleNamespace(echo=types.SimpleNamespace(is_interrupt=lambda t: False))
    box: queue.Queue = queue.Queue()
    box.put(types.SimpleNamespace(text="记一下日志", score=0.9, audio_ms=1200,
                                  accepted=True, at=999.0))
    box.put(None)

    with patch("voxos.main.time.monotonic", return_value=1000.0):
        _dispatch_loop(runtime, pipeline, box, {}, threading.Event())
    assert executed == [("记一下日志", "assistant")], "模式没传下去或话被吞了"


def test_the_voiceprint_hint_only_appears_after_three_rejections(cfg):
    """单次拒绝是正常行为（屋里别人说话就该拒），连续 3 句才提示一次。

    每句都弹提示会把胶囊变成报警器；一次都不弹，用户只能看到「一片安静」。
    """
    import types

    from voxos import ui
    from voxos.main import Runtime, _hint_rejection, _REJECT

    _REJECT.update(voice=0, hinted=False)
    runtime = Runtime(cfg, ui.StatusBus())
    shown: list[str] = []
    runtime.bus.emit = lambda state, label, detail="": shown.append(label)

    rejected = types.SimpleNamespace(reason="声纹不匹配，已丢弃")
    for _ in range(2):
        _hint_rejection(runtime, rejected)
    assert shown == [], "只拒了 2 句，还不该提示"

    _hint_rejection(runtime, rejected)
    assert shown == ["声纹连续拒绝"]

    _hint_rejection(runtime, rejected)
    assert shown == ["声纹连续拒绝"], "同一次故障只提示一次，不要反复弹"


def test_an_unrelated_rejection_resets_the_streak(cfg):
    """「太短」「空转写」这些不是声纹问题，不能算进连续拒绝里。"""
    import types

    from voxos import ui
    from voxos.main import Runtime, _hint_rejection, _REJECT

    _REJECT.update(voice=0, hinted=False)
    runtime = Runtime(cfg, ui.StatusBus())
    shown: list[str] = []
    runtime.bus.emit = lambda state, label, detail="": shown.append(label)

    for _ in range(2):
        _hint_rejection(runtime, types.SimpleNamespace(reason="声纹不匹配，已丢弃"))
    _hint_rejection(runtime, types.SimpleNamespace(reason="太短，已丢弃"))
    _hint_rejection(runtime, types.SimpleNamespace(reason="声纹不匹配，已丢弃"))

    assert shown == [], "中间夹了一条非声纹的，连续计数要断掉"
