"""会话时间线：一次回合一条记录，四种结局都要留下痕迹。

这组测试守的是一个具体的空白：**「我说了十句，一句都没执行」曾经在两份日志
里都查不到**。审计只记工具调用（被丢弃的句子没调工具），状态只记状态变化
（被丢弃的句子连状态都没有）。所以这里每一条都在问同一个问题：
「用户想知道发生了什么的时候，能不能查到」。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from voxos import audio, core, dsp, lessons, timeline
from voxos.main import _turn_of


def test_record_then_read_back_newest_first(tmp_path: Path):
    path = tmp_path / "timeline.jsonl"
    for index in range(3):
        timeline.record(timeline.Turn(outcome="acted", text=f"第 {index} 句"), path=path)

    rows = timeline.recent(path=path, limit=10)

    assert [row["text"] for row in rows] == ["第 2 句", "第 1 句", "第 0 句"]


def test_recent_survives_a_half_written_line(tmp_path: Path):
    """进程被 kill 时最后一行可能只有半截。跳过它，别让整个文件读不出来。"""
    path = tmp_path / "timeline.jsonl"
    timeline.record(timeline.Turn(outcome="acted", text="完整的一句"), path=path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-01-01T00:00:00", "outc')   # 被杀在半路

    rows = timeline.recent(path=path, limit=10)

    assert [row["text"] for row in rows] == ["完整的一句"]


def test_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert timeline.recent(path=tmp_path / "nope.jsonl") == []


def test_write_failure_does_not_break_the_caller(tmp_path: Path):
    """时间线写不进去（磁盘满、权限没了）不能把一次执行带下去。"""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("我是文件不是目录", encoding="utf-8")

    turn = timeline.record(timeline.Turn(outcome="acted", text="x"), path=blocked / "t.jsonl")

    assert turn.text == "x", "写失败时也要把 turn 原样返回"


def test_dropped_turn_keeps_the_recognised_text():
    """被丢弃的句子必须留下「我听到的是这句」。

    以前 `_drop` 返回空字符串，于是「我说了『请打开微信』，因为声纹不匹配被丢了」
    这条信息在音频层就没了，上层只能 `continue`——用户看到的是胶囊上字幕闪一下、
    然后什么都没有。
    """
    lock = dsp.SpeakerLock(threshold=0.99, embedder=lambda a: np.zeros(512, dtype=np.float32))
    pipeline = audio.AudioPipeline(cfg=_cfg(), stt=audio.ScriptedStt(["请打开微信"]),
                                   lock=lock, vad=dsp.EnergyVad())
    pipeline._text = "请打开微信"                      # noqa: SLF001 - 模拟已经出过字幕

    dropped = pipeline._drop(1400.0, 0.74, "声纹不匹配，已丢弃")   # noqa: SLF001

    assert dropped.accepted is False
    assert dropped.text == "请打开微信"
    assert "声纹" in dropped.reason


def test_outcome_distinguishes_chat_from_action():
    """「随便聊两句」和「执行了工具」都走 route='llm'，只能靠 tool 空不空区分。"""
    chatted = _turn_of("随便聊两句", core.DispatchResult(route="llm", text="", reply="行啊"))
    acted = _turn_of("看下磁盘", core.DispatchResult(route="llm", text="", tool="inspect_server_ops",
                                                    reply="磁盘 94%"))
    missed = _turn_of("呃那个啥", core.DispatchResult(route="ignore", text=""))
    failed = _turn_of("删掉它", core.DispatchResult(route="llm", text="", tool="delete_path",
                                                   reply="删除失败：权限不足"))

    assert chatted.outcome == "chatting"
    assert acted.outcome == "acted"
    assert missed.outcome == "missed"
    assert failed.outcome == "failed"
    assert failed.ok is False


def test_diagnose_counts_the_worst_problem_first():
    turns = [
        {"outcome": "dropped", "reason": "声纹不匹配，已丢弃", "text": "请打开微信"},
        {"outcome": "dropped", "reason": "声纹不匹配，已丢弃", "text": "打开文件"},
        {"outcome": "dropped", "reason": "太短，已丢弃", "text": "嗯"},
        {"outcome": "acted", "tool": "tell_time", "reply": "13 点"},
    ]

    findings = lessons.diagnose(turns, limit=3)

    assert findings[0].symptom == "声纹不匹配"
    assert findings[0].count == 2
    assert "enroll" in findings[0].advice, "修法要能直接执行，不能只说「请检查配置」"
    assert "请打开微信" in findings[0].samples


def test_unmatched_sorts_by_how_often_it_was_said():
    """说两遍的那句才值得加正则。去重后按次数排，是「该补哪条快通道」的入口。"""
    turns = [{"outcome": "missed", "text": "把那个东西弄一下"},
             {"outcome": "missed", "text": "把那个东西弄一下"},
             {"outcome": "missed", "text": "嗯"}]

    blind = lessons.unmatched(turns)

    assert blind[0] == "把那个东西弄一下（2×）"
    assert blind[1] == "嗯"


def test_timeline_line_hides_voiceprint_for_typed_input():
    """打字入口没有声纹，硬报「声纹0.00」会让人以为声纹坏了。"""
    typed = timeline.Turn(outcome="acted", tool="tell_time", text="几点了", ts="2026-01-01T13:00:00")
    spoken = timeline.Turn(outcome="dropped", text="打开微信", score=0.74, audio_ms=1400,
                           reason="声纹不匹配，已丢弃", ts="2026-01-01T13:00:01")

    assert "声纹" not in typed.line()
    assert "声纹0.74" in spoken.line()


def test_timeline_line_shows_the_clock_for_both_timestamp_shapes():
    """`record()` 补的是 ISO 全格式，手工构造的可能只给时分秒——两种都得显示。

    以前按固定下标切 `[11:19]`，短格式会被切成空字符串，整列时间消失，
    看着像「时间没记上」。
    """
    full = timeline.Turn(outcome="acted", tool="tell_time", ts="2026-01-01T13:46:55")
    short = timeline.Turn(outcome="acted", tool="tell_time", ts="13:46:55")

    assert full.line().startswith("13:46:55")
    assert short.line().startswith("13:46:55")


def _cfg():
    """最小配置：只要管道能构造起来，这一组不测配置解析。"""
    from voxos.config import Config

    return Config({
        "audio": {"silence_duration_ms": 300, "min_speech_ms": 100},
        "stt": {"backend": "scripted"},
        "ui": {},
    })


def test_timeline_json_round_trips(tmp_path: Path):
    """写进去的字段读回来必须一模一样——`args` 是嵌套字典，最容易在序列化时丢。"""
    path = tmp_path / "t.jsonl"
    turn = timeline.Turn(outcome="acted", text="清缓存", tool="run_command",
                         args={"command": "rm -rf ~/Library/Caches/x"}, ms=12.5)

    timeline.record(turn, path=path)
    row = timeline.recent(path=path)[0]

    assert row["args"] == {"command": "rm -rf ~/Library/Caches/x"}
    assert row["ms"] == 12.5
    assert json.loads(json.dumps(row, ensure_ascii=False))["tool"] == "run_command"
