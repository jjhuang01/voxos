"""路由覆盖：自然说法必须落到对的工具上，认不出来时必须什么都不做。

这张表是产品的"听懂率"契约 —— 改正则就要同步改它，否则就是悄悄退步。
"""

from __future__ import annotations

import pytest

from voxos import core
from voxos.config import Config, load


def test_command_phrase_is_not_stolen_by_open_app():
    """「运行命令 ls」不能被「运行 xx」那条打开应用的正则吃掉。

    快通道是**先注册先匹配**：`open_app` 认「运行 / 打开 / 启动」，如果
    `run_command` 也认「运行」，它排在后面就永远轮不到——用户说「运行命令 ls」
    会得到「没找到「命令 ls」」。所以命令只认「执行 / 跑」。
    """
    assert core._fast_route("执行命令 echo hi") == ("run_command", {"command": "echo hi"})
    assert core._fast_route("跑命令 echo hi") == ("run_command", {"command": "echo hi"})
    assert core._fast_route("运行微信")[0] == "open_app"


def offline() -> Config:
    """真实配置，但把慢通道关掉。

    这条文件说的是「哪种说法落到哪个工具」，和本机有没有配 Key 无关。
    直接 `load()` 会让一条本该 1 毫秒的断言变成一次真实网络请求：
    机器上配好大模型之后它就红了，红的原因还和被测行为毫无关系。
    """
    return load().with_overrides(**{"llm.enabled": False})

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
    # 读日志不是写日志：只按「日志」这个名词匹配，会把「看一下日志」判成写日志，
    # 于是用户想查日志，系统往日记里写了一条。
    ("ignore", "接口返回502，看一下日志", "读日志不是写日志"),
    ("ignore", "看看今天的日志", "运维快通道不能吞掉一切"),
    ("ignore", "日志在哪", "问句不是写"),
    ("append_structured_log", "帮我记一下今天的进展", "「记」才是写"),
    # 大改走确认通道：red_confirm 必须有一条从「说话」出发的可达路径，
    # 否则确认卡片就是个永远弹不出来的摆设。
    ("delegate_to_coding_cli", "重构整个登录模块", "大改要确认"),
    ("delegate_to_coding_cli", "把重构交给 claude 去做", "点名 CLI"),
    # 日常指令：这两句是「听不懂」投诉的原话。工具写好了但没登记快通道，
    # 用户说什么都落到 ignore——能力存在但永远走不到，等于没有。
    ("open_app", "打开微信", "开应用"),
    ("open_app", "帮我打开一下终端", "开应用（带语气词）"),
    ("open_app", "激活微信", "开应用（另一种说法）"),
    ("open_app", "打开 github.com", "网址"),
    ("tell_time", "几点了", "问时间"),
    ("tell_time", "今天星期几", "问星期"),
    ("tell_time", "现在几号", "问日期"),
    ("delegate_to_coding_cli", "这个项目全部重写一遍", "全量重写"),
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


def test_big_change_does_not_steal_small_edits():
    """大改走确认，小修小补仍走编程会话——不然每句话都弹确认，人就会闭眼点允许。"""
    cfg = load()
    small = core.heuristic_plan("把 auth.py 的空值校验补上", cfg)
    assert small[0][0] == "dispatch_code_session"
    big = core.heuristic_plan("重构整个登录模块", cfg)
    assert big[0][0] == "delegate_to_coding_cli"


def test_unrecognized_speech_says_the_same_thing_everywhere(cfg):
    """接口返回与历史记录不能各说各话（曾经一处「(无动作)」一处「没听懂」）。

    并且这句话必须点出**下一步**：没配大模型时说「还没学会」，等于把系统的
    配置问题说成用户的表达问题——他会换个说法再失败一次。
    """
    from voxos import main, ui

    runtime = main.Runtime(cfg, ui.StatusBus())
    reply = main._run_text(runtime, "今天天气不错")
    assert reply == runtime.recent[0]["reply"]
    assert "大模型" in reply and "voxos llm" in reply


@pytest.mark.parametrize("text", ["你好啊", "你好", "在吗", "嗨～", "谢谢啦", "你是谁呀"])
def test_small_talk_is_answered_not_ignored(text: str):
    """真机第一次用，说「你好啊」连吃五条「没听懂」。

    能力没错，但一个能对话的东西不该把问候当失败——所以问候、道谢、问身份
    各有自己的出口，且**不算 ignore**（ignore 不朗读，问它一句会变成沉默）。
    """
    result = core.dispatch(text, cfg=offline())
    assert result.route == "chat", f"「{text}」落到了 {result.route}"
    assert result.reply, f"「{text}」没有回话"


def test_unknown_speech_still_explains_what_it_can_do():
    """听不懂时也要给下一步，不能只留一句「没听懂」。"""
    result = core.dispatch("帮我订一张机票", cfg=offline())
    assert result.route == "ignore"
    assert "打开应用" in result.reply
