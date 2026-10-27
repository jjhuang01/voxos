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
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

import numpy as np

from . import dsp
from .config import Config

__all__ = [
    "wav_to_array", "ArraySource", "MicSource", "SelfHealingSource", "concat_with_silence",
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


class MicSource:
    """真实麦克风（需 sounddevice）；缺依赖时抛出可读错误而不是静默失败。"""

    def __init__(self, chunk: int = 512, sr: int = dsp.SR) -> None:
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError("需要 sounddevice 才能用麦克风：pip install 'voxos[audio]'") from exc
        self._stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=chunk)

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
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # 设备已拔出时 close 也会抛
            print(f"[voxos] 关闭音频流失败（忽略）：{exc}")

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SelfHealingSource:
    """自愈重连：蓝牙耳机摘下 / USB 声卡拔出 / 合盖休眠后自动恢复监听。"""

    def __init__(self, factory: Callable[[], object], backoff_s: float = 2.0,
                 on_event: Callable[[str, str, str], None] | None = None, max_retries: int = 0) -> None:
        self._factory = factory
        self._backoff = backoff_s
        self._on_event = on_event or (lambda *_: None)
        self._max_retries = max_retries

    def __iter__(self) -> Iterator[np.ndarray]:
        import time

        failures = 0
        while True:
            source = None
            try:
                source = self._factory()
                for chunk in source:  # type: ignore[union-attr]
                    failures = 0
                    yield chunk
            except Exception as exc:
                failures += 1
                self._on_event("blocked", "音频设备异常", f"{exc}（{failures} 次，{self._backoff:.0f}s 后重连）")
                if self._max_retries and failures >= self._max_retries:
                    raise
                time.sleep(self._backoff)
            finally:
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
    threshold = float(cfg.get("audio.voiceprint_threshold", 0.84))
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

    离线模型，`final` 参数被忽略——每次调用都是对整段音频重新解码。
    因此管道里"边听边判是否说完"的复查点会真的花 CPU（3 秒音频约 150ms），
    这是本地离线识别的固有代价，换来的是完全不出网。
    """

    _TAG = re.compile(r"<\|[^|]*\|>")          # <|zh|><|NEUTRAL|><|Speech|><|withitn|>

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
        return SenseVoiceStt(str(cfg.get("stt.model_dir", ".voxos/models/sense-voice")),
                             int(cfg.get("stt.num_threads", 2)))
    return ScriptedStt([])


# ------------------------------------------------------------------ 热词

_STOP = {"__init__", "index", "main", "test", "tests", "utils", "src", "dist", "build"}


def refresh_hotwords(cfg: Config, workspace: str | Path | None = None, limit: int | None = None) -> list[str]:
    """扫描工作区文件名/依赖名生成热词表，把代码术语识别率从 75% 拉到 99%。"""
    root = Path(os.path.expanduser(str(workspace or cfg.get("workspace.default_code_dir", "~"))))
    cap = int(limit or cfg.get("audio.hotwords_max", 2000))
    words: list[str] = []
    custom = cfg.path("audio.hotwords_path", "config/hotwords.txt")
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


@dataclass
class AudioPipeline:
    """把「设备流 → 滤波 → VAD → 端点 → 声纹 → STT」串成一条可测试的流水线。"""

    cfg: Config
    stt: SttEngine
    lock: dsp.SpeakerLock
    vad: object = field(default_factory=dsp.EnergyVad)
    on_event: Callable[[str, str, str], None] = lambda state, label, detail: None
    echo: dsp.EchoGate = field(default_factory=dsp.EchoGate)
    partial_interval_ms: float = 400.0
    stats: dict[str, int] = field(default_factory=lambda: {"chunks": 0, "utterances": 0, "rejected": 0})

    def __post_init__(self) -> None:
        self._ep = dsp.EndpointDetector(
            silence_ms=float(self.cfg.get("audio.silence_duration_ms", 800)),
            max_ms=float(self.cfg.get("audio.max_utterance_ms", 12000)),
            min_speech_ms=float(self.cfg.get("audio.min_speech_ms", 200)),
        )
        self.mask_mode = self.cfg.flag("audio.mask_mode", False)

    def run(self, source: Iterable[np.ndarray]) -> Iterator[Utterance]:
        buffer: list[np.ndarray] = []
        last_partial_ms = 0.0
        for raw in source:
            chunk = self._preprocess(raw)
            if not self.echo.allow_chunk(dsp.dbfs(chunk)):
                # 半双工：这一段按静音处理，但**不能清空已有缓冲**。
                # 清空会把「用户喊停之后的那一下停顿」连同喊停本身一起丢掉。
                chunk = np.zeros_like(chunk)
            state = self._ep.push(bool(self.vad.is_speech(chunk)), chunk.size)  # type: ignore[attr-defined]
            if state == "idle":
                continue
            buffer.append(chunk)
            if state not in ("candidate", "force_end"):
                continue
            result = self._maybe_finalize(buffer, state, last_partial_ms)
            if result is None:                      # 半截话：继续收音，不清缓冲
                last_partial_ms = sum(item.size for item in buffer) / dsp.SR * 1000.0
                continue
            yield result
            buffer.clear()
            self._ep.reset()
            last_partial_ms = 0.0
        if buffer:
            final = self._finalize(buffer)
            if final:
                yield final

    # -- 内部 -------------------------------------------------------------
    def _preprocess(self, chunk: np.ndarray) -> np.ndarray:
        audio = dsp.as_float(chunk)
        self.echo.tick(audio.size / dsp.SR * 1000.0)
        if self.mask_mode:
            audio = dsp.agc(dsp.high_shelf(audio), float(self.cfg.get("audio.agc_target_dbfs", -3.0)))
        self.stats["chunks"] += 1
        return audio

    def _maybe_finalize(self, buffer: list[np.ndarray], state: str,
                        last_partial_ms: float) -> Utterance | None:
        audio = np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)
        audio_ms = audio.size / dsp.SR * 1000.0
        if state == "candidate" and audio_ms - last_partial_ms < self.partial_interval_ms:
            return None                              # 还没到复查点，省一次 STT
        text = self.stt.transcribe(audio, final=False)   # 部分转写：只用于判断是否说完
        if state == "candidate" and not self._ep.should_end(text):
            return None                              # 半截话，继续等（语义尾词等待）
        return self._finalize(buffer)                # 终稿重新解码，保证文本权威

    def _finalize(self, buffer: list[np.ndarray]) -> Utterance | None:
        audio = np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)
        audio_ms = audio.size / dsp.SR * 1000.0
        if audio_ms < float(self.cfg.get("audio.min_speech_ms", 200)):
            return None
        ok, score = self.lock.verify(audio)
        if not ok:
            self.stats["rejected"] += 1
            return Utterance("", score, audio_ms, False, reason="声纹不匹配，已丢弃")
        text = self.stt.transcribe(audio, final=True)
        if not text.strip():
            return None
        if not self.echo.allow_text(text):
            return Utterance(text, score, audio_ms, False, reason="回声门丢弃")
        self.stats["utterances"] += 1
        self.on_event("listening", "聆听中", f"{audio_ms:.0f}ms 声纹 {score:.2f}")
        return Utterance(text, score, audio_ms, True)
