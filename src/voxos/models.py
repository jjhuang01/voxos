"""模型下载与校验。

为什么在包里而不是只放在 `scripts/`：`pipx install voxos` 装完只有 `voxos`
命令，`scripts/` 不在 wheel 里——装完的人没有任何官方途径拿到模型，只能去
clone 源码。一个「装好了但用不了」的包，等于没装好。

下载策略：先写同目录独占 `.part`，校验通过再 `os.replace`。半截文件留在原地比没有更糟，
因为下次启动会把它当成有效模型加载。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# HuggingFace 的国内镜像。见 `_mirrors()`：它只是另一条通路，内容仍要过校验。
_HF_MIRROR = "https://hf-mirror.com/"

MODELS: dict[str, dict[str, object]] = {
    "campplus.onnx": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "min_bytes": 20_000_000,
        "desc": "CAM++ 中文声纹嵌入（192 维，CPU 20ms 内完成推理）",
        "sha256": "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11",
    },
    "silero_vad.onnx": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "min_bytes": 500_000,
        "desc": "Silero VAD（512 定长窗口，实时率约 0.4%）",
        "sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
    },
    "sense-voice/model.int8.onnx": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
        "min_bytes": 200_000_000,
        "desc": "SenseVoice 离线识别（int8，中英日韩粤，239MB）",
        "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    },
    "sense-voice/tokens.txt": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
        "min_bytes": 200_000,
        "desc": "SenseVoice 词表",
        "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    },
    "smart-turn-v3.2-cpu.onnx": {
        "url": "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/"
               "smart-turn-v3.2-cpu.onnx",
        "min_bytes": 8_000_000,
        "desc": "smart-turn v3.2 语义判停（int8 8.7MB，吃音频不吃文本，BSD-2）",
        # 摘要是**从镜像 hf-mirror.com 下的那个文件**算的（原站在这台机器上
        # 不通）。它钉不住「上游作者换没换文件」，但能钉住「以后每次下到的都是
        # 同一份」——镜像被替换或传输截断都会在这里失败。功能上也验过：
        # 一句完整的话 +400ms 静音 → 0.94，半截话 → 0.008。
        "sha256": "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
    },
    # 真流式识别（zipformer 中英双语，transducer）。四个文件是一组，缺一个就
    # 加载不了——但缺了不影响使用：`stream.make_streamer` 会自动退回窗口字幕。
    # 下完不用改配置：目录名固定，管道会自己发现它。
    "stream-zipformer/encoder-epoch-99-avg-1.int8.onnx": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/resolve/main/"
               "encoder-epoch-99-avg-1.int8.onnx",
        "min_bytes": 50_000_000,
        "desc": "流式识别编码器（int8，中英双语，实时率约 0.3）",
        "no_sha256_reason": "同上：环境无法访问 HuggingFace",
    },
    "stream-zipformer/decoder-epoch-99-avg-1.onnx": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/resolve/main/"
               "decoder-epoch-99-avg-1.onnx",
        "min_bytes": 800_000,
        "desc": "流式识别解码器（只有 1MB 量级，fp32 就够快）",
        "no_sha256_reason": "同上：环境无法访问 HuggingFace",
    },
    "stream-zipformer/joiner-epoch-99-avg-1.int8.onnx": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/resolve/main/"
               "joiner-epoch-99-avg-1.int8.onnx",
        "min_bytes": 1_000_000,
        "desc": "流式识别联合网络（int8）",
        "no_sha256_reason": "同上：环境无法访问 HuggingFace",
    },
    "stream-zipformer/tokens.txt": {
        "url": "https://huggingface.co/csukuangfj/"
               "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/resolve/main/"
               "tokens.txt",
        "min_bytes": 100_000,
        "desc": "流式识别词表（含中文单字与英文子词）",
        "no_sha256_reason": "同上：环境无法访问 HuggingFace",
    },
}

# 装不上也不影响核心功能，只是识别会退回脚本回放 / 判停会退回文本尾词。
ASR = {"sense-voice/model.int8.onnx", "sense-voice/tokens.txt"} | {
    name for name in MODELS if name.startswith("stream-zipformer/")
}
OPTIONAL = ASR | {"smart-turn-v3.2-cpu.onnx"}


def default_dir() -> Path:
    """模型默认落在用户数据目录，不是安装目录——升级不该丢 267MB。"""
    from .config import load

    return load().path("audio.speaker_model", ".voxos/models/campplus.onnx").parent


def verify(path: Path, spec: dict) -> bool:
    """校验大小、非全零和可选 SHA256；文件不可读时返回 False。"""
    try:
        if not path.exists() or path.stat().st_size < spec["min_bytes"]:  # type: ignore[operator]
            return False
        with open(path, "rb") as handle:
            head = handle.read(4096)
        if not any(head):
            print("  校验失败：文件开头全是零，不是有效模型")
            return False
        expected = spec.get("sha256")
        if expected:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != str(expected).lower():
                print("  校验失败：SHA256 与清单不一致")
                return False
        return True
    except OSError as exc:
        print(f"  校验失败：{type(exc).__name__}")
        return False


def _mirrors(url: str) -> list[str]:
    """提供候选镜像；下载内容仍须通过大小、非全零和可选 SHA256 校验。"""
    prefix = "https://huggingface.co/"
    if url.startswith(prefix):
        return [_HF_MIRROR + url[len(prefix):]]
    return []


def _attempts(spec: dict, proxy: str | None) -> list[tuple[str, str | None]]:
    """先代理后直连，每条通路按原站、镜像的顺序尝试，不推断失败原因。"""
    urls = [str(spec["url"]), *_mirrors(str(spec["url"]))]
    order: list[tuple[str, str | None]] = []
    if proxy:
        order.extend((url, proxy) for url in urls)
    order.extend((url, None) for url in urls)
    return order


def _host(url: str) -> str:
    """日志仅保留主机名，省略 URL 中的凭据、路径和查询参数。"""
    try:
        return urlsplit(url).hostname or "未知主机"
    except ValueError:
        return "未知主机"


def _transfer(url: str, part: Path, proxy: str | None, route: str) -> bool:
    """传输到临时文件，报告通路和状态；异常交由下载边界统一处理。"""
    if proxy:
        result = subprocess.run(
            ["curl", "-q", "-fsSL", "--http1.1", "--max-time", "300",
             "--noproxy", "", "-x", proxy, "-o", str(part), "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=310,
        )
        status = result.stdout.strip()
        status = status if len(status) == 3 and status.isascii() and status.isdecimal() else "未知"
        print(f"  {route}：HTTP {status}")
        if result.returncode:
            print(f"  {route} 失败：CurlExit {result.returncode}")
        return result.returncode == 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=120) as response, open(part, "wb") as handle:
        print(f"  {route}：HTTP {response.status}")
        while chunk := response.read(1 << 16):
            handle.write(chunk)
        expected = response.headers.get("Content-Length")
        if expected is not None and handle.tell() != int(expected):
            print(f"  {route} 失败：ContentLengthMismatch")
            return False
    return True


def download(url: str, target: Path, proxy: str | None, spec: dict) -> bool:
    """使用同目录独占临时文件，校验后原子替换；失败保留旧文件并返回 False。"""
    route = f"{_host(url)} / " + (f"代理({_host(proxy)})" if proxy else "直连")
    part: Path | None = None
    ok = False
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                         suffix=".part", delete=False) as handle:
            part = Path(handle.name)
        if _transfer(url, part, proxy, route):
            if verify(part, spec):
                os.replace(part, target)
                ok = True
            else:
                print(f"  {route} 失败：模型校验失败")
    except urllib.error.HTTPError as exc:
        print(f"  {route} 失败：HTTP {exc.code} ({type(exc).__name__})")
    except Exception as exc:  # 网络、子进程与文件 I/O 均在此边界兜底，不输出异常正文。
        print(f"  {route} 失败：{type(exc).__name__}")
    finally:
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError as exc:
                print(f"  {route} 临时文件清理失败：{type(exc).__name__}")
                ok = False
    return ok


def fetch(target: Path | None = None, *, proxy: str | None = None,
          force: bool = False, with_asr: bool = True) -> int:
    """下载全部模型。返回 0 表示必需项齐了。

    proxy 为空时自己看环境变量：调用方（CLI）不该为了一个兜底值去 import os。
    """
    proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    base = Path(target) if target else default_dir()
    base.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    skipped: list[str] = []
    for name, spec in MODELS.items():
        # `--no-asr` 只跳过识别模型。判停模型也在 OPTIONAL 里，但它只有 8.7MB，
        # 而且「没识别也要能判停」是个正常需求——按 OPTIONAL 跳会把两件事混一起。
        if not with_asr and name in ASR:
            skipped.append(name)
            continue
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if verify(path, spec) and not force:
            print(f"✓ {name} 已存在（{path.stat().st_size / 1e6:.1f}MB）")
            continue
        print(f"↓ {name}：{spec['desc']}")
        ok = False
        for url, via in _attempts(spec, proxy):
            if download(url, path, via, spec):
                ok = True
                break
        if verify(path, spec):
            if ok:
                print(f"{name} 下载完成（{path.stat().st_size / 1e6:.1f}MB）")
            else:
                print(f"{name} 下载失败，已保留旧有效模型，仍可用")
        else:
            (skipped if name in OPTIONAL else failed).append(name)

    _report(failed, skipped, base, proxy)
    return 1 if failed else 0


def _report(failed: list[str], skipped: list[str], base: Path, proxy: str | None) -> None:
    """失败与跳过要分开说：一个能修，一个是设计如此。"""
    if skipped:
        print(f"\n· 可选模型未下载：{', '.join(skipped)}")
        print("  不影响核心功能：识别会退回脚本回放，判停会退回文本尾词。")
    if not failed:
        print(f"\n完成，模型在 {base}。下一步：voxos enroll（10 秒说 2~3 句）&& voxos run")
        return
    print(f"\n⚠️ 下载失败：{', '.join(failed)}")
    print("  可手动下载后放入上述目录；缺失时系统会自动降级（能量 VAD + 频谱声纹），")
    print("  但声纹精度会下降，FRR/FAR 不再满足 PRD 指标。")
    if proxy:
        print(f"  已尝试代理通路（主机 {_host(proxy)}，来自 --proxy 或环境变量）及直连通路。")
    print("  voxos models --proxy http://127.0.0.1:7890")
    print("  HuggingFace 上的那几个还会自动试镜像 hf-mirror.com；两边都不通才需要手动下。")
