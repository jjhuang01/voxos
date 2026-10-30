"""声纹录入的策略层：读哪几句、什么时候算读好、录完到底能不能用。

为什么单独一个模块：这三件事都要反复调（句子内容、时长门、离群门），
而调参不该需要麦克风，也不该动 `main.py` 里那 1700 行接线。
这里**不 import 音频栈**，只吃 numpy 数组和几个数，所以在无声卡的机器上也能跑测试。

一条实测出来的教训写在 `MIN_SEGMENTS` 上：只录 1 句时，同一个人的另一句话
只有 0.75 左右，而阈值就是 0.75——时通时不通。3 句取均值才有余量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import dsp

if TYPE_CHECKING:                       # 只在类型检查时存在：运行时一个 numpy 也不导
    import numpy as np

# 三句。选句标准不是「好听」，是**覆盖不同发音部位**：第一句多齿音与塞音，
# 第二句开口度大、元音靠后，第三句短促、有数字。三句都念完约 8 秒。
# 不用绕口令：声纹认的是音色与声道形状，不是普通话标准度；但也不要三句同韵，
# 那样的模板换个句子就飘。
SCRIPT: tuple[str, ...] = (
    "今天我把登录接口的空值校验补上了，顺手跑了三遍测试。",
    "屋里灯有点暗，我推开窗，风从阳台那边吹进来。",
    "明天上午十点开会，记得带上电脑和充电器。",
)

MIN_SEGMENTS = 3          # 少于 3 段算不出留出分数，模板也不稳
MIN_SPEECH_S = 1.2        # 本项目注册时的人声时长门槛，不是模型可靠性的保证
MIN_PEAK = 0.02           # 低于此值基本是没说话 / 设备选错
MAX_PEAK = 0.995          # 接近 1 就是削波，波形被削平后嵌入会飘
OUTLIER_COS = 0.45        # 与已有均值的余弦低于此值：这句不像同一个人
USABLE_MARGIN = 0.04      # 本项目注册质量余量，未来录音仍需独立验证


@dataclass
class Segment:
    """一段录音的质量画像。`score` 是它与**前面各段均值**的余弦。"""

    audio: np.ndarray
    speech_s: float
    peak: float
    score: float = 0.0


def peak_of(audio: np.ndarray) -> float:
    """峰值。空数组返回 0，不抛异常——录入路径上不该为一个空缓冲崩掉。

    用 `abs(数组).max()` 而不是 `np.abs`：模块顶层不 import numpy，
    这条链（main → enroll → dsp）在 `voxos --version` 上也要付钱。
    """
    signal = dsp.as_float(audio)
    return 0.0 if signal.size == 0 else float(abs(signal).max())


def check(audio: np.ndarray, speech_s: float) -> str:
    """这一段能不能用来建模板。返回空串表示可以，否则是给人看的原因。"""
    if dsp.as_float(audio).size < dsp.SR * 0.3:
        return "这段几乎没有声音（不到 0.3 秒），重来一次"
    peak = peak_of(audio)
    if speech_s < MIN_SPEECH_S:
        return f"只说了 {speech_s:.1f} 秒，太短——把整句读完再停"
    if peak < MIN_PEAK:
        return f"太小声（峰值 {peak:.3f}）——离麦克风近一点，或把系统输入音量调大"
    if peak >= MAX_PEAK:
        return "爆音了（波形被削平）——离麦克风远一点，或把输入音量调小"
    return ""


def outlier(vector: np.ndarray, previous: list[np.ndarray]) -> str:
    """这一段和前面几段是不是同一个人。空串表示没问题。

    这条门专治一种失败：录到一半家里人插了一句，或者蓝牙耳机切换了设备。
    混进模板的那一段会把均值拽偏，之后每句话都差一点点——正是「明明是本人
    却被拒」最常见的成因。
    """
    if not previous:
        return ""
    score = dsp.cosine(vector, dsp.mean_vector(list(previous)))
    if score < OUTLIER_COS:
        return f"这句和前面不像（{score:.2f}）——换人了还是换了设备？重读这一句"
    return ""


def holdout(vectors: list[np.ndarray]) -> float | None:
    """每段与其余段的模板比较，取最差留一分数，避免坏段被平均掩盖。

    这是注册样本的内部质量检查，不保证未来录音的误拒率或误受率。
    """
    if len(vectors) < MIN_SEGMENTS:
        return None
    scores = []
    for index, vector in enumerate(vectors):
        others = [item for position, item in enumerate(vectors) if position != index]
        scores.append(dsp.cosine(vector, dsp.mean_vector(others)))
    return min(scores)


def verdict(score: float, threshold: float, segments: int) -> tuple[bool, str]:
    """这次录入能不能用。返回 (可用, 一行结论)。"""
    if segments < MIN_SEGMENTS:
        return False, (f"⚠️  这次录入不可用：只录到 {segments} 段，至少需要 {MIN_SEGMENTS} 句。"
                       f"重录：voxos enroll，三句都读完")
    margin = score - threshold
    if margin >= USABLE_MARGIN:
        return True, (f"✅ 可用：最差留出验证 {score:.3f}，阈值 {threshold:.2f}，"
                      f"高 {margin:.3f}——本次注册余量合格，未来录音仍需实测")
    return False, (f"⚠️  这次录入不可用：最差留出验证 {score:.3f}，阈值 {threshold:.2f}，"
                   f"只高 {margin:+.3f}，可能出现本人说话也会被拒。\n"
                   f"   重录：voxos enroll —— 三句都读完，每句 2~3 秒，句间停半秒")


def script_lines() -> list[str]:
    """给界面/终端按顺序念的句子。"""
    return [f"{index}. {line}" for index, line in enumerate(SCRIPT, 1)]
