"""VoxOS 命令行入口：text / demo / run / enroll / serve / pill / tools / doctor。

设计约束：CLI 只做编排，不做业务；每个函数不超过 40 行。
可选信息（前台窗口、TTS 播报）一律不阻塞音频主循环 —— 见 CODING_PHILOSOPHY.md §5.5。
"""

from __future__ import annotations

import argparse
import math
import queue
import secrets
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

# 一句话没匹配到任何能力时，终端、接口、历史三处必须说同一句话。
NO_ACTION = "没听懂，什么也没做"

# 识别后端在界面上的人话名（配置里仍是英文标识，不为人机耦合让步）。
STT_LABELS = {
    "scripted": "脚本回放（演示）",
    "sense_voice": "本地 SenseVoice",
    "local_whisper": "本地 Whisper",
    "openai": "OpenAI 接口",
}

# ------------------------------------------------------------------ 运行态

class ConfirmGate:
    """确认闸门：每个请求有自己的 ID 与事件；无人回答按拒绝处理（失败关闭）。

    共享一个 Event + 一个 answer 会让「一次点击」批准两个请求，也会让
    针对旧请求的迟到回答批准新请求。这里按 request id 隔离，且只接受首答。
    """

    def __init__(self, bus: ui.StatusBus, timeout: float) -> None:
        self.bus = bus
        self.timeout = timeout
        self._lock = threading.Lock()
        self._waiters: dict[str, threading.Event] = {}
        self._answers: dict[str, bool] = {}
        self._current = ""

    def ask(self, name: str, args: dict[str, object], verdict: core.Verdict) -> bool:
        request_id = secrets.token_urlsafe(6)
        event = threading.Event()
        with self._lock:
            self._waiters[request_id] = event
            self._current = request_id
        self.bus.emit("confirm", "需要确认", verdict.reason, request=request_id)
        if sys.stdin is not None and sys.stdin.isatty():
            threading.Thread(target=self._read_terminal, args=(request_id,), daemon=True).start()
        answered = event.wait(self.timeout)
        with self._lock:
            allow = bool(self._answers.pop(request_id, False)) if answered else False
            self._waiters.pop(request_id, None)
            if self._current == request_id:
                self._current = ""
        if not answered:
            self.bus.emit("blocked", "确认超时", "按拒绝处理")
        return allow

    def resolve(self, allow: bool, request_id: str = "") -> bool:
        """回答一个**仍在等待**的请求。过期 ID、重复回答都返回 False。"""
        with self._lock:
            target = request_id or self._current
            event = self._waiters.get(target)
            if event is None or target in self._answers:
                return False
            self._answers[target] = bool(allow)
            event.set()
            return True

    def _read_terminal(self, request_id: str) -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        self.resolve(line.strip().lower() in ("y", "yes", "确认"), request_id)


class Runtime:
    """一次运行的全部可变状态：配置、执行器、最近记录。设置改动后就地换掉执行器。"""

    def __init__(self, cfg: Config, bus: ui.StatusBus) -> None:
        self.bus = bus
        self.cfg = cfg
        self.gate = ConfirmGate(bus, float(cfg.get("workspace.confirm_timeout_s", 8)))
        self.executor = self._build()
        self.recent: deque[dict[str, object]] = deque(maxlen=8)
        self.started = time.time()

    def _build(self, lock: threading.Lock | None = None) -> core.Executor:
        executor = core.Executor(cfg=self.cfg, on_event=self.bus, confirm=self.gate.ask)
        if lock is not None:
            # 执行锁必须跨配置重载长寿命：换一把锁等于允许两套写操作并行。
            executor._lock = lock
        return executor

    def dispatch(self, text: str, context: dict[str, object] | None = None) -> core.DispatchResult:
        return core.dispatch(text, executor=self.executor, cfg=self.cfg,
                             context=context if context is not None else _context())

    def record(self, text: str, result: core.DispatchResult) -> None:
        self.recent.appendleft({
            "ts": time.strftime("%H:%M:%S"), "text": text,
            "reply": result.reply or NO_ACTION,
            "route": result.route, "ms": round(result.latency_ms, 1),
        })

    def reload(self, new_cfg: Config) -> None:
        lock = self.executor._lock
        self.cfg = new_cfg
        tools.configure(new_cfg)
        self.gate.timeout = float(new_cfg.get("workspace.confirm_timeout_s", 8))
        self.executor = self._build(lock=lock)


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
    backend = str(cfg.get("stt.backend", ""))
    return {
        "version": __version__,
        "enrolled": bool(lock and lock.profiles),
        "threshold": getattr(lock, "threshold", None),
        "role": cfg.get("workspace.role"),
        "dry_run": cfg.flag("workspace.dry_run"),
        "llm": cfg.flag("llm.enabled"),
        "stt": STT_LABELS.get(backend, backend or "—"),
        "confirm_timeout_s": float(cfg.get("workspace.confirm_timeout_s", 8)),
        "uptime_s": round(time.time() - runtime.started),
    }


def _enroll_now(cfg: Config, lock: dsp.SpeakerLock, seconds: float,
                done: threading.Event | None = None) -> dict[str, object]:
    """从麦克风录一段并写盘。失败也要给出人能看懂的原因。

    先把新声纹算在副本上，保存成功后才提交到运行态；保存失败不能出现
    「内存里已经是新身份、重启后又变回旧身份」的分裂。
    """
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 10:
        return {"ok": False, "error": "录音时长需在 1~10 秒之间"}
    try:
        samples = _record(float(seconds))
    except Exception as exc:                       # 无麦克风/无权限
        return {"ok": False, "error": f"录音失败：{exc}"}
    if not samples:
        return {"ok": False, "error": "没有采集到音频"}
    previous = dict(lock.profiles)
    try:
        consistency = lock.enroll(samples)         # type: ignore[arg-type]
        lock.save(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    except Exception as exc:
        lock.profiles = previous
        return {"ok": False, "error": f"声纹保存失败：{exc}"}
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

    def undo() -> dict[str, object]:
        try:
            new_cfg = settings.restore(settings.target_of(runtime.cfg))
        except FileNotFoundError as exc:            # 没备份：说清楚，别假装成功
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"撤销失败：{exc}"}
        runtime.reload(new_cfg)
        return {"ok": True, "groups": settings.describe(runtime.cfg)}

    return ui.UiContext(
        bus=runtime.bus,
        pill_file=runtime.cfg.path("ui.pill_file", "web/pill.html"),
        on_command=lambda text: _run_text(runtime, text),
        settings_get=lambda: settings.describe(runtime.cfg),
        settings_set=save,
        settings_undo=undo,
        settings_can_undo=lambda: settings.can_undo(runtime.cfg),
        status=lambda: _status(runtime, lock),
        on_enroll=(lambda seconds: _enroll_now(runtime.cfg, lock, seconds, enrolled)) if lock else None,
        on_confirm=runtime.gate.resolve,
        history=lambda: list(runtime.recent),
    )


def _run_text(runtime: Runtime, text: str) -> str:
    result = runtime.dispatch(text)
    runtime.record(text, result)
    return result.reply or NO_ACTION


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
    print(f"[{result.route}] {result.reply or NO_ACTION}  {result.latency_ms:.1f}ms")
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
    hotwords = audio.refresh_hotwords(cfg) if audio.supports_hotwords(cfg) else []
    _, threshold, kind = audio.make_embedder(cfg)
    print(f"VoxOS 已启动：声纹后端={kind} 阈值={threshold} "
          f"识别={_hotword_note(cfg, hotwords)} 胶囊={url}")
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
    """三段式：采集线程 → 识别线程 → 派发主循环。

    旧实现是单线程 `for utterance in pipeline.run(source)`：模型、确认、
    工具任何一个慢下来，麦克风就停止读取，设备缓冲区溢出、句子被吞。
    现在采集与识别各有一个有界队列，慢的只有派发，不会回头卡住设备。
    """
    hotwords = audio.refresh_hotwords(cfg) if audio.supports_hotwords(cfg) else []
    pipeline = audio.AudioPipeline(
        cfg=cfg, stt=audio.make_stt(cfg, hotwords), lock=lock,
        vad=audio.make_vad(cfg), on_event=runtime.bus,
        echo=dsp.EchoGate(decay_ms=float(cfg.get("audio.echo_decay_ms", 300))))
    source = audio.SelfHealingSource(lambda: audio.MicSource(int(cfg.get("audio.chunk_size", 512))),
                                     on_event=runtime.bus, on_reset=pipeline.reset)
    _context()                             # 预热：把首次线程启动的开销挪到进循环之前
    runtime.bus.emit("idle", "待机", "")
    chunks: queue.Queue[object] = queue.Queue(maxsize=64)
    utterances: queue.Queue[object] = queue.Queue(maxsize=4)
    stop = threading.Event()
    threading.Thread(target=_capture_loop, args=(source, chunks, stop), daemon=True).start()
    threading.Thread(target=_recognize_loop, args=(pipeline, chunks, utterances, stop), daemon=True).start()
    try:
        return _dispatch_loop(runtime, pipeline, utterances, sounds, stop)
    finally:
        stop.set()


def _capture_loop(source: object, chunks: queue.Queue[object], stop: threading.Event) -> None:
    """只做采集：队列满时丢最旧的块，绝不阻塞设备读取。"""
    try:
        for chunk in source:  # type: ignore[union-attr]
            if stop.is_set():
                break
            try:
                chunks.put_nowait(chunk)
            except queue.Full:
                try:
                    chunks.get_nowait()
                    chunks.put_nowait(chunk)
                except (queue.Empty, queue.Full):
                    pass
    except Exception as exc:
        _put_latest(chunks, exc)
    finally:
        _put_latest(chunks, None)


def _recognize_loop(pipeline: audio.AudioPipeline, chunks: queue.Queue[object],
                    utterances: queue.Queue[object], stop: threading.Event) -> None:
    """只做 VAD/声纹/识别：把整句交给派发线程，自己继续处理下一块音频。"""
    def feed():
        while True:
            item = chunks.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    try:
        for utterance in pipeline.run(feed()):
            while not stop.is_set():
                try:
                    utterances.put(utterance, timeout=0.2)
                    break
                except queue.Full:
                    continue
    except Exception as exc:
        _put_latest(utterances, exc)
    finally:
        _put_latest(utterances, None)


def _put_latest(channel: queue.Queue[object], item: object) -> None:
    try:
        channel.put_nowait(item)
    except queue.Full:
        try:
            channel.get_nowait()
        except queue.Empty:
            pass
        try:
            channel.put_nowait(item)
        except queue.Full:
            pass


def _dispatch_loop(runtime: Runtime, pipeline: audio.AudioPipeline,
                   utterances: queue.Queue[object], sounds: dict[str, Path],
                   stop: threading.Event) -> int:
    while True:
        item = utterances.get()
        if item is None:
            return 0
        if isinstance(item, Exception):
            runtime.bus.emit("blocked", "音频处理异常", str(item))
            return 1
        utterance = item
        if not utterance.accepted:  # type: ignore[attr-defined]
            continue
        if pipeline.echo.is_interrupt(utterance.text) and stop_speaking():
            runtime.bus.emit("idle", "已停止", utterance.text)
            continue
        # 读 runtime.cfg 而不是启动时的快照：设置页改了开关，下一句就该生效。
        if runtime.cfg.flag("ui.earcons", True):
            ui.play(sounds["listen"])
        runtime.bus.emit("executing", "处理中…", utterance.text)
        result = runtime.dispatch(utterance.text)
        runtime.record(utterance.text, result)
        runtime.bus.emit("done", "完成", result.reply)
        if result.reply and runtime.cfg.flag("ui.speak", True):
            _speak_async(result.reply, pipeline)


def _speak_async(text: str, pipeline: audio.AudioPipeline) -> None:
    """TTS 放到后台：说 3 秒不能把麦克风采集卡住 3 秒。"""
    threading.Thread(target=_speak, args=(text, pipeline), daemon=True).start()


_TTS: dict[str, object] = {"proc": None, "owner": None}


def stop_speaking() -> bool:
    """打断：把正在念的 say 杀掉。没有在念就返回 False。"""
    proc = _TTS.get("proc")
    if not isinstance(proc, subprocess.Popen) or proc.poll() is not None:
        return False
    proc.terminate()
    return True


def _speak(text: str, pipeline: audio.AudioPipeline) -> None:
    """播报。只有**当前所有者**能在结束时清理共享状态。

    旧实现里任何一条旧线程的 finally 都会把 `_TTS["proc"]` 清成 None，
    并把回声门提前放行——新的播报还在响，系统却以为它结束了。
    """
    if not shutil.which("say"):
        return
    owner = object()
    _TTS["owner"] = owner
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
        if _TTS.get("owner") is owner:
            _TTS["proc"] = None
            _TTS["owner"] = None
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
        ("识别后端", _hotword_note(cfg, [])),
        ("dry_run", str(cfg.flag("workspace.dry_run"))),
        ("角色", str(cfg.get("workspace.role"))),
    ]
    for name, value in checks:
        print(f"{name:<12} {value}")
    return 0


def _hotword_note(cfg: Config, words: list[str]) -> str:
    """热词表对当前后端到底有没有用——没用就直说，别报一个好看的数字。"""
    backend = str(cfg.get("stt.backend", ""))
    label = STT_LABELS.get(backend, backend or "未设置")
    if audio.supports_hotwords(cfg):
        return f"{label}（热词表 {len(words)} 条生效）"
    return f"{label}（无热词接口，热词表不生效）"


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
