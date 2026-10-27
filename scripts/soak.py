"""长稳采样：把音频管道挂在合成源上跑，定期采样 RSS / CPU / 缓冲。

为什么不是「8 小时真麦克风」：没人愿意让一个后台进程开麦八小时，这台机器也不行。
但「跑久了内存涨不涨」跟麦克风无关，跟管道里的缓冲、VAD 状态、STT 会话有关——
那部分可以在这里真实地跑出来，而且能量化。

覆盖得到：管道缓冲泄漏、VAD 状态机漂移、STT 会话重复创建、静音段误触发。
覆盖不到：设备热插拔、蓝牙切换、系统休眠唤醒、TCC 授权过期。这几项只能真挂机。

用法：
    .venv/bin/python scripts/soak.py --minutes 30
    .venv/bin/python scripts/soak.py --minutes 30 --json .voxos/soak.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import resource
import sys
import threading
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from voxos import audio, corpus, dsp  # noqa: E402
from voxos.config import load  # noqa: E402

_READER: Callable[[], float] | None = None


class _MachInfo(ctypes.Structure):
    _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64), ("user_time", ctypes.c_int32 * 2),
                ("system_time", ctypes.c_int32 * 2), ("policy", ctypes.c_int32),
                ("suspend_count", ctypes.c_int32)]


def _mach_reader() -> Callable[[], float]:
    """macOS：直接问内核要当前常驻内存。

    不用 `ps` 是因为它在沙箱里起不来（operation not permitted），
    也不用 ru_maxrss——那是历史峰值，涨上去就不下来，看不出「稳定住了」。
    """
    lib = ctypes.CDLL(ctypes.util.find_library("System"))
    lib.mach_task_self.restype = ctypes.c_uint32
    task = lib.mach_task_self()
    size = ctypes.sizeof(_MachInfo) // ctypes.sizeof(ctypes.c_uint32)

    def read() -> float:
        info = _MachInfo()
        count = ctypes.c_uint32(size)
        if lib.task_info(task, 20, ctypes.byref(info), ctypes.byref(count)) != 0:
            raise OSError("task_info 失败")
        return round(info.resident_size / 2 ** 20, 1)
    read()
    return read


def _rusage_reader() -> Callable[[], float]:
    def read() -> float:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20, 1)
    return read


def rss_mb() -> float:
    """当前常驻内存（MB）；拿不到就返回 -1，不编数字。"""
    global _READER
    if _READER is None:
        try:
            _READER = _mach_reader()
        except Exception:                                  # noqa: BLE001
            _READER = _rusage_reader()
    try:
        return _READER()
    except Exception:                                      # noqa: BLE001
        return -1.0


def build_source(root: Path, manifest: dict) -> tuple[list[np.ndarray], int]:
    """把语料拼成「一段人声 + 一段静音」的循环单元。"""
    clips = [corpus.load_clip(root, entry) for entry in manifest["owner"]["clips"]]
    speech = audio.concat_with_silence(clips, gap_ms=1200)
    silence = np.zeros(int(dsp.SR * 2.0), dtype=np.float32)
    return [speech, silence], speech.size + silence.size


def pump(pipeline: audio.AudioPipeline, units: list[np.ndarray], chunk: int,
         counter: dict, stop: threading.Event) -> None:
    """在后台线程里按**实时节奏**喂音频，直到 stop。

    不按实时节奏的话一秒能灌进十几分钟音频，CPU 打满、内存曲线也没有参考价值。
    """
    def source():
        clock = time.perf_counter()
        sent = 0
        while not stop.is_set():
            for unit in units:
                for start in range(0, unit.size, chunk):
                    if stop.is_set():
                        return
                    sent += chunk
                    ahead = sent / dsp.SR - (time.perf_counter() - clock)
                    if ahead > 0.25:               # 攒够 250ms 再睡，别每块都睡
                        time.sleep(ahead)
                    yield unit[start:start + chunk]

    try:
        for utterance in pipeline.run(source()):
            counter["utterances"] += 1
            if utterance.accepted:
                counter["accepted"] += 1
    except Exception as exc:                       # noqa: BLE001
        counter["error"] = f"{type(exc).__name__}: {exc}"
        stop.set()


def enrolled_lock(cfg, root: Path, manifest: dict) -> dsp.SpeakerLock:
    """用主人的语料先录入声纹，否则每一段都会被锁拒掉，等于没测到后半条链路。"""
    embedder, threshold, _ = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    clips = [corpus.load_clip(root, entry) for entry in manifest["owner"]["clips"][:2]]
    lock.enroll(clips, mode="normal")
    return lock


def sample_loop(pipeline: audio.AudioPipeline, counter: dict, args,
                started: float, cpu0: float) -> list[dict]:
    """按间隔采样，返回每个采样点。"""
    samples: list[dict] = []
    print(f"{'分钟':>6} {'RSS(MB)':>9} {'CPU%':>7} {'块':>9} {'语音段':>7}")
    while time.time() - started < args.minutes * 60:
        left = args.minutes * 60 - (time.time() - started)
        time.sleep(min(args.interval, max(1.0, left)))
        elapsed = time.time() - started
        record = {
            "minute": round(elapsed / 60, 2),
            "rss_mb": rss_mb(),
            "cpu_pct": round((time.process_time() - cpu0) / elapsed * 100, 1),
            "chunks": pipeline.stats["chunks"],
            "utterances": counter["utterances"],
            "accepted": counter["accepted"],
            "rejected": pipeline.stats["rejected"],
        }
        samples.append(record)
        print(f"{record['minute']:6.1f} {record['rss_mb']:9.1f} {record['cpu_pct']:7.1f} "
              f"{record['chunks']:9d} {record['utterances']:7d}")
    return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=30.0, help="采样间隔（秒）")
    parser.add_argument("--stt", default="", help="覆盖 stt.backend（scripted / sense_voice）")
    parser.add_argument("--json", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load()
    if args.stt:
        cfg = cfg.with_overrides(**{"stt.backend": args.stt})
    root = cfg.root / ".voxos" / "corpus"
    manifest = corpus.ensure_corpus(root)
    units, unit_samples = build_source(root, manifest)
    chunk = int(dsp.SR * float(cfg.get("audio.chunk_ms", 20)) / 1000)

    pipeline = audio.AudioPipeline(
        cfg=cfg, stt=audio.make_stt(cfg), lock=enrolled_lock(cfg, root, manifest),
        vad=audio.make_vad(cfg))
    counter = {"utterances": 0, "accepted": 0}
    stop = threading.Event()
    worker = threading.Thread(target=pump, args=(pipeline, units, chunk, counter, stop),
                              daemon=True)
    worker.start()

    started, cpu0 = time.time(), time.process_time()
    print(f"跑 {args.minutes:.0f} 分钟，每 {args.interval:.0f}s 采一次；"
          f"循环单元 {unit_samples / dsp.SR:.1f}s，块长 {chunk} 点")
    try:
        samples = sample_loop(pipeline, counter, args, started, cpu0)
    finally:
        stop.set()
        worker.join(timeout=15)

    return report(samples, counter, pipeline, args)


def slope(samples: list[dict]) -> float:
    """一段采样里的 RSS 斜率（MB/小时）。"""
    span = samples[-1]["minute"] - samples[0]["minute"]
    if span <= 0:
        return 0.0
    return (samples[-1]["rss_mb"] - samples[0]["rss_mb"]) / span * 60


def report(samples: list[dict], counter: dict, pipeline: audio.AudioPipeline,
           args: argparse.Namespace) -> int:
    if len(samples) < 2:
        print("采样点太少，得不出结论")
        return 2
    first, last = samples[0], samples[-1]
    drift = last["rss_mb"] - first["rss_mb"]
    late = slope(samples[len(samples) // 2:])
    print("-" * 52)
    print(f"时长 {last['minute']:.1f} 分钟｜RSS {first['rss_mb']:.1f} → {last['rss_mb']:.1f} MB"
          f"（{drift:+.1f}）")
    # 全程斜率包含模型加载/分配器预热，不能当泄漏；后半程才是。
    print(f"全程 {slope(samples):+.1f} MB/小时（含预热）｜后半程 {late:+.1f} MB/小时（更像真实泄漏）")
    print(f"语音段 {counter['utterances']}（接受 {counter['accepted']}）｜"
          f"拒绝 {pipeline.stats['rejected']}｜块 {pipeline.stats['chunks']}")
    if counter.get("error"):
        print(f"管道异常：{counter['error']}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"minutes": last["minute"], "drift_mb": drift, "per_hour_mb": round(slope(samples), 1),
             "late_per_hour_mb": round(late, 1),
             "samples": samples, "error": counter.get("error", "")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细写入 {args.json}")
    return 0 if not counter.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
