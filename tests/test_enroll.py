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
    留出最后一段才看得出「这个人这次录的第三句和前两句不像」。
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
    """两个数字不一致时，用留出分数判断——它才是「本人换句话能拿多少分」。

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
