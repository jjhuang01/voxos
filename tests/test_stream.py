"""流式识别与实时字幕：窗口不越界、终稿不复用半截、字幕不污染状态。

这一组测试的共同前提：**字幕是过程量，终稿是账**。两者混在一起会出两类事故——
把只含后半句的字幕当终稿派发（用户说「帮我把磁盘清一下」），或者把字幕写进
history 把「最近发生了什么」冲干净（用户打开面板看到自己说到一半的草稿）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from voxos import audio, dsp, stream, ui


class Recorder:
    """假引擎：只记下每次被要求解多少采样，返回固定文本。"""

    partial_equals_final = True

    def __init__(self, text: str = "帮我把磁盘清一下") -> None:
        self.text_value = text
        self.partials: list[int] = []
        self.finals: list[int] = []

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        (self.finals if final else self.partials).append(audio.size)
        return self.text_value


class NoPartialReuse(Recorder):
    """像 ScriptedStt 那样：只有 final 才推进队列，partial 只是窥视。"""

    partial_equals_final = False


def _feed(streamer: stream.StreamingStt, seconds: float, chunk: int = 512) -> None:
    total = int(dsp.SR * seconds)
    block = np.zeros(chunk, dtype=np.float32)
    for _ in range(total // chunk):
        streamer.push(block)


def test_window_never_exceeds_its_budget():
    """窗口是常数成本的前提。一旦越界，长句的解码耗时就开始线性涨。"""
    engine = Recorder()
    windowed = stream.WindowedStt(engine, window_ms=2500)

    _feed(windowed, 6.0)
    windowed.text()

    assert engine.partials[-1] <= int(dsp.SR * 2.5) + 512, "解了超过窗口的音频"
    assert engine.partials[-1] > int(dsp.SR * 2.0), "窗口取太短，字幕会缺字"


def test_short_sentence_finish_reuses_the_partial():
    """短句（不超过窗口）解一次就够：partial 就是 final。"""
    engine = Recorder()
    windowed = stream.WindowedStt(engine, window_ms=2500)

    _feed(windowed, 1.0)
    windowed.text()
    whole = np.zeros(engine.partials[-1], dtype=np.float32)   # 就是刚解过的那一段
    text = windowed.finish(whole)

    assert text == "帮我把磁盘清一下"
    assert engine.finals == [], "整句已在窗口内，不该再解一遍"


def test_long_sentence_finish_does_not_reuse_a_truncated_partial():
    """5 秒的句子：窗口只覆盖后半句，那份文本**不能**当终稿。

    这是个真 bug 的形状——把「喂进去多少」当成「解了多少」，于是长句的终稿
    只剩最后 2.5 秒的内容，而且看起来完全正常（文本通顺、长度合理）。
    """
    engine = Recorder()
    windowed = stream.WindowedStt(engine, window_ms=2500)

    _feed(windowed, 5.0)
    windowed.text()
    whole = np.zeros(int(dsp.SR * 5.0), dtype=np.float32)
    windowed.finish(whole)

    assert engine.finals == [whole.size], "长句必须整段重解一次"


def test_engine_without_partial_equals_final_always_gets_a_final_call():
    """脚本回放引擎的 final 才推进队列。复用它的 partial = 同一句话反复派发。"""
    engine = NoPartialReuse()
    windowed = stream.WindowedStt(engine, window_ms=2500)

    _feed(windowed, 0.5)
    windowed.text()
    windowed.finish(np.zeros(int(dsp.SR * 0.5), dtype=np.float32))

    assert len(engine.finals) == 1


def test_pipeline_streams_subtitles_while_user_speaks(cfg, bus, make_voice, stub_embedder, silence):
    """边说边出字：一句话结束之前，界面就已经收到过 partial。"""
    seen: list[str] = []
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, 1.0)])
    pipeline = audio.AudioPipeline(
        cfg=cfg, stt=audio.ScriptedStt(["帮我把磁盘清一下"]), lock=lock,
        on_event=bus, on_partial=seen.append,
    )

    stream_audio = np.concatenate([make_voice(118, 2.0), silence(1600)])
    list(pipeline.run(audio.ArraySource(stream_audio)))

    assert seen, "整句话说完之前一个 partial 都没有"
    assert all(isinstance(item, str) and item for item in seen)


def test_subtitles_are_off_for_remote_engines(cfg):
    """远端引擎不给字幕：字幕必须在声纹校验**之前**出字，等于把没验身份的
    音频提前传上云。这条不能靠配置兜，得由代码硬性关掉。"""

    class RemoteStt(Recorder):
        remote = True

    assert stream.make_streamer(cfg, RemoteStt()) is None


def test_subtitles_can_be_switched_off(cfg):
    off = cfg.with_overrides(**{"audio.subtitles": False})
    assert stream.make_streamer(off, Recorder()) is None


def test_partial_events_stay_out_of_history_and_disk(tmp_path: Path):
    """字幕一分钟能出上百条。写进 history 会把「最近发生了什么」冲干净，
    落盘更是把状态文件当日志写。"""
    bus = ui.StatusBus(tmp_path / ".voxos" / "status.jsonl")
    channel = bus.subscribe()

    for index in range(30):
        bus.emit_partial(f"第 {index} 个字")
    bus.emit("done", "完成", "真的结果")

    assert [event["detail"] for event in bus.history()] == ["真的结果"], "字幕混进了 history"
    written = (tmp_path / ".voxos" / "status.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(written) == 1, f"字幕被写进了状态文件（{len(written)} 行）"
    assert channel.qsize() >= 1, "字幕没有推给在线订阅者"


def test_stream_model_dir_without_tokens_fails_loudly(tmp_path: Path):
    """模型目录选错时要说清缺什么，而不是给一台「听得见但不出字」的机器。"""
    pytest.importorskip("sherpa_onnx", reason="最小依赖环境没有 sherpa-onnx")
    empty = tmp_path / "stream-model"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="tokens.txt"):
        stream.SherpaStreamingStt(empty)


def test_missing_sherpa_says_how_to_install(monkeypatch, tmp_path: Path):
    """缺 sherpa-onnx 时不能只抛 `ModuleNotFoundError`。

    CI 的「最小依赖」任务会真的跑在没有 sherpa-onnx 的环境里——这条不是假想。
    抛 `ModuleNotFoundError: No module named 'sherpa_onnx'` 等于让用户自己去猜
    装什么、怎么装；而 `pip install 'voxos[models]'` 是能直接粘进终端的。
    """
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)     # 让 import 直接失败
    with pytest.raises(RuntimeError, match="voxos\\[models\\]"):
        stream.SherpaStreamingStt(tmp_path)


def test_missing_stream_model_falls_back_to_windowed(cfg, capsys):
    """配置指向一个不存在的目录时退回窗口字幕，而不是整条链起不来。"""
    broken = cfg.with_overrides(**{"stt.stream_model_dir": "/nonexistent/stream-model"})
    made = stream.make_streamer(broken, Recorder())
    assert isinstance(made, stream.WindowedStt)


def test_stream_model_is_found_without_any_configuration(cfg, tmp_path: Path):
    """`voxos models` 下完之后**不该还要改配置**才生效。

    默认落点是声纹模型所在目录下的 `stream-zipformer/`——它和声纹模型一起被
    `voxos models` 放进数据根，所以「下载」和「生效」应该是同一件事。
    """
    models_dir = tmp_path / ".voxos" / "models"
    configured = cfg.with_overrides(**{"audio.speaker_model": str(models_dir / "campplus.onnx")})

    assert stream._stream_dir(configured) == models_dir / "stream-zipformer"


def test_explicit_stream_dir_wins_over_the_default(cfg, tmp_path: Path):
    mine = tmp_path / "my-stream"
    mine.mkdir()
    configured = cfg.with_overrides(**{"stt.stream_model_dir": str(mine)})

    assert stream._stream_dir(configured) == mine


def test_explicit_stream_dir_that_does_not_exist_is_reported_not_silent(cfg, capsys):
    """配置指向一个不存在的目录时要说一声。静默退回等于用户以为自己开了流式。"""
    broken = cfg.with_overrides(**{"stt.stream_model_dir": "/nonexistent/stream-model"})
    stream._stream_dir(broken)

    assert "不存在" in capsys.readouterr().out
