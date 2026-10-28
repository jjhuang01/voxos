"""能动手的那一组：跑命令、打开文件、发消息、定时任务。

这一组全是**有副作用**的能力，所以测试的重点不是「成功了吗」，而是
「边界在不在」：沙箱档位拼错会不会变成全开、只读档位会不会偷偷跑命令、
删东西之前有没有问一句、定时任务重启后还在不在。
"""

from __future__ import annotations

import time
from pathlib import Path

from voxos import messaging, sandbox, scheduler


# ------------------------------------------------------------------ 沙箱

def test_typo_in_sandbox_mode_fails_closed(cfg):
    """配置拼错不能等于「全开」。"""
    for wrong in ("workspace_write", "disabled", "", "  ", "READ-WRITE"):
        assert sandbox.mode(cfg.with_overrides(**{"workspace.sandbox": wrong})) == "read-only"


def test_profile_denies_writes_outside_workspace(cfg):
    text = sandbox.profile(cfg)
    assert "(deny file-write*)" in text
    assert "(allow file-write*" in text
    assert str(cfg.path("workspace.default_code_dir", "~/Projects")) in text
    assert "(deny network*)" in text          # 默认断网


def test_network_flag_opens_only_that_hole(cfg):
    opened = cfg.with_overrides(**{"workspace.network": True})
    assert "(deny network*)" not in sandbox.profile(opened)


def test_start_dir_is_a_write_root_but_slash_is_not(cfg, monkeypatch):
    """启动目录要能写，但 `/`、家目录、以及家目录的**祖先**都不能算。

    真实症状：用户在自己项目里说「跑一下测试」，pytest 连 `.pytest_cache`
    都写不进去。命令没错，是边界画错了（codex 的 workspace-write 也是 cwd 起步）。

    排祖先不只是为了 `/`：`cd /Users` 之后一条 `(subpath "/Users")` 就等于放行
    整个家目录，边界看着还在，其实没了。

    这里直接钉 `_start_dir()` 而不去 chdir：`~/Documents` 在 CI 的 Linux runner
    上不存在，chdir 会抛 FileNotFoundError——测试本身不该依赖某个目录恰好存在。
    """
    inside = Path.home() / "Documents"
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: inside))
    assert inside.resolve() in sandbox.writes_roots(cfg)

    for blocked in (Path("/"), Path.home(), *Path.home().parents):
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls, b=blocked: b))
        assert blocked.resolve() not in sandbox.writes_roots(cfg)


def test_start_dir_outside_home_still_counts(cfg, monkeypatch):
    """项目在外置盘 / `/opt` 下也要能写。

    要求 cwd 必须在家目录下是个看似稳妥、实际很坏的规则：用户在自己的项目里
    跑测试会被莫名拒绝，而报错和「命令有 bug」长得一样。这里直接钉
    `_start_dir()`，不去真建目录——`tmp_path` 落在 `$TMPDIR` 里，那本来就是
    可写根，断言会白白通过。
    """
    external = Path("/Volumes/Work/proj")
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: external))
    assert sandbox._start_dir() == external
    assert external in sandbox.writes_roots(cfg)


def test_dev_is_not_opened_wholesale(cfg):
    """`(subpath "/dev")` 等于允许 `dd of=/dev/disk2`：只开真正需要的节点。"""
    text = sandbox.profile(cfg)
    assert '(subpath "/dev")' not in text
    assert '(literal "/dev/null")' in text
    assert '(subpath "/dev/fd")' in text


def test_extra_write_roots_are_honoured(cfg):
    """配置里写一个字符串也得认——手写 TOML 时很容易漏掉方括号。"""
    listed = cfg.with_overrides(**{"workspace.write_roots": ["/opt/data"]})
    assert Path("/opt/data") in sandbox.writes_roots(listed)
    single = cfg.with_overrides(**{"workspace.write_roots": "/srv/one"})
    assert Path("/srv/one") in sandbox.writes_roots(single)
    empty = cfg.with_overrides(**{"workspace.write_roots": ""})
    assert not [one for one in sandbox.writes_roots(empty) if str(one) == ""]


def test_sandbox_status_is_probed_once(cfg, monkeypatch):
    """探测结论要缓存：每条命令都探一次，等于每条命令多起一个进程。"""
    calls: list[int] = []

    def fake_probe():
        calls.append(1)
        return True, ""

    monkeypatch.setattr(sandbox, "_probe", fake_probe)
    sandbox.reset()
    try:
        assert sandbox.status() == (True, "")
        assert sandbox.status() == (True, "")
        assert len(calls) == 1, "同一个进程里不该重复探测"
        sandbox.reset()
        sandbox.status()
        assert len(calls) == 2, "reset() 之后应该重新探"
    finally:
        sandbox.reset()


def test_sandbox_failure_is_told_apart_from_command_failure():
    """只有 sandbox-exec 自己在抱怨才算「沙箱没起来」。

    认裸的 `Operation not permitted` 会把「命令自己失败」也当成沙箱失败，
    于是同一条命令被再跑一遍——多出来的那次副作用比不降级更糟。
    """
    assert sandbox.failed_by_sandbox("sandbox-exec: sandbox_apply: Operation not permitted")
    assert sandbox.failed_by_sandbox("sandbox-exec: invalid profile")
    assert not sandbox.failed_by_sandbox("rm: /etc/hosts: Operation not permitted")
    assert not sandbox.failed_by_sandbox("fatal: not a git repository")


def test_read_only_mode_refuses_to_run_anything(cfg, executor):
    """只读档位不是「命令跑一下试试」，是根本不起进程。"""
    readonly = cfg.with_overrides(**{"workspace.sandbox": "read-only"})
    from voxos import tools
    tools.configure(readonly)
    try:
        result = executor.execute_safely("run_command", {"command": "echo hi"})
    finally:
        tools.configure(cfg)
    assert "只读" in result and "hi" not in result


# ------------------------------------------------------------------ 跑命令

def test_run_command_returns_stdout(cfg, executor):
    assert "hi" in executor.execute_safely("run_command", {"command": "echo hi"})


def test_run_command_reports_exit_code(cfg, executor):
    result = executor.execute_safely("run_command", {"command": "exit 3"})
    assert "退出码 3" in result


def test_run_command_truncates_long_output(cfg, executor):
    result = executor.execute_safely("run_command", {"command": "seq 1 5000"})
    assert "已截断" in result and len(result) < 2600


def test_run_command_still_respects_redlines(cfg, executor):
    """沙箱管「写到哪」，红线管「这条命令本身就不该出现」。"""
    assert executor.execute_safely("run_command", {"command": "rm -rf /"}).startswith("⛔")


def test_deleting_through_a_command_asks_first(cfg, bus):
    """「删掉 build 目录」走命令通道也必须先问一句——不可逆词表对 YELLOW 生效。"""
    asked: list[str] = []
    from voxos import core
    executor = core.Executor(cfg=cfg, on_event=bus,
                             confirm=lambda name, args, verdict: asked.append(verdict.reason) or False)
    result = executor.execute_safely("run_command", {"command": "rm -rf build"})
    assert asked and "删除" in asked[0]
    assert "已取消" in result


# ------------------------------------------------------------------ 打开文件

def test_open_path_says_when_nothing_is_there(cfg, executor):
    assert "不存在" in executor.execute_safely("open_path", {"path": "/tmp/voxos-never-exists"})


def test_open_path_dry_run_prints_the_command(cfg, executor, tmp_path):
    target = tmp_path / "note.md"
    target.write_text("x", encoding="utf-8")
    result = executor.execute_safely("open_path", {"path": str(target)})
    assert "dry-run" in result and str(target) in result


# ------------------------------------------------------------------ 发消息

def test_unknown_app_is_named_not_guessed(cfg, executor):
    result = executor.execute_safely("send_message", {"contact": "张三", "text": "在吗",
                                                      "app": "飞书"})
    assert "飞书" in result and "微信" in result


def test_send_message_dry_run_does_not_press_keys(cfg, executor):
    result = executor.execute_safely("send_message", {"contact": "张三", "text": "晚点到"})
    assert "dry-run" in result and "张三" in result


def test_applescript_string_escaping():
    """内容里带引号不能把脚本撕成两半。"""
    assert messaging.quote('他说"好"') == '"他说\\"好\\""'
    assert messaging.quote("第一行\n第二行") == '"第一行" & linefeed & "第二行"'
    assert messaging.quote("C:\\tmp") == '"C:\\\\tmp"'


def test_script_uses_the_apps_real_search_shortcut():
    script = messaging.script(*messaging.app_of("微信"), "张三", "在吗")
    assert 'tell process "WeChat"' in script
    assert "command down, shift down" in script      # 微信的搜索是 ⌘⇧F
    assert script.count("key code 36") == 2          # 一次进会话，一次发送


def test_permission_errors_point_at_the_right_settings_page():
    hint = messaging.permission_hint("execution error: … (-1743)")
    assert "辅助功能" in hint
    assert "辅助功能" in messaging.permission_hint("发生权限违例。 (-10004)")   # 中文系统上的原话
    assert messaging.permission_hint("syntax error") == ""


# ------------------------------------------------------------------ 定时

def test_parse_at_accepts_the_three_ways_people_say_time():
    assert scheduler.parse_at("+600") > time.time() + 590
    assert scheduler.parse_at("2026-10-29 09:00") > time.time()
    later = scheduler.parse_at("09:30")
    assert later > time.time() and time.localtime(later).tm_hour == 9
    assert scheduler.parse_at("明天早上") == 0        # 认不出就说认不出


def test_schedule_task_rejects_a_time_that_already_passed(cfg, executor):
    assert "过去了" in executor.execute_safely("schedule_task",
                                              {"text": "给张三发消息", "at": "2020-01-01 09:00"})


def test_schedule_task_survives_a_restart(cfg, executor):
    """存的是那句话本身，所以进程重启、配置重载都还在。"""
    executor.execute_safely("schedule_task", {"text": "给张三发消息说我晚点到",
                                              "at": "+600", "label": "给张三报备"})
    items = scheduler.pending(cfg)
    assert len(items) == 1
    assert items[0]["text"] == "给张三发消息说我晚点到"
    assert items[0]["approved"] is True
    assert scheduler.path_of(cfg).exists()


def test_due_fires_once_then_daily_comes_back_tomorrow(cfg):
    now = time.time()
    scheduler.add(cfg, "一次性任务", now - 1, label="一次")
    scheduler.add(cfg, "每天的任务", now - 1, label="每天", repeat="daily")
    fired = scheduler.due(cfg, now=now)
    assert sorted(item["label"] for item in fired) == ["一次", "每天"]
    rest = scheduler.pending(cfg)
    assert [item["label"] for item in rest] == ["每天"]
    assert float(rest[0]["due"]) > now + 86000


def test_cancel_by_keyword_and_by_all(cfg, executor):
    for label in ("给张三报备", "给李四报备"):
        executor.execute_safely("schedule_task", {"text": "发消息", "at": "+600", "label": label})
    assert "1 条" in executor.execute_safely("cancel_schedule", {"target": "张三"})
    assert "1 条" in executor.execute_safely("cancel_schedule", {})
    assert "还没有定时任务" in executor.execute_safely("list_schedules", {})


def test_list_schedules_is_human(cfg, executor):
    executor.execute_safely("schedule_task", {"text": "给张三发消息", "at": "+600",
                                              "label": "给张三报备"})
    result = executor.execute_safely("list_schedules", {})
    assert "1 条定时任务" in result and "给张三报备" in result


def test_scheduler_thread_actually_fires(cfg):
    """不验这条，调度就只是「看起来能定时」——文件里有、线程不跑，一样没用。"""
    fired: list[str] = []
    scheduler.add(cfg, "到点了", time.time() - 1, label="一次")
    scheduler.start(cfg, run=lambda item: fired.append(item["text"]) or "ok", every=0.05)
    deadline = time.time() + 3
    while not fired and time.time() < deadline:
        time.sleep(0.02)
    assert fired == ["到点了"]


def test_daemon_wiring_dispatches_the_scheduled_sentence(cfg):
    """`voxos run` 里真正那条线：到点 → 把那句话重新派发一遍 → 结果进历史。

    单独测 `scheduler.start` 只证明线程活着，不证明它接对了派发——
    这条测的是 `main._start_scheduler` 这个接线头。
    """
    from voxos import main, ui

    scheduler.add(cfg, "几点了", time.time() - 1, label="问时间", approved=True)
    runtime = main.Runtime(cfg, ui.StatusBus())
    main._start_scheduler(runtime)
    deadline = time.time() + 3
    while not runtime.recent and time.time() < deadline:
        time.sleep(0.02)
    assert runtime.recent, "定时任务到点了但没有派发"
    assert "点" in str(runtime.recent[0]["reply"])
