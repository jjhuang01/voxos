"""VoxOS 命令行入口：text / demo / run / enroll / serve / pill / tools / doctor。

设计约束：CLI 只做编排，不做业务；每个函数不超过 40 行。
可选信息（前台窗口、TTS 播报）一律不阻塞音频主循环 —— 见 CODING_PHILOSOPHY.md §5.5。
"""

from __future__ import annotations

import argparse
import errno
import math
import os
import queue
import secrets
import shutil
import signal
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
# 文案只写在 core 里一处，这里只是别名——两处各写一份必然有一天对不上。
NO_ACTION = core.NO_ACTION

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
    return ui.StatusBus(cfg.data_root / ".voxos" / "status.jsonl")


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
        "listening": not _MUTED.is_set(),
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
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 15:
        return {"ok": False, "error": "录音时长需在 1~15 秒之间"}
    try:
        samples = _record_segments(cfg, float(seconds), cfg.get("audio.input_device", ""))
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
    warning = "" if len(samples) >= 2 else \
        "只切出 1 句：模板不稳，建议再说 2 句（句间停半秒），否则可能被自己拒之门外"
    return {"ok": True, "consistency": round(float(consistency), 3),
            "samples": len(samples), "warning": warning}


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
        pill_file=runtime.cfg.res_path("ui.pill_file", "web/pill.html"),
        on_command=lambda text: _run_text(runtime, text),
        settings_get=lambda: settings.describe(runtime.cfg),
        settings_set=save,
        settings_undo=undo,
        settings_can_undo=lambda: settings.can_undo(runtime.cfg),
        status=lambda: _status(runtime, lock),
        on_enroll=(lambda seconds: _enroll_now(runtime.cfg, lock, seconds, enrolled)) if lock else None,
        on_confirm=runtime.gate.resolve,
        history=lambda: list(runtime.recent),
        on_listen=lambda on: _set_listening(runtime, on),
    )


def _run_text(runtime: Runtime, text: str) -> str:
    result = runtime.dispatch(text)
    runtime.record(text, result)
    return result.reply or NO_ACTION


def _serve(runtime: Runtime, lock: dsp.SpeakerLock | None = None,
           enrolled: threading.Event | None = None) -> str:
    cfg = runtime.cfg
    host, port = str(cfg.get("ui.host", "127.0.0.1")), int(cfg.get("ui.port", 8765))
    try:
        ui.serve(_ui_context(runtime, lock, enrolled), host, port, block=False)
    except OSError as exc:
        # 端口被别的程序占用时，原始 traceback 只会让人以为 VoxOS 坏了。
        # 说清楚是谁占的、改哪个配置，比堆栈有用。
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            holder = _port_holder(port)
            raise SystemExit(
                f"端口 {port} 被占用了{'（' + holder + '）' if holder else ''}。\n"
                f"两种办法：关掉占用它的程序，或把 ~/.voxos/settings.toml 里的 ui.port 改成别的值。"
            ) from exc
        raise
    return f"http://{host}:{port}/"


def _port_holder(port: int) -> str:
    """谁占着这个端口。查不到就返回空串——这条信息是锦上添花，不能反过来报错。"""
    try:
        done = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "cn"],
                              capture_output=True, text=True, timeout=3)
        names = [line[1:] for line in done.stdout.splitlines() if line.startswith("c")]
        return "、".join(dict.fromkeys(names))
    except Exception:
        return ""


# ------------------------------------------------------------------ 子命令

def cmd_text(args: argparse.Namespace, cfg: Config) -> int:
    runtime = Runtime(cfg, _bus(cfg))
    result = runtime.dispatch(args.text)
    print(f"[{result.route}] {result.reply or NO_ACTION}  {result.latency_ms:.1f}ms")
    return 0 if result.route != "ignore" else 2


def cmd_demo(args: argparse.Namespace, cfg: Config) -> int:
    if not args.live:                       # 默认沙箱：不污染真实日志目录
        demo = cfg.data_root / ".voxos" / "demo"
        cfg = cfg.with_overrides(**{
            "workspace.daily_log_dir": str(demo / "logs"),
            "workspace.default_code_dir": str(demo / "projects"),
            "workspace.backup_dir": str(demo / "backups"),
        })
        tools.configure(cfg)
        print(f"沙箱模式：日志写入 {demo / 'logs'}（--live 可写入真实目录）\n")
    runtime = Runtime(cfg, _bus(cfg))
    root = cfg.data_root / ".voxos" / "corpus"
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
    pid_file = _claim_pid(cfg)
    if pid_file is None:
        print("已经在跑了。要重启先 `voxos stop`（双开会让两个进程抢同一个端口）")
        return 0
    try:
        return _run_forever(cfg)
    finally:
        pid_file.unlink(missing_ok=True)


def _run_forever(cfg: Config) -> int:
    runtime = Runtime(cfg, _bus(cfg))
    lock = _lock(cfg)
    enrolled = threading.Event()
    if lock.profiles:
        enrolled.set()
    url = _serve(runtime, lock, enrolled)
    sounds = ui.ensure_earcons(cfg.data_root / ".voxos" / "sounds")
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


def _pid_file(cfg: Config) -> Path:
    return cfg.data_root / ".voxos" / "voxos.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _claim_pid(cfg: Config) -> Path | None:
    """占位：写 pid 文件；已有实例在跑就返回 None。

    .app 双击两次、或 App 和终端各起一个，会撞同一个端口。与其让第二个进程
    抛一个 EADDRINUSE 堆栈，不如在这里说人话。
    """
    path = _pid_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            other = int(path.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            other = 0
        if other and other != os.getpid() and _alive(other):
            return None
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def cmd_stop(_: argparse.Namespace, cfg: Config) -> int:
    """停掉 `voxos run`。App 里没有终端窗口，退出必须有个正经入口。"""
    path = _pid_file(cfg)
    if not path.exists():
        print("没有在跑（找不到 pid 文件）")
        return 0
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or 0)
    except ValueError:
        pid = 0
    if pid <= 0 or not _alive(pid):
        path.unlink(missing_ok=True)
        print("没有在跑（pid 文件是上次崩溃留下的，已清理）")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _alive(pid):
            path.unlink(missing_ok=True)
            print(f"已停止（pid {pid}）")
            return 0
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    print(f"已强制停止（pid {pid}）")
    return 0


def cmd_port(_: argparse.Namespace, cfg: Config) -> int:
    """只打印端口。App 启动器拿它判断「服务起来了没」——写死 8765 会在端口
    被别的程序占用时永远等不到，然后弹一个「没能启动」的框，而真实原因在日志里。
    """
    print(int(cfg.get("ui.port", 8765)))
    return 0


# 持续监听的总开关。关掉之后**仍然继续读设备**——只是不往识别队列里放：
# 停止读会让 CoreAudio 的缓冲堆起来，恢复时一次性吐出几十秒旧音频。
_MUTED = threading.Event()
_RESET_HOOK: dict[str, object] = {"reset": None}


def _set_listening(runtime: Runtime, on: bool) -> dict[str, object]:
    """像智能音箱上的静音键：一按就停，再按就继续。"""
    if on:
        reset = _RESET_HOOK.get("reset")
        if callable(reset):
            reset()                       # 丢掉暂停前攒的半句话
        _MUTED.clear()
        runtime.bus.emit("idle", "待机", "")
    else:
        _MUTED.set()
        runtime.bus.emit("paused", "已暂停监听", "点胶囊 → 状态 → 继续监听")
    return {"ok": True, "listening": on}


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
    chunk = int(cfg.get("audio.chunk_size", 512))
    device = cfg.get("audio.input_device", "")
    source = audio.SelfHealingSource(lambda: audio.MicSource(chunk, device=device),
                                     on_event=runtime.bus, on_reset=pipeline.reset)
    _context()                             # 预热：把首次线程启动的开销挪到进循环之前
    _RESET_HOOK["reset"] = pipeline.reset
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
    """只做采集：队列满时丢最旧的块，绝不阻塞设备读取。

    暂停时继续读、但不入队——见 `_MUTED` 的说明。
    """
    try:
        for chunk in source:  # type: ignore[union-attr]
            if stop.is_set():
                break
            if _MUTED.is_set():
                continue
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
        state, word = _RESULT_WORD.get(result.route, ("done", "完成"))
        runtime.bus.emit(state, word, result.reply)
        # 听不懂的时候只显示、不朗读：常驻监听会把屋里所有闲聊都收进来，
        # 每句都念一遍「还没学会这句」，很快就变成噪音了。
        if result.reply and result.route != "ignore" and runtime.cfg.flag("ui.speak", True):
            _speak_async(result.reply, pipeline)


# 胶囊上那行字要说清「它到底做了什么」：一律写「完成」的话，
# 听不懂和真做成了长得一模一样——用户看到的就是「没反应」。
_RESULT_WORD: dict[str, tuple[str, str]] = {
    "ignore": ("miss", "没听懂"),
    "chat": ("done", "回话"),
}


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
        samples = _record_segments(cfg, args.record, cfg.get("audio.input_device", ""))
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


def _record_segments(cfg: Config, seconds: float, device: object = None) -> list[object]:
    """录一段时间，按 VAD 切成「多句」——声纹模板必须多句取均值才稳。

    实测（真麦克风 + CAM++，阈值量纲为余弦相似度）：
    - 只录 1 句 → 同一个人的另一句话只有 0.75，而默认阈值 0.82，用户录完就被自己拒了；
    - 把**一次**录音切成 2~3 段 → 同人 0.70~0.72、异人 0.66~0.72，间隔 0.05 以内，分不开；
    - 录 3 句取均值 → 同人 0.79、异人最高 0.72，间隔 0.071，这才有可用余量。
    所以切段必须按「句」切，不能按时间切。
    """
    import numpy as np

    print(f"请说 2~3 句话，录音 {seconds:.0f} 秒（句间停半秒）……")
    vad = audio.make_vad(cfg)
    detector = dsp.EndpointDetector(
        silence_ms=float(cfg.get("audio.silence_duration_ms", 800)),
        max_ms=float(cfg.get("audio.max_utterance_ms", 12000)),
        min_speech_ms=float(cfg.get("audio.min_speech_ms", 200)),
    )
    segments: list[object] = []
    buffer: list[object] = []
    source = audio.MicSource(device=device)
    with source:
        start = time.time()
        for chunk in source:
            state = detector.push(bool(vad.is_speech(chunk)), chunk.size)  # type: ignore[attr-defined]
            if state != "idle":
                buffer.append(chunk)
            if state in ("candidate", "force_end"):
                segments.append(np.concatenate(buffer))
                buffer.clear()
                detector.reset()
            if time.time() - start >= seconds:
                break
    if buffer:
        segments.append(np.concatenate(buffer))
    usable = [s for s in segments if s.size >= 1600]
    return usable


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


def cmd_models(args: argparse.Namespace, cfg: Config) -> int:
    """下载模型。装好的包只有 `voxos` 命令，所以这条必须是子命令而不是脚本。"""
    from .models import default_dir, fetch

    target = Path(args.dir) if args.dir else default_dir()
    return fetch(target, proxy=args.proxy, force=args.force, with_asr=not args.no_asr)


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
        ("输入设备", _mic_note(cfg)),
        ("VAD 后端", _vad_note(cfg)),
        ("资源根", str(cfg.root)),
        ("数据根", str(cfg.data_root)),
        ("配置文件", _config_note(cfg)),
        ("dry_run", str(cfg.flag("workspace.dry_run"))),
        ("角色", str(cfg.get("workspace.role"))),
    ]
    for name, value in checks:
        print(f"{name:<12} {value}")
    return 0


def _config_note(cfg: Config) -> str:
    """配置在哪、能不能写、会不会被升级冲掉。

    这一行是 0.1.12 那个 bug 的「可观测性」补丁：当时 doctor 只报依赖和模型，
    看不出自己改的是 `<venv>/share/voxos` 里那份只读资源——于是 268MB 模型配好的
    `stt.backend` 在 `pipx upgrade` 之后悄悄变回出厂值，没人会怀疑到这儿。
    所以这里必须报**路径本身**，而不是一个「正常」。
    """
    target = cfg.source
    if target is None:
        return "未指定（内置默认值，改不了也存不住）"
    resolved = Path(target).expanduser().resolve()
    if Path(sys.prefix).resolve() in resolved.parents:
        return f"{resolved}（在安装目录里，升级会被整份替换）"
    probe = resolved if resolved.exists() else resolved.parent
    writable = os.access(probe, os.W_OK)
    return f"{resolved}（{'可写' if writable else '只读，改配置会失败'}）"


def _hotword_note(cfg: Config, words: list[str]) -> str:
    """热词表对当前后端到底有没有用——没用就直说，别报一个好看的数字。"""
    backend = str(cfg.get("stt.backend", ""))
    label = STT_LABELS.get(backend, backend or "未设置")
    if audio.supports_hotwords(cfg):
        return f"{label}（热词表 {len(words)} 条生效）"
    return f"{label}（无热词接口，热词表不生效）"


def _vad_note(cfg: Config) -> str:
    """报出实际用的 VAD。少了 onnxruntime 会静默降级成能量 VAD：
    能跑，但一声咳嗽也算说话——装完才看得出来，所以 doctor 必须报。
    """
    try:
        return type(audio.make_vad(cfg)).__name__
    except Exception as exc:
        return f"不可用（{exc}）"


def _mic_note(cfg: Config) -> str:
    """报出实际用的输入设备，并采 0.5 秒看有没有信号。

    「设备能打开但收到全零」是本机踩过的坑：系统默认输入是一个没插东西的
    音频接口，录得到帧、峰值恒为 0.0000——所有检查都是绿的，就是听不见人说话。
    所以这一行必须带电平；只报设备名等于什么都没说。
    """
    try:
        name, peak = audio.probe_mic(0.5, cfg.get("audio.input_device", ""))
    except Exception as exc:
        return f"不可用（{exc}）"
    hint = "  ← 几乎无声：换个设备，或检查静音键/输入音量" if peak < 1e-4 else ""
    return f"{name}  峰值 {peak:.4f}{hint}"


def _version(module: str) -> str:
    try:
        return __import__(module).__version__  # type: ignore[no-any-return]
    except Exception:
        return "缺失"


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voxos", description="VoxOS：只听你一个人说话的语音 Agent")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/settings.toml）")
    parser.add_argument("--version", action="version", version=f"voxos {__version__}",
                        help="打印版本后退出")
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
    enroll.add_argument("--record", type=float, default=10.0,
                        help="麦克风录制秒数（期间说 2~3 句，句间停半秒）")
    enroll.add_argument("--mode", choices=["normal", "mask"], default="normal")
    enroll.set_defaults(func=cmd_enroll)

    serve = sub.add_parser("serve", help="只启动胶囊与设置面板")
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("pill", help="无边框胶囊窗口").set_defaults(func=cmd_pill)

    _add_models(sub)
    sub.add_parser("stop", help="停掉在跑的 run").set_defaults(func=cmd_stop)
    sub.add_parser("port", help="打印界面端口").set_defaults(func=cmd_port)
    sub.add_parser("tools", help="列出已注册工具").set_defaults(func=cmd_tools)
    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)
    return parser


def _add_models(sub: argparse._SubParsersAction) -> None:
    """模型下载的参数单独放，build_parser 已经顶到行预算了。"""
    models = sub.add_parser("models", help="下载声纹/VAD/识别模型")
    models.add_argument("--proxy", default=None, help="下载代理，如 http://127.0.0.1:7890")
    models.add_argument("--force", action="store_true", help="即使已存在也重下")
    models.add_argument("--no-asr", action="store_true", help="跳过 239MB 识别模型")
    models.add_argument("--dir", default=None, help="模型目录（默认数据目录下的 .voxos/models）")
    models.set_defaults(func=cmd_models)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load(args.config)
    _bootstrap(cfg)
    return int(args.func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
