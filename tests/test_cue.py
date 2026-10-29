"""主动提示（cue）的降噪策略：静音时段、频率上限、去重。

cue 只提醒、不执行动作，所以这里没有「确认」这回事；要验的是「别烦人」。
"""

from __future__ import annotations

from datetime import datetime

from voxos import cue

BASE = 1_700_000_000.0          # 固定时间戳，测试不依赖运行时的当天日期


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 29, hour, minute)


def _quiet(cfg):
    """测试配置是合成的，不含 config/settings.toml 的默认值，这里显式给窗口。"""
    return cfg.with_overrides(**{"cue.quiet_hours": "23:00-07:00"})


def test_quiet_window_parses_a_midnight_crossing(cfg):
    view = _quiet(cfg)
    assert cue.quiet_window(view) == (23 * 60, 7 * 60)
    assert cue.in_quiet_hours(view, _at(23, 30)) is True
    assert cue.in_quiet_hours(view, _at(3, 0)) is True
    assert cue.in_quiet_hours(view, _at(7, 0)) is False
    assert cue.in_quiet_hours(view, _at(12, 0)) is False


def test_bad_quiet_hours_mean_no_quiet_time(cfg):
    """配置写错宁可多说，也不要静默一整天——那会让「没提醒」变成查不出的 bug。"""
    broken = cfg.with_overrides(**{"cue.quiet_hours": "晚上到早上"})
    assert cue.quiet_window(broken) == (-1, -1)
    assert cue.in_quiet_hours(broken, _at(3, 0)) is False


def test_normal_cue_is_silenced_at_night(cfg):
    allowed, why = cue.CueGuard(_quiet(cfg)).allow(
        cue.Cue("该喝水了"), now=_at(2, 0).timestamp())
    assert allowed is False and why == "静音时段"


def test_high_priority_can_wake_you_up(cfg):
    guard = cue.CueGuard(_quiet(cfg))
    night = datetime(2026, 10, 29, 3, 0).timestamp()
    assert guard.allow(cue.Cue("服务挂了", priority="high"), now=night)[0] is True


def test_two_cues_within_the_interval_are_rate_limited(cfg):
    view = cfg.with_overrides(**{"cue.quiet_hours": "", "cue.min_interval_s": 90})
    guard = cue.CueGuard(view)
    assert guard.allow(cue.Cue("第一件"), now=BASE)[0] is True
    allowed, why = guard.allow(cue.Cue("第二件"), now=BASE + 10)
    assert allowed is False and "近" in why
    assert guard.allow(cue.Cue("第三件"), now=BASE + 91)[0] is True


def test_high_priority_still_respects_the_hourly_cap(cfg):
    """防死循环：再急也不能每分钟刷一条。"""
    view = cfg.with_overrides(**{"cue.quiet_hours": "", "cue.min_interval_s": 0,
                                 "cue.max_per_hour": 2})
    guard = cue.CueGuard(view)
    assert guard.allow(cue.Cue("一", priority="high"), now=BASE)[0] is True
    assert guard.allow(cue.Cue("二", priority="high"), now=BASE + 1)[0] is True
    allowed, why = guard.allow(cue.Cue("三", priority="high"), now=BASE + 2)
    assert allowed is False and "上限" in why


def test_same_key_is_deduped(cfg):
    view = cfg.with_overrides(**{"cue.quiet_hours": "", "cue.min_interval_s": 0,
                                 "cue.max_per_hour": 10, "cue.dedupe_s": 600})
    guard = cue.CueGuard(view)
    first = cue.Cue("构建失败", key="ci-build")
    assert guard.allow(first, now=BASE)[0] is True
    allowed, why = guard.allow(first, now=BASE + 60)
    assert allowed is False and "重复" in why
    assert guard.allow(first, now=BASE + 601)[0] is True


def test_empty_cue_is_refused(cfg):
    allowed, why = cue.CueGuard(cfg).allow(cue.Cue("   "), now=BASE)
    assert allowed is False and why
