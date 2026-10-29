"""流式识别：把「整段说完才出字」变成「边说边出字」。

为什么需要这一层（`docs/VOICE.md` §5）：现在用户从开口到看见文字之间什么都没有。
胶囊只在他**说完之后**才显示那句话，中间几秒钟是完全静默的——豆包和 ChatGPT
都不是这样，它们边说边出字。这不是装饰，是「它到底听到没有」的唯一反馈。

两条路，接口一模一样，可以随时换：

- `WindowedStt`：包住现有的离线引擎，反复解「最近 N 秒」。**不需要任何新模型**，
  现在这台机器就能跑。代价是每 `interval` 解一次窗口。
- `SherpaStreamingStt`：sherpa-onnx 的真流式识别器（zipformer/paraformer），
  增量解码，长句不退化，还能吃热词。需要单独下模型。

为什么先做前者：真流式模型在这台机器上下不下来（网络不通），而**实时字幕本身
不需要新模型**——候选态本来就在解那 3 秒窗口用来判停，把结果顺手交给胶囊即可。
后者是同一个接口的更好实现，模型到了就换，管道一行不用改。

## 三个必须做对的细节（都不是想出来的）

1. **一句一个流**。`OnlineStream` 是有状态的，跨句复用会把上一句的解码状态带进
   下一句——第一句之后全错，而且错得很像「模型不准」。
2. **partial 会改写**。真流式拿到的是「到目前为止的最优假设」，同一个字在下一块
   可能被改成另一个字。所以字幕必须**整行替换**，绝不能往后追加。
3. **窗口会丢前缀**。`WindowedStt` 只解最近 N 秒，句子超过 N 秒时字幕开头会掉字。
   这是拿「抖动」换「常数成本」的自觉取舍：短句（绝大多数指令）根本不触发，
   长句在 2.5 秒之后才开始滚。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from . import dsp

__all__ = ["StreamingStt", "WindowedStt", "SherpaStreamingStt", "make_streamer"]


class StreamingStt(Protocol):
    """边说边喂的识别器。三个方法的分工必须分清，混了就出 bug。

    - `push`：喂音频，**每块都要喂**。真流式漏一块就丢音频，不是「少一个字」。
    - `text`：问「现在到哪了」。可以随便多问（节流由管道管），但每次都可能很贵
      （`WindowedStt` 每次都要真解一次），所以调用方必须自己限频。
    - `finish`：整段音频交出去要终稿。**调用后本句作废**，下句必须 `reset`。
    """

    def push(self, chunk: np.ndarray) -> None: ...

    def text(self) -> str: ...

    def finish(self, audio: np.ndarray) -> str: ...

    def reset(self) -> None: ...


class WindowedStt:
    """离线引擎 + 滑动窗口：不需要新模型就能出实时字幕。

    解的是「最近 `window_ms`」，所以**成本是常数**，12 秒的长句和 1 秒的短句
    一样贵（本机实测 SenseVoice int8：2.5s ≈ 60ms）。这是它唯一比真流式好的地方，
    也是它唯一的用处。
    """

    def __init__(self, engine: Any, window_ms: float = 2500.0) -> None:
        self._engine = engine
        self._window = max(1, int(dsp.SR * window_ms / 1000.0))
        self._chunks: list[np.ndarray] = []
        self._size = 0
        self._last = ""
        self._last_size = 0        # 上次解码那一刻已经攒了多少：`finish` 靠它判断能否复用

    def reset(self) -> None:
        self._chunks, self._size, self._last, self._last_size = [], 0, "", 0

    def push(self, chunk: np.ndarray) -> None:
        audio = dsp.as_float(chunk)
        if audio.size:
            self._chunks.append(audio)
            self._size += audio.size

    def _tail(self) -> np.ndarray:
        """取最后 `_window` 个采样。从尾部往回取够就停，不拼整段。"""
        if self._size <= self._window:
            return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        picked: list[np.ndarray] = []
        need = self._window
        for chunk in reversed(self._chunks):
            picked.append(chunk)
            need -= chunk.size
            if need <= 0:
                break
        return np.concatenate(picked[::-1])[-self._window:]

    def text(self) -> str:
        tail = self._tail()
        if tail.size < dsp.SR * 0.2:            # 不足 200ms 解不出东西，白花
            return ""
        # 记「这次解码覆盖了多少采样」，不是「一共喂进来多少」。两者在长句里
        # 差得很远：5 秒的句子喂了 80000，但窗口只覆盖最后 40000。记错的话
        # `finish` 会以为全句都解过了，把只含后半句的文本当终稿交出去。
        self._last_size = tail.size
        try:
            self._last = str(self._engine.transcribe(tail, final=False)).strip()
        except Exception:
            # 字幕解不出来不该影响识别本身：这里吞掉，终稿还有一次机会。
            self._last = ""
        return self._last

    def finish(self, audio: np.ndarray) -> str:
        """终稿。窗口没截断时直接复用 `text()` 的结果，省一次整段解码。

        三条必须同时满足：① 上次解码时攒的音频已经不短于整段（说明那次解码
        覆盖了全句，不是只覆盖了前 2 秒）；② 引擎自己声明了
        `partial_equals_final`（离线引擎解一次就是整段）。

        少判一条都会出事——脚本回放引擎的 `final=True` 才推进队列，复用它的
        partial 会让同一句话被反复派发（0.1.25 踩过，`test_e2e` 当场逮住）。
        """
        whole = dsp.as_float(audio)
        if (self._last and self._last_size >= whole.size
                and getattr(self._engine, "partial_equals_final", False)):
            return self._last
        return str(self._engine.transcribe(whole, final=True)).strip()


class SherpaStreamingStt:
    """sherpa-onnx 真流式：增量解码，长句不退化，支持热词偏置。

    比 `WindowedStt` 强在两处，都值得为它下模型：
    1. **前缀不会丢**。有状态的解码器只在末尾追加，字幕不会滚掉开头。
    2. **长句成本线性但常数很小**。每块只算新增的 320ms，而不是重解整个窗口。

    端点检测关掉（`enable_endpoint_detection=False`）：我们自己的 VAD + 判停模型
    已经管这件事了，两套端点各判各的，谁先到就切，用户感受到的容忍度会随
    音频内容随机变化。
    """

    def __init__(self, model_dir: str | Path, num_threads: int = 2,
                 hotwords: list[str] | None = None) -> None:
        try:
            import sherpa_onnx  # noqa: PLC0415
        except ImportError as exc:
            # 抛 ModuleNotFoundError 等于让用户自己去猜装什么。和 `SenseVoiceStt`
            # 保持同一句话：缺依赖要说清怎么补。
            raise RuntimeError("需要 sherpa-onnx：pip install 'voxos[models]'") from exc

        root = Path(str(model_dir)).expanduser()
        tokens = root / "tokens.txt"
        if not tokens.exists():
            raise FileNotFoundError(f"{root} 里缺 tokens.txt")
        self._recognizer = _build_online(sherpa_onnx, root, tokens, num_threads)
        self._stream = self._recognizer.create_stream()
        self._last = ""
        self._fed = False

    def reset(self) -> None:
        # 必须新建流：`OnlineStream` 带着上一句的解码状态，复用等于把上一句的
        # 上下文喂给下一句——第一句之后全错。
        self._stream = self._recognizer.create_stream()
        self._last = ""
        self._fed = False

    def push(self, chunk: np.ndarray) -> None:
        audio = dsp.as_float(chunk)
        if not audio.size:
            return
        self._fed = True
        self._stream.accept_waveform(dsp.SR, audio)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)

    def text(self) -> str:
        result = self._recognizer.get_result(self._stream)
        self._last = str(getattr(result, "text", result)).strip()
        return self._last

    def finish(self, audio: np.ndarray) -> str:
        """流里已经有全部音频了，`audio` 只用于兜底（流被重置过的情况）。"""
        if audio.size and not self._fed:
            self.push(audio)
        self._stream.input_finished()
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self.text()


def _pick(root: Path, *patterns: str) -> Path | None:
    """按通配找模型文件：不同 zipformer 导出的文件名差很多（`encoder-epoch-99…`
    / `encoder.int8.onnx` / `encoder-2023-02-20.onnx`），写死一个名字等于只支持
    一个模型。优先 int8（体积小、CPU 快），其次 fp32。"""
    for pattern in patterns:
        hits = sorted(root.glob(pattern))
        int8 = [hit for hit in hits if "int8" in hit.name]
        if int8:
            return int8[0]
        if hits:
            return hits[0]
    return None


def _build_online(sherpa_onnx: Any, root: Path, tokens: Path, threads: int) -> Any:
    """按目录里的文件判断模型类型，建对应的流式识别器。

    三种都支持，因为用户下哪个模型不由我们决定：
    - transducer（encoder+decoder+joiner）：zipformer 流式中英双语，支持热词
    - paraformer（encoder+decoder）：中文流式
    - zipformer2-ctc（只有 encoder）：CTC 导出
    """
    encoder = _pick(root, "encoder*.onnx")
    decoder = _pick(root, "decoder*.onnx")
    joiner = _pick(root, "joiner*.onnx")
    if encoder is None:
        raise FileNotFoundError(f"{root} 里没有 encoder*.onnx，不是流式识别模型目录")
    common = dict(tokens=str(tokens), num_threads=threads, sample_rate=dsp.SR,
                  feature_dim=80, enable_endpoint_detection=False)
    if joiner is not None and decoder is not None:
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(encoder), decoder=str(decoder), joiner=str(joiner),
            decoding_method="greedy_search", **common)
    if decoder is not None:
        return sherpa_onnx.OnlineRecognizer.from_paraformer(
            encoder=str(encoder), decoder=str(decoder), **common)
    return sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(model=str(encoder), **common)


def _stream_dir(cfg: Any) -> Path | None:
    """流式模型在哪：显式配置优先，其次是 `voxos models` 的默认落点。

    自动探测默认落点是故意的：跑一次 `voxos models` 就该**直接生效**，不该还要
    去配置文件里填一个路径。少一步配置，就少一个「为什么没生效」。

    注意空配置时**不能**用 `cfg.path("")` 探路：`Path("")` 是 `.`，拼上去就是
    数据根本身，于是每构造一次管道都去数据根找 `tokens.txt`，刷一行报错。
    """
    raw = str(cfg.get("stt.stream_model_dir", "") or "").strip()
    if raw:
        root = cfg.path("stt.stream_model_dir", "")
        if root.exists():
            return root
        print(f"[voxos] stt.stream_model_dir = {raw} 不存在，先用窗口字幕")
        return None
    return (cfg.path("audio.speaker_model", ".voxos/models/campplus.onnx").parent
            / "stream-zipformer")


def make_streamer(cfg: Any, engine: Any) -> StreamingStt | None:
    """按配置造流式识别器。任何异常都返回窗口字幕——**没有字幕也要能听**。

    远端引擎一律不给字幕：字幕要在声纹校验**之前**出字，而声纹校验需要整段
    音频。也就是说开字幕等于把没验过身份的音频提前传上云，这是不能接受的。
    `audio.subtitles` 显式关掉也返回 None。
    """
    if not cfg.flag("audio.subtitles", True):
        return None
    if getattr(engine, "remote", False):
        return None
    root = _stream_dir(cfg)
    if root is not None and (root / "tokens.txt").exists():
        try:
            return SherpaStreamingStt(root, int(cfg.get("stt.num_threads", 2)))
        except Exception as exc:
            print(f"[voxos] 流式识别加载失败，退回窗口字幕：{exc}")
    return WindowedStt(engine, float(cfg.get("audio.subtitle_window_ms", 2500)))
