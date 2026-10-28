#!/usr/bin/env python3
"""性能标定：把 `docs/PERFORMANCE.md` 里的每个数字都变成一条命令。

    python scripts/bench.py boot     # 启动分段（进程内，不起浏览器）
    python scripts/bench.py mem      # 常驻内存分阶段
    python scripts/bench.py audio    # VAD 每次推理 / 一句话出字
    python scripts/bench.py all

为什么不写在文档里让人手抄：性能数字会随版本漂，抄一次就过期一次。
为什么不测「空闲 CPU 占几个点」：macOS 上要看这个得读 `ps`/`top`，
在受限环境里会被拦；改用「VAD 单次推理 ÷ 音频块时长」，口径更硬且不依赖权限。
"""

from __future__ import annotations

import resource
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

T0 = time.perf_counter()


def mark(label: str, start: float | None = None) -> None:
    """打印「距进程开始」和「距上一步」两个数：只看累计值分不清哪一步慢。"""
    now = time.perf_counter()
    total = (now - T0) * 1000
    step = (now - start) * 1000 if start is not None else total
    print(f"{total:8.0f} ms  (+{step:6.0f} ms)  {label}")


def rss() -> str:
    return f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024:.0f} MB"


# ------------------------------------------------------------------ 启动分段

def boot() -> None:
    from voxos import audio, main
    from voxos.config import load

    cfg = _free_port(load())
    start = time.perf_counter()
    runtime = main.Runtime(cfg, main._bus(cfg))
    mark("Runtime（读配置 + 状态总线）", start)

    start = time.perf_counter()
    lock = main._lock(cfg)
    mark(f"声纹后端（{len(lock.profiles)} 份档案）", start)

    start = time.perf_counter()
    url = main._serve(runtime, lock, __import__("threading").Event())
    mark(f"HTTP 服务可连 {url}", start)

    start = time.perf_counter()
    audio.AudioPipeline(cfg, audio.make_stt(cfg), lock, vad=audio.make_vad(cfg))
    mark("音频管道（VAD + STT 都建好）", start)
    # 不 stop：HTTP 服务是守护线程，进程退出即回收。这里量的是「建起来要多久」。


def _free_port(cfg):
    """8765 上很可能已经有个真的 VoxOS 在跑，撞端口会让人以为「启动失败」。"""
    return cfg.with_overrides(**{"ui.port": 8799})


# ------------------------------------------------------------------ 常驻内存

def mem() -> None:
    import numpy  # noqa: F401
    mark("import numpy")
    from voxos import audio, dsp
    from voxos.config import load

    mark("import voxos 全套")
    cfg = load()
    for label, build in (("声纹后端", audio.make_embedder), ("VAD", audio.make_vad),
                         ("STT", audio.make_stt)):
        start = time.perf_counter()
        made = build(cfg)
        name = made[-1] if isinstance(made, tuple) else type(made).__name__
        mark(f"{label} {name}  → RSS {rss()}", start)
    start = time.perf_counter()
    dsp.SpeakerLock(embedder=lambda x: x).load(
        cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    mark(f"声纹档案  → RSS {rss()}", start)


# ------------------------------------------------------------------ 音频成本

def audio_cost() -> None:
    import numpy as np

    from voxos import audio
    from voxos.config import load

    cfg = load()
    vad = audio.make_vad(cfg)
    block = _loud_block()
    for _ in range(10):                    # 预热：第一次推理要建 onnx 图
        vad.is_speech(block)
    start = time.perf_counter()
    runs = 300
    for _ in range(runs):
        vad.is_speech(block)
    per = (time.perf_counter() - start) / runs * 1000
    print(f"VAD 每次推理 {per:.2f} ms  （音频块 32ms → 常驻 CPU ≈ {per / 32 * 100:.1f}% 单核）")

    stt = audio.make_stt(cfg)
    samples, seconds = _utterance()
    start = time.perf_counter()
    text = stt.transcribe(samples, 16000)
    took = (time.perf_counter() - start) * 1000
    print(f"识别 {seconds:.1f}s 音频 {took:.0f} ms  →「{text}」（{type(stt).__name__}）")


def _loud_block():
    """静音块会走能量前置判断直接返回，量出来是 0.00ms——必须取最响的那 32ms。"""
    import numpy as np

    loud = _utterance()[0]
    best = max(range(0, loud.size - 512, 512),
               key=lambda i: float(np.abs(loud[i:i + 512]).mean()))
    return loud[best:best + 512].copy()


def _utterance():
    import numpy as np

    # 优先中文语料：拿英文句子量识别耗时会让人误判（中英混合的 token 数不一样）
    found = sorted((ROOT / ".voxos").rglob("*.wav"))
    found.sort(key=lambda path: ("meijia" not in path.name, "bilingual" in str(path), str(path)))
    if not found:
        raise SystemExit("没有可用的测试音频，先跑 `voxos demo --refresh`")
    wav = found[0]
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        seconds = handle.getnframes() / handle.getframerate()
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, seconds


# ------------------------------------------------------------------ 入口

def main(argv: list[str]) -> int:
    which = (argv[0] if argv else "all").lower()
    jobs = {"boot": [boot], "mem": [mem], "audio": [audio_cost]}
    jobs["all"] = jobs["boot"] + jobs["mem"] + jobs["audio"]
    if which not in jobs:
        print(f"不认识的项：{which}（可用：{' / '.join(jobs)}）")
        return 2
    if which == "boot":
        mark("python 起来 → import 完成")
    for job in jobs[which]:
        job()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
