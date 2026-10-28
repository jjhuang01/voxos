"""发布门禁的豁免机制：能豁免，但不能变成"永久假装通过"。

为什么值得单独测：`make verify-release` 是发版前唯一一道自动闸。豁免逻辑一旦写松，
闸门就等于没有；写紧到不能用，人就会绕过它（0.1.11 和 0.1.12 就是这么发出去的）。
这里钉住三条：豁免必须留理由和到期日、到期自动失效、关键字写错要拦。
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

GAP = {"item": "中英混说英文术语召回 ≥ 95%", "measured": "29.4%", "pass": False, "gap": True}


def _verifier():
    """按文件路径加载 scripts/verify.py——它不是包，直接 import 拿不到。"""
    spec = importlib.util.spec_from_file_location("voxos_verify", ROOT / "scripts" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**kwargs) -> types.SimpleNamespace:
    base = {"allow_gap": [], "reason": "", "until": ""}
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def test_waiver_requires_reason_and_deadline():
    """只写 --allow-gap 就等于把门禁改成永不开火，必须拦住。"""
    verify = _verifier()
    with pytest.raises(SystemExit):
        verify._apply_waivers([GAP], _args(allow_gap=["中英混说"]))
    with pytest.raises(SystemExit):
        verify._apply_waivers([GAP], _args(allow_gap=["中英混说"], reason="缺到期日"))


def test_expired_waiver_stops_being_valid():
    """到期日是让豁免自动失效用的，过期后必须重新判断一次。"""
    verify = _verifier()
    with pytest.raises(SystemExit):
        verify._apply_waivers([GAP], _args(allow_gap=["中英混说"], reason="r", until="2020-01-01"))


def test_waiver_must_match_a_real_gap():
    """关键字写错要拦：否则"豁免配好了"是假象，下次真出现新 GAP 会被一起放过。"""
    verify = _verifier()
    waived, blocking = verify._apply_waivers(
        [GAP], _args(allow_gap=["中英混说"], reason="r", until="2099-01-01"))
    assert len(waived) == 1 and not blocking, "正常豁免应该把这一项移出拦截集"
    with pytest.raises(SystemExit):
        verify._apply_waivers([GAP], _args(allow_gap=["写错的项"], reason="r", until="2099-01-01"))
