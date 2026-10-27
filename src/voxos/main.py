"""VoxOS 命令行入口：text / demo / run / enroll / serve / tools / doctor。

设计约束：CLI 只做编排，不做业务；每个子命令不超过 40 行。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import audio, core, corpus, dsp, tools, ui
from .config import Config, load


# ------------------------------------------------------------------ 公共

def _bootstrap(cfg: Config) -> None:
    tools.configure(cfg)
    core.ensure_tools()


def _bus(cfg: Config) -> ui.StatusBus:
    return ui.StatusBus(cfg.root / ".voxos" / "status.jsonl")


def _context() -> dict[str, object]:
    """把焦点窗口与最近文件作为隐式上下文注入（PRD §5）。"""
    window = "unknown"
    if shutil.which("osascript"):
        try:
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first application process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            window = (out.stdout or "").strip() or "unknown"
        except (subprocess.SubprocessError, OSError):
            window = "unknown"
    return {"active_window": window, "recent_files": tools.RT.recent_files}


def _confirm(cfg: Config):
    timeout = float(cfg.get("workspace.confirm_timeout_s", 8))

    def confirm(name: str, args: dict[str, object], verdict: core.Verdict) -> bool:
        print(f"⚠️  需要二次确认：{verdict.reason}\n    参数：{args}\n    {timeout:.0f}s 内输入 y 确认：",
              end="", flush=True)
        box: dict[str, str] = {}
        worker = threading.Thread(target=lambda: box.update(value=input().strip().lower()), daemon=True)
        worker.start()
        worker.join(timeout)
        return box.get("value") in ("y", "yes", "确认")

    return confirm


def _executor(cfg: Config, bus: ui.StatusBus) -> core.Executor:
    return core.Executor(cfg=cfg, on_event=bus, confirm=_confirm(cfg))


# ------------------------------------------------------------------ 子命令

def cmd_text(args: argparse.Namespace, cfg: Config) -> int:
    bus = _bus(cfg)
    result = core.dispatch(args.text, executor=_executor(cfg, bus), cfg=cfg, context=_context())
    print(f"[{result.route}] {result.reply or '(无动作)'}  {result.latency_ms:.1f}ms")
    return 0 if result.route != "ignore" else 2


def cmd_demo(args: argparse.Namespace, cfg: Config) -> int:
    if not args.live:                       # 默认沙箱：不污染真实日志目录
        demo = cfg.root / ".voxos" / "demo"
        cfg = cfg.with_overrides(**{
            "workspace.daily_log_dir": str(demo / "logs"),
            "workspace.default_code_dir": str(demo / "projects"),
            "workspace.backup_dir": str(demo / "backups"),
        })
        tools.configure(cfg)
        print(f"沙箱模式：日志写入 {demo / 'logs'}（--live 可写入真实目录）\n")
    bus = _bus(cfg)
    root = cfg.root / ".voxos" / "corpus"
    manifest = corpus.ensure_corpus(root, force=args.refresh)
    embedder, threshold, kind = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    owner = manifest["owner"]["clips"]  # type: ignore[index]
    consistency = lock.enroll([corpus.load_clip(root, clip) for clip in owner[:2]])
    print(f"语料={manifest['kind']} 声纹后端={kind} 阈值={threshold} 录入一致性={consistency:.3f}\n")

    stt = audio.ScriptedStt([owner[2]["text"], owner[3]["text"]])
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=lock, vad=audio.make_vad(cfg), on_event=bus)
    impostor = manifest["impostors"][0]["clips"][0]  # type: ignore[index]
    stream = audio.concat_with_silence(
        [corpus.load_clip(root, owner[2]), corpus.load_clip(root, impostor), corpus.load_clip(root, owner[3])]
    )
    executor = _executor(cfg, bus)
    print(f"{'#':<2} {'声纹':<6} {'音频':<8} {'路由':<8} 转写 / 结果")
    print("-" * 78)
    for index, utterance in enumerate(pipeline.run(audio.ArraySource(stream)), 1):
        if not utterance.accepted:
            print(f"{index:<2} {'拒绝':<6} {utterance.audio_ms:>5.0f}ms {utterance.reason}")
            continue
        result = core.dispatch(utterance.text, executor=executor, cfg=cfg, context=_context())
        print(f"{index:<2} {utterance.score:<6.2f} {utterance.audio_ms:>5.0f}ms {result.route:<8} {utterance.text}")
        print(f"   ↳ {result.reply}  ({result.latency_ms:.1f}ms)")
    print("-" * 78)
    print(f"管道统计：{pipeline.stats}")
    if args.open_pill:
        webbrowser.open(f"http://{cfg.get('ui.host')}:{cfg.get('ui.port')}/")
    return 0


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    bus = _bus(cfg)
    executor = _executor(cfg, bus)
    ui.serve(
        bus, str(cfg.get("ui.host", "127.0.0.1")), int(cfg.get("ui.port", 8765)),
        cfg.path("ui.pill_file", "web/pill.html"),
        on_command=lambda text: core.dispatch(text, executor=executor, cfg=cfg, context=_context()).reply,
        block=False,
    )
    sounds = ui.ensure_earcons(cfg.root / ".voxos" / "sounds")
    hotwords = audio.refresh_hotwords(cfg)
    embedder, threshold, kind = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder).load(
        cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    if not lock.profiles:
        print("⚠️  尚未录入声纹，先运行：voxos enroll --record 3")
        return 2
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.make_stt(cfg, hotwords), lock=lock,
                                   vad=audio.make_vad(cfg), on_event=bus)
    source = audio.SelfHealingSource(lambda: audio.MicSource(int(cfg.get("audio.chunk_size", 512))),
                                     on_event=bus)
    print(f"VoxOS 已启动：声纹后端={kind} 阈值={threshold} 热词={len(hotwords)} "
          f"胶囊=http://{cfg.get('ui.host')}:{cfg.get('ui.port')}/")
    bus.emit("idle", "待机", "")
    for utterance in pipeline.run(source):
        if not utterance.accepted:
            continue
        if cfg.flag("ui.earcons", True):
            ui.play(sounds["listen"])
        bus.emit("executing", "处理中…", utterance.text)
        result = core.dispatch(utterance.text, executor=executor, cfg=cfg, context=_context())
        bus.emit("done", "完成", result.reply)
        if args.speak and result.reply:
            _speak(result.reply, pipeline)
    return 0


def _speak(text: str, pipeline: audio.AudioPipeline) -> None:
    """TTS 播报，播报期间拉起回声门（防止 Agent 听到自己）。"""
    if not shutil.which("say"):
        return
    pipeline.echo.tts_start()
    try:
        subprocess.run(["say", "-r", "220", text[:60]], timeout=15, check=False)
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        pipeline.echo.tts_end()


def cmd_enroll(args: argparse.Namespace, cfg: Config) -> int:
    embedder, threshold, kind = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    path = cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz")
    lock.load(path)
    samples: list[object] = []
    if args.wav:
        samples = [audio.wav_to_array(Path(item).expanduser()) for item in args.wav]
    else:
        samples = _record(args.record)
    if not samples:
        print("没有可用音频，录入失败")
        return 2
    consistency = lock.enroll(samples, args.mode)  # type: ignore[arg-type]
    lock.save(path)
    print(f"已录入 {len(samples)} 段（{args.mode}），一致性 {consistency:.3f}，阈值 {threshold} → {path}")
    if consistency < 0.8:
        print("⚠️  一致性偏低：建议在安静环境重录，或每段说满 3 秒")
    return 0


def _record(seconds: float) -> list[object]:
    import numpy as np

    print(f"请说话，录音 {seconds:.0f} 秒……")
    source = audio.MicSource()
    with source:
        chunks = []
        start = time.time()
        for chunk in source:
            chunks.append(chunk)
            if time.time() - start >= seconds:
                break
    return [np.concatenate(chunks)] if chunks else []


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    bus = _bus(cfg)
    executor = _executor(cfg, bus)
    url = f"http://{cfg.get('ui.host')}:{cfg.get('ui.port')}/"
    ui.serve(bus, str(cfg.get("ui.host")), int(cfg.get("ui.port")),
             cfg.path("ui.pill_file", "web/pill.html"),
             on_command=lambda text: core.dispatch(text, executor=executor, cfg=cfg).reply, block=False)
    print(f"胶囊界面：{url}（Ctrl-C 退出）")
    if not args.no_open:
        webbrowser.open(url)
    ui.wait_forever()
    return 0


def cmd_tools(_: argparse.Namespace, cfg: Config) -> int:
    for name, meta in sorted(core.REGISTRY.items()):
        params = ", ".join(meta.schema["function"]["parameters"]["properties"])
        print(f"{name:<24} {meta.risk.value:<13} fast={'yes' if meta.fast else 'no ':>3}  ({params})")
    print(f"\n共 {len(core.REGISTRY)} 个工具，{len(core.FAST_ROUTES)} 条快通道")
    return 0


def cmd_doctor(_: argparse.Namespace, cfg: Config) -> int:
    checks = [
        ("python", sys.version.split()[0]),
        ("numpy", _version("numpy")),
        ("sherpa-onnx", _version("sherpa_onnx")),
        ("sounddevice", _version("sounddevice")),
        ("tmux", shutil.which("tmux") or "缺失"),
        ("say/afplay", shutil.which("say") or "缺失"),
        ("声纹模型", str(cfg.path("audio.speaker_model", "")) if cfg.path("audio.speaker_model", "").exists() else "缺失（降级）"),
        ("声纹档案", "已录入" if cfg.path("audio.voiceprint_profiles").exists() else "未录入"),
        ("LLM", f"{cfg.get('llm.model')} @ {cfg.get('llm.base_url')}" if cfg.flag("llm.enabled") else "未启用（走快通道+离线兜底）"),
        ("dry_run", str(cfg.flag("workspace.dry_run"))),
        ("角色", str(cfg.get("workspace.role"))),
    ]
    for name, value in checks:
        print(f"{name:<12} {value}")
    return 0


def _version(module: str) -> str:
    try:
        return __import__(module).__version__  # type: ignore[no-any-return]
    except Exception:
        return "缺失"


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voxos", description="VoxOS：只听你一个人说话的语音 Agent")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/settings.toml）")
    sub = parser.add_subparsers(dest="command", required=True)

    text = sub.add_parser("text", help="用一句话触发（无需麦克风）")
    text.add_argument("text")
    text.set_defaults(func=cmd_text)

    demo = sub.add_parser("demo", help="脚本化音频端到端演示")
    demo.add_argument("--refresh", action="store_true", help="重新生成语料")
    demo.add_argument("--live", action="store_true", help="写入真实日志/代码目录（默认沙箱）")
    demo.add_argument("--open-pill", action="store_true")
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="守护进程（麦克风 + 胶囊）")
    run.add_argument("--speak", action="store_true", help="用 TTS 播报结果")
    run.set_defaults(func=cmd_run)

    enroll = sub.add_parser("enroll", help="录入声纹")
    enroll.add_argument("--wav", action="append", help="用现成 wav 录入（可多次）")
    enroll.add_argument("--record", type=float, default=3.0, help="麦克风录制秒数")
    enroll.add_argument("--mode", choices=["normal", "mask"], default="normal")
    enroll.set_defaults(func=cmd_enroll)

    serve = sub.add_parser("serve", help="只启动胶囊状态服务")
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("tools", help="列出已注册工具").set_defaults(func=cmd_tools)
    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load(args.config)
    _bootstrap(cfg)
    return int(args.func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
