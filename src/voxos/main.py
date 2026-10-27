"""VoxOS 命令行入口：text / demo / run / enroll / serve / pill / tools / doctor。

设计约束：CLI 只做编排，不做业务；每个函数不超过 40 行。
可选信息（前台窗口、TTS 播报）一律不阻塞音频主循环 —— 见 CODING_PHILOSOPHY.md §5.5。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from . import __version__, audio, core, corpus, dsp, settings, tools, ui
from .config import Config, load

# ------------------------------------------------------------------ 运行态

class ConfirmGate:
    """确认闸门：终端与胶囊谁先回答谁生效；无人回答按拒绝处理（失败关闭）。"""

    def __init__(self, bus: ui.StatusBus, timeout: float) -> None:
        self.bus = bus
        self.timeout = timeout
        self._event = threading.Event()
        self._answer = False

    def ask(self, name: str, args: dict[str, object], verdict: core.Verdict) -> bool:
        self._event.clear()
        self._answer = False
        self.bus.emit("confirm", "需要确认", verdict.reason)
        if sys.stdin is not None and sys.stdin.isatty():
            threading.Thread(target=self._read_terminal, daemon=True).start()
        answered = self._event.wait(self.timeout)
        if not answered:
            self.bus.emit("blocked", "确认超时", "按拒绝处理")
        return self._answer

    def resolve(self, allow: bool) -> None:
        self._answer = allow
        self._event.set()

    def _read_terminal(self) -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        self.resolve(line.strip().lower() in ("y", "yes", "确认"))


class Runtime:
    """一次运行的全部可变状态：配置、执行器、最近记录。设置改动后就地换掉执行器。"""

    def __init__(self, cfg: Config, bus: ui.StatusBus) -> None:
        self.bus = bus
        self.cfg = cfg
        self.gate = ConfirmGate(bus, float(cfg.get("workspace.confirm_timeout_s", 8)))
        self.executor = self._build()
        self.recent: deque[dict[str, object]] = deque(maxlen=8)
        self.started = time.time()

    def _build(self) -> core.Executor:
        return core.Executor(cfg=self.cfg, on_event=self.bus, confirm=self.gate.ask)

    def dispatch(self, text: str, context: dict[str, object] | None = None) -> core.DispatchResult:
        return core.dispatch(text, executor=self.executor, cfg=self.cfg,
                             context=context if context is not None else _context())

    def record(self, text: str, result: core.DispatchResult) -> None:
        self.recent.appendleft({
            "ts": time.strftime("%H:%M:%S"), "text": text, "reply": result.reply,
            "route": result.route, "ms": round(result.latency_ms, 1),
        })

    def reload(self, new_cfg: Config) -> None:
        self.cfg = new_cfg
        tools.configure(new_cfg)
        self.gate.timeout = float(new_cfg.get("workspace.confirm_timeout_s", 8))
        self.executor = self._build()


# ------------------------------------------------------------------ 隐式上下文

_CTX = {"window": "unknown", "at": 0.0, "busy": False}
_CTX_TTL = 3.0
_CTX_LOCK = threading.Lock()


def _context() -> dict[str, object]:
    """立刻返回缓存值，同时在后台刷新 —— 绝不让 osascript 拖慢派发（实测 271ms）。"""
    _warm_context()
    return {"active_window": _CTX["window"], "recent_files": tools.RT.recent_files}


def _warm_context() -> None:
    with _CTX_LOCK:
        if _CTX["busy"] or time.monotonic() - float(_CTX["at"]) < _CTX_TTL:
            return
        _CTX["busy"] = True
    threading.Thread(target=_refresh_window, daemon=True).start()


def _refresh_window() -> None:
    try:
        window = _frontmost_window()
    except Exception:                      # 上下文是可选信息，取不到就算了
        window = "unknown"
    finally:
        with _CTX_LOCK:
            _CTX["at"] = time.monotonic()
            _CTX["busy"] = False
    if window != "unknown":
        _CTX["window"] = window


def _frontmost_window() -> str:
    if not shutil.which("osascript"):
        return "unknown"
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return (out.stdout or "").strip() or "unknown"


# ------------------------------------------------------------------ 公共

def _bootstrap(cfg: Config) -> None:
    tools.configure(cfg)
    core.ensure_tools()


def _bus(cfg: Config) -> ui.StatusBus:
    return ui.StatusBus(cfg.root / ".voxos" / "status.jsonl")


def _lock(cfg: Config) -> dsp.SpeakerLock:
    embedder, threshold, _ = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    return lock.load(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))


def _status(runtime: Runtime, lock: dsp.SpeakerLock | None) -> dict[str, object]:
    cfg = runtime.cfg
    return {
        "version": __version__,
        "enrolled": bool(lock and lock.profiles),
        "threshold": getattr(lock, "threshold", None),
        "role": cfg.get("workspace.role"),
        "dry_run": cfg.flag("workspace.dry_run"),
        "llm": cfg.flag("llm.enabled"),
        "stt": cfg.get("stt.backend"),
        "uptime_s": round(time.time() - runtime.started),
    }


def _enroll_now(cfg: Config, lock: dsp.SpeakerLock, seconds: float,
                done: threading.Event | None = None) -> dict[str, object]:
    """从麦克风录一段并写盘。失败也要给出人能看懂的原因。"""
    try:
        samples = _record(seconds)
    except Exception as exc:                       # 无麦克风/无权限
        return {"ok": False, "error": f"录音失败：{exc}"}
    if not samples:
        return {"ok": False, "error": "没有采集到音频"}
    consistency = lock.enroll(samples)             # type: ignore[arg-type]
    lock.save(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    if done is not None:
        done.set()
    return {"ok": True, "consistency": round(float(consistency), 3), "samples": len(samples)}


def _ui_context(runtime: Runtime, lock: dsp.SpeakerLock | None = None,
                enrolled: threading.Event | None = None) -> ui.UiContext:
    def save(values: dict[str, object]) -> dict[str, object]:
        new_cfg, errors = settings.apply(runtime.cfg, values)
        if errors:
            return {"ok": False, "errors": errors}
        runtime.reload(new_cfg)
        return {"ok": True, "groups": settings.describe(runtime.cfg)}

    return ui.UiContext(
        bus=runtime.bus,
        pill_file=runtime.cfg.path("ui.pill_file", "web/pill.html"),
        on_command=lambda text: _run_text(runtime, text),
        settings_get=lambda: settings.describe(runtime.cfg),
        settings_set=save,
        status=lambda: _status(runtime, lock),
        on_enroll=(lambda seconds: _enroll_now(runtime.cfg, lock, seconds, enrolled)) if lock else None,
        on_confirm=runtime.gate.resolve,
        history=lambda: list(runtime.recent),
    )


def _run_text(runtime: Runtime, text: str) -> str:
    result = runtime.dispatch(text)
    runtime.record(text, result)
    return result.reply or "(无动作)"


def _serve(runtime: Runtime, lock: dsp.SpeakerLock | None = None,
           enrolled: threading.Event | None = None) -> str:
    cfg = runtime.cfg
    host, port = str(cfg.get("ui.host", "127.0.0.1")), int(cfg.get("ui.port", 8765))
    ui.serve(_ui_context(runtime, lock, enrolled), host, port, block=False)
    return f"http://{host}:{port}/"


# ------------------------------------------------------------------ 子命令

def cmd_text(args: argparse.Namespace, cfg: Config) -> int:
    runtime = Runtime(cfg, _bus(cfg))
    result = runtime.dispatch(args.text)
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
    runtime = Runtime(cfg, _bus(cfg))
    root = cfg.root / ".voxos" / "corpus"
    manifest = corpus.ensure_corpus(root, force=args.refresh)
    embedder, threshold, kind = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    owner = manifest["owner"]["clips"]  # type: ignore[index]
    consistency = lock.enroll([corpus.load_clip(root, clip) for clip in owner[:2]])
    print(f"语料={manifest['kind']} 声纹后端={kind} 阈值={threshold} 录入一致性={consistency:.3f}\n")

    stt = audio.ScriptedStt([owner[2]["text"], owner[3]["text"]])
    pipeline = audio.AudioPipeline(cfg=cfg, stt=stt, lock=lock, vad=audio.make_vad(cfg),
                                   on_event=runtime.bus)
    impostor = manifest["impostors"][0]["clips"][0]  # type: ignore[index]
    stream = audio.concat_with_silence(
        [corpus.load_clip(root, owner[2]), corpus.load_clip(root, impostor), corpus.load_clip(root, owner[3])]
    )
    _demo_table(pipeline, runtime, stream)
    print(f"管道统计：{pipeline.stats}")
    if args.open_pill:
        webbrowser.open(f"http://{cfg.get('ui.host')}:{cfg.get('ui.port')}/")
    return 0


def _demo_table(pipeline: audio.AudioPipeline, runtime: Runtime, stream) -> None:
    print(f"{'#':<2} {'声纹':<6} {'音频':<8} {'路由':<8} 转写 / 结果")
    print("-" * 78)
    for index, utterance in enumerate(pipeline.run(audio.ArraySource(stream)), 1):
        if not utterance.accepted:
            print(f"{index:<2} {'拒绝':<6} {utterance.audio_ms:>5.0f}ms {utterance.reason}")
            continue
        result = runtime.dispatch(utterance.text)
        runtime.record(utterance.text, result)
        print(f"{index:<2} {utterance.score:<6.2f} {utterance.audio_ms:>5.0f}ms {result.route:<8} {utterance.text}")
        print(f"   ↳ {result.reply}  ({result.latency_ms:.1f}ms)")
    print("-" * 78)


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    runtime = Runtime(cfg, _bus(cfg))
    lock = _lock(cfg)
    enrolled = threading.Event()
    if lock.profiles:
        enrolled.set()
    url = _serve(runtime, lock, enrolled)
    sounds = ui.ensure_earcons(cfg.root / ".voxos" / "sounds")
    hotwords = audio.refresh_hotwords(cfg)
    _, threshold, kind = audio.make_embedder(cfg)
    print(f"VoxOS 已启动：声纹后端={kind} 阈值={threshold} 热词={len(hotwords)} 胶囊={url}")
    if not lock.profiles:
        print("首次使用：在胶囊面板里点「录入声纹」（3 秒），或运行 voxos enroll --record 3")
        runtime.bus.emit("onboard", "请先录入声纹", "点胶囊 → 录入声纹")
        try:
            enrolled.wait()
        except KeyboardInterrupt:
            return 0
        runtime.bus.emit("idle", "待机", "")
    return _listen(runtime, cfg, lock, sounds)


def _listen(runtime: Runtime, cfg: Config, lock: dsp.SpeakerLock, sounds: dict[str, Path]) -> int:
    """主循环：只做「取一句话 → 派发 → 回报」，别的都丢给线程。"""
    hotwords = audio.refresh_hotwords(cfg)
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.make_stt(cfg, hotwords), lock=lock,
                                   vad=audio.make_vad(cfg), on_event=runtime.bus)
    source = audio.SelfHealingSource(lambda: audio.MicSource(int(cfg.get("audio.chunk_size", 512))),
                                     on_event=runtime.bus)
    _context()                             # 预热：把首次线程启动的开销挪到进循环之前
    runtime.bus.emit("idle", "待机", "")
    for utterance in pipeline.run(source):
        if not utterance.accepted:
            continue
        if pipeline.echo.is_interrupt(utterance.text) and stop_speaking():
            runtime.bus.emit("idle", "已停止", utterance.text)
            continue
        if cfg.flag("ui.earcons", True):
            ui.play(sounds["listen"])
        runtime.bus.emit("executing", "处理中…", utterance.text)
        result = runtime.dispatch(utterance.text)
        runtime.record(utterance.text, result)
        runtime.bus.emit("done", "完成", result.reply)
        if result.reply and cfg.flag("ui.speak", True):
            _speak_async(result.reply, pipeline)
    return 0


def _speak_async(text: str, pipeline: audio.AudioPipeline) -> None:
    """TTS 放到后台：说 3 秒不能把麦克风采集卡住 3 秒。"""
    threading.Thread(target=_speak, args=(text, pipeline), daemon=True).start()


_TTS: dict[str, subprocess.Popen | None] = {"proc": None}


def stop_speaking() -> bool:
    """打断：把正在念的 say 杀掉。没有在念就返回 False。"""
    proc = _TTS.get("proc")
    if proc is None or proc.poll() is not None:
        return False
    proc.terminate()
    return True


def _speak(text: str, pipeline: audio.AudioPipeline) -> None:
    if not shutil.which("say"):
        return
    pipeline.echo.tts_start()
    proc = None
    try:
        proc = subprocess.Popen(["say", "-r", "220", text[:60]])
        _TTS["proc"] = proc
        proc.wait(timeout=15)
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        _TTS["proc"] = None
        pipeline.echo.tts_end()


def cmd_enroll(args: argparse.Namespace, cfg: Config) -> int:
    lock = _lock(cfg)
    if args.wav:
        samples: list[object] = [audio.wav_to_array(Path(item).expanduser()) for item in args.wav]
    else:
        samples = _record(args.record)
    if not samples:
        print("没有可用音频，录入失败")
        return 2
    consistency = lock.enroll(samples, args.mode)  # type: ignore[arg-type]
    lock.save(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    print(f"已录入 {len(samples)} 段（{args.mode}），一致性 {consistency:.3f} → "
          f"{cfg.path('audio.voiceprint_profiles')}")
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
    runtime = Runtime(cfg, _bus(cfg))
    url = _serve(runtime, _lock(cfg))
    print(f"胶囊界面：{url}（Ctrl-C 退出）")
    if not args.no_open:
        webbrowser.open(url)
    ui.wait_forever()
    return 0


def cmd_pill(args: argparse.Namespace, cfg: Config) -> int:
    url = f"http://{cfg.get('ui.host', '127.0.0.1')}:{cfg.get('ui.port', 8765)}/"
    if not _open_app_window(url):
        webbrowser.open(url)
        print(f"已用默认浏览器打开 {url}（装 Chrome 可获得无边框胶囊窗口）")
    return 0


def _open_app_window(url: str) -> bool:
    """用 Chromium 系浏览器的 --app 模式开一个无边框小窗，最接近「悬浮胶囊」。"""
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
        if not Path(f"/Applications/{app}.app").exists():
            continue
        profile = Path.home() / ".voxos" / "pill-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                              f"--user-data-dir={profile}", "--window-size=420,620",
                              "--window-position=80,80"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return True
    return False


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

    run = sub.add_parser("run", help="守护进程（麦克风 + 胶囊 + 设置面板）")
    run.set_defaults(func=cmd_run)

    enroll = sub.add_parser("enroll", help="录入声纹")
    enroll.add_argument("--wav", action="append", help="用现成 wav 录入（可多次）")
    enroll.add_argument("--record", type=float, default=3.0, help="麦克风录制秒数")
    enroll.add_argument("--mode", choices=["normal", "mask"], default="normal")
    enroll.set_defaults(func=cmd_enroll)

    serve = sub.add_parser("serve", help="只启动胶囊与设置面板")
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    pill = sub.add_parser("pill", help="用无边框窗口打开胶囊（最接近悬浮）")
    pill.set_defaults(func=cmd_pill)

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
