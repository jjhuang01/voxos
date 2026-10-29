#!/usr/bin/env python3
"""语音链路的可复现测量。不靠感觉，靠数字。

现在能测的只有一件：**端点延迟**——「最后一个语音块被读进来」到「管道交出一句话」
之间，白等了多少毫秒。这是「丝滑」里最容易被用户感知的一段，也是纯算法决定的，
不依赖网络和 LLM。

用法：
    python scripts/voice_bench.py endpoint            # 脚本回放 STT，秒出结果
    python scripts/voice_bench.py endpoint --real     # 用真 SenseVoice，含解码耗时

刻意**不测**的（需要语料/新模型，见 docs/VOICE.md §9）：
判停 TPR/TNR、打断延迟、长跑稳定性。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voxos import audio, dsp                      # noqa: E402
from voxos.config import load                     # noqa: E402

SR = 16000
CHUNK = 512


class _Pass:
    """声纹永远通过的替身：这一项测分段，不测身份。"""

    def verify(self, audio):  # noqa: A002 - 与 SpeakerLock 的签名保持一致
        return True, 1.0


def _voice(seconds: float, seed: int = 1) -> np.ndarray:
    """合成一段「像人声」的信号：谐波堆叠 + 轻噪声，VAD 能判成人声。"""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    x = sum(np.sin(2 * np.pi * 118 * k * t) / k for k in range(1, 6))
    x += 0.02 * rng.standard_normal(n)
    return (x / np.abs(x).max() * 0.3).astype(np.float32)


def _silence(ms: float) -> np.ndarray:
    return np.zeros(int(SR * ms / 1000), dtype=np.float32)


def _make_stt(real: bool, cfg):
    if not real:
        return audio.ScriptedStt(["把那个文件打开"])
    cfg = cfg.with_overrides(**{"stt.backend": "sense_voice"})
    return audio.make_stt(cfg, [])


def _one_run(cfg, stt, speech_s: float) -> tuple[float, float, float]:
    """跑一遍，返回（端点白等的毫秒，产出的音频毫秒，判定花的 CPU 毫秒）。

    前两个是**音频长度**（算法延迟，与机器无关）；第三个是墙钟——它才是解码的代价。
    分开报是故意的：算法延迟要靠改架构降，CPU 延迟靠改模型/窗口降，两者别混。
    """
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=_Pass(), on_event=lambda *a: None)
    stream = np.concatenate([_silence(400), _voice(speech_s), _silence(2000)])
    started = time.perf_counter()
    produced_ms = 0.0
    for utterance in pipeline.run(_source(stream)):
        if utterance.accepted:
            produced_ms = utterance.audio_ms
        break
    cpu_ms = (time.perf_counter() - started) * 1000.0
    # 端点延迟 = 从「最后一个语音块」到「交出这句话」之间被读入的音频长度。
    waited_ms = max(0.0, produced_ms - speech_s * 1000.0 - 300.0)   # 减去 prefix padding
    return waited_ms, produced_ms, cpu_ms


def _source(stream: np.ndarray):
    for start in range(0, stream.size, CHUNK):
        yield stream[start:start + CHUNK]


def cmd_endpoint(args: argparse.Namespace) -> int:
    cfg = load()
    stt = _make_stt(args.real, cfg)
    rows = []
    for seconds in (0.8, 1.2, 2.0, 4.0, 8.0):
        _one_run(cfg, stt, seconds)                     # 预热：首次解码要加载内核
        rows.append((seconds, *_one_run(cfg, stt, seconds)))

    print(f"{'语音长度':>8} {'端点白等':>10} {'判定耗时':>10} {'产出音频':>10}")
    for seconds, waited, produced, cpu in rows:
        print(f"{seconds:>7.1f}s {waited:>9.0f}ms {cpu:>9.0f}ms {produced:>9.0f}ms")

    waits = [row[1] for row in rows]
    cpus = [row[3] for row in rows]
    print(f"\n端点白等：p50 {statistics.median(waits):.0f}ms  "
          f"max {max(waits):.0f}ms（音频长度，与机器无关）")
    print(f"判定耗时：p50 {statistics.median(cpus):.0f}ms  "
          f"max {max(cpus):.0f}ms（墙钟，含解码）")
    print(f"（配置：静音 {cfg.get('audio.silence_duration_ms')}ms，"
          f"延长 {cfg.get('audio.endpoint_extend_ms')}ms，"
          f"前缀 {cfg.get('audio.prefix_padding_ms')}ms，"
          f"复查窗口 {cfg.get('audio.partial_window_ms')}ms）")
    return 0


def _say_voice(text: str, voice: str = "Tingting") -> np.ndarray:
    """用 macOS 自带的中文语音生成一段**真的在说话**的音频。

    为什么不用合成谐波：谐波在 32ms 时模型就能解出「字」来，于是首字延迟量出来
    是 52ms——数字好看，但它是假的。真实人声要等韵母出来才有字。合成谐波测
    分段逻辑可以，测识别质量不行，会让人误以为达标了。
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "a.aiff", Path(tmp) / "a.wav"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True,
                       capture_output=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        str(aiff), str(wav)], check=True, capture_output=True)
        return audio.wav_to_array(wav)


# 三条不同长度的话：短指令是常态，长句才是判停和字幕的真正压力。
CASES: tuple[tuple[str, str], ...] = (
    ("短指令", "打开文件"),
    ("中句", "帮我把磁盘清一下"),
    ("长句", "帮我看一下昨天写的开发日志，然后总结成三条要点发给我"),
)


def _measure_one(cfg, stt, voice: np.ndarray) -> tuple[float, int, float, str]:
    """跑一句话，返回（首字延迟 ms，字幕条数，单次解码 p50 ms，终稿）。

    首字延迟看的是**出第一个字时 VAD 已经确认了多少人声**，不是墙钟：墙钟会把
    机器快慢混进来，而「人声开始 → 出现字」是算法决定的，换台机器应该一样。
    """
    holder: list[audio.AudioPipeline] = []
    marks: list[float] = []
    costs: list[float] = []

    def on_partial(_text: str) -> None:
        marks.append(holder[0]._ep.speech_ms)     # noqa: SLF001 - 基准脚本就该看内部

    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=_Pass(),
                                   on_event=lambda *a: None, on_partial=on_partial)
    holder.append(pipeline)
    original = pipeline.streamer.text

    def timed_text() -> str:
        started = time.perf_counter()
        try:
            return original()
        finally:
            costs.append((time.perf_counter() - started) * 1000.0)

    pipeline.streamer.text = timed_text           # type: ignore[union-attr]

    stream = np.concatenate([_silence(400), voice, _silence(2000)])
    final = ""
    for utterance in pipeline.run(_source(stream)):
        if utterance.accepted:
            final = utterance.text
        break
    return (marks[0] if marks else float("nan"), len(marks),
            statistics.median(costs) if costs else 0.0, final)


def cmd_subtitle(args: argparse.Namespace) -> int:
    """实时字幕：**首字延迟**多少、每句解几次、终稿认不认得对。

    解码次数一起报，因为字幕的全部代价就在这里：每 400ms 一次，每次都要真解。
    终稿文本也打出来，因为「有字幕」和「字幕是对的」是两件事。
    """
    cfg = load()
    stt = _make_stt(args.real, cfg)
    rows = []
    for name, text in CASES:
        voice = _say_voice(text) if args.say else _voice(1.5)
        first, count, cost, final = _measure_one(cfg, stt, voice)
        rows.append((name, text, voice.size / SR * 1000.0, first, count, cost, final))

    print(f"{'场景':<6} {'人声':>6} {'首字':>6} {'条数':>4} {'单次解码':>8}  原文 / 终稿")
    for name, text, voice_ms, first, count, cost, final in rows:
        flag = "" if (not final or text in final or final in text) else "  ← 不一致"
        print(f"{name:<6} {voice_ms:>5.0f}ms {first:>5.0f}ms {count:>4d} {cost:>7.0f}ms  "
              f"{text}\n{'':<26}{final}{flag}")
    firsts = [row[3] for row in rows if row[3] == row[3]]
    if firsts:
        print(f"\n首字延迟：p50 {statistics.median(firsts):.0f}ms  max {max(firsts):.0f}ms"
              f"（门槛 300ms）")
    print(f"（配置：首字 {cfg.get('audio.first_partial_ms')}ms 起，"
          f"之后每 {cfg.get('audio.subtitle_interval_ms')}ms 一次，"
          f"窗口 {cfg.get('audio.subtitle_window_ms')}ms，"
          f"前缀 {cfg.get('audio.prefix_padding_ms')}ms）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="语音链路测量")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("endpoint", help="端点延迟（最后一个语音块 → 交出整句）")
    one.add_argument("--real", action="store_true", help="用真 SenseVoice（含解码耗时）")
    two = sub.add_parser("subtitle", help="实时字幕（人声开始 → 第一个字）")
    two.add_argument("--real", action="store_true", help="用真 SenseVoice（含解码耗时）")
    two.add_argument("--say", action="store_true", help="用 macOS 中文语音当语料（推荐）")
    args = parser.parse_args()
    return {"endpoint": cmd_endpoint, "subtitle": cmd_subtitle}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
