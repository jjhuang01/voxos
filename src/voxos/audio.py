"""音频 I/O 与管道编排：设备源、自愈重连、VAD/声纹/STT 适配、端点切句。

dsp.py 负责纯计算，本模块负责副作用（设备、文件、网络）。
四个物理世界陷阱的落点：
- 自愈重连 → `SelfHealingSource`
- 回声自激 → `EchoGate`（dsp）+ 管道内门控
- 半截话误触发 → `EndpointDetector.should_end`（dsp）+ 部分转写
- 并发写坏 → `core.Executor` 单飞锁
"""

from __future__ import annotations

import os
import re
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

import numpy as np

from . import dsp, stream, turn
from .config import Config

__all__ = [
    "wav_to_array", "ArraySource", "MicSource", "SelfHealingSource", "concat_with_silence",
    "input_devices", "resolve_device", "probe_mic",
    "ScriptedStt", "OpenAiStt", "LocalWhisperStt", "make_stt", "refresh_hotwords",
    "SileroVad", "make_vad", "SherpaEmbedder", "SpectralEmbedder", "make_embedder",
    "Utterance", "AudioPipeline",
]


# ------------------------------------------------------------------ 音频源

def wav_to_array(path: str | Path) -> np.ndarray:
    """读 16-bit PCM WAV 为 float32 [-1,1]；非 16k 单声道会报错提示先转换。"""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError(f"{path} 需为 16-bit 单声道 WAV（可用 afconvert 转换）")
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != dsp.SR:
        raise ValueError(f"{path} 采样率 {rate} != {dsp.SR}")
    return data


class ArraySource:
    """把内存里的波形按 chunk 切片，尾部补静音（保证端点能被触发）。"""

    def __init__(self, samples: np.ndarray, chunk: int = 512, tail_silence_ms: float = 1500) -> None:
        tail = np.zeros(int(dsp.SR * tail_silence_ms / 1000), dtype=np.float32)
        self._data = np.concatenate([dsp.as_float(samples), tail])
        self._chunk = chunk

    def __iter__(self) -> Iterator[np.ndarray]:
        for start in range(0, self._data.size, self._chunk):
            yield self._data[start:start + self._chunk]

    def close(self) -> None:
        return None


def input_devices() -> list[tuple[int, str, float]]:
    """(索引, 名字, 默认采样率)。查不到就返回空列表——doctor 要能说「没有设备」而不是崩。"""
    try:
        import sounddevice as sd  # noqa: PLC0415
        return [(index, str(info["name"]), float(info["default_samplerate"]))
                for index, info in enumerate(sd.query_devices())
                if info["max_input_channels"] > 0]
    except Exception:  # 没装 PortAudio / 无设备 / 被沙箱拦住
        return []


def _device_list() -> str:
    return "、".join(f"[{index}] {name}" for index, name, _ in input_devices()) or "（一个都没有）"


def resolve_device(spec: object) -> int | None:
    """把 `audio.input_device` 的写法变成 sounddevice 的索引。空值 = 系统默认。

    为什么需要这个开关：本机的系统默认输入是一个没插东西的音频接口，
    录得到帧、峰值恒为 0.0000——一切"正常"，就是听不见人说话。
    所以设备必须能指定；写错时列出可选设备，而不是悄悄回落到默认值假装没事。
    """
    text = str(spec or "").strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    hits = [(index, name) for index, name, _ in input_devices() if text.lower() in name.lower()]
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        raise RuntimeError(f"audio.input_device = {text!r} 没匹配到输入设备。可选：{_device_list()}")
    detail = "、".join(f"[{index}] {name}" for index, name in hits)
    raise RuntimeError(f"audio.input_device = {text!r} 匹配到多个设备，写详细一点：{detail}")


def _missing_sounddevice(exc: ImportError) -> RuntimeError:
    """缺 sounddevice 时说什么。

    给的命令必须能**直接粘进终端**：pipx 装出来的包用 `pip install` 补，会补到
    系统 Python 里，补完还是 ImportError——用户照着提示做一遍，问题一点没变，
    这比不给提示更糟。所以按安装方式分开说。
    """
    hint = ("pipx inject voxos sounddevice" if "/pipx/venvs/" in sys.prefix
            else "pip install 'voxos[audio]'")
    return RuntimeError(f"需要 sounddevice 才能用麦克风：{hint}")


def probe_mic(seconds: float = 0.5, device: object = None) -> tuple[str, float]:
    """采一小段，返回 (设备名, 峰值)。打不开就抛 RuntimeError。

    只回答一个问题：**现在这个配置能不能听到人声**。doctor 报这一行，
    是为了让「设备选错了」当场可见，而不是等用户说十句话都没反应。
    """
    try:
        import sounddevice as sd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise _missing_sounddevice(exc) from exc
    index = resolve_device(device)
    with sd.InputStream(samplerate=dsp.SR, channels=1, dtype="float32", device=index) as stream:
        name = str(sd.query_devices(stream.device, kind="input")["name"])
        block, _ = stream.read(max(1, int(seconds * dsp.SR)))
    return name, float(np.abs(block).max())


class MicSource:
    """真实麦克风（需 sounddevice）；缺依赖时抛出可读错误而不是静默失败。"""

    def __init__(self, chunk: int = 512, sr: int = dsp.SR, device: object = None) -> None:
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise _missing_sounddevice(exc) from exc
        index = resolve_device(device)
        try:
            self._stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                          blocksize=chunk, device=index)
        except Exception as exc:
            raise RuntimeError(
                f"打不开输入设备（audio.input_device={device!r}）：{exc}。可选：{_device_list()}"
            ) from exc

    def __enter__(self) -> "MicSource":
        self._stream.start()
        return self

    def __iter__(self) -> Iterator[np.ndarray]:
        while True:
            block, overflowed = self._stream.read(self._stream.blocksize)
            if overflowed:
                print("[voxos] 音频溢出（采集跟不上），已丢弃该块")
            yield np.asarray(block, dtype=np.float32).reshape(-1)

    def close(self) -> None:
        # stop 失败也必须继续 close：旧实现一个异常就把释放整个跳过。
        try:
            self._stream.stop()
        except Exception as exc:  # 设备已拔出时 stop 也会抛
            print(f"[voxos] 停止音频流失败（继续关闭）：{exc}")
        try:
            self._stream.close()
        except Exception as exc:
            print(f"[voxos] 关闭音频流失败（忽略）：{exc}")

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SelfHealingSource:
    """自愈重连：蓝牙耳机摘下 / USB 声卡拔出 / 合盖休眠后自动恢复监听。"""

    def __init__(self, factory: Callable[[], object], backoff_s: float = 2.0,
                 on_event: Callable[[str, str, str], None] | None = None, max_retries: int = 0,
                 on_reset: Callable[[], None] | None = None) -> None:
        self._factory = factory
        self._backoff = backoff_s
        self._on_event = on_event or (lambda *_: None)
        self._max_retries = max_retries
        self._on_reset = on_reset or (lambda: None)

    def __iter__(self) -> Iterator[np.ndarray]:
        import time

        failures = 0
        while True:
            source = None
            try:
                source = self._factory()
                # 有些设备源（MicSource）把 start 放在 __enter__ 里。旧实现
                # 只构造、不进入上下文，真实监听路径读到的是「未启动的流」。
                enter = getattr(source, "__enter__", None)
                if callable(enter):
                    entered = enter()
                    if entered is not None and hasattr(entered, "__iter__"):
                        source = entered
                for chunk in source:  # type: ignore[union-attr]
                    failures = 0
                    yield chunk
            except Exception as exc:
                failures += 1
                # 断流前后的音频不能拼成一句话：先让管道丢掉半截缓冲。
                self._on_reset()
                self._on_event("blocked", "音频设备异常", f"{exc}（{failures} 次，{self._backoff:.0f}s 后重连）")
                if self._max_retries and failures >= self._max_retries:
                    raise
                time.sleep(self._backoff)
            finally:
                self._release(source)

    @staticmethod
    def _release(source: object) -> None:
        exit_context = getattr(source, "__exit__", None)
        if callable(exit_context):
            try:
                exit_context(None, None, None)
                return
            except Exception:
                pass
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def concat_with_silence(parts: Iterable[np.ndarray], gap_ms: float = 1200) -> np.ndarray:
    """把多段语音用静音拼接，模拟连续多句话。"""
    gap = np.zeros(int(dsp.SR * gap_ms / 1000), dtype=np.float32)
    pieces: list[np.ndarray] = []
    for part in parts:
        pieces.extend([dsp.as_float(part), gap])
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)


# ------------------------------------------------------------------ VAD

class SileroVad:
    """Silero VAD（ONNX），同时兼容 v4（x/h/c，512 定长）与 v5（input/state/sr）两种导出。

    模型缺失或加载失败时由 `make_vad` 降级到能量 VAD，绝不让管道起不来。
    """

    WINDOW = 512

    def __init__(self, model_path: str | Path, threshold: float = 0.5,
                 floor_dbfs: float = -55.0) -> None:
        import onnxruntime as ort  # noqa: PLC0415

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self._inputs = [item.name for item in self._session.get_inputs()]
        self._outputs = [item.name for item in self._session.get_outputs()]
        self._legacy = "x" in self._inputs          # v4：定长 512 + h/c
        self._threshold = threshold
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._floor = floor_dbfs

    def is_speech(self, chunk: np.ndarray) -> bool:
        audio = dsp.as_float(chunk)
        if dsp.dbfs(audio) < self._floor:
            # 数字静音/极低底噪不进模型：实测 Silero 在纯零上仍有约 4% 的假阳性
            self._pending = np.zeros(0, dtype=np.float32)
            return False
        self._pending = np.concatenate([self._pending, audio])
        speech = False
        while self._pending.size >= self.WINDOW:
            frame, self._pending = self._pending[:self.WINDOW], self._pending[self.WINDOW:]
            speech = self._infer(frame) >= self._threshold or speech
        return speech

    def _infer(self, frame: np.ndarray) -> float:
        if self._legacy:
            outputs = self._session.run(None, {
                "x": frame.reshape(1, -1),
                "h": self._h,
                "c": self._c,
            })
            self._h = np.asarray(outputs[self._outputs.index("new_h")], dtype=np.float32)
            self._c = np.asarray(outputs[self._outputs.index("new_c")], dtype=np.float32)
            return float(np.asarray(outputs[0]).reshape(-1)[0])
        audio = np.concatenate([self._context, frame]).reshape(1, -1).astype(np.float32)
        outputs = self._session.run(None, {
            "input": audio, "state": self._state, "sr": np.array(dsp.SR, dtype=np.int64),
        })
        self._state = np.asarray(outputs[self._outputs.index("stateN")], dtype=np.float32)
        self._context = audio[:, -64:]
        return float(np.asarray(outputs[0]).reshape(-1)[0])

    def reset(self) -> None:
        self._h[:] = 0.0
        self._c[:] = 0.0
        self._state[:] = 0.0
        self._context[:] = 0.0          # v5 的 64 样本上下文也必须清，否则断流后仍带旧音频
        self._pending = np.zeros(0, dtype=np.float32)


def make_vad(cfg: Config) -> object:
    """按配置选择 VAD；任何异常都降级到能量 VAD，绝不让管道起不来。"""
    backend = str(cfg.get("audio.vad_backend", "auto"))
    model_path = cfg.path("audio.silero_model", "")
    if backend in ("auto", "silero") and model_path.exists():
        try:
            return SileroVad(model_path, float(cfg.get("audio.vad_threshold", 0.5)))
        except Exception as exc:
            print(f"[voxos] Silero VAD 加载失败，降级能量 VAD：{exc}")
    return dsp.EnergyVad()


# ------------------------------------------------------------------ 声纹

class SherpaEmbedder:
    """生产路径：sherpa-onnx + CAM++/WeSpeaker，512 维说话人嵌入。"""

    def __init__(self, model_path: str | Path, num_threads: int = 2) -> None:
        import sherpa_onnx  # noqa: PLC0415

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=num_threads, provider="cpu"
        )
        if not config.validate():
            raise ValueError(f"声纹模型无效：{model_path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=dsp.SR, waveform=dsp.as_float(audio))
        stream.input_finished()
        return np.asarray(self._extractor.compute(stream), dtype=np.float32)


class SpectralEmbedder:
    """降级路径：MFCC 统计 + 音高，配合背景模型（UBM）做中心化后取余弦。

    不承诺生产级 FRR/FAR，仅保证没有模型文件时全链路仍可跑通。
    """

    def __init__(self, ubm_path: str | Path | None = None) -> None:
        self._mu: np.ndarray | None = None
        self._sd: np.ndarray | None = None
        if ubm_path and Path(ubm_path).exists():
            data = np.load(str(ubm_path))
            self._mu, self._sd = data["mu"].astype(np.float32), data["sd"].astype(np.float32)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        vector = dsp.spectral_features(audio)
        if self._mu is not None and self._mu.size == vector.size:
            vector = (vector - self._mu) / (self._sd + 1e-6)
        vector = vector - vector.mean()
        return (vector / (np.linalg.norm(vector) + 1e-9)).astype(np.float32)


def make_embedder(cfg: Config) -> tuple[Callable[[np.ndarray], np.ndarray], float, str]:
    """返回 (embedder, threshold, 说明)。优先真实模型，失败则降级并显式告知。"""
    model_path = cfg.path("audio.speaker_model", "")
    threshold = float(cfg.get("audio.voiceprint_threshold", 0.75))
    if model_path and model_path.exists():
        try:
            return SherpaEmbedder(model_path), threshold, "sherpa-cam++"
        except Exception as exc:
            print(f"[voxos] 声纹模型加载失败，降级频谱声纹：{exc}")
    ubm = cfg.root / "config" / "ubm.npz"
    fallback = float(cfg.get("audio.voiceprint_threshold_fallback", 0.45))
    return SpectralEmbedder(ubm), fallback, "spectral-fallback"


# ------------------------------------------------------------------ STT

class SttEngine(Protocol):
    def transcribe(self, audio: np.ndarray, final: bool = False) -> str: ...


class ScriptedStt:
    """测试/演示用：按顺序返回预置转写。`final=True` 才消耗一条，部分转写只窥视。"""

    def __init__(self, transcripts: list[str]) -> None:
        self._queue = list(transcripts)
        self._cursor = 0

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        if not self._queue:
            return ""
        if final:
            text = self._queue[min(self._cursor, len(self._queue) - 1)]
            self._cursor += 1
            return text
        return self._queue[min(self._cursor, len(self._queue) - 1)]

    def pending(self) -> int:
        return max(0, len(self._queue) - self._cursor)


class OpenAiStt:
    """云端 Whisper 兼容接口；热词通过 prompt 注入。"""

    remote = True                                  # 音频会离开本机：必须先过声纹

    def __init__(self, base_url: str, api_key: str, model: str = "whisper-1",
                 hotwords: list[str] | None = None, timeout: float = 30.0) -> None:
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._key, self._model, self._timeout = api_key, model, timeout
        self.hotwords = hotwords or []

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        import io
        import json
        import urllib.request

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(dsp.SR)
            handle.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        boundary = "----voxos"
        prompt = "以下是可能的专有名词：" + "、".join(self.hotwords[:40]) if self.hotwords else ""
        fields = {"model": self._model, "language": "zh", "prompt": prompt}
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, v in fields.items()
        )
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n").encode() + buffer.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Authorization": f"Bearer {self._key}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return str(json.loads(response.read().decode()).get("text", "")).strip()


class LocalWhisperStt:
    """本地 faster-whisper（可选依赖）。"""

    partial_equals_final = True      # 忽略 final 参数，每次都是整段重解

    def __init__(self, model: str = "small", hotwords: list[str] | None = None) -> None:
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("需要 faster-whisper：pip install 'voxos[local-stt]'") from exc
        self._model = WhisperModel(model, device="cpu", compute_type="int8")
        self.hotwords = hotwords or []

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        segments, _ = self._model.transcribe(dsp.as_float(audio), language="zh", beam_size=1,
                                             initial_prompt="、".join(self.hotwords[:40]) or None)
        return "".join(segment.text for segment in segments).strip()


class SenseVoiceStt:
    """本地 SenseVoice（sherpa-onnx）：中英混说 + 代码术语，int8 在 CPU 上快于实时。

    离线模型，`final` 参数被忽略——每次调用都是对整段音频重新解码，而且耗时随
    音频长度线性涨（本机实测：0.5s→22ms，2s→68ms，6s→138ms，12s→283ms）。
    所以管道做了两件事：判「说完没」只解结尾那一小段（`partial_window_ms`），
    终稿直接复用候选态那次解码（`partial_equals_final`）。换来的是完全不出网。
    """

    _TAG = re.compile(r"<\|[^|]*\|>")          # <|zh|><|NEUTRAL|><|Speech|><|withitn|>

    # 离线引擎：`final` 参数被忽略，解一次就是整段。所以候选态那次解码的结果
    # 和终稿完全一致，没必要再解一遍——实测 6 秒音频能省 138ms，12 秒省 283ms。
    partial_equals_final = True

    def __init__(self, model_dir: str | Path, num_threads: int = 2,
                 language: str = "auto") -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("需要 sherpa-onnx：pip install 'voxos[models]'") from exc
        root = Path(os.path.expanduser(str(model_dir)))
        model, tokens = root / "model.int8.onnx", root / "tokens.txt"
        if not model.exists() or not tokens.exists():
            raise FileNotFoundError(f"{root} 里缺 model.int8.onnx 或 tokens.txt")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=num_threads,
            language=language, use_itn=True,
        )

    def transcribe(self, audio: np.ndarray, final: bool = False) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(dsp.SR, dsp.as_float(audio))
        self._recognizer.decode_stream(stream)
        return self._TAG.sub("", stream.result.text).strip()


def make_stt(cfg: Config, hotwords: list[str] | None = None) -> SttEngine:
    backend = str(cfg.get("stt.backend", "scripted"))
    if backend == "openai":
        return OpenAiStt(str(cfg.get("stt.api_base", "")), str(cfg.get("stt.api_key", "")),
                         str(cfg.get("stt.model", "whisper-1")), hotwords)
    if backend == "local_whisper":
        return LocalWhisperStt(str(cfg.get("stt.model", "small")), hotwords)
    if backend == "sense_voice":
        # 必须走 cfg.path：model_dir 是相对路径，相对的是数据根，不是当前工作目录。
        # 写成 cfg.get 会让「在 /tmp 下跑」和「在仓库根跑」解析出两个不同的目录。
        model_dir = cfg.path("stt.model_dir", ".voxos/models/sense-voice")
        # 语种必须传下去：默认 auto 时短句「嗯」会被认成日文「う」，附和判定和识别一起错
        return SenseVoiceStt(str(model_dir), int(cfg.get("stt.num_threads", 2)),
                             language=str(cfg.get("stt.language", "auto")))
    return ScriptedStt([])


# ------------------------------------------------------------------ 热词

_STOP = {"__init__", "index", "main", "test", "tests", "utils", "src", "dist", "build"}

# 只有这两个后端吃热词：OpenAI 兼容接口走 prompt，faster-whisper 走 initial_prompt。
# SenseVoice 在 sherpa-onnx 里没有热词偏置接口——扫一堆文件名却喂不进去，
# 不如不扫（`refresh_hotwords` 要 rglob 几千个文件）。
HOTWORD_BACKENDS = ("openai", "local_whisper")


def supports_hotwords(cfg: Config) -> bool:
    return str(cfg.get("stt.backend", "scripted")) in HOTWORD_BACKENDS


def refresh_hotwords(cfg: Config, workspace: str | Path | None = None, limit: int | None = None) -> list[str]:
    """扫描工作区文件名/依赖名生成热词表。只对 `HOTWORD_BACKENDS` 里的后端有意义。"""
    root = Path(os.path.expanduser(str(workspace))) if workspace else cfg.path("workspace.default_code_dir", "~")
    cap = int(limit or cfg.get("audio.hotwords_max", 2000))
    words: list[str] = []
    custom = cfg.res_path("audio.hotwords_path", "config/hotwords.txt")
    if custom.exists():
        words += [line.strip() for line in custom.read_text(encoding="utf-8").splitlines() if line.strip()]
    if root.exists():
        for path in sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:3000]:
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".js", ".md", ".toml", ".json", ".go", ".rs"}:
                stem = path.stem
                if stem.lower() not in _STOP and len(stem) > 2:
                    words.append(stem)
            if len(words) >= cap:
                break
    unique = list(dict.fromkeys(words))[:cap]
    target = cfg.path("audio.hotwords_generated", "config/hotwords.txt.generated")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(unique), encoding="utf-8")
    return unique


# ------------------------------------------------------------------ 管道

@dataclass
class Utterance:
    text: str
    score: float
    audio_ms: float
    accepted: bool
    reason: str = ""
    at: float = 0.0        # 这句话收尾的时刻（monotonic）。0 表示不知道，不参与过期判断
    during_tts: bool = False


@dataclass
class AudioPipeline:
    """把「设备流 → 滤波 → VAD → 端点 → 声纹 → STT」串成一条可测试的流水线。"""

    cfg: Config
    stt: SttEngine
    lock: dsp.SpeakerLock
    vad: object = field(default_factory=dsp.EnergyVad)
    on_event: Callable[[str, str, str], None] = lambda state, label, detail: None
    on_partial: Callable[[str], None] | None = None
    on_barge_in: Callable[[], None] | None = None
    echo: dsp.EchoGate = field(default_factory=dsp.EchoGate)
    partial_interval_ms: float = 400.0
    turn_detector: Any = None        # 显式注入（测试用）；None 时按配置造
    stats: dict[str, int] = field(default_factory=lambda: {"chunks": 0, "utterances": 0, "rejected": 0})

    def __post_init__(self) -> None:
        self._ep = dsp.EndpointDetector(
            silence_ms=float(self.cfg.get("audio.silence_duration_ms", 500)),
            extended_ms=float(self.cfg.get("audio.endpoint_extend_ms", 2500)),
            max_ms=float(self.cfg.get("audio.max_utterance_ms", 12000)),
            min_speech_ms=float(self.cfg.get("audio.min_speech_ms", 200)),
        )
        # 必须 reset 一次：`deadline_ms` 是 dataclass 默认值，只有 `reset()` 才把它
        # 设成上面那个 `silence_ms`。不调的话，**第一句话**用的是 800 而不是配置值——
        # 这个 bug 靠读代码看不出来，是靠 `scripts/voice_bench.py` 量出来的。
        self._ep.reset()
        # 判停器：有 smart-turn 模型就用音频判停，没有就退回文本判停。
        # 这一步**不加载模型**（`turn.make` 里才建 session），所以构造管道仍然是毫秒级。
        self.smart_turn = self.turn_detector if self.turn_detector is not None else turn.make(self.cfg)
        self.turn_threshold = turn.threshold(self.cfg)
        # 三态下界：低于它延长等待，中间地带才交给文本判据。
        self.turn_veto = float(self.cfg.get("audio.turn_veto", 0.2))
        self.mask_mode = self.cfg.flag("audio.mask_mode", False)
        self.prefix_ms = float(self.cfg.get("audio.prefix_padding_ms", 300))
        self.window_ms = float(self.cfg.get("audio.partial_window_ms", 3000))
        self._prefix: list[np.ndarray] = []
        self._prefix_ms = 0.0
        # 实时字幕：`stream.make_streamer` 自己决定要不要（远端引擎一律不给，
        # 因为字幕必须在声纹校验之前出字，等于把没验身份的音频提前传上云）。
        self.streamer = stream.make_streamer(self.cfg, self.stt)
        self.subtitle_interval_ms = float(self.cfg.get("audio.subtitle_interval_ms", 400))
        self.first_partial_ms = float(self.cfg.get("audio.first_partial_ms", 250))
        self._text = ""            # 本句当前 partial（字幕 + 文本判停共用这一次解码）
        self._text_ms = 0.0        # 上次解码时的句内时长
        self._buf_ms = 0.0         # 本句已攒多长
        self._shown = ""           # 上次推给界面的字幕，重复的不推
        self._barge: list[np.ndarray] = []   # TTS 期间攒下的「可能是插话」的音频
        self._barge_ms = 0.0
        self._bc_seen = (-1.0, False)            # 上次判附和时攒到多长、结论是什么
        self._during_tts = False
        # 声纹不是分离器：重叠音频仍含回声，不能作为可执行指令回灌。
        self._await_restart = False
        self._restart_silence_ms = 0.0
        self._restart_wait_ms = 0.0
        self._raw_level_dbfs = -120.0

    def reset(self) -> None:
        """设备断流后清掉句级状态：旧缓冲、端点状态和 VAD 上下文都不能跨断流。"""
        self._ep.reset()
        self._prefix, self._prefix_ms = [], 0.0
        self._barge, self._barge_ms = [], 0.0
        self._during_tts = False
        self._await_restart, self._restart_silence_ms = False, 0.0
        self._restart_wait_ms = 0.0
        self._clear_sentence()
        reset = getattr(self.vad, "reset", None)
        if callable(reset):
            reset()

    def _clear_sentence(self) -> None:
        """一句话结束时把句级状态全部归零。少清一个，下一句就会带着上一句的尾巴。"""
        self._buf_ms = 0.0
        self._text, self._text_ms, self._shown = "", 0.0, ""
        if self.streamer is not None:
            self.streamer.reset()

    def _keep_prefix(self, chunk: np.ndarray) -> None:
        """攒「说话之前」那一小段音频，最多 `prefix_padding_ms`。

        VAD 是逐块判的：判出「这块有语音」时，人声的第一个音素往往已经在**上一块**
        里了。直接从第一块「有语音」的音频开始攒，开头几个字就被削掉——中文里
        最常被削的正是「嗯」「把」「那」这类轻声起头。OpenAI 的 `server_vad` 有
        `prefix_padding_ms`（示例值 300ms）就是为这件事，这里照抄。
        """
        if self.prefix_ms <= 0:
            return
        self._prefix.append(chunk)
        self._prefix_ms += chunk.size / dsp.SR * 1000.0
        while len(self._prefix) > 1 and self._prefix_ms - \
                self._prefix[0].size / dsp.SR * 1000.0 >= self.prefix_ms:
            head = self._prefix.pop(0)
            self._prefix_ms -= head.size / dsp.SR * 1000.0

    def run(self, source: Iterable[np.ndarray]) -> Iterator[Utterance]:
        buffer: list[np.ndarray] = []
        last_partial_ms = 0.0
        started = False
        self._prefix, self._prefix_ms = [], 0.0
        self._clear_sentence()
        for raw in source:
            chunk = self._preprocess(raw)
            for c in self._gate_playback(chunk):
                state = self._ep.push(bool(self.vad.is_speech(c)), c.size)  # type: ignore[attr-defined]
                if state == "idle":
                    self._keep_prefix(c)
                    continue
                if not started:
                    self._start_sentence(buffer)
                    started = True
                buffer.append(c)
                self._absorb(c)
                if state not in ("candidate", "force_end"):
                    continue
                result = self._maybe_finalize(buffer, state, last_partial_ms)
                if result is None:                      # 半截话：继续收音，不清缓冲
                    last_partial_ms = sum(item.size for item in buffer) / dsp.SR * 1000.0
                    continue
                # 先清缓冲再 yield：消费者暂停期间不该继续持有整段 PCM。
                buffer.clear()
                self._ep.reset()
                last_partial_ms = 0.0
                started = False
                yield result
        if buffer:
            yield self._finalize(buffer)             # 收尾必产一段，缓冲随之清空
            self._clear_sentence()

    # -- 内部 -------------------------------------------------------------
    def _absorb(self, chunk: np.ndarray) -> None:
        """收一块音频：累计句内时长、喂给流式识别器、到点出字幕。

        **每块都要喂**。真流式识别器漏一块是「丢音频」，不是「少一个字」——
        它的内部状态是靠逐块推进的，跳过一块等于把那 32ms 从句子里剪掉。
        """
        self._buf_ms += chunk.size / dsp.SR * 1000.0
        if self.streamer is not None:
            self.streamer.push(chunk)
            self._maybe_subtitle()

    def _start_sentence(self, buffer: list[np.ndarray]) -> None:
        """只补未与播放重叠的起手音频，不把原始混音拼入指令。"""
        buffer.extend(self._prefix)
        self._begin_sentence()
        self._prefix, self._prefix_ms = [], 0.0

    def _begin_sentence(self) -> None:
        """清句级状态，同时把起手缓冲送入流式识别器，防止字幕丢首字。"""
        self._clear_sentence()
        if self.streamer is not None:
            for item in self._prefix:
                self.streamer.push(item)
                self._buf_ms += item.size / dsp.SR * 1000.0

    def _ensure_text(self) -> str:
        """保证本句 partial 是最新的。**字幕和文本判停共用这一次解码**。

        同一个句内时长只解一次：`_text_ms` 记着上次解码时攒了多长，没新音频就
        直接返回缓存。否则字幕一次、判停一次，一句话解两遍，白烧一倍 CPU。
        """
        if self.streamer is None:
            return ""
        if self._text and self._text_ms >= self._buf_ms:
            return self._text
        self._text = self.streamer.text()
        self._text_ms = self._buf_ms
        return self._text

    def _maybe_subtitle(self) -> None:
        """到点就把当前 partial 推给界面。首字比后续更快——见 `first_partial_ms`。

        首字单独一个阈值（默认 250ms）是因为「开口之后多久看见字」和「后面的字
        滚得多快」是两件事：前者决定用户觉不觉得它听见了，后者只是可读性。
        都用 400ms 的话，最坏情况要 470ms 才出第一个字。
        """
        if self.streamer is None or self.on_partial is None:
            return
        gap = self.subtitle_interval_ms if self._shown else self.first_partial_ms
        if self._buf_ms - self._text_ms < gap:
            return
        text = self._ensure_text()
        if text and text != self._shown:
            self._shown = text
            self.on_partial(text)

    def _verify(self, audio: np.ndarray) -> tuple[bool, float, float]:
        """声纹验证，返回 (是否本人, 最高相似度, 像本人的窗口占比)。

        **分段而不是整段**：整段一个嵌入会把「我说 + 别人插一句」平均成一个谁也
        不像的音色，结果是本人被拒、指令丢掉。判据换成「多数窗口像不像」。

        `SpeakerLock` 没实现 `verify_segments` 时（测试替身、将来的别的后端）
        退回整段验证，占比按「通过=1.0 / 拒绝=0.0」处理，语义保持一致。
        """
        segmented = getattr(self.lock, "verify_segments", None)
        if callable(segmented):
            return segmented(audio)
        ok, score = self.lock.verify(audio)
        return ok, score, 1.0 if ok else 0.0

    def _verify_owner(self, audio: np.ndarray, audio_ms: float) -> tuple[Utterance | None, float]:
        """声纹校验。返回 (被拒的那条记录 或 None, 相似度)。

        相似度要一起返回：后面几处丢弃（识别失败、空转写、回声门）都要把它带进
        时间线，否则「为什么没反应」又少一个维度。

        拒绝原因要能区分「不是本人」和「混了别人的话」：后者多半是屋里有人
        插嘴，用户的重试方式完全不同（重说 vs 让别人先说完）。
        """
        ok, score, ratio = self._verify(audio)
        if ok:
            return None, score
        reason = "声纹不匹配，已丢弃"
        if 0.0 < ratio < 0.5:
            reason = f"声纹不匹配（只有 {ratio:.0%} 的片段像本人，可能混进了别人的话）"
        return self._drop(audio_ms, score, reason), score

    def _preprocess(self, chunk: np.ndarray) -> np.ndarray:
        audio = dsp.as_float(chunk)
        self._raw_level_dbfs = dsp.dbfs(audio)
        self.echo.tick(audio.size / dsp.SR * 1000.0)
        if self.mask_mode:
            audio = dsp.agc(dsp.high_shelf(audio), float(self.cfg.get("audio.agc_target_dbfs", -3.0)))
        self.stats["chunks"] += 1
        return audio

    def _gate_playback(self, chunk: np.ndarray) -> list[np.ndarray]:
        """播放重叠段用于打断；打断后直接把已攒音频作为开头继续收音。"""
        if self.echo.muted:
            return self._during_playback(chunk)
        self._barge, self._barge_ms = [], 0.0
        if not self._await_restart:
            return [chunk]
        return self._handle_restart(chunk)

    def _handle_restart(self, chunk: np.ndarray) -> list[np.ndarray]:
        self._restart_wait_ms += chunk.size / dsp.SR * 1000.0
        # 使用增益补偿前的能量；否则面罩 AGC 抬高底噪会让重说等待永不结束。
        if self._raw_level_dbfs > float(self.cfg.get("audio.restart_silence_dbfs", -42.0)):
            self._restart_silence_ms = 0.0
        else:
            self._restart_silence_ms += chunk.size / dsp.SR * 1000.0
        if self._restart_silence_ms >= self._ep.silence_ms:
            self._resume_restart("恢复收音", "静音间隔已到，恢复收音")
        elif self._restart_wait_ms >= self._restart_max_ms():
            # 屋里底噪本来就高于门限时，「等一段安静」永远等不到；无限等待
            # 会让麦克风从此只听不说，比多一段残留回声糟得多。
            self._resume_restart("恢复收音", "未等到安静间隔，已恢复收音；开头可能含残留回声")
        return [np.zeros_like(chunk)]

    def _restart_max_ms(self) -> float:
        """重说等待的上限：至少两秒，正常情况是静音门槛的四倍。"""
        return max(2000.0, 4.0 * self._ep.silence_ms)

    def _resume_restart(self, title: str, detail: str) -> None:
        self._await_restart, self._restart_wait_ms = False, 0.0
        self.on_event("idle", title, detail)

    def _during_playback(self, chunk: np.ndarray) -> list[np.ndarray]:
        """连续越过能量门才做声纹验证；打断确认后释放已攒音频继续收音。"""
        if not self.echo.speaking or dsp.dbfs(chunk) <= self.echo.gate_dbfs:
            self._barge, self._barge_ms, self._bc_seen = [], 0.0, (-1.0, False)
            return [np.zeros_like(chunk)]
        self._barge.append(chunk)
        self._barge_ms += chunk.size / dsp.SR * 1000.0
        if self._barge_ms >= self.echo.interrupt_ms and not self._just_backchannel():
            if self._fire_barge_in():
                released = list(self._barge)
                self._barge, self._barge_ms = [], 0.0
                return released
        return [np.zeros_like(chunk)]

    def _just_backchannel(self) -> bool:
        """播报中说「嗯/对」不能打断：先听清攒下的这段，像附和就再等等。

        250ms 只够判「是不是本人」，不够判「是不是在插话」。附和词或还认不出字时
        继续攒，直到 `backchannel_ms` 仍在说才算插话；真插话的头一个字一认出就打断。
        """
        limit = float(getattr(self.echo, "backchannel_ms", 0.0))
        if self._barge_ms >= limit:
            return False
        at, verdict = self._bc_seen
        if at <= self._barge_ms < at + 120.0:      # 音频线程上每块都解码太重，120ms 听一次
            return verdict
        text = self.stt.transcribe(np.concatenate(self._barge), final=False).strip()
        verdict = dsp.maybe_backchannel(text)
        self._bc_seen = (self._barge_ms, verdict)
        return verdict

    def _fire_barge_in(self) -> bool:
        """声纹确认是本人打断：停播报并标记状态，保留已攒音频送入本句开头。"""
        if not self.echo.speaking:
            return False
        audio = np.concatenate(self._barge) if self._barge else np.zeros(0, dtype=np.float32)
        ok, score = self.lock.verify(audio)
        if not ok:
            self._barge, self._barge_ms = [], 0.0
            return False
        self.echo.interrupt()
        self._during_tts = True
        self._prefix, self._prefix_ms = [], 0.0
        self.on_event("warn", "打断", f"已请求停止播报（声纹 {score:.2f}）")
        if self.on_barge_in is not None:
            self.on_barge_in()
        return True

    def _maybe_finalize(self, buffer: list[np.ndarray], state: str,
                        last_partial_ms: float) -> Utterance | None:
        """候选态判断「说完了没」，答不出就返回 None 继续等；硬上限直接收尾。"""
        if state == "force_end":
            return self._finalize(buffer)          # 到顶了，不必再为判断多解一次
        audio = np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)
        audio_ms = audio.size / dsp.SR * 1000.0
        if audio_ms - last_partial_ms < self.partial_interval_ms:
            return None                            # 还没到复查点，省一次推理
        if self.smart_turn is not None:
            verdict = self._judge_by_audio(audio)
            if verdict == "end":
                return self._finalize(buffer)
            if verdict == "wait":
                rejected, _ = self._verify_owner(audio, audio_ms)
                if rejected is not None:
                    return rejected  # 别人的半句不能占住回合，把本人下一句粘进来。
                if self._ep.extended:              # 宽限也走完了，不再等
                    return self._finalize(buffer)
                self._ep.extend()
                return None
            # "unsure"：模型没把握，交给文本判据
        return self._judge_by_text(buffer, audio, audio_ms)

    def _judge_by_audio(self, audio: np.ndarray) -> str:
        """音频判停：返回 `end` / `wait` / `unsure` 三态之一。

        上游是两态（`prob >= 0.5` 收尾，否则等）。三态的理由是**中文上的置信度
        不均匀**：实测 6 句完整指令里 4 句落在 0.94–0.99，2 句只有 0.27 和 0.42
        （「今天下午三点提醒我开会」「把刚才那段话翻译成英文」）。二态的话这两句
        会走延长等待。该实验是合成音频，不能推断真人错误率或三态收益。

        | 概率 | 结论 | 为什么 |
        | :--- | :--- | :--- |
        | ≥ `turn_threshold` | `end` | 高置信，直接收尾，**不需要识别** |
        | < `turn_veto` | `wait` | 高置信没说完（「我想说的是那个」0.008），给宽限 |
        | 中间 | `unsure` | 模型自己都没把握，别替文本判据做主 |

        注意 `end` 这一支仍然保住了「判停不需要识别」这个架构收益——只是把
        不确定的那部分退回去了。
        """
        prob = self.smart_turn.complete(audio)
        if not np.isfinite(prob) or not 0 <= prob <= 1:
            # 模型坏了（文件截断、onnxruntime 版本不对、输入对不上）。摘掉它，
            # 本轮起退回文本判停——一个坏模型不该把整条采集链一起带下去。
            self.on_event("warn", "判停", "smart-turn 推理失败，本轮起退回文本判停")
            self.smart_turn = None
            return "unsure"
        if prob >= self.turn_threshold:
            return "end"
        return "wait" if prob < self.turn_veto else "unsure"

    def _judge_by_text(self, buffer: list[np.ndarray], audio: np.ndarray,
                       audio_ms: float) -> Utterance | None:
        """文本判停：看结尾是不是半截话。

        只解最后 `partial_window_ms` 是故意的：判「说完没」只看末尾有没有「把」
        「然后」这类词，整段重解是白花。本机实测 SenseVoice 解码耗时随音频线性涨
        （0.5s→22ms，12s→283ms），而这一步只关心最后 3 秒。

        有流式识别器时直接用它的 partial——那份结果已经在出字幕时算过了，
        这里再解一次等于同一段音频解两遍。
        """
        if getattr(self.stt, "remote", False):
            # 远端识别会把音频上传。身份没确认之前，一个字节都不该离开本机。
            ok, score = self.lock.verify(audio)
            if not ok:
                return self._drop(audio_ms, score, "声纹不匹配，已丢弃")
        if self.streamer is not None:
            text = self._ensure_text()
        else:
            tail = audio[-int(dsp.SR * self.window_ms / 1000):] if self.window_ms > 0 else audio
            text = self.stt.transcribe(tail, final=False)
        if not self._ep.should_end(text):
            return None                            # 半截话，继续等（语义尾词等待）
        # 不把 partial 当终稿传下去：窗口解出来的文本不完整（可能缺开头），
        # 终稿由 `_finalize` 自己拿（流式的 `finish` 或整段重解）。
        return self._finalize(buffer)

    def _finalize(self, buffer: list[np.ndarray]) -> Utterance:
        """收尾一段话，**永远返回一个 Utterance**——哪怕是空转写。

        返回 `None` 会被 `run()` 理解成「还没说完，继续攒」，而那只在候选态成立。
        一旦 VAD 判了硬上限或静音收尾，缓冲就必须清掉：留着它，下一块音频会把
        整段重新拼一遍再解码一次，句子越长越慢，最后把管道拖死。
        真机上的触发条件很普通——一声咳嗽、一次 VAD 假阳性，转写出来是空的。

        终稿一律走识别器自己的 `finish`：只有它知道「partial 能不能当 final 用」。
        以前这个判断散在管道里（`partial_equals_final`），现在收敛到识别器内部，
        因为流式识别器的 partial **永远**不能当终稿——它会改字。
        """
        audio = np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)
        audio_ms = audio.size / dsp.SR * 1000.0
        # 用真实人声时长判「太短」，不用缓冲长度：加了 prefix padding 之后，
        # 缓冲一定比人声长 300ms，一声咳嗽就能凑过 200ms 的门槛。
        if self._ep.speech_ms < float(self.cfg.get("audio.min_speech_ms", 200)):
            return self._drop(audio_ms, 0.0, "太短，已丢弃")
        rejected, score = self._verify_owner(audio, audio_ms)
        if rejected is not None:
            return rejected
        try:
            if self.streamer is not None:
                text = self.streamer.finish(audio)
            else:
                text = self.stt.transcribe(audio, final=True)
        except Exception as exc:                   # 单句识别失败不能杀死整条监听
            return self._drop(audio_ms, score, f"识别失败：{exc}")
        if not text.strip():
            return self._drop(audio_ms, score, "空转写，已丢弃")
        if not self.echo.allow_text(text):
            return self._drop(audio_ms, score, "回声门丢弃")
        self.stats["utterances"] += 1
        # detail 放最终文本而不是「1800ms 声纹 0.82」：胶囊会把这一行显示出来，
        # 用户最想看的是「它到底听到的是哪句话」——那是唯一能纠正误识别的反馈。
        self.on_event("listening", "聆听中", text)
        during_tts = self._during_tts
        self._during_tts = False
        return Utterance(text, score, audio_ms, True, at=time.monotonic(), during_tts=during_tts)

    def _drop(self, audio_ms: float, score: float, reason: str) -> Utterance:
        """丢弃一段话。**把已经识别出来的半句带上**。

        以前这里返回的是空字符串，于是「我听到的是『请打开微信』，因为声纹不匹配
        被丢掉了」这条信息在音频层就消失了——上层只能 `continue`，用户看到的
        是胶囊上那句字幕闪一下、然后什么都没有。那是整个系统里最容易让人
        以为「软件坏了」的空白。
        """
        during_tts = self._during_tts
        self._during_tts = False
        self.stats["rejected"] += 1
        return Utterance(self._text, score, audio_ms, False, reason=reason,
                         at=time.monotonic(), during_tts=during_tts)
