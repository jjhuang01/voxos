"""UI 控制契约：取消是正常结局，拒绝不能伪装成功，停止走同一控制入口。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from voxos import main, ui


def _post(ctx, path, token="test-only", body=None):
    server = ui.serve(ctx, "127.0.0.1", 0, block=False)
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", "X-VoxOS-Token": token})
    try:
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
    finally:
        server.shutdown()
        server.server_close()


def test_enrollment_cancelled_is_a_normal_structured_result():
    ctx = ui.UiContext(ui.StatusBus(None), token="test-only", on_enroll=lambda seconds: {
        "ok": False, "cancelled": True, "error": "已停止录入"})
    code, result = _post(ctx, "/api/enroll")
    assert code == 200 and result["cancelled"] is True


def test_enrollment_cancel_rejection_is_not_http_success():
    ctx = ui.UiContext(ui.StatusBus(None), token="test-only", on_enroll_cancel=lambda: {
        "ok": False, "error": "正在保存，请稍候"})
    code, result = _post(ctx, "/api/enroll/cancel")
    assert code == 409 and result["ok"] is False


def test_stop_endpoint_requires_token_and_reports_actual_control_result():
    seen = []
    ctx = ui.UiContext(ui.StatusBus(None), token="test-only",
                       on_stop=lambda: seen.append(True) or {"ok": True, "stopped": True})
    assert _post(ctx, "/api/stop", token="wrong")[0] == 401 and seen == []
    code, result = _post(ctx, "/api/stop")
    assert code == 200 and result["stopped"] is True and seen == [True]


def test_command_exception_reports_failure_not_done():
    def command(text):
        raise RuntimeError("测试执行失败")

    ctx = ui.UiContext(ui.StatusBus(None), token="test-only", on_command=command)
    code, result = _post(ctx, "/command")
    assert code == 500 and result["ok"] is False and "执行失败" in result["error"]


def test_real_command_business_failure_is_not_wrapped_as_success(cfg):
    """真实安全网关拒绝后的 HTTP 结果不能因回调正常返回而变成成功。"""
    runtime = main.Runtime(cfg, ui.StatusBus(None))
    ctx = main._ui_context(runtime, None)
    ctx.token = "test-only"
    missing = cfg.path("workspace.default_code_dir") / "不存在的测试文件"
    code, result = _post(ctx, "/command", body={"text": f"打开文件 {missing}"})
    assert code == 200 and result["ok"] is False
    assert result["outcome"] == "failed" and "不存在" in result["reply"]


def test_confirmation_snapshot_survives_unrelated_status(cfg):
    """识别半句或状态更新不能覆盖仍在等待的确认卡。"""
    import threading
    from voxos import core

    bus = ui.StatusBus(None)
    gate = main.ConfirmGate(bus, 2)
    worker = threading.Thread(target=gate.ask, args=("write_code", {}, core.Verdict(
        core.RiskLevel.RED_CONFIRM, "需要确认这次测试")), daemon=True)
    channel = bus.subscribe()
    worker.start()
    event = channel.get(timeout=1)
    try:
        bus.emit("listening", "聆听中", "这是另一句")
        pending = bus.snapshot().get("confirmation")
        assert pending and pending["request"] == event.request
        assert gate.resolve(False, event.request)
    finally:
        gate.resolve(False, event.request)
        worker.join(timeout=3)
        bus.unsubscribe(channel)
    assert bus.snapshot().get("confirmation") is None
