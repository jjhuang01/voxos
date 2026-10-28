"""长稳采样：把音频管道挂在合成源上跑，定期采样内存 / CPU / 缓冲。

为什么不是「8 小时真麦克风」：没人愿意让一个后台进程开麦八小时，这台机器也不行。
但「跑久了内存涨不涨」跟麦克风无关，跟管道里的缓冲、VAD 状态、STT 会话有关——
那部分可以在这里真实地跑出来，而且能量化。

覆盖得到：管道缓冲泄漏、VAD 状态机漂移、STT 会话重复创建、静音段误触发。
覆盖不到：设备热插拔、蓝牙切换、系统休眠唤醒、TCC 授权过期。这几项只能真挂机。

内存读的是 `phys_footprint` 而不是 RSS——理由见 `_footprint_reader()`。

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
import struct
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

# proc_pid_rusage(RUSAGE_INFO_V4) 的 rusage_info_v4 里 ri_phys_footprint 的偏移：
# uuid[16] + user_time + system_time + pkg_idle_wkups + interrupt_wkups + pageins
# + wired_size + resident_size = 16 + 7*8 = 72
_FOOTPRINT_OFFSET = 72


def _footprint_reader() -> Callable[[], float]:
    """macOS：问内核要 `phys_footprint`，也就是活动监视器里「内存」那一列。

    为什么不用 RSS：模型的 229MB 权重是 mmap 进来的，`resident_size` 数的是
    「此刻有多少页还留在物理内存里」——页被回收就掉、被读到就涨。实测同一个
    进程的 RSS 在 **28MB 和 364MB 之间来回跳**（28MB 比裸 Python 还低），
    而它每分钟干的活一模一样。**拿 RSS 判泄漏，量到的是页缓存命中率。**

    `phys_footprint` 把已压缩页也算进来，是「这个进程到底占了多少」。
    解码 3 次前后它从 555.7 走到 557.2，稳定且单调——这才是能判泄漏的量。

    也不用 `ps`（沙箱里 operation not permitted）和 `ru_maxrss`（历史峰值，
    涨上去就不下来）。缓冲区给 1KB 是因为 `proc_pid_rusage` 没有长度参数，
    内核按自己的结构体写满；按结构体精确开 80 字节会被写穿并段错误。
    """
    lib = ctypes.CDLL(ctypes.util.find_library("System"))
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_pid_rusage.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(1024)
    pid = os.getpid()

    def read() -> float:
        if lib.proc_pid_rusage(pid, 4, buf) != 0:          # 4 = RUSAGE_INFO_V4
            raise OSError("proc_pid_rusage 失败")
        return round(struct.unpack_from("<Q", buf.raw, _FOOTPRINT_OFFSET)[0] / 2 ** 20, 1)
    read()
    return read


def _rusage_reader() -> Callable[[], float]:
    def read() -> float:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20, 1)
    return read


def mem_mb() -> float:
    """当前内存占用（MB）；拿不到就返回 -1，不编数字。"""
    global _READER
    if _READER is None:
        try:
            _READER = _footprint_reader()
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
    print(f"{'分钟':>6} {'内存(MB)':>9} {'CPU%':>7} {'块':>9} {'语音段':>7}")
    while time.time() - started < args.minutes * 60:
        left = args.minutes * 60 - (time.time() - started)
        time.sleep(min(args.interval, max(1.0, left)))
        elapsed = time.time() - started
        record = {
            "minute": round(elapsed / 60, 2),
            "mem_mb": mem_mb(),
            "cpu_pct": round((time.process_time() - cpu0) / elapsed * 100, 1),
            "chunks": pipeline.stats["chunks"],
            "utterances": counter["utterances"],
            "accepted": counter["accepted"],
            "rejected": pipeline.stats["rejected"],
        }
        samples.append(record)
        print(f"{record['minute']:6.1f} {record['mem_mb']:9.1f} {record['cpu_pct']:7.1f} "
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
    root = cfg.data_root / ".voxos" / "corpus"
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
    """一段采样里的内存斜率（MB/小时）。"""
    span = samples[-1]["minute"] - samples[0]["minute"]
    if span <= 0:
        return 0.0
    return (samples[-1]["mem_mb"] - samples[0]["mem_mb"]) / span * 60


def spread(samples: list[dict]) -> float:
    """去掉第一分钟后，内存最高与最低的差（MB）。

    短时长的斜率会被噪声淹没，但「稳定住了没有」可以直接看这个：
    一个在泄漏的进程，跨度只会越拉越大。
    """
    warm = [s["mem_mb"] for s in samples[1:]] or [s["mem_mb"] for s in samples]
    return round(max(warm) - min(warm), 1)


def report(samples: list[dict], counter: dict, pipeline: audio.AudioPipeline,
           args: argparse.Namespace) -> int:
    if len(samples) < 2:
        print("采样点太少，得不出结论")
        return 2
    first, last = samples[0], samples[-1]
    drift = last["mem_mb"] - first["mem_mb"]
    late = slope(samples[len(samples) // 2:])
    print("-" * 52)
    print(f"时长 {last['minute']:.1f} 分钟｜内存 {first['mem_mb']:.1f} → {last['mem_mb']:.1f} MB"
          f"（{drift:+.1f}），预热后跨度 {spread(samples):.1f} MB")
    # 全程斜率包含模型加载/分配器预热，不能当泄漏；后半程才是。
    print(f"全程 {slope(samples):+.1f} MB/小时（含预热）｜后半程 {late:+.1f} MB/小时（更像真实泄漏）")
    print(f"语音段 {counter['utterances']}（接受 {counter['accepted']}）｜"
          f"拒绝 {pipeline.stats['rejected']}｜块 {pipeline.stats['chunks']}")
    if counter.get("error"):
        print(f"管道异常：{counter['error']}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"minutes": last["minute"], "drift_mb": drift, "spread_mb": spread(samples),
             "per_hour_mb": round(slope(samples), 1), "late_per_hour_mb": round(late, 1),
             "samples": samples, "error": counter.get("error", "")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细写入 {args.json}")
    return 0 if not counter.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
