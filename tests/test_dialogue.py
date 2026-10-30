"""对话层：模式开关、慢动作确认语、念出来的长度。

这一层没有 I/O，所以断言都是纯字符串——不需要麦克风、不需要扬声器。
"""

from __future__ import annotations

import time

from voxos import core, dialogue, main


def _cfg(cfg, **values):
    return cfg.with_overrides(**values)


def test_unknown_mode_falls_back_to_assistant(cfg):
    """配置写错不该让系统突然变哑：未知值退回默认模式。"""
    assert dialogue.mode(_cfg(cfg, **{"dialogue.mode": "chta"})) == "assistant"
    assert dialogue.mode(_cfg(cfg, **{"dialogue.mode": "  CHAT "})) == "chat"


def test_off_mode_is_reported_as_off(cfg):
    assert dialogue.mode(_cfg(cfg, **{"dialogue.mode": "off"})) == "off"
    assert dialogue.is_chat(_cfg(cfg, **{"dialogue.mode": "off"})) is False


def test_ack_after_zero_disables_confirmation(cfg):
    assert dialogue.ack_after_s(_cfg(cfg, **{"dialogue.ack_after_ms": 0})) == 0.0
    assert dialogue.ack_after_s(_cfg(cfg, **{"dialogue.ack_after_ms": 1200})) == 1.2


def test_ack_says_what_it_understood(cfg):
    """确认语要能区分类别：写代码、巡检、发送不可能都说同一句「收到」。"""
    assert "写" in dialogue.ack("把 auth.py 的空值校验补上", cfg)
    assert "看" in dialogue.ack("看看服务器负载和磁盘", cfg)
    assert "发" in dialogue.ack("给张三发个消息", cfg)
    assert dialogue.ack("随便说点什么", cfg).startswith("收到")


def test_fixed_ack_text_wins(cfg):
    view = _cfg(cfg, **{"dialogue.ack_text": "好嘞"})
    assert dialogue.ack("把 auth.py 的空值校验补上", view) == "好嘞"


def test_shape_drops_what_cannot_be_spoken(cfg):
    raw = ("改好了。\n```python\nprint(1)\n```\n详见 [文档](https://example.com/a)，"
           "或 <https://example.com/b>。")
    spoken = dialogue.shape(raw, cfg)
    assert "```" not in spoken and "https://" not in spoken
    assert "代码已显示在胶囊上" in spoken


def test_shape_truncates_at_a_sentence(cfg):
    view = _cfg(cfg, **{"dialogue.speech_chars": 40})
    raw = ("第一句话说完了。第二句话也还行。第三句话很长很长很长很长很长很长很长很长很长。"
           "第四句话完全不该被念到。")
    spoken = dialogue.shape(raw, view)
    assert len(spoken) <= 44 and spoken.endswith("……")
    assert "第一句话" in spoken and "第四句" not in spoken


def test_chat_mode_allows_a_longer_reply(cfg):
    raw = "这是一段比较长的解释，用来验证聊天模式允许更长的回复被念出来。" * 3
    short = dialogue.shape(raw, cfg)
    chat = dialogue.shape(raw, _cfg(cfg, **{"dialogue.mode": "chat"}))
    assert len(chat) > len(short)


def test_empty_reply_stays_empty(cfg):
    assert dialogue.shape("   ", cfg) == ""
    assert dialogue.shape("```\ncode\n```", cfg) == "代码已显示在胶囊上。"


def test_slow_dispatch_acknowledges_once(cfg, monkeypatch):
    """慢派发只应一声，而且派发本身只发生一次（它带副作用）。"""
    spoke: list[str] = []
    monkeypatch.setattr(main, "_speak_async",
                        lambda piece, pipeline, config: spoke.append(piece))

    class Slow:
        calls = 0

        def dispatch(self, text, context=None, mode="assistant", on_sentence=None, should_stop=None):
            Slow.calls += 1
            time.sleep(0.15)
            return core.DispatchResult("llm", text, reply="好的")

    result = main._dispatch_with_ack(
        Slow(), "把 auth.py 的空值校验补上", object(),
        _cfg(cfg, **{"dialogue.ack_after_ms": 30, "llm.stream": False}))
    assert result.reply == "好的" and Slow.calls == 1
    assert len(spoke) == 1 and "写" in spoke[0]


def test_fast_dispatch_says_nothing_extra(cfg, monkeypatch):
    """快通道本来就几十毫秒，不该为它加一句废话。"""
    spoke: list[str] = []
    monkeypatch.setattr(main, "_speak_async",
                        lambda piece, pipeline, config: spoke.append(piece))

    class Fast:
        def dispatch(self, text, context=None, mode="assistant", on_sentence=None, should_stop=None):
            return core.DispatchResult("fast", text, reply="现在 19:30")

    main._dispatch_with_ack(Fast(), "现在几点", object(),
                            _cfg(cfg, **{"dialogue.ack_after_ms": 200}))
    assert spoke == []


def test_chat_mode_only_adds_a_prompt_section(cfg):
    """聊天模式是「多一段 prompt」，不是「换一套权限」：规则与角色纪律必须原样保留。

    反过来做（chat 用一份独立 system）会悄悄丢掉角色纪律 —— 而角色纪律正是
    「OPS 不许写文件」这类规则的载体。少一段没人会发现，直到某天它真写了文件。

    位置也有讲究：对话段插在规则与角色纪律**之间**，角色纪律仍然收尾。
    模型对最后一段的服从度最高，安全约束不该被「可以聊得久一点」压过去。
    """
    from voxos import prompts

    assistant = prompts.active(cfg, mode="assistant")
    chat = prompts.active(cfg, mode="chat")

    assert "对话模式" not in assistant
    assert "对话模式" in chat
    assert "【规则】" in chat, "加了对话段也不能把基础规则顶掉"
    role_block = assistant.rsplit("\n\n", 1)[-1]
    assert role_block in chat, "角色纪律必须一字不动地保留"
    assert chat.rstrip().endswith(role_block.rstrip()), "角色纪律仍要排在最后说话"
