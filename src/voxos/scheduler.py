"""定时任务：一句话说「十分钟后提醒我给张三发消息」，进程重启也不丢。

为什么不用 cron / launchd：那些要写系统配置、要管理员权限、改完要重载，
和「说一句就成」的交互完全不搭。为什么不用 `threading.Timer`：进程一重启
定时就没了——「每天早上九点提醒我」必须活过重启，否则这功能只是看起来能用。

所以落一个 JSON 文件 + 一个 5 秒轮询的线程。文件删了就等于清空，
状态可见、可手改、可进版本库（如果有人想的话）。

存的是**那句话本身**，不是「工具名 + 参数」：到点时重新走一遍正常的派发，
路由、提示词、新加的工具全都自动生效。存参数等于把今天的工具表冻在磁盘上。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import Config

__all__ = ["add", "pending", "due", "clear", "path_of", "start", "parse_at", "stamp"]

DAY = 86400.0


def path_of(cfg: Config) -> Path:
    return cfg.path("workspace.schedule_file", ".voxos/schedules.json")


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save(path: Path, items: list[dict[str, Any]]) -> None:
    """先写临时文件再替换：写到一半断电不会留下一个解析不了的调度表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_at(text: str) -> float:
    """把「2026-10-29 09:00」或「+600」（秒）解析成时间戳。解析不了返回 0。

    只给时刻（`09:30`）时补今天的日期；已经过了就顺延到明天——人说「九点半
    提醒我」在晚上九点说出来，指的是明天那个九点半。带日期的写法不猜年份：
    算出来是过去就如实返回过去，由调用方说「这个时间已经过去了」。
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0
    if raw.startswith("+"):
        try:
            return time.time() + float(raw[1:])
        except ValueError:
            return 0.0
    now = time.localtime()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"):
        try:
            stamp = time.strptime(raw, fmt)
        except ValueError:
            continue
        clock_only = fmt == "%H:%M"
        moment = time.mktime((now.tm_year,
                              now.tm_mon if clock_only else stamp.tm_mon,
                              now.tm_mday if clock_only else stamp.tm_mday,
                              stamp.tm_hour, stamp.tm_min, 0, 0, 0, -1))
        return moment + DAY if clock_only and moment <= time.time() else moment
    # `10-29 09:00`：先补今年再按完整格式解析。交给 `%m-%d` 会在 Python 3.13
    # 触发「缺年份」弃用警告，3.15 起直接报错——补一个年份比等它炸便宜。
    head = raw.split(" ")[0]
    if head.count("-") == 1 and all(part.isdigit() for part in head.split("-")):
        return parse_at(f"{now.tm_year}-{raw}")
    return 0.0


def stamp(when: float) -> str:
    """给用户看的时间：`10-29 09:00`。"""
    return time.strftime("%m-%d %H:%M", time.localtime(when))


def add(cfg: Config, text: str, when: float, label: str = "",
        repeat: str = "", approved: bool = False) -> dict[str, Any]:
    """排一条。`repeat` 目前只支持 `daily`（每天同一时刻）。

    `approved` 记的是「创建这条任务时用户点过头」。到点时只有它为真才允许
    执行需要二次确认的动作——否则一次「每天早上发消息」会在半夜卡在确认上，
    然后按超时拒绝掉，用户永远不知道它没发出去。
    """
    path = path_of(cfg)
    items = _load(path)
    item = {"id": uuid.uuid4().hex[:8], "due": when, "text": text,
            "label": label or text, "repeat": repeat, "approved": bool(approved)}
    items.append(item)
    _save(path, items)
    return item


def pending(cfg: Config) -> list[dict[str, Any]]:
    return sorted(_load(path_of(cfg)), key=lambda item: float(item.get("due", 0)))


def clear(cfg: Config, target: str = "") -> int:
    """删掉一条（按 id 或说明里的关键词）或全部。返回删了几条。"""
    path = path_of(cfg)
    items = _load(path)
    keep = [item for item in items
            if target and str(item.get("id")) != target and target not in str(item.get("label"))]
    _save(path, keep)
    return len(items) - len(keep)


def due(cfg: Config, now: float | None = None) -> list[dict[str, Any]]:
    """取出到期的项并从表里摘掉（`daily` 的顺延到明天）。"""
    path = path_of(cfg)
    moment = time.time() if now is None else now
    items = _load(path)
    ready = [item for item in items if float(item.get("due", 0)) <= moment]
    if not ready:
        return []
    rest = [item for item in items if float(item.get("due", 0)) > moment]
    for item in ready:
        if item.get("repeat") == "daily":
            rest.append({**item, "due": float(item["due"]) + DAY})
    _save(path, rest)
    return ready


def start(cfg: Config, run: Callable[[dict[str, Any]], str],
          every: float = 5.0, on_event: Callable[[str, str, str], None] | None = None) -> threading.Thread:
    """起一个守护线程。`run(item)` 由调用方提供（派发在 core/tools 层）。"""
    def loop() -> None:
        while True:
            try:
                for item in due(cfg):
                    if on_event:
                        on_event("executing", "定时任务", str(item.get("label", "")))
                    run(item)
            except Exception as exc:          # 一条坏任务不能把整个调度线程带走
                if on_event:
                    on_event("blocked", "定时任务失败", str(exc))
            time.sleep(every)

    thread = threading.Thread(target=loop, name="voxos-scheduler", daemon=True)
    thread.start()
    return thread
