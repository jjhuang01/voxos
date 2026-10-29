"""文件投递箱：脚本推提示的载体，必须是「要么完整、要么不存在」。

为什么值得单独测：这条路径要长期被 cron、CI、其它脚本使用，而它不是 HTTP——
没有令牌把关，正确性全靠「写的时候原子、读的时候认新鲜度」。
坏文件、旧文件、半个 JSON 都不能变成「每次都重念一遍」或者「轮询线程崩掉」。
"""

from __future__ import annotations

import json

import pytest

from voxos import cuebox


def test_a_posted_cue_comes_out_once(cfg):
    """取过就删：同一条提示不能因为轮询跑了十轮就被念十遍。"""
    cuebox.post(cfg, "喝水", priority="high", key="water", source="test", now=1000.0)

    first = cuebox.take(cfg, now=1001.0)
    assert [(item["text"], item["priority"], item["key"], item["source"]) for item in first] == [
        ("喝水", "high", "water", "test")]
    assert cuebox.take(cfg, now=1002.0) == []


def test_a_stale_cue_is_dropped_instead_of_spoken_late(cfg):
    """三小时前的提醒现在说出来只会让人困惑——不如不说。"""
    cuebox.post(cfg, "开会", now=1000.0)

    assert cuebox.take(cfg, now=1000.0 + 3600) == []


def test_take_on_a_missing_box_is_empty_not_an_error(cfg):
    """守护进程从没收到过提示时不该报错：第一次轮询就是这个状态。"""
    assert cuebox.take(cfg, now=1000.0) == []


def test_a_broken_file_is_removed_instead_of_retried_forever(cfg):
    """磁盘上留了一个坏 JSON 时，不能让它每两秒被重读一次。"""
    box = cuebox.box_dir(cfg)
    box.mkdir(parents=True, exist_ok=True)
    (box / "1-bad.json").write_text("{不是 JSON", encoding="utf-8")

    assert cuebox.take(cfg, now=1000.0) == []
    assert not (box / "1-bad.json").exists()


def test_an_entry_without_time_is_treated_as_ancient(cfg):
    """缺时间戳就当很久以前：宁可少念一句，也不要反复念。"""
    box = cuebox.box_dir(cfg)
    box.mkdir(parents=True, exist_ok=True)
    (box / "2-notime.json").write_text(json.dumps({"text": "无时间"}), encoding="utf-8")

    assert cuebox.take(cfg, now=1000.0) == []


def test_writing_leaves_no_temporary_files_behind(cfg):
    """原子替换的另一半：临时文件不能留在箱子里被当成一条提示。"""
    cuebox.post(cfg, "编译完成", now=1000.0)

    assert not list(cuebox.box_dir(cfg).glob("*.tmp"))
    assert len(cuebox.take(cfg, now=1001.0)) == 1


def test_empty_text_is_refused_at_the_door(cfg):
    """空提示在写入时就该拒绝，别让它变成一个「说了但什么都没说」的事件。"""
    with pytest.raises(ValueError):
        cuebox.post(cfg, "   ", now=1000.0)


def test_a_boxed_cue_goes_through_the_same_door_as_the_api(cfg, bus, monkeypatch):
    """投递箱与 `/api/cue` 必须共用一条路：两套行为早晚会一套静音、一套不静音。"""
    import time

    from voxos import main as vm

    spoken: list[str] = []
    monkeypatch.setitem(vm._SPEAK_HOOK, "speak", spoken.append)
    runtime = vm.Runtime(cfg, bus)
    cuebox.post(cfg, "构建完成", key="build", source="ci", now=time.time())

    delivered = [vm._deliver_cue(runtime, entry) for entry in cuebox.take(cfg, now=time.time())]
    assert delivered == [{"ok": True, "spoken": True}]
    assert spoken == ["构建完成"]


def test_reloading_settings_does_not_reset_the_cue_limits(cfg, bus, monkeypatch):
    """「保存一次设置」不该把每小时上限与最小间隔清零——那等于给刷屏开了后门。

    guard 挂在 Runtime 上而不是模块级全局，就是为了这条：配置重载只换门限，
    已发的记录留着。反过来（重载即重置）在设置页多点两下就能发一串提示。
    """
    from voxos import main as vm

    monkeypatch.setitem(vm._SPEAK_HOOK, "speak", lambda piece: None)
    runtime = vm.Runtime(cfg, bus)
    assert vm._deliver_cue(runtime, {"text": "第一条"}) == {"ok": True, "spoken": True}

    runtime.reload(cfg.with_overrides(**{"dialogue.mode": "chat"}))
    assert vm._deliver_cue(runtime, {"text": "第二条"}) == {"ok": False, "skipped": "距上一条太近"}


def test_a_quiet_hour_cue_is_skipped_with_a_reason(cfg, bus, monkeypatch):
    """被静音时段拦下时要说清原因，不能悄悄吞掉——脚本那头还以为说过了。"""
    import time

    from voxos import main as vm

    quiet = cfg.with_overrides(**{"cue.quiet_hours": "00:00-23:59"})
    runtime = vm.Runtime(quiet, bus)
    monkeypatch.setitem(vm._SPEAK_HOOK, "speak", lambda piece: None)

    result = vm._deliver_cue(runtime, {"text": "该休息了", "at": time.time()})
    assert result == {"ok": False, "skipped": "静音时段"}
