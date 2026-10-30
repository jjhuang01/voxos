"""VoxOS 命令行入口：text / demo / run / enroll / serve / pill / tools / doctor。

设计约束：CLI 只做编排，不做业务；每个函数不超过 40 行。
可选信息（前台窗口、TTS 播报）一律不阻塞音频主循环 —— 见 CODING_PHILOSOPHY.md §5.5。
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import webbrowser
from collections import deque
from pathlib import Path

from . import (__version__, core, cue, cuebox, dialogue, enroll, lessons, prompts, sandbox,
               scheduler, settings, timeline, tools, ui)
# audio / corpus / dsp 不在顶层导入：它们把 numpy（实测 58ms）一起拖进来，
# 而 `voxos text`、`voxos schedule`、`--version` 一条都用不到。谁用谁在里面导。
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
        # 提示的去重与限流状态跟着 Runtime 走，不跟着配置走：
        # 存成模块级全局的话，一次「保存设置」就会把它换掉，等于给刷屏开了个后门。
        self.cues = cue.CueGuard(cfg)
        self.recent: deque[dict[str, object]] = deque(maxlen=8)
        self.started = time.time()
        self._last_text = ""           # 上一句说的是什么（给「接续句」当上下文）

    def _build(self, lock: threading.Lock | None = None) -> core.Executor:
        executor = core.Executor(cfg=self.cfg, on_event=self.bus, confirm=self.gate.ask)
        if lock is not None:
            # 执行锁必须跨配置重载长寿命：换一把锁等于允许两套写操作并行。
            executor._lock = lock
        return executor

    def dispatch(self, text: str, context: dict[str, object] | None = None,
                 mode: str = "assistant",
                 on_sentence: Callable[[str], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> core.DispatchResult:
        """派发一句话。**带上上一句**——端点判定会在停顿处切句，切完第二句就孤立了。

        实测（端点消融）：停顿 600ms 且结尾不是未完词就会被切断，而人说话中途
        停 600~800ms 很常见。切句本身不可怕，可怕的是切完之后「然后发个消息」
        没有上文——快通道匹配不上，LLM 也不知道要发给谁。行业做法是两级端点
        加语义判停模型（LiveKit 的 min/max_endpointing_delay、pipecat 的
        smart-turn），模型我们暂时缺，先补一层上下文，让第二句至少能被读懂。
        """
        if self._is_withdrawal(text):          # 先判撤回：这句不需要任何上下文
            return self._withdraw(text)
        ctx: dict[str, object] = dict(context) if context is not None else _context()
        if self._last_text:
            ctx["previous"] = self._last_text
        return core.dispatch(text, executor=self.executor, cfg=self.cfg, context=ctx, mode=mode,
                             on_sentence=on_sentence, should_stop=should_stop)

    def _is_withdrawal(self, text: str) -> bool:
        """整句就是「算了 / 取消 / 不用了」，而且此刻没有播报可停。

        有播报时由音频层直接停（`stop_speaking`），轮不到这里。能走到这里的
        只有「答案早就念完了，人现在改主意」——以前这句会掉进「还没学会这句」
        加一整段能力清单：用户想收回，收到的是一篇功能介绍。

        先卡长度再导 dsp：dsp 会把 numpy 一起拖进来（实测 59ms），而
        「算了」这种短句才可能是撤回，长句一律直接放行，不为它付这笔钱。
        """
        if len(text) > 12 or _TTS.get("owner") is not None:
            return False
        from . import dsp  # 局部导入：见上

        return dsp.bare_interrupt(text)

    def _withdraw(self, text: str) -> core.DispatchResult:
        """撤回：挂着的确认当作**拒绝**（失败关闭），否则就应一声。

        路由用 `chat` 而不是 `ignore`：`ignore` 那一支是不朗读的（常驻监听
        下屋里闲聊太多，逐句念「还没学会这句」会变噪音），但「好」是回话，
        用户需要听见一声确认；胶囊上也会显示「回话」而不是「没听懂」。
        """
        denied = self.gate.resolve(False)
        return core.DispatchResult(route="chat", text=text,
                                   reply="好，不做了" if denied else "好")

    def record(self, text: str, result: core.DispatchResult,
               utterance: object = None) -> None:
        """记一次回合：内存里的最近列表（给界面）+ 持久化时间线（给事后查）。

        两份都要。内存那份是「刚才发生了什么」的即时视图，进程一退就没了；
        时间线是「昨天为什么没反应」的，必须落盘——用户问的正是后者。
        """
        _REJECT["voice"] = 0                   # 成功一次就重置连续拒绝计数
        self._last_text = text                 # 下一句派发时当「上一句」用
        self.recent.appendleft({
            "ts": time.strftime("%H:%M:%S"), "text": text,
            "reply": result.reply or NO_ACTION,
            "route": result.route, "ms": round(result.latency_ms, 1),
        })
        timeline.record(_turn_of(text, result, utterance), cfg=self.cfg)

    def note_dropped(self, utterance: object) -> None:
        """被音频层丢掉的句子也要留一条。

        **这是最重要的一条记录，因为它以前完全不存在**：`_consume` 里一句
        `if not utterance.accepted: continue` 把原因扔了，于是「我说了十句一句
        都没执行」在两份日志里都是空白，用户只能猜「是不是软件坏了」。
        """
        timeline.record(timeline.Turn(
            outcome="dropped",
            text=str(getattr(utterance, "text", "") or ""),
            score=float(getattr(utterance, "score", 0.0) or 0.0),
            audio_ms=float(getattr(utterance, "audio_ms", 0.0) or 0.0),
            reason=str(getattr(utterance, "reason", "")),
        ), cfg=self.cfg)

    def reload(self, new_cfg: Config) -> None:
        lock = self.executor._lock
        self.cfg = new_cfg
        tools.configure(new_cfg)
        self.gate.timeout = float(new_cfg.get("workspace.confirm_timeout_s", 8))
        self.executor = self._build(lock=lock)
        self.cues.cfg = new_cfg          # 只换门限，已发的记录留着（见 __init__ 说明）


_FAIL_WORDS = ("失败", "没能", "出错", "拒绝", "不允许", "不在允许")


def _turn_of(text: str, result: core.DispatchResult, utterance: object = None) -> timeline.Turn:
    """把派发结果翻译成一个回合。四种结局必须分清——混在一起就看不出问题在哪。

    「没听懂」和「执行失败」在胶囊上都只显示一行字，但它们要修的地方完全不同：
    前者是快通道正则的盲区，后者是工具或环境的问题。

    判「闲聊」看的是 `tool` 空不空，不是 `route`：闲聊和带工具的回答**都走
    `route="llm"`**，只看 route 会把「随便聊两句」记成「执行了某个动作」。
    """
    if result.route == "ignore":
        outcome = "missed"
    elif not result.tool and result.route in ("chat", "llm"):
        outcome = "chatting"
    elif any(word in result.reply for word in _FAIL_WORDS):
        outcome = "failed"
    else:
        outcome = "acted"
    return timeline.Turn(
        outcome=outcome, text=text,
        score=float(getattr(utterance, "score", 0.0) or 0.0),
        audio_ms=float(getattr(utterance, "audio_ms", 0.0) or 0.0),
        route=result.route, tool=result.tool, args=dict(result.args or {}),
        ok=outcome != "failed", reply=result.reply, ms=result.latency_ms,
    )


_REJECT = {"voice": 0, "hinted": False}


def _hint_rejection(runtime: Runtime, utterance: object) -> None:
    """连续被声纹拒才提示一次。

    单次拒绝是**正常行为**——屋里别人说话就该被拒，每句都弹提示会把胶囊变成
    报警器。但连续 3 句都拒，多半是麦克风换了、或者声纹只录了 1 句（同人换句话
    就可能掉到阈值以下），这时不提示，用户只能看到「一片安静」。
    """
    if "声纹" not in str(getattr(utterance, "reason", "")):
        _REJECT["voice"] = 0
        return
    _REJECT["voice"] += 1
    if _REJECT["voice"] >= 3 and not _REJECT["hinted"]:
        _REJECT["hinted"] = True
        runtime.bus.emit("warn", "声纹连续拒绝",
                         "连续 3 句没通过声纹，可能是麦克风变了：voxos enroll（10 秒说 2~3 句）")


# ------------------------------------------------------------------ 过期的话

# 派发是**单线程顺序**的：一句话在跑（模型、确认、工具），后面说的话只能排队。
# 实测一次 LLM 回合 4.8 秒（`voxos text` 量的），排过一整个回合，那句就已经是
# 「过去的意思」了——十秒前说的「打开微信」现在才执行，用户早忘了自己说过。
# 所以超过这个时长就跳过并说明原因。留的是一个完整回合加余量。
_STALE_S = 10.0


def _stale_age(utterance: object) -> float:
    """这句话在队列里等了多久（秒）。`at` 为 0 表示不知道，不判过期。"""
    at = float(getattr(utterance, "at", 0.0) or 0.0)
    return 0.0 if at <= 0 else time.monotonic() - at


def _stale_note(utterance: object, age: float) -> types.SimpleNamespace:
    """把「排队排到过期」写成一条丢弃记录，好让它进时间线。"""
    return types.SimpleNamespace(
        text=str(getattr(utterance, "text", "") or ""),
        score=float(getattr(utterance, "score", 0.0) or 0.0),
        audio_ms=float(getattr(utterance, "audio_ms", 0.0) or 0.0),
        reason=f"排队 {age:.0f} 秒，已过期（上一条还在跑）",
    )


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
    from . import audio, dsp

    embedder, threshold, _ = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    return lock.load(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))


def _status(runtime: Runtime, lock: dsp.SpeakerLock | None) -> dict[str, object]:
    cfg = runtime.cfg
    backend = str(cfg.get("stt.backend", ""))
    ready, why = core.llm_ready(cfg)
    return {
        "version": __version__,
        "enrolled": bool(lock and lock.profiles),
        "threshold": getattr(lock, "threshold", None),
        # 只报「已录入」等于替档案撒谎：录 1 句和录 3 句在界面上长得一模一样。
        # 这里把段数、留出分数、录的时候用的设备一起发出去，界面才有话可说。
        "voiceprint": _voiceprint_state(cfg, lock),
        "enroll_script": list(enroll.SCRIPT),     # 界面上照着念的句子，与终端同一份
        "role": cfg.get("workspace.role"),
        "dry_run": cfg.flag("workspace.dry_run"),
        "listening": not _MUTED.is_set(),
        # 对话模式跟着状态一起发：胶囊不用为一个开关单开一次请求，
        # 在设置页改了也会在下一次轮询时自动同步回来。
        "dialogue": dialogue.mode(cfg),
        "llm": cfg.flag("llm.enabled"),
        # 只报「开没开」是不够的：开了但缺 Key 和真能回话，界面上长得一模一样。
        "llm_model": cfg.get("llm.model") if ready else "",
        "llm_ready": ready,
        "llm_note": why,
        "stt": STT_LABELS.get(backend, backend or "—"),
        "confirm_timeout_s": float(cfg.get("workspace.confirm_timeout_s", 8)),
        "uptime_s": round(time.time() - runtime.started),
    }


def _enroll_now(cfg: Config, lock: dsp.SpeakerLock, seconds: float,
                done: threading.Event | None = None,
                capture: Callable[[], list[object]] | None = None) -> dict[str, object]:
    """从麦克风录一段并写盘。失败也要给出人能看懂的原因。

    先把新声纹算在副本上，保存成功后才提交到运行态；保存失败不能出现
    「内存里已经是新身份、重启后又变回旧身份」的分裂。
    """
    if capture is None and (not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                            or not 1 <= seconds <= 15):
        return {"ok": False, "error": "录音时长需在 1~15 秒之间"}
    try:
        samples = capture() if capture else _record_segments(
            cfg, float(seconds), cfg.get("audio.input_device", ""))
    except Exception as exc:                       # 无麦克风/无权限/某句两次没读好
        return {"ok": False, "error": f"录音失败：{exc}"}
    if not samples:
        return {"ok": False, "error": "没有采集到音频"}
    previous = dict(lock.profiles)
    holdout = _holdout_score(lock, samples)
    try:
        consistency = lock.enroll(samples)         # type: ignore[arg-type]
    except Exception as exc:
        lock.profiles = previous
        return {"ok": False, "error": f"声纹录入失败：{exc}"}
    # 胶囊这边以前只看「段数 ≥ 2」就说成功：10 秒切 5 段、一致性 0.755、阈值
    # 0.75 也照样弹「录入完成」，用户以为好了，然后每句话都被自己拒之门外。
    warning = _enroll_verdict(samples, float(consistency), lock.threshold, holdout)
    if not warning and len(samples) < 2:
        warning = "只切出 1 句：模板不稳，建议再说 2 句（句间停半秒），否则可能被自己拒之门外"
    failed = _commit_voiceprint(cfg, lock, previous, warning)
    if failed:
        return failed
    if done is not None:
        done.set()
    _save_enroll_meta(cfg, samples, float(consistency), holdout, lock.threshold,
                      device=str(cfg.get("audio.input_device", "")), usable=not warning)
    return {"ok": True, "consistency": round(float(consistency), 3),
            "holdout": None if holdout is None else round(holdout, 3),
            "usable": not warning, "samples": len(samples), "warning": warning}


def _enroll_from_ui(runtime: Runtime, lock: dsp.SpeakerLock,
                    done: threading.Event | None) -> dict[str, object]:
    """胶囊里的录入：和 `voxos enroll` 一样一句一录，期间暂停监听。

    以前这里是「一次录 10 秒再按停顿切段」：逗号处的停顿把一句切成碎片，
    10 秒又常截掉第 3 句，同样三句话留出验证只有 0.18（逐句录 0.94）。
    监听不停的话，念的句子还会被当成指令同时识别一遍。
    """
    was_muted = _MUTED.is_set()
    _MUTED.set()
    try:
        result = _enroll_now(runtime.cfg, lock, 0.0, done,
                             capture=lambda: _ui_samples(runtime, lock))
    finally:
        if not was_muted:
            reset = _RESET_HOOK.get("reset")
            if callable(reset):
                reset()                   # 录入期间攒的半句话不能当指令
            _MUTED.clear()
    print(f"[enroll/ui] {json.dumps(result, ensure_ascii=False)}", flush=True)
    runtime.bus.emit("idle" if result.get("ok") else "warn", "声纹录入",
                     str(result.get("warning") or result.get("error") or "完成"))
    return result


def _ui_samples(runtime: Runtime, lock: dsp.SpeakerLock) -> list[object]:
    """逐句录：每句最多两次，状态栏实时显示该念哪句。"""
    from . import dsp

    device = runtime.cfg.get("audio.input_device", "")
    samples: list[object] = []
    vectors: list[object] = []
    for index, line in enumerate(enroll.SCRIPT, 1):
        why = ""
        for attempt in (1, 2):
            tip = f"请念第 {index}/{len(enroll.SCRIPT)} 句" + ("（重读）" if attempt == 2 else "")
            runtime.bus.emit("listening", tip, line)
            wave, speech_s = _capture_one(runtime.cfg, device)
            why = enroll.check(wave, speech_s) or enroll.outlier(
                lock.embedder(dsp.as_float(wave)), vectors)
            print(f"[enroll/ui] 第 {index} 句第 {attempt} 次：{speech_s:.1f}s "
                  f"峰值 {enroll.peak_of(wave):.3f} {why or '合格'}", flush=True)
            if not why:
                break
        if why:
            raise ValueError(f"第 {index} 句两次都没读好：{why}")
        samples.append(wave)
        vectors.append(lock.embedder(dsp.as_float(wave)))
    return samples


def _commit_voiceprint(cfg: Config, lock: dsp.SpeakerLock, previous: dict[str, object],
                       warning: str) -> dict[str, object] | None:
    """写盘新声纹；失败或读坏时回滚到旧档案，返回错误结果，成功返回 None。"""
    if warning and previous:
        # 重录读坏了不能把能用的旧声纹冲掉：界面上随时能点「重录」，误触一次不该锁死自己
        lock.profiles = previous            # type: ignore[assignment]
        return {"ok": False, "error": f"{warning}（已保留原声纹）"}
    try:
        lock.save(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    except Exception as exc:
        lock.profiles = previous            # type: ignore[assignment]
        return {"ok": False, "error": f"声纹保存失败：{exc}"}
    return None


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
        on_enroll=(lambda seconds: _enroll_from_ui(runtime, lock, enrolled)) if lock else None,
        on_confirm=runtime.gate.resolve,
        # 读**落盘的时间线**，不是内存里那 8 条：用户问「昨天为什么没反应」，
        # 内存那份早就没了。而且时间线里有被丢弃的回合——那才是「没反应」的答案。
        history=lambda: timeline.recent(cfg=runtime.cfg, limit=60),
        on_listen=lambda on: _set_listening(runtime, on),
        on_cue=lambda body: _deliver_cue(runtime, body),
    )


def _run_text(runtime: Runtime, text: str) -> str:
    result = runtime.dispatch(text, mode=dialogue.mode(runtime.cfg))
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
    """打字入口。**也进时间线**——不然「我刚才让它干了什么」只有语音那半有记录。"""
    runtime = Runtime(cfg, _bus(cfg))
    result = runtime.dispatch(args.text)
    runtime.record(args.text, result)
    print(f"[{result.route}] {result.reply or NO_ACTION}  {result.latency_ms:.1f}ms")
    return 0 if result.route != "ignore" else 2


def cmd_demo(args: argparse.Namespace, cfg: Config) -> int:
    from . import audio, corpus, dsp

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
    from . import audio

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
    from . import audio

    runtime = Runtime(cfg, _bus(cfg))
    lock = _lock(cfg)
    enrolled = threading.Event()
    if lock.profiles:
        enrolled.set()
    url = _serve(runtime, lock, enrolled)
    _start_scheduler(runtime)
    _start_cuebox(runtime)
    # 先报「已经能用了」和沙箱结论：这两件事**不依赖模型**。
    # 以前整条横幅押在模型加载之后，而模型加载 + 麦克风授权可能阻塞几十秒
    # （第一次还会弹系统授权框等人点）。日志看起来就是「服务在跑，但什么都没说」，
    # 而「命令沙箱到底生不生效」正是最不该等的那一条。
    print(f"VoxOS 已启动：胶囊={url} 命令沙箱={_sandbox_note(cfg)}", flush=True)
    sounds = ui.ensure_earcons(cfg.data_root / ".voxos" / "sounds")
    hotwords = audio.refresh_hotwords(cfg) if audio.supports_hotwords(cfg) else []
    _, threshold, kind = audio.make_embedder(cfg)
    print(f"声纹后端={kind} 阈值={threshold} 识别={_hotword_note(cfg, hotwords)}", flush=True)
    if not lock.profiles:
        print("首次使用：在胶囊面板里点「录入声纹」，或运行 voxos enroll（10 秒说 2~3 句）")
        runtime.bus.emit("onboard", "请先录入声纹", "点胶囊 → 录入声纹")
        try:
            enrolled.wait()
        except KeyboardInterrupt:
            return 0
        runtime.bus.emit("idle", "待机", "")
    return _listen(runtime, cfg, lock, sounds)


def _start_scheduler(runtime: Runtime) -> None:
    """到点的任务重新派发一遍（存的是那句话本身，所以新工具自动生效）。

    确认只认「创建这条任务时点过头」（`approved`）。否则一次「每天早上九点
    给张三发消息」会在九点弹一张没人看的确认卡，超时后按拒绝处理，
    用户永远不知道那条消息没发出去。
    """
    def run(item: dict[str, object]) -> str:
        text = str(item.get("text", ""))
        executor = core.Executor(cfg=runtime.cfg, on_event=runtime.bus,
                                 confirm=lambda *_: bool(item.get("approved")))
        result = core.dispatch(text, executor=executor, cfg=runtime.cfg,
                               mode=dialogue.mode(runtime.cfg))
        runtime.record(text, result)
        _speak_scheduled(runtime, result.reply)
        return result.reply or NO_ACTION

    scheduler.start(runtime.cfg, run=run, on_event=runtime.bus.emit)


def _speak_scheduled(runtime: Runtime, reply: str) -> None:
    """到点的那句话要念出来：只上屏的提醒，人不在屏幕前就等于没提醒。

    对话关掉（`dialogue.mode=off`）、`ui.speak=false`、或还没进监听（没有
    pipeline）时都不念——宁可安静，也不假装说过。
    """
    if not reply or dialogue.mode(runtime.cfg) == "off":
        return
    if not runtime.cfg.flag("ui.speak", True):
        return
    speak, piece = _SPEAK_HOOK.get("speak"), dialogue.shape(reply, runtime.cfg)
    if callable(speak) and piece:
        speak(piece)


def _deliver_cue(runtime: Runtime, body: dict[str, object]) -> dict[str, object]:
    """投递一条主动提示：过降噪策略，然后说一句、上屏一行。**不执行动作。**

    与 `/command` 分开是刻意的：命令会派发、可能改文件；cue 只说话。
    脚本、日历、CI 都该走这条，否则一句「提醒我…」被误触发就变成一次执行。
    """
    item = cue.Cue(text=str(body.get("text") or ""),
                   priority=str(body.get("priority") or "normal"),
                   key=str(body.get("key") or ""),
                   source=str(body.get("source") or "api"))
    guard = runtime.cues
    ok, why = guard.allow(item)
    if not ok:
        return {"ok": False, "skipped": why}
    runtime.bus.emit("cue", "提示", item.text.strip())
    piece, speak = dialogue.shape(item.text, runtime.cfg), _SPEAK_HOOK.get("speak")
    if not piece or not callable(speak) or dialogue.mode(runtime.cfg) == "off" \
            or not runtime.cfg.flag("ui.speak", True):
        return {"ok": True, "spoken": False}
    speak(piece)
    return {"ok": True, "spoken": True}


def _start_cuebox(runtime: Runtime, every: float = 2.0) -> None:
    """把文件投递箱里的提示搬进同一条投递路径。

    与 `/api/cue` 共用 `_deliver_cue`：降噪、静音时段、播报整形只有一份实现。
    轮询线程不能因为一条坏提示就死掉——上报后继续下一轮。
    """
    def loop() -> None:
        while True:
            time.sleep(every)
            try:
                for entry in cuebox.take(runtime.cfg):
                    _deliver_cue(runtime, entry)
            except Exception as exc:               # 线程里必须自己兜住
                runtime.bus.emit("error", "提示投递失败", str(exc))
    threading.Thread(target=loop, name="cuebox", daemon=True).start()


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
# 定时任务比监听线程先起来（要能报「首次录入」），但那会儿还没有 pipeline。
# 所以出声走一个后填的钩子：拿不到就只上屏，不假装念过。
_SPEAK_HOOK: dict[str, object] = {"speak": None}


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
    from . import audio, dsp

    """三段式：采集线程 → 识别线程 → 派发主循环。

    旧实现是单线程 `for utterance in pipeline.run(source)`：模型、确认、
    工具任何一个慢下来，麦克风就停止读取，设备缓冲区溢出、句子被吞。
    现在采集与识别各有一个有界队列，慢的只有派发，不会回头卡住设备。
    """
    hotwords = audio.refresh_hotwords(cfg) if audio.supports_hotwords(cfg) else []
    pipeline = audio.AudioPipeline(
        cfg=cfg, stt=audio.make_stt(cfg, hotwords), lock=lock,
        vad=audio.make_vad(cfg), on_event=runtime.bus,
        on_partial=runtime.bus.emit_partial,
        # 播报中被本人插话：立刻停念。判据是「人声持续 interrupt_ms + 声纹像本人」，
        # 不是「用户喊了某个词」——后者要求用户先学会咒语，而真实场景里人说的是
        # 「等一下，我说的是另一个」。对齐 LiveKit 的 min_interruption_duration。
        on_barge_in=stop_speaking,
        echo=dsp.EchoGate(decay_ms=float(cfg.get("audio.echo_decay_ms", 300)),
                          interrupt_ms=float(cfg.get("audio.interrupt_ms", 250)),
                          backchannel_ms=float(cfg.get("audio.backchannel_ms", 700))))
    chunk = int(cfg.get("audio.chunk_size", 512))
    device = cfg.get("audio.input_device", "")
    source = audio.SelfHealingSource(lambda: audio.MicSource(chunk, device=device),
                                     on_event=runtime.bus, on_reset=pipeline.reset)
    _context()                             # 预热：把首次线程启动的开销挪到进循环之前
    _RESET_HOOK["reset"] = pipeline.reset
    _SPEAK_HOOK["speak"] = lambda piece: _speak_async(piece, pipeline, cfg)
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
        _dispatch_one(runtime, pipeline, item, sounds)


def _dispatch_one(runtime: Runtime, pipeline: audio.AudioPipeline,
                  utterance: Any, sounds: dict[str, Path]) -> None:
    if not utterance.accepted:
        # 丢弃也要留痕。以前这里是裸的 `continue`：用户说完一句话，
        # 胶囊上字幕闪一下，然后什么都没有——最容易被误判成「软件坏了」。
        runtime.note_dropped(utterance)
        _hint_rejection(runtime, utterance)
        return
    age = _stale_age(utterance)
    if age > _STALE_S:
        runtime.bus.emit("warn", "已跳过",
                         f"{utterance.text}（{age:.0f} 秒前说的，不做了）")
        runtime.note_dropped(_stale_note(utterance, age))
        return
    is_bc = getattr(pipeline.echo, "is_backchannel", None)
    if is_bc is None:
        from . import dsp
        is_bc = dsp.is_backchannel
    speaking = getattr(pipeline.echo, "speaking", False)
    if is_bc(utterance.text) and (getattr(utterance, "during_tts", False) or speaking):
        runtime.bus.emit("idle", "反馈音", utterance.text)
        return
    if pipeline.echo.is_interrupt(utterance.text):
        stop_speaking()
        runtime.bus.emit("idle", "已停止", utterance.text)
        return
    # 读 runtime.cfg 而不是启动时的快照：设置页改了开关，下一句就该生效。
    if runtime.cfg.flag("ui.earcons", True):
        ui.play(sounds["listen"])
    runtime.bus.emit("executing", "处理中…", utterance.text)
    result = _dispatch_with_ack(runtime, utterance.text, pipeline, runtime.cfg)
    runtime.record(utterance.text, result, utterance)
    state, word = _RESULT_WORD.get(result.route, ("done", "完成"))
    runtime.bus.emit(state, word, result.reply)
    # 听不懂的时候只显示、不朗读：常驻监听会把屋里所有闲聊都收进来，
    # 每句都念一遍「还没学会这句」，很快就变成噪音了。
    spoken = dialogue.shape(result.reply, runtime.cfg) if result.reply else ""
    if spoken and result.route != "ignore" and runtime.cfg.flag("ui.speak", True) and not getattr(result, "streamed", False):
        _speak_async(spoken, pipeline, runtime.cfg)


class _StreamSpeaker:
    """流式播报队列：第一句先播、后续句排队；支持中途打断。"""

    def __init__(self, pipeline: audio.AudioPipeline, cfg: Config) -> None:
        self.pipeline = pipeline
        self.cfg = cfg
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.owner = object()
        self.heard: list[str] = []
        self.started = False
        self.thread: threading.Thread | None = None

    def enqueue(self, sentence: str) -> None:
        if _is_stale(self.owner):
            return
        if not self.started:
            self.started = True
            with _TTS_LOCK:
                _signal_say(_TTS.get("proc"))
                _TTS.update(owner=self.owner, stopped=False, proc=None)
                self.pipeline.echo.tts_start()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        self.queue.put(sentence)

    def finish(self, text: str = "") -> None:
        if self.started:
            self.full_text = text
            self.queue.put(None)

    def should_stop(self) -> bool:
        return _is_stale(self.owner)

    def _run(self) -> None:
        try:
            while not _is_stale(self.owner):
                try:
                    piece = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if piece is None or _is_stale(self.owner):
                    break
                spoken = dialogue.shape(piece, self.cfg)
                if spoken and not _is_stale(self.owner):
                    if not _say_one(spoken, self.cfg, self.owner, self.pipeline):
                        break
                    self.heard.append(spoken)
        except Exception as exc:  # noqa: BLE001
            core.note_tts_failure(str(exc))
        finally:
            with _TTS_LOCK:
                if _TTS.get("owner") is self.owner:
                    _TTS.update(proc=None, owner=None, stopped=False)
                    self.pipeline.echo.tts_end()
                    full = getattr(self, "full_text", "".join(self.heard))
                    core.note_spoken(full, "".join(self.heard))


def _run_dispatch_worker(
    runtime: Runtime, text: str, cfg: Config, box: dict[str, object], done: threading.Event,
    speaker: _StreamSpeaker | None,
) -> None:
    on_s = speaker.enqueue if speaker else None
    stop_s = speaker.should_stop if speaker else None
    try:
        try:
            box["result"] = runtime.dispatch(
                text, mode=dialogue.mode(cfg), on_sentence=on_s, should_stop=stop_s)
        except TypeError:
            box["result"] = runtime.dispatch(text, mode=dialogue.mode(cfg))
    except BaseException as exc:  # noqa: BLE001
        box["error"] = exc
    finally:
        done.set()


def _dispatch_with_ack(runtime: Runtime, text: str, pipeline: audio.AudioPipeline,
                       cfg: Config) -> core.DispatchResult:
    """慢派发先应一声：等过 `ack_after_ms` 还没结果，就不让用户干等。"""
    box: dict[str, object] = {}
    done = threading.Event()
    can_speak = cfg.flag("ui.speak", True) and dialogue.mode(cfg) in ("chat", "assistant")
    speaker = _StreamSpeaker(pipeline, cfg) if (can_speak and cfg.flag("llm.stream", True)) else None
    threading.Thread(target=_run_dispatch_worker, args=(runtime, text, cfg, box, done, speaker), daemon=True).start()
    wait = dialogue.ack_after_s(cfg)
    if wait > 0 and not done.wait(wait) and can_speak and (speaker is None or not speaker.started):
        _speak_async(dialogue.ack(text, cfg), pipeline, cfg)
    done.wait()
    if box.get("error") is not None:
        raise box["error"]  # type: ignore[misc]
    res: core.DispatchResult = box["result"]  # type: ignore[assignment]
    if speaker is not None:
        speaker.finish(res.reply)
    return res


# 胶囊上那行字要说清「它到底做了什么」：一律写「完成」的话，
# 听不懂和真做成了长得一模一样——用户看到的就是「没反应」。
_RESULT_WORD: dict[str, tuple[str, str]] = {
    "ignore": ("miss", "没听懂"),
    "chat": ("done", "回话"),
}


def _speak_async(text: str, pipeline: audio.AudioPipeline, cfg: Config) -> None:
    """TTS 放到后台：说 3 秒不能把麦克风采集卡住 3 秒。

    `cfg` 每次现传：设置页改了 TTS 供应商，下一句就该生效，不用重启。
    """
    threading.Thread(target=_speak, args=(text, pipeline, cfg), daemon=True).start()


_TTS: dict[str, object] = {"proc": None, "owner": None}
_TTS_LOCK = threading.RLock()


def _signal_say(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except ProcessLookupError:
            return  # poll 与 kill 之间已退出，由持有者 wait 回收。


def stop_speaking() -> bool:
    """打断当前会话；句间没有 say 进程时也能取消。"""
    with _TTS_LOCK:
        if _TTS.get("owner") is None:
            return False
        _TTS["stopped"] = True
        _signal_say(_TTS.get("proc"))
        return True


def _wait_say(proc: subprocess.Popen, piece: str, owner: object, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    text: bytes | None = piece.encode("utf-8")
    while not _is_stale(owner):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            proc.communicate(input=text, timeout=min(0.05, remaining))
            return proc.returncode == 0 and not _is_stale(owner)
        except subprocess.TimeoutExpired:
            text = None  # communicate 会继续发送上次未写完的 stdin。
    return False


def _run_say(piece: str, cfg: Config, owner: object, path: Path | None = None) -> bool:
    """文本只经 stdin；进程创建与归属绑定原子化，回收只使用本地句柄。"""
    args = ["say", "-r", "220"]
    if path is not None:
        rate = int(cfg.get("tts.sample_rate", 24000))
        args += ["--file-format=WAVE", f"--data-format=LEI16@{rate}",
                 "--channels=1", "-o", str(path)]
    proc = None
    try:
        with _TTS_LOCK:
            if _is_stale(owner):
                return False
            proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _TTS["proc"] = proc
        return _wait_say(proc, piece, owner, float(cfg.get("tts.local_timeout_s", 15)))
    finally:
        if proc is not None:
            try:
                _signal_say(proc)
                proc.wait(timeout=1)
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
                with _TTS_LOCK:
                    if _TTS.get("owner") is owner and _TTS.get("proc") is proc:
                        _TTS["proc"] = None


def _say_locally(piece: str, cfg: Config, owner: object,
                 reference: Callable[[bytes, int, float], None] | None = None) -> bool:
    """say 先合成临时 WAV，再走公共 PCM 播放；缺设备依赖才直播。"""
    from . import tts
    if _is_stale(owner):
        return False
    completed: bool | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="voxos-tts-") as directory:
            path = Path(directory) / "speech.wav"
            if not _run_say(piece, cfg, owner, path) or _is_stale(owner):
                return False
            completed = (tts.play_wav(path, cfg, lambda: _is_stale(owner), reference)
                         if reference is not None else
                         tts.play_wav(path, cfg, lambda: _is_stale(owner)))
        return completed
    except OSError:
        if completed is None:
            raise
        core.note_tts_failure("本地语音临时文件清理失败；保留已完成的播放结果")
        return completed
    except ImportError:
        if _is_stale(owner):
            return False
        core.note_tts_failure("sounddevice 不可用，退回 say 直播：无 PCM 参考信号，不具备 AEC")
        return _run_say(piece, cfg, owner)


def _say_one(piece: str, cfg: Config, owner: object,
             pipeline: Any = None) -> bool:
    """云端优先；仅未开始播放且仍属当前会话时允许整句降级。"""
    from . import tts
    if _is_stale(owner):
        return False
    ref = getattr(pipeline, "feed_reference", None) if pipeline is not None else None
    if tts.available(cfg):
        try:
            return (tts.speak(piece, cfg, lambda: _is_stale(owner), ref)
                    if ref is not None else
                    tts.speak(piece, cfg, lambda: _is_stale(owner)))
        except tts.PlaybackStartedError as exc:
            core.note_tts_failure(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - 未播放的合成失败可退回本地
            core.note_tts_failure(str(exc))
    if _is_stale(owner):
        return False
    return (_say_locally(piece, cfg, owner, ref)
            if ref is not None else
            _say_locally(piece, cfg, owner))


def _is_stale(owner: object) -> bool:
    """这一轮播报还该继续吗：被打断了，或者已经换人了。"""
    with _TTS_LOCK:
        return bool(_TTS.get("stopped")) or _TTS.get("owner") is not owner


def _speak(text: str, pipeline: audio.AudioPipeline, cfg: Config) -> None:
    """按句播报并记录完整播完的句子；仅当前 owner 能清理会话与回声门。"""
    from . import dsp, tts               # numpy 别拖进 --version 这类快路径
    if not tts.available(cfg) and not shutil.which("say"):
        return                           # 两条路都没有：一句话也念不出来
    owner = object()
    with _TTS_LOCK:
        _signal_say(_TTS.get("proc"))
        _TTS.update(owner=owner, stopped=False, proc=None)
        pipeline.echo.tts_start()
    heard: list[str] = []
    try:
        for piece in dsp.sentences(text):
            if _is_stale(owner):
                break
            if not _say_one(piece, cfg, owner, pipeline):
                break
            heard.append(piece)
    except Exception as exc:  # noqa: BLE001 - 后台播报失败不影响采集
        core.note_tts_failure(str(exc))
    finally:
        with _TTS_LOCK:
            if _TTS.get("owner") is owner:
                _TTS.update(proc=None, owner=None, stopped=False)
                pipeline.echo.tts_end()
                core.note_spoken(text, "".join(heard))


def cmd_enroll(args: argparse.Namespace, cfg: Config) -> int:
    from . import audio

    if args.script:
        print("按顺序读这三句，每句读完停半秒：")
        for line in enroll.script_lines():
            print(f"  {line}")
        return 0
    lock = _lock(cfg)
    if args.wav:
        samples = [audio.wav_to_array(Path(item).expanduser()) for item in args.wav]
        return _commit_enroll(cfg, lock, samples, args.mode)
    if args.check:
        return _check_voice(cfg, lock)
    if args.batch or not sys.stdin.isatty():
        # 没有终端可交互（脚本 / CI / 从别的程序里调）：退回一次录满再按 VAD 切句，
        # 行为与 0.1.38 一致。交互才是默认，因为「切出几段」全看运气。
        samples = _record_segments(cfg, args.record, cfg.get("audio.input_device", ""))
        return _commit_enroll(cfg, lock, samples, args.mode)
    samples, device = _guided_samples(cfg, lock)
    if not samples:
        print("没有可用音频，录入失败")
        return 2
    return _commit_enroll(cfg, lock, samples, args.mode, device=device)


def _guided_samples(cfg: Config, lock: dsp.SpeakerLock) -> tuple[list[object], str]:
    """逐句录、逐句判。这是「怎么录才对」的全部答案。

    一次录 10 秒再让 VAD 去切，切出几段全看运气：三句连在一起只剩 1 段，
    中间一声咳嗽又多一段噪声。改成一句一次，段数就是确定的，而且哪句不合格
    当场重读，不会带着一段坏音频去建模板。
    """
    from . import audio, dsp

    device = cfg.get("audio.input_device", "")
    try:
        name, peak = audio.probe_mic(0.4, device)
    except Exception as exc:                       # 没麦克风 / 没权限
        print(f"⚠️  打不开麦克风：{exc}")
        return [], ""
    print(f"麦克风：{name}（峰值 {peak:.3f}）")
    if peak < enroll.MIN_PEAK:
        # 这时还没开口，峰值低多半只是屋里安静；真没声音会在每句读完时当场拦下
        print("（现在很安静。读完每句会报峰值，若一直很低再检查系统输入设备）")
    print(f"一共 {len(enroll.SCRIPT)} 句，每句读完停半秒，它会自己收尾。\n")
    samples: list[object] = []
    vectors: list[object] = []
    for index, line in enumerate(enroll.SCRIPT, 1):
        sample = _read_one(cfg, lock, device, index, line, vectors)
        if sample is None:
            print("录入中断：这句两次都没读好。整段重来：voxos enroll")
            return [], name
        samples.append(sample)
        vectors.append(lock.embedder(dsp.as_float(sample)))
    return samples, name


def _read_one(cfg: Config, lock: dsp.SpeakerLock, device: object, index: int,
              line: str, vectors: list[object]) -> object | None:
    """读一句，最多两次。合格返回这一段波形，不合格返回 None。"""
    from . import dsp

    for attempt in (1, 2):
        print(f"第 {index}/{len(enroll.SCRIPT)} 句（第 {attempt} 次）：{line}")
        if sys.stdin.isatty():
            input("  按回车开始…")
        wave, speech_s = _capture_one(cfg, device)
        why = enroll.check(wave, speech_s)
        if not why:
            why = enroll.outlier(lock.embedder(dsp.as_float(wave)), vectors)
        if why:
            print(f"  ✗ {why}\n")
            continue
        print(f"  ✓ {speech_s:.1f} 秒，峰值 {enroll.peak_of(wave):.3f}\n")
        return wave
    return None


def _capture_one(cfg: Config, device: object, max_s: float = 6.0) -> tuple[object, float]:
    """录一句：VAD 判到句尾就停，最多 `max_s` 秒。返回 (波形, 人声秒数)。"""
    from . import audio, dsp

    import numpy as np

    vad = audio.make_vad(cfg)
    detector = dsp.EndpointDetector(
        silence_ms=float(cfg.get("audio.silence_duration_ms", 500)),
        max_ms=max_s * 1000.0,
        min_speech_ms=float(cfg.get("audio.min_speech_ms", 200)),
    )
    buffer: list[object] = []
    with audio.MicSource(device=device) as source:
        started = time.time()
        for chunk in source:
            state = detector.push(bool(vad.is_speech(chunk)), chunk.size)  # type: ignore[attr-defined]
            if state != "idle":
                buffer.append(chunk)
            if state in ("candidate", "force_end") or time.time() - started > max_s + 1.0:
                break
    wave = np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)
    return wave, detector.speech_ms / 1000.0


def _commit_enroll(cfg: Config, lock: dsp.SpeakerLock, samples: list[object],
                   mode: str = "normal", device: str = "") -> int:
    """先算留出分数，再建模板、落盘、给结论。顺序不能反。"""
    from . import dsp

    if not samples:
        print("没有可用音频，录入失败")
        return 2
    holdout = enroll.holdout([lock.embedder(dsp.as_float(item)) for item in samples])
    consistency = lock.enroll(samples, mode)  # type: ignore[arg-type]
    lock.save(cfg.path("audio.voiceprint_profiles", ".voxos/voiceprint_profiles.npz"))
    usable, line = enroll.verdict(holdout if holdout is not None else float(consistency),
                                  lock.threshold, len(samples))
    print(line)
    print(f"已写入 {cfg.path('audio.voiceprint_profiles')}（{len(samples)} 段，{mode}）")
    _save_enroll_meta(cfg, samples, float(consistency), holdout, lock.threshold,
                      device=device, usable=usable)
    return 0


def _check_voice(cfg: Config, lock: dsp.SpeakerLock) -> int:
    """录一句，只报分数，不写档案——回答「现在这张嘴能不能过」。"""
    if not lock.profiles:
        print("还没有声纹档案：先跑 voxos enroll")
        return 2
    print(f"读一句就行（阈值 {lock.threshold:.2f}）：{enroll.SCRIPT[0]}")
    if sys.stdin.isatty():
        input("  按回车开始…")
    wave, speech_s = _capture_one(cfg, cfg.get("audio.input_device", ""))
    why = enroll.check(wave, speech_s)
    if why:
        print(f"✗ {why}")
        return 2
    ok, score = lock.verify(wave)
    print(f"{'✓ 通过' if ok else '✗ 未通过'}：声纹 {score:.3f} / 阈值 {lock.threshold:.2f}")
    if not ok:
        gap = lock.threshold - score
        print(f"  差 {gap:.3f}。" + ("差 0.1 以内多半是录音条件变了（离得远 / 小声），"
                                    "凑近点再试一次；" if gap < 0.1 else "")
              + "差得多就是档案本身不行：voxos enroll 重录。")
    return 0 if ok else 1


def _holdout_score(lock: object, samples: list[object]) -> float | None:
    """留一段做验证：本人换句话大概能拿多少分。

    用全部段建模板、再拿其中一段去验证，分数必然偏高——那一段参与了均值。
    留出最后一段，得到的才是新句子的真实分数。段数少于 3 时返回 None。
    """
    from . import dsp

    if not hasattr(lock, "embedder") or len(samples) < 3:
        return None
    vectors = [lock.embedder(dsp.as_float(s)) for s in samples]  # type: ignore[attr-defined]
    return dsp.cosine(vectors[-1], dsp.mean_vector(vectors[:-1]))


def _enroll_verdict(samples: list[object], consistency: float, threshold: float,
                    holdout: float | None = None) -> str:
    """这次录入到底能不能用。**返回一行结论**（给接口当 warning），细节打印给终端。

    判据是**比实际阈值高多少**，不是一个写死的魔法数。而且优先用**留出分数**：
    一致性是「每段和均值有多像」，留出分数才是「本人换句话能拿多少分」——
    后者才是真正决定会不会被拒的那个数。

    实测踩过两次：只录 1 句时一致性 0.751、阈值 0.75，用户时通时不通；录 10 秒
    切成 5 段时 0.755，还是同一个坑。**不报清楚，用户会以为「已经录好了」。**
    """
    from . import dsp

    spans = [dsp.as_float(s).size / dsp.SR for s in samples]  # type: ignore[arg-type]
    score = holdout if holdout is not None else consistency
    label = "留出验证" if holdout is not None else "一致性"
    margin = score - threshold
    if margin >= 0.04:
        print(f"✅ 可用：{label} {score:.3f} 比阈值 {threshold:.2f} 高 {margin:.3f}，"
              f"本人换句话也能过")
        return ""
    average = sum(spans) / len(spans)
    print(f"⚠️  这次录入**不可用**：{label} {score:.3f}，阈值 {threshold:.2f}，"
          f"只高 {margin:+.3f}——本人说话也会被拒。")
    print(f"   切出 {len(spans)} 段，平均 {average:.1f}s，最短 {min(spans):.1f}s")
    short = [span for span in spans if span < 1.0]
    if short:
        print(f"   其中 {len(short)} 段短于 1 秒：碎片的声纹不准，是拉低分数的元凶")
    print("   重录：voxos enroll  —— 说 3 句完整的话，每句 2~3 秒，句间停半秒")
    return (f"这次录入不可用：{label} {score:.3f}，阈值 {threshold:.2f}，"
            f"本人说话也会被拒。重录时说 3 句完整的话，每句 2~3 秒")


def _record_segments(cfg: Config, seconds: float, device: object = None) -> list[object]:
    """录一段时间，按 VAD 切成「多句」——声纹模板必须多句取均值才稳。

    实测（真麦克风 + CAM++，阈值量纲为余弦相似度）：
    - 只录 1 句 → 同一个人的另一句话只有 0.75，而默认阈值 0.82，用户录完就被自己拒了；
    - 把**一次**录音切成 2~3 段 → 同人 0.70~0.72、异人 0.66~0.72，间隔 0.05 以内，分不开；
    - 录 3 句取均值 → 同人 0.79、异人最高 0.72，间隔 0.071，这才有可用余量。
    所以切段必须按「句」切，不能按时间切。
    """
    from . import audio, dsp

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
    if _open_native_window(cfg, url):
        return 0
    if not _open_app_window(url):
        webbrowser.open(url)
        print(f"已用默认浏览器打开 {url}（装 Chrome 可获得无边框胶囊窗口）")
    return 0


def _native_window_binary(cfg: Config) -> Path | None:
    """原生窗口二进制在哪。装成 App 时和启动器同目录，源码布局在 `build/`。

    找不到不是错误：这条路本来就是加速项，退回 Chrome 一样能用。
    `VOXOS_WINDOW` 可显式指定（CI、多实例、自己编译的实验版）。
    """
    candidates = [os.environ.get("VOXOS_WINDOW", ""),
                  str(Path(sys.argv[0]).resolve().parent / "VoxOSWindow"),
                  str(cfg.root / "build" / "VoxOSWindow"),
                  str(cfg.data_root / ".voxos" / "bin" / "VoxOSWindow")]
    for raw in candidates:
        if raw and Path(raw).is_file() and os.access(raw, os.X_OK):
            return Path(raw)
    return None


def _open_native_window(cfg: Config, url: str) -> bool:
    """用自带的 WKWebView 宿主开窗：~0.32s、~60MB。

    对比 Chrome 的 `--app`：冷启动 1.2–1.5s、常驻 300MB+。差的不只是时间，
    还有「窗口先出来、内容后到」——原生窗口会自己重试连服务，所以启动器
    不必等端口就绪，双击后第一帧几乎是即时的。
    """
    binary = _native_window_binary(cfg)
    if binary is None:
        return False
    try:
        subprocess.Popen([str(binary), "--url", url],  # noqa: S603
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def _open_app_window(url: str) -> bool:
    """用 Chromium 系浏览器的 --app 模式开一个无边框小窗，最接近「悬浮胶囊」。

    **启动成功不等于窗口出现**：`open -na` 会返回 0 却什么都不弹——配置目录被上次
    强杀留下的单例锁占住（`SingletonLock` 指向一个已死的 pid）、Chrome 正在升级、
    企业策略禁用多实例，都会这样。旧实现只要 Popen 没抛异常就 return True，
    于是「点了图标什么都没发生」被静默吞掉。现在等窗口真的出现，等不到就让
    调用方回落到默认浏览器。
    """
    profile = Path.home() / ".voxos" / "pill-profile"
    profile.mkdir(parents=True, exist_ok=True)
    _drop_stale_lock(profile)
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
        if not Path(f"/Applications/{app}.app").exists():
            continue
        try:
            subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                              f"--user-data-dir={profile}", "--window-size=420,620",
                              "--window-position=80,80"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        if _window_alive(profile):
            return True
    return False


def _drop_stale_lock(profile: Path) -> None:
    """清掉指向已死进程的单例锁。

    只在**确认没有进程在用这个配置目录**时才删：反过来的话会同时起两个实例，
    两个都以为自己独占，配置目录就真的坏了。
    """
    if _window_alive(profile, timeout=0.5):
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink()
        except OSError:
            pass


def _window_alive(profile: Path, timeout: float = 4.0) -> bool:
    """有进程正在用这个配置目录，才算窗口真的起来了。"""
    deadline = time.time() + timeout
    while True:
        try:
            found = subprocess.run(["pgrep", "-f", str(profile)],  # noqa: S603,S607
                                   capture_output=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return True          # 判断不了的时候不要谎报失败
        if found.returncode == 0 and found.stdout.strip():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)
    return False


def cmd_tools(_: argparse.Namespace, cfg: Config) -> int:
    for name, meta in sorted(core.REGISTRY.items()):
        params = ", ".join(meta.schema["function"]["parameters"]["properties"])
        print(f"{name:<24} {meta.risk.value:<13} fast={'yes' if meta.fast else 'no ':>3}  ({params})")
    print(f"\n共 {len(core.REGISTRY)} 个工具，{len(core.FAST_ROUTES)} 条快通道")
    return 0


def cmd_schedule(_: argparse.Namespace, cfg: Config) -> int:
    """列定时任务。到点执行的是**那句话本身**，所以这里只显示文本。"""
    items = scheduler.pending(cfg)
    if not items:
        print("还没有定时任务")
        return 0
    for item in items:
        repeat = "（每天）" if item.get("repeat") == "daily" else ""
        print(f"{scheduler.stamp(float(item['due']))}  {item['id']}  {item['label']}{repeat}")
    print(f"\n共 {len(items)} 条，存在 {scheduler.path_of(cfg)}")
    return 0


def cmd_cue(args: argparse.Namespace, cfg: Config) -> int:
    """推一条提示。守护进程没在跑也不会静默丢失：起来后按新鲜度补，太旧的丢弃。"""
    try:
        path = cuebox.post(cfg, args.text, priority=args.priority, key=args.key)
    except (OSError, ValueError) as exc:
        print(f"没写成：{exc}")
        return 2
    print(f"已投递 {path.name}；`voxos run` 在跑的话几秒内会说一句。")
    return 0


def cmd_models(args: argparse.Namespace, cfg: Config) -> int:
    """下载模型。装好的包只有 `voxos` 命令，所以这条必须是子命令而不是脚本。"""
    from .models import default_dir, fetch

    target = Path(args.dir) if args.dir else default_dir()
    return fetch(target, proxy=args.proxy, force=args.force, with_asr=not args.no_asr)


def cmd_log(args: argparse.Namespace, cfg: Config) -> int:
    """会话时间线 + 诊断：「我说了什么、它做了什么、为什么没做」。

    这个命令存在的理由只有一个：**「为什么没反应」要能一次答完**。
    以前这个问题的答案在两份日志里都不存在——审计只记工具调用，状态只记
    状态变化，而被丢弃的句子两边都没有。用户只能猜「是不是坏了」。
    """
    turns = timeline.recent(cfg=cfg, limit=int(args.limit))
    if not turns:
        print("还没有记录。说一句话试试（voxos run）。")
        return 0
    fields = timeline.Turn.__dataclass_fields__
    print(f"{'时间':<9} {'结局':<4} {'音频':>7} {'声纹':>5} {'动作':<18} 详情")
    print("-" * 92)
    for row in reversed(turns):
        turn = timeline.Turn(**{k: v for k, v in row.items() if k in fields})
        print(turn.line())

    findings = lessons.diagnose(turns, limit=3)
    if findings:
        print("\n当前最该修的：")
        for finding in findings:
            print("  " + finding.line().replace("\n", "\n  "))
    blind = lessons.unmatched(turns, limit=8)
    if blind:
        print("\n没听懂的话（攒够几条就该加一条快通道正则）：")
        for text in blind:
            print(f"  · {text}")
    return 0


def _aec_note(cfg: Config) -> str:
    if not cfg.flag("audio.aec", True):
        return "已禁用（audio.aec=false）"
    return "已启用（NLMS 自适应滤波，TTS 参考对齐）"


def cmd_doctor(_: argparse.Namespace, cfg: Config) -> int:
    checks = [
        ("python", sys.version.split()[0]),
        ("numpy", _version("numpy")),
        ("sherpa-onnx", _version("sherpa_onnx")),
        ("sounddevice", _version("sounddevice")),
        ("tmux", shutil.which("tmux") or "缺失"),
        ("say/afplay", shutil.which("say") or "缺失"),
        ("声纹模型", str(cfg.path("audio.speaker_model", "")) if cfg.path("audio.speaker_model", "").exists() else "缺失（降级）"),
        ("声纹档案", _voiceprint_note(cfg)),
        ("LLM", _llm_note(cfg)),
        ("语音合成", _tts_note(cfg)),
        ("回声消除", _aec_note(cfg)),
        ("识别后端", _hotword_note(cfg, [])),
        ("实时字幕", _subtitle_note(cfg)),
        ("判停", _turn_note(cfg)),
        ("输入设备", _mic_note(cfg)),
        ("VAD 后端", _vad_note(cfg)),
        ("资源根", str(cfg.root)),
        ("数据根", str(cfg.data_root)),
        ("配置文件", _config_note(cfg)),
        ("命令沙箱", _sandbox_note(cfg)),
        ("可写根", _roots_note(cfg)),
        ("dry_run", str(cfg.flag("workspace.dry_run"))),
        ("角色", str(cfg.get("workspace.role"))),
    ]
    for name, value in checks:
        print(f"{name:<12} {value}")
    return 0


def cmd_llm(args: argparse.Namespace, cfg: Config) -> int:
    """看渠道 / 写密钥 / 真发一次请求。"""
    if args.set_key:
        return _write_key(cfg, args.set_key)
    ready, why = core.llm_ready(cfg)
    rows = (("渠道", core.provider(cfg)), ("模型", cfg.get("llm.model")),
            ("地址", cfg.get("llm.base_url")), ("思考", cfg.get("llm.thinking", "auto")),
            ("密钥", "已配置" if cfg.get("llm.api_key") else
             f"未配置（可写 {cfg.path('llm.api_key_file', '.voxos/llm_key')}）"),
            ("状态", "可用" if ready else why))
    for name, value in rows:
        print(f"{name:<6} {value}")
    return _llm_probe(cfg) if args.check else 0


def _write_key(cfg: Config, key: str) -> int:
    """密钥单独落一个 0600 的文件：写进 settings.toml 等于往公开仓库推一次。"""
    path = cfg.path("llm.api_key_file", ".voxos/llm_key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"密钥已写入 {path}（权限 600，不在仓库里）")
    return 0


def _llm_probe(cfg: Config) -> int:
    """真发一次：余额 402、模型名写错、网关套信封，只有发出去才知道。"""
    started = time.perf_counter()
    try:
        message = core.chat(
            base_url=str(cfg.get("llm.base_url")), api_key=str(cfg.get("llm.api_key", "")),
            model=str(cfg.get("llm.model")), messages=[{"role": "user", "content": "只回复两个字：收到"}],
            timeout=float(cfg.get("llm.timeout_s", 20)), extra=core.llm_extra(cfg))
    except Exception as exc:
        print(f"请求失败：{exc}")
        return 1
    took = (time.perf_counter() - started) * 1000
    print(f"通：{(message.get('content') or '').strip()[:40]}  {took:.0f}ms")
    return 0


def cmd_prompts(args: argparse.Namespace, cfg: Config) -> int:
    """打印每一段提示词的来源，以及真正会发给模型的那段文字。"""
    view = cfg.with_overrides(**{"workspace.role": args.role.upper()}) if args.role else cfg
    for name, source in prompts.sources(view).items():
        print(f"{name:<10} {source}")
    print("-" * 64)
    print(prompts.active(view, role=view.get("workspace.role"),
                         now=time.strftime("%Y-%m-%d %H:%M"), active_window="（运行时填）",
                         workspace=view.path("workspace.default_code_dir", "~"),
                         recent_files="（运行时填）", max_rounds=view.get("llm.max_tool_rounds", 3)))
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


def _llm_note(cfg: Config) -> str:
    """慢通道的真实状态。报「已启用」而实际缺 Key，等于替它撒谎。"""
    ready, why = core.llm_ready(cfg)
    if ready:
        return f"{cfg.get('llm.model')} @ {cfg.get('llm.base_url')}（思考 {cfg.get('llm.thinking', 'auto')}）"
    return f"{why}（走快通道 + 离线兜底）"


def _tts_note(cfg: Config) -> str:
    """念出来的是谁的声音。降级到系统 `say` 时要说清为什么——不然只会觉得时好时坏。"""
    from . import tts

    ok, why = tts.probe(cfg)
    if ok:
        return why
    last = core.tts_failure()
    return f"{why}（上次失败：{last}）" if last else why


def _enroll_meta_path(cfg: Config) -> Path:
    """录入元数据落在声纹档案旁边。

    npz 里存的是**均值向量**——录 1 句和录 5 段取均值，文件里都是 1 个 192 维
    数组，从文件本身分不出来。而这两者的可靠性差很远。所以段数和留出分数必须
    单独记，否则 doctor 只能一直报「只有 1 个样本」，那是假话。
    """
    return cfg.path("audio.voiceprint_profiles").with_suffix(".json")


def _save_enroll_meta(cfg: Config, samples: list[object], consistency: float,
                      holdout: float | None, threshold: float,
                      device: str = "", usable: bool = True) -> None:
    """把「这份档案怎么来的」记在档案旁边。

    `device` 是这一版新加的：换了麦克风之后同一个人会掉 0.2 分，而档案里
    看不出来。记下设备名，doctor 才能说「这份档案是 MacBook 麦克风录的，
    你现在用的是 AirPods」——否则只能看到「已录入」和一堆拒绝。
    """
    try:
        _enroll_meta_path(cfg).write_text(json.dumps({
            "segments": len(samples),
            "consistency": round(float(consistency), 4),
            "holdout": None if holdout is None else round(float(holdout), 4),
            "threshold": float(threshold),
            "device": device,
            "usable": bool(usable),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:                        # 记不下来不该让录入失败
        print(f"[voxos] 录入元数据写入失败（忽略）：{exc}")


def _voiceprint_note(cfg: Config) -> str:
    """声纹档案的真实质量。

    **只报「已录入」是不够的**：录 1 个样本和录 5 段取均值，是两种完全不同的
    可靠性。而 npz 里两者都只表现为 1 个均值向量，所以要读录入时记下的元数据。
    """
    if not cfg.path("audio.voiceprint_profiles").exists():
        return "未录入（voxos enroll，跟着读 3 句）"
    try:
        meta = json.loads(_enroll_meta_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "已录入（早期档案，质量未记录；voxos enroll 重录一次即可看到分数）"
    return _meta_note(meta)


def _meta_note(meta: dict[str, object]) -> str:
    """把录入元数据翻成一句能判断「能不能用」的话。"""
    segments = meta.get("segments") or 0
    threshold = float(meta.get("threshold") or 0.75)
    score, label = meta.get("holdout"), "留出验证"
    if score is None:
        score, label = meta.get("consistency"), "一致性"
    where = f"，{meta.get('device')}" if meta.get("device") else ""
    if score is None:
        return f"已录入（{segments} 段均值{where}，质量未记录）"
    margin = float(score) - threshold
    verdict = "可用" if margin >= 0.04 else "← 会被自己拒，重录：voxos enroll"
    return (f"已录入（{segments} 段均值{where}，{label} {float(score):.3f} / "
            f"阈值 {threshold:.2f}，{verdict}）")


def _voiceprint_state(cfg: Config, lock: dsp.SpeakerLock | None) -> dict[str, object]:
    """给界面用的结构化档案状态。没有档案就返回空 dict。"""
    if not (lock and lock.profiles):
        return {}
    state: dict[str, object] = {"threshold": getattr(lock, "threshold", None)}
    try:
        meta = json.loads(_enroll_meta_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state["known"] = False
        return state
    state.update({
        "known": True,
        "segments": meta.get("segments"),
        "holdout": meta.get("holdout"),
        "device": meta.get("device") or "",
        "usable": bool(meta.get("usable", True)),
        "at": meta.get("at"),
    })
    return state


def _turn_note(cfg: Config) -> str:
    """判停用的是哪条路：音频模型还是文本尾词。

    这条必须单独报，因为两条路的**行为不一样**——音频判停看语调，文本判停只
    认「然后」「比如」这类词。用户觉得「今天怎么老抢话」的时候，第一件事就是
    确认跑的是哪条。`available()` 只查文件在不在，`make()` 才知道能不能真跑，
    所以两个都问：分清「没下模型」和「下了但加载不了」。
    """
    from . import turn

    ok, why = turn.available(cfg)
    if not ok:
        return f"文本判停（{why}）"
    if turn.make(cfg) is None:
        return "文本判停（模型加载失败，删掉重下：voxos models --force）"
    return f"smart-turn v3.2（音频判停，阈值 {turn.threshold(cfg):g}）"


def _subtitle_note(cfg: Config) -> str:
    """实时字幕走哪条路，以及为什么可能没开。

    三种状态必须分得清：真流式（有模型）、窗口字幕（拿离线引擎反复解）、关闭。
    最糟的是「用户以为开着」——远端识别后端会被**代码**关掉字幕（字幕必须在
    声纹校验之前出字，等于把没验过身份的音频提前传上云），这件事如果不在
    doctor 里说，用户只会觉得「字幕坏了」。
    """
    if not cfg.flag("audio.subtitles", True):
        return "关闭（audio.subtitles = false）"
    if str(cfg.get("stt.backend", "scripted")) == "openai":
        return "关闭（远端识别：字幕会在声纹校验之前出字）"
    raw = str(cfg.get("stt.stream_model_dir", "") or "").strip()
    if raw:
        root = cfg.path("stt.stream_model_dir", "")
        if (root / "tokens.txt").exists():
            return f"流式识别（{root.name}）"
        return f"窗口字幕（{raw} 缺 tokens.txt）"
    return (f"窗口字幕（解最后 {cfg.get('audio.subtitle_window_ms')}ms，"
            f"首字 {cfg.get('audio.first_partial_ms')}ms 起）")


def _hotword_note(cfg: Config, words: list[str]) -> str:
    """热词表对当前后端到底有没有用——没用就直说，别报一个好看的数字。"""
    from . import audio

    backend = str(cfg.get("stt.backend", ""))
    label = STT_LABELS.get(backend, backend or "未设置")
    if audio.supports_hotwords(cfg):
        return f"{label}（热词表 {len(words)} 条生效）"
    return f"{label}（无热词接口，热词表不生效）"


def _sandbox_note(cfg: Config) -> str:
    """命令沙箱到底生不生效。

    「装好了但悄悄没生效」是这一组里最危险的一种「看起来正常」：用户以为命令
    被关在笼子里，其实没有。所以 doctor 和启动横幅都必须报它，而且要报**为什么**。
    """
    current = sandbox.mode(cfg)
    if current == "danger-full-access":
        return "danger-full-access（不限制，命令直接跑）"
    if current == "read-only":
        return "read-only（一条命令都不执行）"
    usable, why = sandbox.status()
    return f"{current}（生效）" if usable else f"{current}（不生效：{why}）"


def _roots_note(cfg: Config) -> str:
    """沙箱允许写哪儿。不报出来，用户只能靠「为什么写不进去」去猜边界——
    而那条错误信息长得和「命令自己有 bug」一模一样。"""
    if sandbox.mode(cfg) != "workspace-write":
        return "不适用"
    home = str(Path.home())
    return " ".join(str(one).replace(home, "~") for one in sandbox.writes_roots(cfg))


def _vad_note(cfg: Config) -> str:
    """报出实际用的 VAD。少了 onnxruntime 会静默降级成能量 VAD：
    能跑，但一声咳嗽也算说话——装完才看得出来，所以 doctor 必须报。
    """
    from . import audio

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
    from . import audio

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

    _add_enroll(sub)

    serve = sub.add_parser("serve", help="只启动胶囊与设置面板")
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("pill", help="无边框胶囊窗口").set_defaults(func=cmd_pill)

    _add_extra(sub)               # models / llm / prompts / stop / port / tools / doctor
    return parser


def _add_enroll(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("enroll", help="录入声纹（逐句读，逐句判）")
    p.add_argument("--wav", action="append", help="用现成 wav 录入（可多次）")
    p.add_argument("--record", type=float, default=10.0,
                   help="非交互时一次录满的秒数（期间说 2~3 句，句间停半秒）")
    p.add_argument("--mode", choices=["normal", "mask"], default="normal")
    p.add_argument("--script", action="store_true", help="只打印要读的三句")
    p.add_argument("--check", action="store_true", help="录一句试一下，只报分数，不写档案")
    p.add_argument("--batch", action="store_true", help="不逐句交互，退回一次录满再切句（脚本用）")
    p.set_defaults(func=cmd_enroll)


def _add_extra(sub: argparse._SubParsersAction) -> None:
    """其余子命令的参数单独放：build_parser 已经顶到行预算了。"""
    models = sub.add_parser("models", help="下载声纹/VAD/识别模型")
    models.add_argument("--proxy", default=None, help="下载代理，如 http://127.0.0.1:7890")
    models.add_argument("--force", action="store_true", help="即使已存在也重下")
    models.add_argument("--no-asr", action="store_true", help="跳过 239MB 识别模型")
    models.add_argument("--dir", default=None, help="模型目录（默认数据目录下的 .voxos/models）")
    models.set_defaults(func=cmd_models)

    llm = sub.add_parser("llm", help="看/配大模型渠道")
    llm.add_argument("--set-key", default=None, help="把密钥写进 api_key_file（不进仓库）")
    llm.add_argument("--check", action="store_true", help="真发一次请求，看通不通")
    llm.set_defaults(func=cmd_llm)

    prompts = sub.add_parser("prompts", help="看当前生效的提示词与来源文件")
    prompts.add_argument("--role", default=None, help="临时换角色看效果（DEVELOPER/OPS/DOCTOR）")
    prompts.set_defaults(func=cmd_prompts)

    log = sub.add_parser("log", help="会话时间线：说了什么、做了什么、为什么没做")
    log.add_argument("--limit", type=int, default=30, help="显示最近多少条（默认 30）")
    log.set_defaults(func=cmd_log)

    tell = sub.add_parser("cue", help="推一条提示（只说，不执行任何动作）")
    tell.add_argument("text")
    tell.add_argument("--priority", choices=cue.PRIORITIES, default="normal",
                      help="high 可以越过静音时段")
    tell.add_argument("--key", default="", help="相同 key 在去重窗口内只提醒一次")
    tell.set_defaults(func=cmd_cue)

    for name, hint, func in (("stop", "停掉在跑的 run", cmd_stop),
                             ("port", "打印界面端口", cmd_port),
                             ("tools", "列出已注册工具", cmd_tools),
                             ("schedule", "列出定时任务", cmd_schedule),
                             ("doctor", "环境自检", cmd_doctor)):
        sub.add_parser(name, help=hint).set_defaults(func=func)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load(args.config)
    _bootstrap(cfg)
    return int(args.func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
