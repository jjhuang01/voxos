"""语义判停：smart-turn v3.2 —— 吃音频，不吃文本。

**为什么要有这一层。** 现在的判停是「先解码成文本，再看结尾是不是『然后』『比如』」。
这有两个毛病：

1. **依赖识别**。为了决定「要不要停」，得先花 82ms 解码——而解码本身是最贵的
   那一步。判停和识别被绑在一条链上，识别换成流式的之后这条链还得重写。
2. **看不到语调**。人问「你是说……？」和陈述「你是说。」结尾的静音一样长、
   字面一样，文本判不出来。音频模型能。

smart-turn v3.2（Pipecat/Daily，BSD-2-Clause）是 Whisper Tiny 编码器 + 一个
线性头，8.7MB int8，本机 10–65ms，23 语言含中文。用法照上游：

    VAD 判出静音 → 取这一整轮音频（最多 8 秒，**前面补零**）→ 模型给一个
    「说完了」的概率 → >0.5 收尾，否则继续等。

补零补在前面是有讲究的：让最后一句永远贴着窗口末尾。补在后面等于给模型看
一段尾巴上的静音，它会以为用户说完之后又沉默了 3 秒。

**降级是默认行为，不是异常路径。** 模型没下、onnxruntime 没装、配置里写了
`text`——三种情况都安静地退回文本判停，`doctor` 那一行会说清是哪一种。
"""

from __future__ import annotations

import numpy as np

from . import mel
from .config import Config

MODEL_NAME = "smart-turn-v3.2-cpu.onnx"
DEFAULT_MODEL = f".voxos/models/{MODEL_NAME}"
SAMPLE_RATE = 16000
MAX_SAMPLES = mel.CHUNK_SAMPLES          # 8 秒
DEFAULT_THRESHOLD = 0.5


class SmartTurn:
    """把一段音频判成「说完了 / 没说完」。"""

    audio_native = True

    def __init__(self, model_path: str | object, threads: int = 1) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        # 和上游一样的选项：单流顺序执行，图优化全开。这两项影响的是延迟，
        # 不是精度——但「都抄了」比「大概一样」省事。
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = max(1, threads)
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(str(model_path), sess_options=options)
        inputs = self._session.get_inputs()
        if len(inputs) != 1:
            raise RuntimeError(f"smart-turn 期望一个输入，这个模型有 {len(inputs)} 个")
        self._input = inputs[0].name
        self.errors = 0

    def complete(self, audio: np.ndarray) -> float:
        """「说完了」的概率，0–1。**判不了时返回 -1**，调用方据此退回文本判停。

        不在这里抛异常：这一层跑在音频线程里，一个坏模型不该把整条采集链带下去。
        """
        try:
            return self._run(audio)
        except Exception:
            self.errors += 1
            return -1.0

    def _run(self, audio: np.ndarray) -> float:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return -1.0
        x = x[-MAX_SAMPLES:]                       # 超过 8 秒：只留结尾
        if x.size < MAX_SAMPLES:
            x = np.pad(x, (MAX_SAMPLES - x.size, 0), mode="constant")
        features = mel.compute(x)[None]            # (1, 80, 800)
        out = self._session.run(None, {self._input: features})[0]
        return float(np.asarray(out).reshape(-1)[0])


def available(cfg: Config) -> tuple[bool, str]:
    """能不能用 smart-turn。返回（可用，不可用的原因）。

    只做便宜检查（配置、文件在不在、依赖有没有）。模型能不能真跑起来由
    `make()` 回答——`doctor` 两个都问，才能分清「没下」和「下了但坏了」。
    """
    backend = str(cfg.get("audio.turn_backend", "auto")).strip().lower()
    if backend == "text":
        return False, "配置里指定了文本判停"
    path = cfg.path("audio.turn_model", DEFAULT_MODEL)
    if not path.exists():
        return False, f"缺模型 {path.name}（voxos models 下载）"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "缺 onnxruntime（pipx inject voxos onnxruntime）"
    return True, ""


def make(cfg: Config) -> SmartTurn | None:
    """造一个判停器；造不出来就返回 None（= 用文本判停）。"""
    ok, _ = available(cfg)
    if not ok:
        return None
    try:
        return SmartTurn(cfg.path("audio.turn_model", DEFAULT_MODEL),
                         threads=int(cfg.get("audio.turn_threads", 1)))
    except Exception:
        return None


def threshold(cfg: Config) -> float:
    """「说完了」的门槛。0.5 是上游的取值，先照抄，等量出 TPR/TNR 再动。"""
    try:
        return float(cfg.get("audio.turn_threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


__all__ = ["SmartTurn", "available", "make", "threshold", "MODEL_NAME", "DEFAULT_MODEL"]
