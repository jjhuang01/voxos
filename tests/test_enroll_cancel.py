"""录入取消边界：录音后处理、释放锁与原身份不可被取消请求绕过。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voxos import dsp, main, ui
from voxos.config import Config


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    """仅替换录音和模型；质量门禁、保存、取消和监听恢复都用真实实现。"""
    path = tmp_path / "profile.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=lambda wave: np.array([1., 0.]))
    previous = np.array([0., 1.], dtype=np.float32)
    lock.profiles = {"normal": previous}
    lock.save(path)
    path.with_suffix(".json").write_text('{"usable": true, "segments": 3}')
    original = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    samples = [np.full(2 * dsp.SR, 0.2, dtype=np.float32) for _ in range(3)]
    runtime = SimpleNamespace(cfg=cfg, bus=ui.StatusBus(None))
    monkeypatch.setattr(main, "_ui_samples", lambda *args: samples)
    monkeypatch.setattr(main, "_MUTED", threading.Event())
    monkeypatch.setattr(main, "_ENROLL_BUSY", threading.Lock())
    monkeypatch.setattr(main, "_ENROLL_CANCEL", threading.Event())
    monkeypatch.setattr(main, "_ENROLL_GUARD", threading.Lock())
    monkeypatch.setattr(main, "_ENROLL_STATE", {"phase": "idle", "index": 0, "total": 3, "line": "", "result": None})
    monkeypatch.setattr(main, "_RESET_HOOK", {"reset": None})
    return runtime, lock, previous, original, samples


@pytest.mark.parametrize("phase", ["capture", "quality", "embedding"])
@pytest.mark.parametrize("muted", [False, True])
def test_cancel_during_preparation_never_saves_identity(enrollment, monkeypatch, phase, muted):
    """停止发生在录音完成/校验/模型计算时，也不能写盘或发布新身份。"""
    runtime, lock, previous, original, samples = enrollment
    if muted:
        main._MUTED.set()
    done = threading.Event()
    if phase == "capture":
        def capture(*args):
            main._cancel_enroll()
            return samples
        monkeypatch.setattr(main, "_ui_samples", capture)
    elif phase == "quality":
        quality = main._enroll_quality
        def check(cfg, waves):
            result = quality(cfg, waves)
            main._cancel_enroll()
            return result
        monkeypatch.setattr(main, "_enroll_quality", check)
    else:
        def embed(wave):
            main._cancel_enroll()
            return np.array([1., 0.], dtype=np.float32)
        lock.embedder = embed

    result = main._enroll_from_ui(runtime, lock, done)

    assert result.get("cancelled") is True
    assert not done.is_set()
    assert lock.profiles["normal"] is previous
    assert {file.name: file.read_bytes() for file in runtime.cfg.data_root.iterdir()} == original
    assert main._MUTED.is_set() is muted


def test_enrollment_lock_stays_held_through_listener_restore(enrollment, monkeypatch):
    """旧录入恢复监听期间不能开始下一次，否则下一次的静音会被旧 finally 清掉。"""
    runtime, lock, *_ = enrollment
    entered = []
    def restore():
        acquired = main._ENROLL_BUSY.acquire(blocking=False)
        entered.append(acquired)
        if acquired:
            main._ENROLL_BUSY.release()
    monkeypatch.setattr(main, "_RESET_HOOK", {"reset": restore})

    main._enroll_from_ui(runtime, lock, None)

    assert entered == [False, False]
    assert not main._MUTED.is_set()


def test_listener_restore_failure_does_not_lock_recording(enrollment, monkeypatch):
    """恢复钩子异常不能泄漏录入锁；监听保留暂停并显示恢复失败，而非静默假成功。"""
    runtime, lock, *_ = enrollment
    attempts = []
    def restore():
        attempts.append(True)
        if len(attempts) > 1:
            raise RuntimeError("injected reset failure")
    monkeypatch.setattr(main, "_RESET_HOOK", {"reset": restore})

    result = main._enroll_from_ui(runtime, lock, None)

    assert "恢复" in str(result.get("warning", ""))
    assert main._MUTED.is_set()
    assert main._ENROLL_BUSY.acquire(blocking=False)
    main._ENROLL_BUSY.release()


@pytest.mark.parametrize("on", [False, True])
def test_enrollment_restores_latest_listening_choice(enrollment, monkeypatch, on):
    """录入期间改变监听意图，不能被录入开始时的快照覆盖。"""
    runtime, lock, *_ = enrollment
    if on:
        main._MUTED.set()

    def capture(*args):
        result = main._set_listening(runtime, on)
        assert result["listening"] is False, "录入未结束，不能恢复普通识别"
        main._cancel_enroll()
        return []

    monkeypatch.setattr(main, "_ui_samples", capture)
    result = main._enroll_from_ui(runtime, lock, None)
    assert result.get("cancelled") and main._MUTED.is_set() is not on


def test_cancel_after_atomic_save_begins_is_explicitly_rejected(enrollment, monkeypatch):
    """保存边界已经跨过时不能谎报取消成功。"""
    runtime, lock, *_ = enrollment
    real_save = dsp.SpeakerLock.save
    replies = []

    def save(candidate, path):
        replies.append(main._cancel_enroll())
        real_save(candidate, path)

    monkeypatch.setattr(dsp.SpeakerLock, "save", save)
    result = main._enroll_from_ui(runtime, lock, threading.Event())
    assert result["ok"] and not result.get("cancelled")
    assert len(replies) == 1 and replies[0]["ok"] is False
    assert "保存" in replies[0]["error"]
