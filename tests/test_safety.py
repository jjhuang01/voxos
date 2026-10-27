"""安全红线测试：禁区拦截、角色隔离、快照回滚、单飞锁、审计。"""

from __future__ import annotations

import json
import threading

from voxos import core
from voxos.config import load_redlines


def test_forbidden_commands_are_hard_blocked(executor):
    cases = [
        {"file_path": "a.py", "operation": "CREATE_OR_OVERWRITE", "new_code": "rm -rf /"},
        {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "git push origin main --force"},
        {"file_path": "b.sql", "operation": "CREATE_OR_OVERWRITE", "new_code": "DROP TABLE users;"},
        {"file_path": "c.sh", "operation": "APPEND_CODE", "new_code": "sudo shutdown -h now"},
    ]
    for args in cases:
        result = executor.execute_safely("write_code", args)
        assert result.startswith("⛔"), result


def test_redline_patterns_cover_documented_list():
    patterns = load_redlines()["forbidden"]
    assert len(patterns) >= 10
    assert any("rm" in pattern for pattern in patterns)
    assert any("prescription" in pattern for pattern in patterns)


def test_ops_role_cannot_write_files(cfg, bus):
    ops_cfg = cfg.with_overrides(**{"workspace.role": "OPS"})
    executor = core.Executor(cfg=ops_cfg, on_event=bus)
    result = executor.execute_safely("write_code", {"file_path": "x.py", "operation": "CREATE_OR_OVERWRITE"})
    assert "OPS" in result and "权限" in result
    assert "已巡检" not in result


def test_doctor_role_marks_draft(cfg, bus):
    doctor = cfg.with_overrides(**{"workspace.role": "DOCTOR"})
    executor = core.Executor(cfg=doctor, on_event=bus)
    result = executor.execute_safely("append_structured_log", {"content": "查房记录：患者体温 37.2"})
    assert result.startswith("【未审核草稿】")
    assert "已记录" in result


def test_unknown_tool_is_rejected(cfg, executor):
    assert "未知工具" in executor.execute_safely("rm_rf_everything", {})


def test_red_confirm_requires_approval(cfg, bus):
    denied = core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: False)
    result = denied.execute_safely("delegate_to_coding_cli", {"prompt": "重构登录模块"})
    assert "已取消" in result

    approved = core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: True)
    assert "dry-run" in approved.execute_safely("delegate_to_coding_cli", {"prompt": "重构登录模块"})


def test_confirm_channel_exception_fails_closed(cfg, bus):
    def boom(*_: object) -> bool:
        raise RuntimeError("确认通道炸了")

    executor = core.Executor(cfg=cfg, on_event=bus, confirm=boom)
    result = executor.execute_safely("delegate_to_coding_cli", {"prompt": "x"})
    assert "已取消" in result or "已拦截" in result


def test_confirm_text_is_human(cfg, bus):
    """确认卡是最后一道防线：上面写的必须是人话，不能是 red_confirm · tool_name。"""
    seen: list[object] = []
    executor = core.Executor(cfg=cfg, on_event=bus,
                             confirm=lambda name, args, verdict: seen.append(verdict) or False)
    executor.execute_safely("delegate_to_coding_cli", {"prompt": "重构登录模块"})
    verdict = seen[0]
    assert "red_confirm" not in verdict.reason.lower()
    assert "delegate_to_coding_cli" not in verdict.reason
    assert "确认" in verdict.reason or "点头" in verdict.reason


def test_blocked_reason_is_human(cfg, bus):
    """角色越权与红线拦截的理由同样要能直接读给用户听。"""
    guard = core.Guard(role="OPS")
    reason = guard.inspect("write_code", {"path": "/tmp/a.py"}).reason
    assert "OPS" in reason and "write_code" not in reason
    forbidden = guard.inspect("write_code", {"content": "rm -rf /"}).reason
    assert "红线" in forbidden and "已直接拒绝" in forbidden


def test_every_tool_has_a_human_name():
    """人话名字来自工具自己的中文描述首句——新加工具忘了写中文，这里就会红。"""
    core.ensure_tools()
    missing = [name for name in core.REGISTRY if core.human_name(name) == name]
    assert not missing, f"这些工具没有中文描述：{missing}"


def test_snapshot_and_rollback(cfg, executor, tmp_path):
    target = tmp_path / "projects" / "demo.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")

    executor.execute_safely("write_code", {"file_path": "demo.py", "operation": "APPEND_CODE",
                                           "new_code": "value = 2"})
    assert "value = 2" in target.read_text(encoding="utf-8")

    snapshots = list((tmp_path / "backups").glob("*/demo.py"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == "value = 1\n"

    rollback = executor.execute_safely("rollback_file", {"file_path": "demo.py"})
    assert "已回滚" in rollback
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_rollback_without_snapshot_is_honest(cfg, executor):
    assert "没有可回滚" in executor.execute_safely("rollback_file", {"file_path": "never.py"})


def test_single_flight_lock_blocks_concurrent_write(cfg, bus):
    executor = core.Executor(cfg=cfg, on_event=bus, lock_timeout_s=0.05)
    executor._lock.acquire()
    try:
        result = executor.execute_safely("write_code", {"file_path": "x.py",
                                                        "operation": "CREATE_OR_OVERWRITE"})
        assert "还在执行" in result
    finally:
        executor._lock.release()


def test_green_tools_not_blocked_by_lock(cfg, bus):
    executor = core.Executor(cfg=cfg, on_event=bus, lock_timeout_s=0.01)
    executor._lock.acquire()
    try:
        assert "日志已记录" in executor.execute_safely("append_structured_log", {"content": "绿色不受锁影响"})
    finally:
        executor._lock.release()


def test_audit_record_written(cfg, executor):
    executor.execute_safely("append_structured_log", {"content": "审计测试"}, text="记一条审计测试")
    lines = cfg.path("audit.path").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["tool"] == "append_structured_log"
    assert record["risk"] == "GREEN"
    assert record["ok"] is True
    assert "ms" in record


def test_events_emitted_in_order(cfg, bus):
    executor = core.Executor(cfg=cfg, on_event=bus)
    executor.execute_safely("append_structured_log", {"content": "状态事件"})
    states = [event["state"] for event in bus.history()]
    assert states[0] == "executing"
    assert states[-1] == "done"


def test_tool_exception_does_not_escape(cfg, bus):
    executor = core.Executor(cfg=cfg, on_event=bus)
    result = executor.execute_safely("read_file_head", {"file_path": "/nonexistent/xx.py"})
    assert "不存在" in result


def test_concurrent_writes_are_serialized(cfg, bus, tmp_path):
    executor = core.Executor(cfg=cfg, on_event=bus, lock_timeout_s=5.0)
    target = tmp_path / "projects" / "race.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")

    def worker(index: int) -> None:
        executor.execute_safely("write_code", {"file_path": "race.py", "operation": "APPEND_CODE",
                                               "new_code": f"line_{index}"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    content = target.read_text(encoding="utf-8")
    assert all(f"line_{i}" in content for i in range(4))
    assert len(list((tmp_path / "backups").glob("*/race.py"))) == 4
