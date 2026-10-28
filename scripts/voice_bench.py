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


def main() -> int:
    parser = argparse.ArgumentParser(description="语音链路测量")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("endpoint", help="端点延迟（最后一个语音块 → 交出整句）")
    one.add_argument("--real", action="store_true", help="用真 SenseVoice（含解码耗时）")
    args = parser.parse_args()
    return {"endpoint": cmd_endpoint}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
