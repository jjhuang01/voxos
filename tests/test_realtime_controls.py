"""实时控制回归：首句能播、取消不排队、混音不执行、暂停不复活旧话。"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from voxos import audio, core, dsp, main, ui


@pytest.fixture
def playback(cfg, monkeypatch):
    """运行真实流水线与播报队列，只替换扬声器和窗口上下文 I/O。"""
    cfg = cfg.with_overrides(**{"dialogue.mode": "chat", "dialogue.ack_after_ms": 0,
                                "ui.speak": True, "llm.stream": True})
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt([]), dsp.SpeakerLock())
    monkeypatch.setattr(main, "_TTS", {"proc": None, "owner": None, "stopped": False})
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_context", lambda: {})
    said = []
    monkeypatch.setattr(main, "_say_one", lambda text, *args: said.append(text) or True)
    return cfg, pipeline, said


def test_stream_first_sentence_starts_without_a_previous_owner(playback):
    """未开始不是过期；首句必须真的经过播放适配器且仅一次。"""
    cfg, pipeline, said = playback
    speaker = main._StreamSpeaker(pipeline, cfg)
    assert speaker.should_stop() is False
    speaker.enqueue("第一句。")
    speaker.finish("第一句。")
    assert speaker.thread is not None
    speaker.thread.join(timeout=1)
    assert not speaker.thread.is_alive() and said == ["第一句。"]


def test_stop_before_first_sentence_prevents_late_playback(playback):
    """等待首句时点停止，迟到的模型输出不能突然开口。"""
    cfg, pipeline, said = playback
    speaker = main._StreamSpeaker(pipeline, cfg)
    main.stop_speaking()
    speaker.enqueue("迟到的第一句。")
    speaker.finish("迟到的第一句。")
    assert speaker.should_stop() and speaker.thread is None and said == []


def test_real_dispatch_stream_plays_both_sentences(playback, monkeypatch):
    """穿过真实 Runtime/core/句子切分/播报归属，不只验证回调桩。"""
    cfg, pipeline, said = playback
    cfg = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "http://example.invalid",
                                "llm.api_key": "test-only"})
    speakers = []
    real_speaker = main._StreamSpeaker

    def make_speaker(*args):
        speaker = real_speaker(*args)
        speakers.append(speaker)
        return speaker

    monkeypatch.setattr(main, "_StreamSpeaker", make_speaker)
    monkeypatch.setattr(core, "chat", lambda **kwargs: iter(["第一句。", "第二句。"]))
    result = main._dispatch_with_ack(main.Runtime(cfg, ui.StatusBus(None)),
                                    "解释一下当前方案", pipeline, cfg)
    for speaker in speakers:
        if speaker.thread is not None:
            speaker.thread.join(timeout=1)
    assert result.reply == "第一句。第二句。" and result.streamed
    assert said == ["第一句。", "第二句。"]
    assert all(speaker.thread is not None and not speaker.thread.is_alive() for speaker in speakers)


def test_cancel_between_tool_calls_prevents_the_second_write(cfg, monkeypatch):
    """第一条日志写入后取消，同一模型回合的第二条不得产生副作用。"""
    cfg = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "http://example.invalid",
                                "llm.api_key": "test-only", "llm.stream": False})
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    monkeypatch.setattr(main, "_context", lambda: {})
    meta = core.REGISTRY["append_structured_log"]
    original = meta.fn

    def first_then_cancel(**args):
        result = original(**args)
        runtime.cancel()
        return result

    monkeypatch.setitem(core.REGISTRY, meta.name, replace(meta, fn=first_then_cancel))
    calls = [{"id": str(index), "type": "function", "function": {
        "name": "append_structured_log", "arguments": json.dumps({"content": text})}}
        for index, text in enumerate(["第一条测试记录", "第二条不得出现"])]
    monkeypatch.setattr(core, "chat", lambda **kwargs: {"tool_calls": calls})

    result = runtime.dispatch("整理刚才的讨论")

    content = "".join(path.read_text() for path in cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert "第一条测试记录" in content and "第二条不得出现" not in content
    assert result.route == "cancelled"
    assert result.results == ["日志已记录（DEV）"]
    assert result.args == {"content": "第一条测试记录"}
    assert "已返回结果：日志已记录" in result.reply


class _Owner:
    """确定性本人替身；测试不宣称测量真实声纹质量。"""
    profiles = {"normal": np.ones(2)}

    def verify(self, wave):
        return True, 0.99


@pytest.mark.parametrize("aec", [False, True])
def test_overlap_can_stop_but_only_restatement_reaches_final_stt(cfg, aec):
    """关闭或未就绪的 AEC 都不能把外放混音回灌成可执行指令。"""
    finals, fired = [], []

    class Stt:
        def transcribe(self, wave, final=False):
            if final:
                finals.append(wave.copy())
            return "现在几点了"

    pipeline = audio.AudioPipeline(cfg.with_overrides(**{"audio.aec": aec}), Stt(), _Owner(),
                                   on_barge_in=lambda: fired.append(True))
    pipeline.streamer = None
    pipeline.echo.tts_start()
    mixed = np.full(dsp.SR, 0.6, dtype=np.float32)
    fresh = np.full(dsp.SR, 0.1, dtype=np.float32)
    source = np.concatenate([mixed, np.zeros(dsp.SR * 2), fresh, np.zeros(dsp.SR)])

    result = list(pipeline.run(audio.ArraySource(source)))

    assert fired and len(result) == 1 and result[0].accepted and not result[0].during_tts
    assert len(finals) == 1 and float(np.max(finals[0])) <= 0.10001


def test_legacy_mixed_utterance_cannot_execute_a_tool(cfg, monkeypatch):
    """派发端也防守旧代混音，不能只依赖采集端过滤。"""
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt([]), _Owner())
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_speak_async", lambda *args: None)
    utterance = audio.Utterance("写日志，混音不应写入", 0.99, 1000, True, during_tts=True)

    main._dispatch_one(runtime, pipeline, utterance, {})

    assert not list(cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert any("重说" in event["detail"] for event in runtime.bus.history())


def test_pipeline_reset_discards_generator_local_audio(cfg):
    """reset 不只清端点对象，还必须丢掉 run 持有的旧半句话。"""
    finals = []

    class Stt:
        def transcribe(self, wave, final=False):
            if final:
                finals.append(wave.copy())
            return "现在几点了"

    pipeline = audio.AudioPipeline(cfg, Stt(), _Owner())
    pipeline.streamer = None

    def source():
        for _ in range(12):
            yield np.full(512, 0.6, dtype=np.float32)
        pipeline.reset()
        yield from audio.ArraySource(np.concatenate([
            np.full(dsp.SR, 0.1, dtype=np.float32), np.zeros(dsp.SR)]))

    result = list(pipeline.run(source()))

    assert len(result) == 1 and result[0].accepted
    assert len(finals) == 1 and float(np.max(finals[0])) <= 0.10001


def test_stop_control_precedes_optional_status_output(cfg):
    """慢状态写盘不能排在真正的停播控制前。"""
    order = []
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt(["停"]), _Owner(),
                                   on_barge_in=lambda: order.append("stop"),
                                   on_event=lambda *args: order.append("status"))
    pipeline.echo.tts_start()
    list(pipeline.run(audio.ArraySource(np.full(dsp.SR, 0.4, dtype=np.float32))))
    assert order and order[0] == "stop"


def test_pause_generation_discards_pending_audio_and_commands(cfg, monkeypatch):
    """暂停后又恢复，旧队列不能复活；只有当前代的整句能执行。"""
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_RESET_HOOK", {"generation": 1, "epoch": 1, "reset": None})
    runtime = main.Runtime(cfg.with_overrides(**{"ui.speak": False}), ui.StatusBus(None))
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt([]), _Owner())
    pending = queue.Queue()
    pending.put((0, audio.Utterance("写日志，旧话不得执行", 0.99, 1000, True)))
    pending.put((1, audio.Utterance("写日志，新的完整指令", 0.99, 1000, True)))
    pending.put(None)

    main._dispatch_loop(runtime, pipeline, pending, {}, threading.Event())

    content = "".join(path.read_text() for path in cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert "旧话不得执行" not in content and "新的完整指令" in content


def test_recognition_queue_full_does_not_block_stop_control(cfg, monkeypatch):
    """普通指令排满时，识别仍继续；干净的本人取消优先到达当前回合。"""
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_RESET_HOOK", {"generation": 0, "reset": None})
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    runtime._active_cancel = threading.Event()
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt([]), _Owner())
    pending = queue.Queue(maxsize=4)
    for index in range(4):
        pending.put((0, audio.Utterance(f"排队指令{index}", 0.99, 1000, True)))
    chunks = queue.Queue()
    chunks.put(None)
    utterances = [audio.Utterance("写日志，队列已经满了", 0.99, 1000, True),
                  audio.Utterance("取消", 0.99, 1000, True)]
    monkeypatch.setattr(pipeline, "run", lambda source: iter(utterances))

    worker = threading.Thread(target=main._recognize_loop,
                              args=(pipeline, chunks, pending, threading.Event(), runtime), daemon=True)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive(), "普通指令积压把识别和插话一起阻塞了"
    assert runtime._active_cancel.is_set()


@pytest.mark.parametrize("action", ["stop", "replace"])
def test_waiting_enqueue_cannot_reclaim_cancelled_or_replaced_playback(playback, monkeypatch, action):
    """排队等待播放锁期间取消或换回合，旧生产者不能重新启动播报。"""
    cfg, pipeline, said = playback
    speaker = main._StreamSpeaker(pipeline, cfg)
    checked, proceed = threading.Event(), threading.Event()
    real_stale = main._is_stale

    def stale(owner):
        result = real_stale(owner)
        if owner is speaker.owner and not checked.is_set():
            checked.set()
            assert proceed.wait(1)
        return result

    monkeypatch.setattr(main, "_is_stale", stale)
    producer = threading.Thread(target=speaker.enqueue, args=("旧回合不该再播。",), daemon=True)
    producer.start()
    assert checked.wait(1)
    replacement = None
    if action == "stop":
        main.stop_speaking()
    else:
        replacement = main._StreamSpeaker(pipeline, cfg)
    proceed.set()
    producer.join(timeout=1)
    speaker.finish()
    if speaker.thread is not None:
        speaker.thread.join(timeout=1)
    if replacement is not None:
        replacement.enqueue("新回合。")
        replacement.finish("新回合。")
        replacement.thread.join(timeout=1)
    assert not producer.is_alive()
    assert said == ([] if action == "stop" else ["新回合。"])


def test_requested_reset_at_source_end_does_not_finalize_old_audio(cfg):
    """源结束前的取消仍要清掉尾部缓存，不能额外完成一次旧话转写。"""
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt(["写日志，旧半句"]), _Owner())
    def source():
        yield from audio.ArraySource(np.full(6000, 0.6, dtype=np.float32), tail_silence_ms=0)
        pipeline.request_reset()
    assert list(pipeline.run(source())) == []


def test_internal_type_error_never_replays_a_completed_dispatch(cfg, monkeypatch):
    """派发内部报类型错误也不重试；已经写入的日志必须只有一条。"""
    cfg = cfg.with_overrides(**{"ui.speak": False})
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    dispatch = runtime.dispatch

    def fail_after_write(*args, **kwargs):
        dispatch(*args, **kwargs)
        raise TypeError("测试：副作用后的内部类型错误")

    monkeypatch.setattr(runtime, "dispatch", fail_after_write)
    with pytest.raises(TypeError, match="副作用后的内部类型错误"):
        main._dispatch_with_ack(runtime, "写日志，只能写入一次", object(), cfg)
    content = "".join(path.read_text() for path in cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert content.count("只能写入一次") == 1


def test_text_cancel_reaches_active_turn_while_stream_waits_for_first_sentence(playback, monkeypatch):
    """还没收到首句也可用文字取消，不能排在正在等模型的回合后面。"""
    cfg, pipeline, said = playback
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    speaker = main._StreamSpeaker(pipeline, cfg)
    active = threading.Event()
    runtime._active_cancel = active
    runtime._turn_lock.acquire()
    replies = []
    worker = threading.Thread(target=lambda: replies.append(runtime.dispatch("取消")), daemon=True)
    try:
        worker.start()
        worker.join(timeout=0.5)
        assert not worker.is_alive(), "文字取消被排在当前回合之后"
        assert active.is_set() and speaker.should_stop() and said == []
        assert replies[0].route == "cancelled" and "不会撤销" in replies[0].reply
    finally:
        runtime._turn_lock.release()
        worker.join(timeout=1)
        speaker.finish()


def test_recognizer_stops_without_waiting_for_another_audio_chunk(cfg, monkeypatch):
    """退出信号已到时，空音频队列不能把识别线程永久挂住。"""
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_RESET_HOOK", {"generation": 0, "reset": None})
    pipeline = audio.AudioPipeline(cfg, audio.ScriptedStt([]), _Owner())
    chunks, pending = queue.Queue(), queue.Queue()
    stop = threading.Event()
    waiting = threading.Event()
    get = chunks.get

    def observed_get(*args, **kwargs):
        waiting.set()
        return get(*args, **kwargs)

    monkeypatch.setattr(chunks, "get", observed_get)
    worker = threading.Thread(target=main._recognize_loop,
                              args=(pipeline, chunks, pending, stop, main.Runtime(cfg, ui.StatusBus(None))),
                              daemon=True)
    worker.start()
    assert waiting.wait(1)
    stop.set()
    try:
        worker.join(timeout=0.5)
        assert not worker.is_alive(), "退出仍在等待下一块音频"
        assert pending.get_nowait() is None
    finally:
        chunks.put(None)
        worker.join(timeout=1)


def test_speaker_that_never_streams_must_release_the_echo_gate(playback):
    """抢了播放权却一句没播，也必须把回声门放下——否则麦克风从此全哑。

    时序：上一句回复正在播 → 新回合建 speaker 抢占归属（上一轮因此跳过 tts_end）
    → 本回合走 ignore/离线路由，on_sentence 一次都不调用 → finish() 只清归属。
    结果是 `echo.speaking` 永远为真，采集端把每一块音频都门成静音。
    """
    cfg, pipeline, _said = playback
    main._TTS.update(owner=object(), stopped=False)
    pipeline.echo.tts_start()                    # 上一轮正在播
    speaker = main._StreamSpeaker(pipeline, cfg)  # 本轮抢占归属
    assert pipeline.echo.muted
    speaker.finish()                             # 本轮没有可播的句子
    assert not pipeline.echo.speaking, "回声门被永久拉上：之后所有语音都会被门掉"
    pipeline.echo.tick(1000)                     # 播报后的混响消退是有意为之，能排空即可
    assert not pipeline.echo.muted


def test_streamed_ignore_sentinel_is_never_spoken(playback, monkeypatch):
    """模型用 IGNORE 表示「不是对我说的」，流式路径不能把它念出来。"""
    cfg, pipeline, said = playback
    cfg = cfg.with_overrides(**{"llm.enabled": True, "llm.base_url": "http://example.invalid",
                                "llm.api_key": "test-only"})
    speakers = []
    real_speaker = main._StreamSpeaker

    def make_speaker(*args):
        speaker = real_speaker(*args)
        speakers.append(speaker)
        return speaker

    monkeypatch.setattr(main, "_StreamSpeaker", make_speaker)
    monkeypatch.setattr(core, "chat", lambda **kwargs: iter(["IGNORE"]))

    result = main._dispatch_with_ack(main.Runtime(cfg, ui.StatusBus(None)), "今天天气不错",
                                     pipeline, cfg)
    for speaker in speakers:
        if speaker.thread is not None:
            speaker.thread.join(timeout=1)

    assert result.route == "ignore"
    assert said == [], f"哨兵被念出来了：{said}"


def test_chunk_overflow_keeps_a_command_already_recognized(cfg, monkeypatch):
    """采集端丢一块音频，不该连已经识别定稿、正排队的指令一起作废。

    丢块只说明「别把缺口两边拼成一句」，而排在队列里的那句早已定稿；
    以前它和半句一起被静默丢弃，用户看到的是「说了没反应」且时间线空白。
    """
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_RESET_HOOK", {"generation": 0, "epoch": 0, "reset": None})
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    runtime = main.Runtime(cfg.with_overrides(**{"ui.speak": False}), ui.StatusBus(None))
    pipeline = SimpleNamespace(echo=SimpleNamespace(is_interrupt=lambda text: False))
    box: queue.Queue = queue.Queue()
    box.put((0, audio.Utterance("写日志，丢块前已定稿", 0.99, 1000, True, at=999.0)))
    main._invalidate_audio(drop_commands=False)   # 采集端丢块：只换代，不作废已定稿指令
    box.put(None)

    main._dispatch_loop(runtime, pipeline, box, {}, threading.Event())

    content = "".join(path.read_text() for path in cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert "丢块前已定稿" in content, "已识别定稿的指令被丢块连坐丢弃了"


def test_pause_still_discards_a_queued_command_and_says_why(cfg, monkeypatch):
    """暂停/取消是「我的意思变了」，排队中的指令要作废，并且必须留痕。"""
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_RESET_HOOK", {"generation": 0, "epoch": 0, "reset": None})
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    runtime = main.Runtime(cfg.with_overrides(**{"ui.speak": False}), ui.StatusBus(None))
    dropped: list[str] = []
    runtime.note_dropped = lambda utterance: dropped.append(utterance.text)
    pipeline = SimpleNamespace(echo=SimpleNamespace(is_interrupt=lambda text: False))
    box: queue.Queue = queue.Queue()
    box.put((0, audio.Utterance("暂停前说的那句", 0.99, 1000, True, at=999.0)))
    main._invalidate_audio()                      # 默认：暂停/取消一并作废排队指令
    box.put(None)

    main._dispatch_loop(runtime, pipeline, box, {}, threading.Event())

    content = "".join(path.read_text() for path in cfg.path("workspace.daily_log_dir").glob("*.md"))
    assert "暂停前说的那句" not in content
    assert dropped == ["暂停前说的那句"], "作废了却没留痕，用户只能猜是不是坏了"
