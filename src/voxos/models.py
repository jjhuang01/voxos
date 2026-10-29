"""模型下载与校验。

为什么在包里而不是只放在 `scripts/`：`pipx install voxos` 装完只有 `voxos`
命令，`scripts/` 不在 wheel 里——装完的人没有任何官方途径拿到模型，只能去
clone 源码。一个「装好了但用不了」的包，等于没装好。

下载策略：先写 `.part`，校验通过再 `os.replace`。半截文件留在原地比没有更糟，
因为下次启动会把它当成有效模型加载。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.request
from pathlib import Path

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
        # 故意**没有** sha256：写这份清单的机器连不上 HuggingFace（沙箱只放行
        # 业务域名），抄一个自己没算过的摘要等于把校验变成摆设。宁可只做大小 +
        # 非全零这两项检查，也不填一个假的。有了能联网的机器就补上，然后删掉
        # 下面这个字段——`test_model_manifest_entries_are_complete` 会盯着。
        "no_sha256_reason": "清单作者所在环境无法访问 HuggingFace，没算过摘要",
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
    """下载结果是否可信：大小、非全零、可选 SHA256。只看大小会被稀疏文件骗过。"""
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


def download(url: str, target: Path, proxy: str | None, spec: dict) -> bool:
    """先写 `.part`，校验通过再原子替换；失败不破坏已有好模型。"""
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    try:
        if proxy:
            result = subprocess.run(
                ["curl", "-fsSL", "--http1.1", "--max-time", "300", "-x", proxy, "-o", str(part), url],
                capture_output=True, text=True,
            )
            ok = result.returncode == 0
            if not ok:
                # 不说清楚「代理这条路也不通」，用户会以为只是网络慢。
                reason = (result.stderr or "").strip().splitlines()[-1:] or [str(result.returncode)]
                print(f"  经代理 {proxy} 失败：{reason[0][:160]}")
        else:
            opener = urllib.request.build_opener()
            with opener.open(url, timeout=120) as response, open(part, "wb") as handle:
                while chunk := response.read(1 << 16):
                    handle.write(chunk)
            ok = True
    except Exception as exc:  # 网络问题种类很多，统一报告
        print(f"  直连失败：{exc}")
        ok = False
    if ok and verify(part, spec):
        os.replace(part, target)
        return True
    part.unlink(missing_ok=True)
    return False


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
        ok = (download(str(spec["url"]), path, proxy, spec)
              or download(str(spec["url"]), path, None, spec))
        if ok and verify(path, spec):
            print(f"✓ {name}（{path.stat().st_size / 1e6:.1f}MB）")
        else:
            path.unlink(missing_ok=True)
            (skipped if name in OPTIONAL else failed).append(name)

    _report(failed, skipped, base, proxy)
    return 1 if failed else 0


def _report(failed: list[str], skipped: list[str], base: Path, proxy: str | None) -> None:
    """失败与跳过要分开说：一个能修，一个是设计如此。"""
    if skipped:
        print(f"\n· 可选模型未下载：{', '.join(skipped)}")
        print("  不影响核心功能：识别会退回脚本回放，判停会退回文本尾词。")
    if not failed:
        print(f"\n完成，模型在 {base}。下一步：voxos enroll --record 3 && voxos run")
        return
    print(f"\n⚠️ 下载失败：{', '.join(failed)}")
    print("  可手动下载后放入上述目录；缺失时系统会自动降级（能量 VAD + 频谱声纹），")
    print("  但声纹精度会下降，FRR/FAR 不再满足 PRD 指标。")
    if proxy:
        print(f"  当前用的代理是 {proxy}（来自 --proxy 或环境变量），换成能连 GitHub 的再试：")
    print("  voxos models --proxy http://127.0.0.1:7890")
