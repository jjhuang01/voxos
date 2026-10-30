"""录入结果能不能用——判据是「一致性比实际阈值高多少」。

这组测试守的是一个反复踩的坑：`enroll` 报了个数字，用户不知道该不该当真。

实测踩过两次：
- 只录 1 句 → 一致性 0.751，阈值 0.75，用户时通时不通；
- 录 10 秒被切成 5 段 → 一致性 0.755，还是同一个坑。

两次的提示都是「一致性偏低：建议重录」。用户看到「建议」会以为「已经录好了，
只是不完美」，然后继续用，继续被拒，继续以为软件坏了。所以判据必须直接说
「这次录入不可用」，并且说清为什么。
"""

from __future__ import annotations

import numpy as np
import pytest

from voxos import dsp
from voxos.main import _enroll_verdict


def _segments(count: int, seconds: float) -> list[object]:
    return [np.zeros(int(dsp.SR * seconds), dtype=np.float32) for _ in range(count)]


def test_verdict_rejects_a_profile_that_barely_clears_its_own_threshold(capsys):
    """一致性 0.755 / 阈值 0.75 —— 就是真机上那次，必须报「不可用」。"""
    _enroll_verdict(_segments(5, 2.0), 0.755, 0.75)

    out = capsys.readouterr().out
    assert "不可用" in out
    assert "本人说话也会被拒" in out


def test_verdict_accepts_a_profile_with_real_margin(capsys):
    """同人 0.79 / 异人 0.72 / 阈值 0.75 是标定过的可用组合。"""
    _enroll_verdict(_segments(3, 2.5), 0.79, 0.75)

    out = capsys.readouterr().out
    assert "可用" in out
    assert "不可用" not in out


def test_verdict_names_the_short_segments_that_drag_consistency_down(capsys):
    """碎段是主因，但用户看不出来——10 秒切 5 段平均 2 秒，其中可能混着 0.3 秒的碎片。

    只报「一致性偏低」等于让用户猜；报出「N 段短于 1 秒」才是可操作的。
    """
    samples = _segments(2, 2.5) + _segments(3, 0.4)

    _enroll_verdict(samples, 0.72, 0.75)

    out = capsys.readouterr().out
    assert "3 段短于 1 秒" in out
    assert "平均 1.2s" in out


def test_verdict_margin_is_measured_against_the_real_threshold(capsys):
    """阈值是配置项，不是常量。0.79 对 0.75 够用，对 0.85 就不够。

    以前写死 `if consistency < 0.8`：阈值 0.85 时 0.79 会被判「通过」，
    而它连自己的阈值都过不了。
    """
    _enroll_verdict(_segments(3, 2.5), 0.79, 0.75)
    assert "可用" in capsys.readouterr().out

    _enroll_verdict(_segments(3, 2.5), 0.79, 0.85)
    assert "不可用" in capsys.readouterr().out


def _direction_embedder(audio):
    """把音频第一个采样值映射成一个 one-hot 方向：同值=同一个人。"""
    vector = np.zeros(8, dtype=np.float32)
    vector[int(abs(float(audio[0]))) % 8] = 1.0
    return vector


def test_holdout_score_reveals_a_segment_that_does_not_match_the_rest():
    """留出分数必须来自「没参与建模」的那一段。

    用全部段建模板、再拿其中一段去验证，分数永远好看——那一段就在均值里。
    逐段留一才看得出「某句与其余两句不像」，不能只挑最后一段。
    """
    from voxos.main import _holdout_score

    lock = dsp.SpeakerLock(embedder=_direction_embedder)
    consistent = [np.full(100, 3.0, dtype=np.float32) for _ in range(3)]
    outlier = consistent[:2] + [np.full(100, 5.0, dtype=np.float32)]

    assert _holdout_score(lock, consistent) > 0.99
    assert _holdout_score(lock, outlier) < 0.1


def test_holdout_score_gives_up_when_there_is_nothing_to_hold_out():
    """只有 1~2 段时留出来就没得建模了，宁可返回 None 也不报个假分数。"""
    from voxos.main import _holdout_score

    lock = dsp.SpeakerLock(embedder=_direction_embedder)

    assert _holdout_score(lock, [np.full(100, 3.0, dtype=np.float32)]) is None
    assert _holdout_score(lock, [np.full(100, 3.0, dtype=np.float32)] * 2) is None


def test_verdict_prefers_the_holdout_score_over_consistency(capsys):
    """两个数字不一致时，用未参与该模板的留出段检查注册质量。

    一致性 0.80 看着漂亮，但留出只有 0.74，说明模板被某一段带偏了。
    """
    _enroll_verdict(_segments(3, 2.5), 0.80, 0.75, holdout=0.74)

    out = capsys.readouterr().out
    assert "不可用" in out
    assert "留出验证 0.740" in out


def _enrolled_cfg(tmp_path, segments: int, consistency: float, holdout, threshold: float):
    """造一个「已经录过音」的配置：npz 里放均值向量，旁边放录入元数据。"""
    from voxos.config import Config
    from voxos.main import _save_enroll_meta

    npz = tmp_path / "vp.npz"
    np.savez(npz, normal=np.ones(192, dtype=np.float32))
    cfg = Config({"audio": {"voiceprint_profiles": str(npz)}}, data_root=tmp_path)
    _save_enroll_meta(cfg, _segments(segments, 2.0), consistency, holdout, threshold)
    return cfg


def test_voiceprint_note_reports_quality_not_a_misleading_sample_count(tmp_path):
    """npz 里永远是 1 个均值向量，所以「只有 1 个样本」这句话从来就不对。

    录 1 句和录 5 段取均值，文件里长得一模一样（都是 1 个 192 维数组）。
    只数 npz 的 shape，doctor 会把「5 段均值」也报成「只有 1 个样本」，
    然后建议用户重录——而用户刚录完。必须读录入时记下的元数据。
    """
    from voxos.main import _voiceprint_note

    cfg = _enrolled_cfg(tmp_path, segments=5, consistency=0.755, holdout=None, threshold=0.75)
    note = _voiceprint_note(cfg)

    assert "5 段" in note
    assert "0.755" in note
    assert "会被自己拒" in note
    assert "只有 1 个样本" not in note


def test_voiceprint_note_calls_a_good_profile_usable(tmp_path):
    """留出 0.81 / 阈值 0.75 是标定过的可用组合，不该再劝人重录。"""
    from voxos.main import _voiceprint_note

    cfg = _enrolled_cfg(tmp_path, segments=3, consistency=0.84, holdout=0.81, threshold=0.75)
    note = _voiceprint_note(cfg)

    assert "可用" in note
    assert "重录" not in note


def test_voiceprint_note_admits_when_quality_was_never_recorded(tmp_path):
    """早期档案没有元数据。宁可说「未记录」，也不要编一个样本数出来。"""
    from voxos.config import Config
    from voxos.main import _voiceprint_note

    npz = tmp_path / "vp.npz"
    np.savez(npz, normal=np.ones(192, dtype=np.float32))
    cfg = Config({"audio": {"voiceprint_profiles": str(npz)}}, data_root=tmp_path)

    assert "质量未记录" in _voiceprint_note(cfg)


def test_guided_samples_runs_end_to_end(monkeypatch):
    """逐句录入整条跑一遍：0.1.39 曾因函数内漏导 dsp，读完第 1 句就 NameError。"""
    from voxos import audio, main
    from voxos.config import Config

    wave = np.zeros(dsp.SR, dtype=np.float32)
    monkeypatch.setattr(audio, "probe_mic", lambda *a, **k: ("测试麦", 0.5))
    monkeypatch.setattr(main, "_read_one", lambda *a, **k: wave)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=lambda x: np.ones(4, dtype=np.float32))
    samples, name = main._guided_samples(Config({}), lock)
    assert name == "测试麦" and len(samples) == len(main.enroll.SCRIPT)


def _quality_embedder(audio):
    """模型边界替身：首句余弦 0.68，其余两句同向；不替换质量或保存逻辑。"""
    if float(audio[0]) < 0.15:
        return np.array([0.68, np.sqrt(1 - 0.68 ** 2)], dtype=np.float32)
    return np.array([1.0, 0.0], dtype=np.float32)


def _quality_samples(values=(0.1, 0.2, 0.2)):
    return [np.full(2 * dsp.SR, value, dtype=np.float32) for value in values]


@pytest.mark.parametrize("bad_index", [0, 1, 2])
def test_holdout_rejects_the_worst_sentence_regardless_of_position(bad_index):
    """平均 0.838 会掩盖一条 0.680；只有最差留一分数能拦住这份模板。"""
    from voxos import enroll

    vectors = [np.array([1.0, 0.0], dtype=np.float32) for _ in range(3)]
    vectors[bad_index] = np.array([0.68, np.sqrt(1 - 0.68 ** 2)], dtype=np.float32)
    score = enroll.holdout(vectors)
    assert score == pytest.approx(0.68)
    assert enroll.verdict(score, 0.75, 3)[0] is False


def test_ui_holdout_catches_a_bad_first_sentence():
    """只留最后一条会得到 0.917，把第一条不匹配的录入误报为可用。"""
    from voxos.main import _holdout_score

    lock = dsp.SpeakerLock(threshold=0.75, embedder=_quality_embedder)
    assert _holdout_score(lock, _quality_samples()) == pytest.approx(0.68)


@pytest.mark.parametrize("count", [1, 2])
def test_ui_verdict_rejects_too_few_consistent_sentences(count, capsys):
    """自身一致性为 1 也不能让不足三句的首次录入变成成功。"""
    warning = _enroll_verdict(_segments(count, 2.0), 1.0, 0.75)
    assert warning and "不可用" in warning
    assert "重录" in capsys.readouterr().out


def _wav_files(samples, folder):
    """真实文件入口使用独立目录，不能混进身份档案字节比较。"""
    import wave

    folder.mkdir()
    files = []
    for index, signal in enumerate(samples):
        path = folder / f"input-{index}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(dsp.SR)
            handle.writeframes((signal * 32767).astype(np.int16).tobytes())
        files.append(str(path))
    return files


def _script_enroll(cfg, files, embedder, monkeypatch):
    """真实显式脚本，仅替换配置读取与模型边界，注册/质量/写盘保持真实。"""
    import importlib.util
    from pathlib import Path
    from voxos import audio

    script = Path(__file__).resolve().parents[1] / "scripts/enroll_voice.py"
    spec = importlib.util.spec_from_file_location("enroll_voice", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load", lambda: cfg)
    monkeypatch.setattr(audio, "make_embedder", lambda config: (embedder, 0.75, "test"))
    monkeypatch.setattr(module.sys, "argv", [str(script), "--mode", "normal"]
                        + [arg for file in files for arg in ("--wav", file)])
    return module.main()


@pytest.mark.parametrize("entrypoint", ["cli", "ui", "script"])
@pytest.mark.parametrize("existing", [False, True], ids=["first-enroll", "reenroll"])
@pytest.mark.parametrize("values", [(0.1, 0.2, 0.2), (0.2,), (0.2, 0.2)],
                         ids=["bad-first-sentence", "one-sentence", "two-sentences"])
def test_unusable_enrollment_never_commits_identity(tmp_path, monkeypatch, entrypoint, existing, values):
    """真实 npz/json 副作用：失败返回非成功，内存和磁盘都保留先前身份。"""
    from voxos import main
    from voxos.config import Config

    folder = tmp_path / "identity"
    folder.mkdir()
    path = folder / "vp.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_quality_embedder)
    previous = np.array([0.0, 1.0], dtype=np.float32)
    original = {}
    if existing:
        lock.profiles = {"normal": previous}
        lock.save(path)
        path.with_suffix(".json").write_text('{"usable": true, "holdout": 0.8676}')
        original = {item.name: item.read_bytes() for item in folder.iterdir()}
    samples = _quality_samples(values)
    if entrypoint == "cli":
        assert main._commit_enroll(cfg, lock, samples) == 2
    elif entrypoint == "script":
        files = _wav_files(samples, tmp_path / "inputs")
        assert _script_enroll(cfg, files, lock.embedder, monkeypatch) == 2
    else:
        result = main._enroll_now(cfg, lock, 0.0, capture=lambda: samples)
        assert result["ok"] is False
    assert {item.name: item.read_bytes() for item in folder.iterdir()} == original
    if existing:
        assert lock.profiles["normal"] is previous
    else:
        assert lock.profiles == {}


@pytest.mark.parametrize("entrypoint", ["cli", "ui", "script"])
def test_good_enrollment_commits_the_new_score_method(tmp_path, monkeypatch, entrypoint):
    """合格的三句仍能落盘；新元数据必须与旧版平均/最后一条分数区分。"""
    import json
    from voxos import main
    from voxos.config import Config

    path = tmp_path / "vp.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_quality_embedder)
    samples = _quality_samples((0.2, 0.2, 0.2))
    if entrypoint == "cli":
        assert main._commit_enroll(cfg, lock, samples) == 0
    elif entrypoint == "script":
        files = _wav_files(samples, tmp_path / "inputs")
        assert _script_enroll(cfg, files, lock.embedder, monkeypatch) == 0
    else:
        result = main._enroll_now(cfg, lock, 0.0, capture=lambda: samples)
        assert result["ok"] is True and result["usable"] is True
    with np.load(path) as profiles:
        np.testing.assert_allclose(profiles["normal"], [1.0, 0.0], atol=1e-6)
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta.get("holdout_method") == "leave_one_out_min"
    assert meta["holdout"] == 1.0 and meta["usable"] is True
    state = main._voiceprint_state(cfg, dsp.SpeakerLock().load(path))
    assert state.get("holdout_method") == "leave_one_out_min"


def test_legacy_holdout_is_not_relabelled_as_worst_leave_one_out():
    """旧档案没有原始录音，不能升级标签后声称已逐句复核。"""
    from voxos.main import _meta_note

    note = _meta_note({"segments": 3, "holdout": 0.8676, "threshold": 0.75,
                       "usable": True})
    assert "旧版" in note and "未复核各段" in note
    assert "最差留一" not in note


@pytest.mark.parametrize("entrypoint", ["wav", "batch", "ui", "script"])
@pytest.mark.parametrize("existing", [False, True], ids=["first-enroll", "reenroll"])
@pytest.mark.parametrize("seconds, amplitude, speech_seconds", [
    (0.2, 0.2, 0.2), (1.0, 0.2, 1.0), (2.0, 0.001, 2.0),
    (2.0, 1.0, 2.0), (2.0, 0.2, 0.2),
], ids=["tiny", "short-speech", "too-quiet", "clipped", "silence-padding"])
def test_bad_waveform_never_commits_even_with_perfect_embeddings(
        tmp_path, monkeypatch, entrypoint, existing, seconds, amplitude, speech_seconds):
    """三句嵌入全同向也不能掩盖时长/峰值不合格；真实 CLI 和 UI 都须保留身份。"""
    import argparse
    import threading
    from voxos import main
    from voxos.config import Config

    folder = tmp_path / "identity"
    folder.mkdir()
    path = folder / "vp.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    previous = np.array([0.0, 1.0], dtype=np.float32)
    lock = dsp.SpeakerLock(threshold=0.75,
                           embedder=lambda signal: np.array([1.0, 0.0], dtype=np.float32))
    if existing:
        lock.profiles = {"normal": previous}
        lock.save(path)
        path.with_suffix(".json").write_text('{"usable": true, "holdout": 0.8676}')
    original = {item.name: item.read_bytes() for item in folder.iterdir()}
    bad = np.zeros(int(seconds * dsp.SR), dtype=np.float32)
    bad[:int(speech_seconds * dsp.SR)] = amplitude
    samples = _quality_samples((0.2, 0.2, 0.2))
    samples[1] = bad
    done = threading.Event()
    if entrypoint == "ui":
        result = main._enroll_now(cfg, lock, 0.0, done=done, capture=lambda: samples)
        assert result["ok"] is False and "不可用" in result["error"]
        assert not done.is_set()
    elif entrypoint == "script":
        files = _wav_files(samples, tmp_path / "inputs")
        assert _script_enroll(cfg, files, lock.embedder, monkeypatch) == 2
    else:
        monkeypatch.setattr(main, "_lock", lambda config: lock)
        files = _wav_files(samples, tmp_path / "inputs") if entrypoint == "wav" else []
        if entrypoint == "batch":
            monkeypatch.setattr(main, "_record_segments", lambda *args: samples)
        args = argparse.Namespace(script=False, wav=files, check=False, batch=True,
                                  record=10.0, mode="normal")
        assert main.cmd_enroll(args, cfg) == 2
    assert {item.name: item.read_bytes() for item in folder.iterdir()} == original
    if existing:
        assert lock.profiles["normal"] is previous
    else:
        assert lock.profiles == {}


@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
@pytest.mark.parametrize("existing", [False, True], ids=["first-enroll", "reenroll"])
@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_profile_write_preserves_identity_and_removes_only_its_tempfile(
        tmp_path, monkeypatch, entrypoint, existing, failure):
    """真实 np.savez 中途磁盘满或替换失败：原 npz/json 完整，运行态和完成事件不变。"""
    import errno
    import os
    import threading
    from voxos import main
    from voxos.config import Config

    path = tmp_path / "vp.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    previous = np.array([0.0, 1.0], dtype=np.float32)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_quality_embedder)
    if existing:
        lock.profiles = {"normal": previous}
        lock.save(path)
        path.with_suffix(".json").write_text('{"usable": true, "holdout": 0.8676}')
    (tmp_path / "unrelated.tmp").write_text("must stay")
    original = {item.name: item.read_bytes() for item in tmp_path.iterdir()}

    def write_failure(handle, *args, **kwargs):
        handle.write(b"partial new array")
        raise OSError(errno.ENOSPC, "injected disk full")

    def replace_failure(*args):
        raise OSError(errno.EACCES, "injected replace failure")

    if failure == "write":
        monkeypatch.setattr(np.lib.format, "write_array", write_failure)
    else:
        monkeypatch.setattr(os, "replace", replace_failure)
    samples = _quality_samples((0.2, 0.2, 0.2))
    done = threading.Event()
    if entrypoint == "cli":
        assert main._commit_enroll(cfg, lock, samples) == 2
    else:
        result = main._enroll_now(cfg, lock, 0.0, done=done, capture=lambda: samples)
        assert result["ok"] is False and "保存失败" in result["error"]
    assert not done.is_set()
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == original
    if existing:
        assert lock.profiles["normal"] is previous
        np.testing.assert_array_equal(dsp.SpeakerLock().load(path).profiles["normal"], previous)
    else:
        assert lock.profiles == {}


@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
def test_live_identity_is_published_only_after_profile_save(tmp_path, monkeypatch, entrypoint):
    """写盘进行中运行态仍是旧身份，成功后才一次替换为新模板。"""
    from voxos import main
    from voxos.config import Config

    path = tmp_path / "vp.npz"
    cfg = Config({"audio": {"voiceprint_profiles": str(path), "vad_backend": "energy"}},
                 data_root=tmp_path)
    previous = np.array([0.0, 1.0], dtype=np.float32)
    lock = dsp.SpeakerLock(threshold=0.75, embedder=_quality_embedder,
                           profiles={"normal": previous})
    lock.save(path)
    original = path.read_bytes()
    write_array = np.lib.format.write_array

    def observe_save(handle, *args, **kwargs):
        assert lock.profiles["normal"] is previous
        assert path.read_bytes() == original
        return write_array(handle, *args, **kwargs)

    monkeypatch.setattr(np.lib.format, "write_array", observe_save)
    samples = _quality_samples((0.2, 0.2, 0.2))
    if entrypoint == "cli":
        assert main._commit_enroll(cfg, lock, samples) == 0
    else:
        assert main._enroll_now(cfg, lock, 0.0, capture=lambda: samples)["ok"] is True
    np.testing.assert_allclose(lock.profiles["normal"], [1.0, 0.0], atol=1e-6)
    np.testing.assert_allclose(dsp.SpeakerLock().load(path).profiles["normal"], [1.0, 0.0], atol=1e-6)


def test_ui_enroll_can_be_stopped_midway(monkeypatch, tmp_path):
    """胶囊录入点「停止」：下一块音频就退出，原声纹不动，锁释放后能再录。"""
    from types import SimpleNamespace

    from voxos import audio, main, ui
    from voxos.config import Config

    class Mic:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            for index in range(1000):
                if index == 3:
                    main._cancel_enroll()          # 模拟用户在第 3 块时点停止
                yield np.zeros(512, dtype=np.float32)

    monkeypatch.setattr(audio, "MicSource", Mic)
    monkeypatch.setattr(audio, "make_vad", lambda cfg: SimpleNamespace(is_speech=lambda c: False))
    original = {"normal": np.ones(4, dtype=np.float32)}
    lock = dsp.SpeakerLock(threshold=0.75, embedder=lambda x: np.ones(4, dtype=np.float32))
    lock.profiles = dict(original)
    runtime = SimpleNamespace(cfg=Config({}, data_root=tmp_path), bus=ui.StatusBus(None))

    result = main._enroll_from_ui(runtime, lock, None)

    assert result["cancelled"] is True and result["ok"] is False
    assert lock.profiles.keys() == original.keys()
    assert not main._MUTED.is_set()
    assert main._ENROLL_BUSY.acquire(blocking=False)
    main._ENROLL_BUSY.release()


def test_ui_enroll_rejects_a_second_concurrent_start(tmp_path):
    from types import SimpleNamespace

    from voxos import main, ui
    from voxos.config import Config

    runtime = SimpleNamespace(cfg=Config({}, data_root=tmp_path), bus=ui.StatusBus(None))
    lock = dsp.SpeakerLock(threshold=0.75, embedder=lambda x: np.ones(4, dtype=np.float32))
    assert main._ENROLL_BUSY.acquire(blocking=False)
    try:
        assert main._enroll_from_ui(runtime, lock, None)["error"] == "已经在录入了"
    finally:
        main._ENROLL_BUSY.release()
