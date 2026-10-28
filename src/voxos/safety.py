"""安全网关与执行器：红线检查 → 二次确认 → 快照 → 单飞锁 → 执行 → 审计。

这是全系统唯一的工具执行入口（REDLINES.md §0）：任何模型、任何语音指令，
都必须经过这里才能碰到文件系统或子进程。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config, load, load_redlines
from .registry import REGISTRY, RiskLevel, Tool, ensure_tools, validate_args

__all__ = ["Verdict", "Guard", "Executor", "human_name", "snapshot_file", "restore_latest"]


@dataclass(frozen=True)
class Verdict:
    """一次安全检查的结论：风险等级、理由、是否需要打草稿标记。"""

    level: RiskLevel
    reason: str
    draft: bool = False


# ---------------------------------------------------------------- 安全网关

class Guard:
    """数据驱动的红线检查：策略全在 config/redlines.toml，内核零角色分支。"""

    def __init__(self, redlines: dict[str, Any] | None = None, role: str = "DEVELOPER") -> None:
        redlines = redlines if redlines is not None else load_redlines()
        self.role = role
        self.missing = bool(redlines.get("missing"))
        self.forbidden = [re.compile(p, re.IGNORECASE) for p in redlines.get("forbidden", [])]
        self.irreversible = [(re.compile(p, re.IGNORECASE), label)
                             for p, label in redlines.get("confirm", {}).items()]
        self.risk = {k: RiskLevel(str(v).upper()) for k, v in redlines.get("risk", {}).items()}
        # 未知角色必须失败关闭：拼错一个角色名不能等于「拿到全部权限」。
        self.policy = dict(redlines.get("roles", {}).get(role, {"allow": [], "draft": False}))

    def inspect(self, name: str, args: dict[str, Any]) -> Verdict:
        if self.missing:
            return Verdict(RiskLevel.RED_FORBIDDEN,
                           "安全策略文件缺失，已拒绝全部工具；请恢复 config/redlines.toml")
        payload = f"{name} {json.dumps(args, ensure_ascii=False, default=str)}"
        for pattern in self.forbidden:
            if pattern.search(payload):
                return Verdict(RiskLevel.RED_FORBIDDEN,
                               f"红线规则 /{pattern.pattern}/ 命中了这次调用，已直接拒绝")
        allow = list(self.policy.get("allow", ["*"]))
        if "*" not in allow and name not in allow:
            return Verdict(RiskLevel.RED_FORBIDDEN,
                           f"当前角色 {self.role} 没有「{human_name(name)}」的权限")
        level = self.risk.get(name, RiskLevel.RED_CONFIRM)  # 未登记 → 保守处理
        if level is RiskLevel.YELLOW:
            label = self._irreversible(payload)
            if label:
                return Verdict(RiskLevel.RED_CONFIRM,
                               f"这次要{label}，不可撤销，需要你点头",
                               draft=bool(self.policy.get("draft")))
        if level is RiskLevel.RED_CONFIRM:
            reason = f"「{human_name(name)}」会真的改动东西，需要你点头"
        else:
            reason = f"{level.value} · {name}"
        return Verdict(level, reason, draft=bool(self.policy.get("draft")))

    def _irreversible(self, payload: str) -> str:
        """参数里有没有「删掉/清空」这类不可逆动作，有就返回它的中文名。"""
        for pattern, label in self.irreversible:
            if pattern.search(payload):
                return label
        return ""


def human_name(name: str) -> str:
    """把工具名翻成人话：用工具自己的中文描述首句，取不到就退回原名。"""
    meta = REGISTRY.get(name)
    schema = (meta.schema.get("function", {}) if meta else {})
    text = schema.get("description") or name
    return re.split(r"[。；，,（(]", text, maxsplit=1)[0].strip() or name




# ---------------------------------------------------------------- 快照与回滚

def snapshot_file(path: Path, backup_root: Path) -> Path | None:
    """写盘前快照：`<backup>/<时间戳>/<文件名>` + `meta.json`（记录原始绝对路径）。

    正文与元数据必须用不同路径：目标文件本身就叫 `meta.json` 时，旧实现会让
    元数据覆盖唯一的备份正文，回滚把用户文件变成一段 JSON。
    文件尚不存在也留一条 `exists=false` 记录，这样「新建」也能撤销。
    """
    exists = path.exists()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch = backup_root / stamp
    batch.mkdir(parents=True, exist_ok=True)
    stored = "data.bin" if path.name == "meta.json" else path.name
    target = batch / stored
    digest = ""
    if exists:
        shutil.copy2(path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (batch / "meta.json").write_text(
        json.dumps({"original": str(path.resolve()), "name": path.name, "stored": stored,
                    "exists": exists, "sha256": digest, "ts": stamp}, ensure_ascii=False),
        encoding="utf-8",
    )
    (backup_root / "LATEST").write_text(str(batch), encoding="utf-8")
    return target if exists else None


def _latest_batch(backup_root: Path, filename: str | None = None) -> Path:
    batches = [p for p in backup_root.glob("*/meta.json")]
    batches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if filename:
        batches = [p for p in batches if _batch_meta(p).get("name") == filename]
    if not batches:
        raise FileNotFoundError("没有可回滚的快照，或该文件从未被修改过")
    return batches[0].parent


def _batch_meta(meta_path: Path) -> dict[str, Any]:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def restore_latest(backup_root: Path, filename: str | None = None) -> Path:
    """把最近一次快照写回原路径，返回被恢复的原始文件路径。

    回滚本身也要可撤销：覆盖前先把当前版本再存一份。备份正文要校验摘要，
    并且只接受「原始路径的 basename 与元数据 name 一致」的批次，避免被
    篡改的 meta 指向任意文件。
    """
    batch = _latest_batch(backup_root, filename)
    meta = _batch_meta(batch / "meta.json")
    original = Path(str(meta.get("original", "")))
    stored = str(meta.get("stored") or meta.get("name") or "")
    if not original.is_absolute() or not stored or Path(stored).name != stored:
        raise ValueError("快照元数据不完整，已拒绝回滚")
    if original.name != str(meta.get("name", "")):
        raise ValueError("快照元数据与目标文件名不一致，已拒绝回滚")
    if not meta.get("exists", True):
        original.unlink(missing_ok=True)
        return original
    source = batch / stored
    if not source.exists():
        raise FileNotFoundError(f"快照文件丢失：{source}")
    expected = str(meta.get("sha256") or "")
    if expected and hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError("快照内容校验失败，已拒绝回滚")
    if original.exists():
        snapshot_file(original, backup_root)
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, original)
    return original



# ---------------------------------------------------------------- 执行器

@dataclass
class Executor:
    """所有工具调用的唯一入口：红线 → 确认 → 快照 → 单飞 → 执行 → 审计。"""

    cfg: Config = field(default_factory=load)
    guard: Guard | None = None
    on_event: Callable[[str, str, str], None] = lambda state, label, detail: None
    confirm: Callable[[str, dict[str, Any], Verdict], bool] | None = None
    lock_timeout_s: float = 5.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.guard is None:
            self.guard = Guard(load_redlines(), str(self.cfg.get("workspace.role", "DEVELOPER")))
        self.backup_dir = self.cfg.path("workspace.backup_dir", ".voxos_backup")
        self.audit_path = self.cfg.path("audit.path", ".voxos/audit.jsonl")
        self.audit_enabled = self.cfg.flag("audit.enabled", True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # -- 公开入口 ---------------------------------------------------------
    def execute_safely(self, name: str, args: object = None, *, text: str = "") -> str:
        """唯一执行入口。参数在这里先过 Schema，再进红线、确认、快照。"""
        raw_args = {} if args is None else args
        if not isinstance(raw_args, dict):
            raw_args = {}
        started = time.perf_counter()
        meta = REGISTRY.get(name)
        if meta is None:
            ensure_tools()
            meta = REGISTRY.get(name)
        if meta is None:
            return self._record(name, raw_args, "未知工具", RiskLevel.RED_FORBIDDEN, started, text, "")
        invalid = validate_args(meta, args)
        if invalid:
            self.on_event("blocked", "参数不匹配", invalid)
            return self._record(name, raw_args, f"参数不匹配：{invalid}",
                                RiskLevel.RED_FORBIDDEN, started, text, "")
        verdict = self.guard.inspect(name, raw_args)  # type: ignore[union-attr]
        if verdict.level is RiskLevel.RED_FORBIDDEN:
            self.on_event("blocked", "已拦截", verdict.reason)
            return self._record(name, raw_args, f"⛔ {verdict.reason}", verdict.level, started, text, "")
        if verdict.level is RiskLevel.RED_CONFIRM and not self._ask(name, raw_args, verdict):
            self.on_event("blocked", "已取消", verdict.reason)
            return self._record(name, raw_args, "已取消（未获得二次确认）", verdict.level, started, text, "")
        result = self._run(meta, raw_args)
        if verdict.draft:
            result = f"【未审核草稿】{result}"
        return self._record(name, raw_args, result, verdict.level, started, text, meta.backup_arg)

    # -- 内部 -------------------------------------------------------------
    def _ask(self, name: str, args: dict[str, Any], verdict: Verdict) -> bool:
        # 确认事件由确认通道发出：那里才知道本次请求的 ID。
        # 这里再发一次会让界面出现一张没有 ID、点了也没用的卡。
        if self.confirm is None:
            self.on_event("blocked", "已拦截", "没有确认通道，按拒绝处理")
            return False
        try:
            return bool(self.confirm(name, args, verdict))
        except Exception as exc:  # 确认通道自身异常也要 fail-closed
            self.on_event("blocked", "已拦截", f"确认通道异常：{exc}")
            return False

    def _run(self, meta: Tool, args: dict[str, Any]) -> str:
        self.on_event("executing", _label(meta.name), "")
        gate = nullcontext() if meta.risk is RiskLevel.GREEN else self._single_flight()
        try:
            with gate:
                self._snapshot(meta, args)
                return str(meta.fn(**args))
        except TimeoutError:
            return "上一条修改类指令还在执行，请稍后再说一次"
        except TypeError as exc:
            return f"参数不匹配：{exc}"
        except Exception as exc:  # 工具内部异常不得冒泡到音频线程
            return f"执行异常：{exc}"

    @contextmanager
    def _single_flight(self) -> Iterator[None]:
        if not self._lock.acquire(timeout=self.lock_timeout_s):
            raise TimeoutError("single-flight timeout")
        try:
            yield
        finally:
            self._lock.release()

    def _snapshot(self, meta: Tool, args: dict[str, Any]) -> None:
        if not meta.backup_arg:
            return
        # 必须和 tools._resolve 用同一套解析（含 strip），否则「 demo.py 」
        # 会写到 demo.py、备份却找一个叫「 demo.py 」的文件，等于没备份。
        raw = str(args.get(meta.backup_arg) or "").strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():  # 与 tools._resolve 保持同一套相对路径规则
            path = self.cfg.path("workspace.default_code_dir", "~/Projects") / path
        if path.exists():
            snapshot_file(path, self.backup_dir)

    def _record(
        self,
        name: str,
        args: dict[str, Any],
        result: str,
        level: RiskLevel,
        started: float,
        text: str,
        backup_arg: str | None,
    ) -> str:
        elapsed = (time.perf_counter() - started) * 1000
        self._audit({
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "text": text,
            "tool": name,
            "args": {k: _shorten(v) for k, v in args.items()},
            "risk": level.value,
            "ms": round(elapsed, 3),
            # 成功不能靠「没命中某个中文前缀」推断：锁超时、业务错误、
            # 草稿包裹的异常都曾因此被记成 ok=true。
            "ok": _ok_text(result),
            "result": _shorten(result),
            "backup": backup_arg,
        })
        self.on_event("done", "完成" if not result.startswith("⛔") else "已拦截", result)
        return result

    def _audit(self, record: dict[str, Any]) -> None:
        if not self.audit_enabled:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[voxos] 审计写入失败：{exc}")


def _label(name: str) -> str:
    return {
        "append_structured_log": "写日志…",
        "dispatch_code_session": "改代码…",
        "inspect_server_ops": "巡检…",
        "write_code": "改文件…",
        "rollback_file": "回滚…",
        "search_notes": "查笔记…",
        "read_file_head": "读文件…",
        "open_in_ide": "打开 IDE…",
    }.get(name, f"{name}…")


_FAILURE_PREFIXES = ("⛔", "执行异常", "参数不匹配", "已取消", "错误：", "没有可回滚",
                     "快照", "上一条修改类指令还在执行", "未找到", "文件不存在")


def _ok_text(result: str) -> bool:
    """工具结果是不是成功。草稿前缀先剥掉，避免异常被【未审核草稿】藏住。"""
    text = str(result).lstrip()
    while text.startswith("【未审核草稿】"):
        text = text[len("【未审核草稿】"):].lstrip()
    return not text.startswith(_FAILURE_PREFIXES)


def _shorten(value: Any, limit: int = 120) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


