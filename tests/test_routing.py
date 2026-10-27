"""路由覆盖：自然说法必须落到对的工具上，认不出来时必须什么都不做。

这张表是产品的"听懂率"契约 —— 改正则就要同步改它，否则就是悄悄退步。
"""

from __future__ import annotations

import pytest

from voxos import core
from voxos.config import load

CASES: list[tuple[str, str, str]] = [
    # (期望工具, 说法, 备注)
    ("append_structured_log", "Siri，我要写日志，今天把登录接口的空值校验补上了", "标准句式"),
    ("append_structured_log", "记一条日志：修了登录 bug", "简短"),
    ("append_structured_log", "帮我记录一下今天的进展", "口语"),
    ("append_structured_log", "写个日志", "极简"),
    ("dispatch_code_session", "我要写代码，给 auth.py 加空值校验", "标准句式"),
    ("dispatch_code_session", "把 auth.py 的空值校验补上", "把字句（曾经漏网）"),
    ("dispatch_code_session", "在 login.ts 里加个限流", "在字句"),
    ("dispatch_code_session", "继续写刚才那个接口", "续接"),
    ("dispatch_code_session", "改一下 auth.py", "改文件"),
    ("inspect_server_ops", "检查一下服务器负载", "标准句式"),
    ("inspect_server_ops", "服务器怎么样了", "口语"),
    ("inspect_server_ops", "看看磁盘", "极简"),
    ("inspect_server_ops", "磁盘还剩多少", "问句"),
    ("rollback_file", "撤销刚才的修改", "标准句式"),
    ("rollback_file", "回滚", "极简"),
    ("rollback_file", "还原上次改动", "还原"),
]


@pytest.fixture(scope="module", autouse=True)
def _tools_ready():
    core.ensure_tools()


@pytest.mark.parametrize("expected,text,note", CASES, ids=[c[2] for c in CASES])
def test_route_hits_expected_tool(expected: str, text: str, note: str):
    cfg = load()
    fast = core._fast_route(text, core._wake_pattern(cfg))
    plan = core.heuristic_plan(text, cfg)
    hit = fast[0] if fast else (plan[0][0] if plan else "ignore")
    assert hit == expected, f"{note}：「{text}」落到了 {hit}"


@pytest.mark.parametrize("text", ["今天天气怎么样", "帮我订一张机票", "放首歌"])
def test_unknown_text_does_nothing(text: str):
    cfg = load()
    assert core._fast_route(text, core._wake_pattern(cfg)) is None
    assert core.heuristic_plan(text, cfg) == []


def test_wake_word_is_stripped_before_matching():
    cfg = load()
    for prefix in ("Siri，", "嘿 Siri ", "小助手，", "Jarvis:"):
        assert core._fast_route(f"{prefix}我要写日志，今天收尾", core._wake_pattern(cfg)), prefix


def test_multi_intent_is_split():
    cfg = load()
    plan = core.heuristic_plan("记一下今天修了登录 bug；然后看看服务器负载", cfg)
    assert [name for name, _ in plan] == ["append_structured_log", "inspect_server_ops"]


def test_code_intent_needs_file_and_verb():
    """只有动词没有文件名 → 不进编程会话；只有文件名没有动词 → 也不进。"""
    cfg = load()
    assert core.heuristic_plan("把这件事补上", cfg) == []
    assert core.heuristic_plan("auth.py 在哪", cfg) == []
