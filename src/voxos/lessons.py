"""抗体库：把「犯过的错」变成下次能查的东西。

## 为什么需要它

同一个毛病会反复出现，而每次出现都像第一次：用户不知道原因，我们也不知道——
因为**上一轮的结论没有被保存下来**。这一层就是保存结论的地方。

它做三件事，对应三种不同的「经验」：

1. **症状 → 修法**（`SYMPTOMS`）：已知的坑。声纹拒了自己、麦克风静音、
   空转写……每条都配一句能直接执行的修法，而不是一句「请检查配置」。
2. **从时间线统计**（`diagnose`）：不是猜，是数。最近 200 个回合里哪一类
   失败最多，就把它排在最前面——用户打开面板第一眼看到的就是当前最该修的那件。
3. **未匹配话术**（`unmatched`）：用户说了但没匹配上任何工具的句子。
   这是**唯一能自动发现「该加哪条正则」的入口**——单句看不出来，
   十句放在一起就是需求。

第 3 条是「自增强」最实在的一条：系统不需要变聪明，只需要把「没听懂的话」
攒起来给人看。攒够十条，加一条正则，永久解决。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["SYMPTOMS", "Finding", "diagnose", "unmatched", "advice_for"]

# 症状 → (为什么，怎么办)。**怎么办必须是能直接执行的一句话**：
# 「请检查配置」等于没说，用户不知道检查哪一项。
SYMPTOMS: dict[str, tuple[str, str]] = {
    "声纹不匹配": (
        "档案里的声纹和现在这个麦克风对不上，或者只录了 1 句（同人换句话就可能掉到阈值以下）",
        "voxos enroll --record 3   # 录 3 句取均值，单句在阈值边缘会时通时不通",
    ),
    "太短": (
        "人声时长没到 audio.min_speech_ms（默认 200ms），被当成咳嗽放过去了",
        "说完整一点；或者在设置里把「最短人声」调到 150ms",
    ),
    "空转写": (
        "识别器一个字都没出：麦克风可能静音、增益太低，或者 VAD 把噪声当成了人声",
        "voxos doctor 看「输入设备」那行的峰值，正常说话应该在 0.05 以上",
    ),
    "回声门丢弃": (
        "正在播报时麦克风被关掉了（半双工），你说的话正好落在播报期里",
        "等它念完再说；根治要等 AEC（TODO_ROADMAP Task 2.14）",
    ),
    "识别失败": (
        "识别后端抛了异常（模型文件损坏、onnxruntime 版本不对、内存不足）",
        "voxos doctor 看「识别后端」那行；必要时 voxos models --force 重下",
    ),
    "音频设备异常": (
        "麦克风断流：蓝牙耳机摘了、USB 声卡拔了、或者权限被系统收回",
        "voxos doctor 看「输入设备」；系统设置 → 隐私与安全性 → 麦克风",
    ),
}

# 没匹配上任何工具。这条不是「错误」，是「盲区」——所以修法和上面不同。
UNMATCHED = ("没匹配上任何工具（快通道正则和 LLM 都没接住）",
             "看看下面这些句子，给最常说的那条加一条快通道正则（PROMPTS_AND_ROUTING.md）")


@dataclass
class Finding:
    """一条结论：症状 + 出现几次 + 怎么办 + 典型样本。"""

    symptom: str
    count: int
    advice: str
    samples: list[str] = field(default_factory=list)

    def line(self) -> str:
        sample = f"  例：{self.samples[0]}" if self.samples else ""
        return f"{self.count:>3}× {self.symptom}\n     → {self.advice}{sample}"


def advice_for(reason: str) -> tuple[str, str]:
    """把一条原始 reason 映射到（症状, 修法）。

    `reason` 的写法来自 `AudioPipeline._drop`，形如「声纹不匹配，已丢弃」。
    这里只做**前缀包含**匹配：症状名是我们自己写的，不该要求调用方精确拼串。
    """
    for symptom, (_, advice) in SYMPTOMS.items():
        if symptom in reason:
            return symptom, advice
    return reason.strip("，。 ") or "未知原因", "voxos log --limit 50 看完整记录"


def diagnose(turns: Iterable[dict[str, Any]], limit: int = 3) -> list[Finding]:
    """从时间线里数出「当前最该修的三件事」。

    只看失败和丢弃：成功的回合没有可修的东西，混进来会把真正的问题稀释掉。
    """
    counter: Counter[str] = Counter()
    advice: dict[str, str] = {}
    samples: dict[str, list[str]] = {}

    for turn in turns:
        symptom, fix = _classify(turn)
        if not symptom:
            continue
        counter[symptom] += 1
        advice[symptom] = fix
        text = str(turn.get("text", "")).strip()
        if text and len(samples.setdefault(symptom, [])) < 3:
            samples[symptom].append(text)

    return [Finding(name, count, advice[name], samples.get(name, []))
            for name, count in counter.most_common(limit)]


def _classify(turn: dict[str, Any]) -> tuple[str, str]:
    """一个回合算哪种症状。成功返回空字符串。"""
    outcome = str(turn.get("outcome", ""))
    if outcome == "dropped":
        return advice_for(str(turn.get("reason", "")))
    if outcome == "failed":
        return ("执行失败：" + str(turn.get("tool", "")),
                "voxos log --limit 20 看那次的完整参数和报错")
    if outcome == "missed":
        return UNMATCHED
    return "", ""


def unmatched(turns: Iterable[dict[str, Any]], limit: int = 20) -> list[str]:
    """没听懂的话。**去重后按出现次数排**——说两遍的那句才值得加正则。"""
    counter: Counter[str] = Counter()
    for turn in turns:
        if str(turn.get("outcome", "")) == "missed":
            text = str(turn.get("text", "")).strip()
            if text:
                counter[text] += 1
    return [f"{text}（{count}×）" if count > 1 else text
            for text, count in counter.most_common(limit)]
