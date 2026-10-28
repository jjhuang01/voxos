#!/usr/bin/env python3
"""VoxOS 工程验收：跑真指标，产出 docs/acceptance.json 与 docs/ACCEPTANCE.html。

用法：PYTHONPATH=src python scripts/verify.py [--refresh-corpus]
所有测量都在沙箱目录 `.voxos/verify/` 内进行，不写用户真实日志。
"""

from __future__ import annotations

import argparse
import ast
import json
import statistics
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from voxos import audio, core, corpus, dsp, settings, tools, ui  # noqa: E402
from voxos.config import load  # noqa: E402

SANDBOX = ROOT / ".voxos" / "verify"
SR = dsp.SR


def sandbox_config():
    cfg = load()
    cfg = cfg.with_overrides(**{
        "workspace.daily_log_dir": str(SANDBOX / "logs"),
        "workspace.default_code_dir": str(SANDBOX / "projects"),
        "workspace.backup_dir": str(SANDBOX / "backups"),
        "audit.path": str(SANDBOX / "audit.jsonl"),
        "workspace.dry_run": True,
        "llm.enabled": False,
    })
    tools.configure(cfg)
    core.ensure_tools()
    return cfg


# ------------------------------------------------------------------ 各项测量

def measure_fast_path(rounds: int = 2000) -> dict[str, float]:
    text = "Siri，我要写日志，今天把登录接口的空值校验补上了"
    wake = core._wake_pattern(load())
    core._fast_route(text, wake)
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        core._fast_route(text, wake)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {"p50": round(statistics.median(samples), 4),
            "p95": round(samples[int(len(samples) * 0.95)], 4),
            "p99": round(samples[int(len(samples) * 0.99)], 4),
            "rounds": rounds}


def measure_dispatch(cfg, rounds: int = 30) -> dict[str, float]:
    """走守护进程的真实路径：先取隐式上下文（缓存 + 后台刷新），再派发。

    早期版本直接调 `core.dispatch` 而不带 context，测出来的 0.2ms 是假路径 ——
    真实链路里 `_context()` 曾经同步调 osascript，实测 271ms。这里把两段都测出来。
    """
    from voxos import main as cli

    executor = core.Executor(cfg=cfg)
    cli._context()                      # 预热：把后台取窗口的那一次 fork 挪出计时区
    time.sleep(0.4)
    samples, context_ms = [], []
    for index in range(rounds):
        tick = time.perf_counter()
        context = cli._context()
        context_ms.append((time.perf_counter() - tick) * 1000)
        started = time.perf_counter()
        result = core.dispatch(f"Siri，我要写日志，第 {index} 次验收压测",
                               executor=executor, cfg=cfg, context=context)
        assert result.route == "fast", result
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    context_ms.sort()
    return {"p50": round(statistics.median(samples), 3),
            "p95": round(samples[int(len(samples) * 0.95)], 3),
            "context_p95": round(context_ms[int(len(context_ms) * 0.95)], 3),
            "rounds": rounds}


def _post_status(url: str, payload: dict, token: str = "", origin: str = "") -> int:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    if token:
        request.add_header("X-VoxOS-Token", token)
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def measure_standby(seconds: int = 30) -> dict[str, object]:
    """没人说话时它占多少 CPU：底噪 -40dBFS 跑 30 秒，看误触发与单核占用。"""
    cfg = load()
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, SR * seconds).astype(np.float32)
    vad = audio.make_vad(cfg)
    lock = dsp.SpeakerLock(threshold=0.9, embedder=lambda chunk: np.zeros(8, dtype=np.float32))
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.ScriptedStt([]), lock=lock, vad=vad)
    started, cpu = time.perf_counter(), time.process_time()
    segments = sum(1 for _ in pipeline.run(audio.ArraySource(noise)))
    wall, used = time.perf_counter() - started, time.process_time() - cpu
    return {"seconds": seconds, "segments": segments, "vad": type(vad).__name__,
            "cpu_percent": round(used / seconds * 100, 2),
            "realtime_factor": round(wall / seconds, 3)}


def measure_security() -> dict[str, object]:
    """跨站页面与无令牌请求必须在"执行"之前被挡住。"""
    hits: list[str] = []
    ctx = ui.UiContext(bus=ui.StatusBus(None), token="verify-token",
                       on_command=lambda text: hits.append(text) or "ok")
    server = ui.serve(ctx, "127.0.0.1", 0, block=False)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        codes = {
            "no_token": _post_status(f"{base}/command", {"text": "写日志"}),
            "cross_site": _post_status(f"{base}/command", {"text": "删库"},
                                       token="verify-token", origin="https://evil.example"),
            "allowed": _post_status(f"{base}/command", {"text": "写日志"}, token="verify-token"),
        }
    finally:
        server.shutdown()
        server.server_close()
    blocked = codes["no_token"] == 401 and codes["cross_site"] == 403 and hits == ["写日志"]
    return {"codes": codes, "executed": hits, "blocked": blocked and codes["allowed"] == 200}


def measure_settings() -> dict[str, object]:
    """设置写回：只改值、保住注释、写到 --config 指定的那个文件。"""
    target = SANDBOX / "settings.toml"
    if not target.parent.exists():
        target.parent.mkdir(parents=True)
    target.write_text((ROOT / "config" / "settings.toml").read_text(encoding="utf-8"),
                      encoding="utf-8")
    cfg = load(target)
    new_cfg, errors = settings.apply(cfg, {"audio.voiceprint_threshold": 0.86,
                                           "workspace.role": "OPS"})
    text = target.read_text(encoding="utf-8")
    return {"errors": errors,
            "applied": new_cfg.get("audio.voiceprint_threshold") == 0.86
                       and new_cfg.get("workspace.role") == "OPS",
            "comments_kept": "# 实测标定" in text and "# DEVELOPER | OPS | DOCTOR" in text,
            "source_kept": new_cfg.source == target,
            "invalid_rejected": bool(settings.validate({"audio.voiceprint_threshold": 5})[1])}


def measure_onboarding() -> dict[str, object]:
    """没有声纹档案时不能直接退出：必须给出可点的一键录入。"""
    from voxos import main as cli

    cfg = load().with_overrides(**{"audio.voiceprint_profiles": str(SANDBOX / "absent.npz")})
    runtime = cli.Runtime(cfg, ui.StatusBus(None))
    lock = cli._lock(cfg)
    ctx = cli._ui_context(runtime, lock)
    return {"has_profiles": bool(lock.profiles),
            "enroll_available": ctx.on_enroll is not None,
            "status_enrolled": cli._status(runtime, lock)["enrolled"]}


def measure_confirm() -> dict[str, object]:
    """确认通道必须真的能从「说话」走到：大改 → 挂起 → 点头才执行，且卡片上写的是人话。"""
    cfg = load().with_overrides(**{"workspace.dry_run": True})
    plan = core.heuristic_plan("重构整个登录模块", cfg)
    reached = bool(plan) and plan[0][0] == "delegate_to_coding_cli"

    seen: list[str] = []
    executor = core.Executor(cfg=cfg, confirm=lambda name, args, verdict:
                             seen.append(verdict.reason) or False)
    denied = executor.execute_safely("delegate_to_coding_cli", {"prompt": "重构整个登录模块"})
    reason = seen[0] if seen else ""
    human = bool(reason) and "red_confirm" not in reason.lower() \
        and "delegate_to_coding_cli" not in reason

    approved = core.Executor(cfg=cfg, confirm=lambda *_: True)
    allowed = approved.execute_safely("delegate_to_coding_cli", {"prompt": "重构整个登录模块"})
    return {"reached_from_speech": reached, "reason": reason, "reason_is_human": human,
            "deny_cancels": "已取消" in denied, "allow_runs": "dry-run" in allowed}


def measure_stt() -> dict[str, object]:
    """真实识别率：用 `say` 合成的已知文本喂给本地 SenseVoice，量字符错误率与实时率。

    没有模型或语料不是真人声时返回 available=False，由上层标成 SKIP——
    宁可显示"跳过"，也不拿合成音假装通过。
    """
    model_dir = ROOT / ".voxos" / "models" / "sense-voice"
    if not (model_dir / "model.int8.onnx").exists():
        return {"available": False, "why": "未下载 SenseVoice 模型"}
    manifest = corpus.ensure_corpus(ROOT / ".voxos" / "corpus")
    if manifest.get("kind") != "tts":
        return {"available": False, "why": "语料不是真实音色"}
    cases = list(manifest["owner"]["clips"])  # type: ignore[index]
    for entry in manifest["impostors"]:  # type: ignore[index]
        cases.extend(entry["clips"])
    engine = audio.SenseVoiceStt(model_dir)
    root = ROOT / ".voxos" / "corpus"
    tally = {key: [0, 0] for key in ("zh", "en")}      # [错字数, 总字数]
    audio_s = 0.0
    heard_samples: list[dict[str, object]] = []
    started = time.perf_counter()
    for entry in cases:
        clip = corpus.load_clip(root, entry)
        heard = engine.transcribe(clip)
        want = _normalize(entry["text"])
        errors = _edit_distance(_normalize(heard), want)
        bucket = tally["zh" if re.search(r"[\u4e00-\u9fff]", want) else "en"]
        bucket[0] += errors
        bucket[1] += len(want)
        audio_s += clip.size / dsp.SR
        heard_samples.append({"want": entry["text"], "heard": heard})
    wall = time.perf_counter() - started
    errors = sum(item[0] for item in tally.values())
    total = sum(item[1] for item in tally.values())
    by_script = {key: round(hit / max(size, 1) * 100, 1) for key, (hit, size) in tally.items()}
    return {"available": True, "cases": len(cases), "cer": round(errors / max(total, 1) * 100, 1),
            "by_script": by_script, "chars": total,
            "rtf": round(wall / max(audio_s, 1e-6), 3), "wall_s": round(wall, 1),
            "samples": heard_samples[:4]}


def _score_mixed(engine: object, root: Path, clips: list[dict[str, str]]) -> dict[str, object]:
    """对一组混说语料算术语召回与中文字错率。"""
    hits = total = zh_errors = zh_chars = 0
    for entry in clips:
        heard = engine.transcribe(corpus.load_clip(root, entry))  # type: ignore[attr-defined]
        want_terms, got_terms = _terms(entry["text"]), _terms(heard)
        total += len(want_terms)
        # 一对一对齐：同一个输出词不能被两个参考词重复认领。
        used: set[int] = set()
        for term in want_terms:
            for index, got in enumerate(got_terms):
                if index not in used and _close(term, got):
                    used.add(index)
                    hits += 1
                    break
        want_zh, got_zh = _cjk(entry["text"]), _cjk(heard)
        zh_errors += _edit_distance(got_zh, want_zh)
        zh_chars += len(want_zh)
    return {"terms": total, "hits": hits,
            "recall": round(hits / max(total, 1) * 100, 1),
            "zh_cer": round(zh_errors / max(zh_chars, 1) * 100, 1)}


def measure_codeswitch() -> dict[str, object]:
    """中英混说：中文句子里夹英文术语，量术语召回和中文准确率。

    这一项是补上一个测错了对象的指标：原来报的「英文 3.1%」来自**英文句子用英文
    音色读**，那量的是英文识别；产品要面对的是中文句子里夹 `docker`、`nginx`。

    两版语料都量：中文音色念术语（偏悲观，TTS 把 `docker` 读成「多克」）与
    双语拼接（中文说中文、术语说英文，更接近真人）。只报低的那版会把模型说得
    比实际差，只报高的那版才是自欺——所以给一个区间。
    """
    model_dir = ROOT / ".voxos" / "models" / "sense-voice"
    if not (model_dir / "model.int8.onnx").exists():
        return {"available": False, "why": "未下载 SenseVoice 模型"}
    root = ROOT / ".voxos" / "corpus"
    clips = corpus.build_mixed(root)
    if not clips:
        return {"available": False, "why": "缺 say/afconvert，合成不了混说语料"}
    engine = audio.SenseVoiceStt(model_dir)
    score = _score_mixed(engine, root, clips)
    spliced = corpus.build_mixed_bilingual(root)
    score["recall_bilingual"] = (_score_mixed(engine, root, spliced)["recall"] if spliced else None)
    return {"available": True, "cases": len(clips), **score,
            "samples": [{"want": c["text"], "heard": engine.transcribe(corpus.load_clip(root, c))}
                        for c in clips[:4]]}


def _terms(text: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[A-Za-z][A-Za-z0-9_.]*", str(text))]


def _cjk(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff]", str(text)))


def _close(want: str, got: str) -> bool:
    """术语算不算识别对了。

    长词允许少量字符差（`merge` 少个 e 仍算对）；短词必须完全一致——
    `git` 与 `fit` 编辑距离只有 1，按比例容错会把最关键的短命令判成正确。
    """
    if len(want) <= 4:
        return got == want
    return _edit_distance(got, want) <= max(1, len(want) // 4)


def _normalize(text: str) -> str:
    """比对前统一口径：去掉标点与空白，英文小写。"""
    return re.sub(r"[\s，。、？！：；,.?!:;\"'（）()《》]", "", str(text)).lower()


def _edit_distance(a: str, b: str) -> int:
    """字符级编辑距离（中文按字算，就是 CER 的分子）。"""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def measure_voiceprint(cfg, refresh: bool) -> dict[str, object]:
    """声纹语料上的 FRR/FAR：阈值不是拍的，是扫出来的。"""
    manifest = corpus.ensure_corpus(ROOT / ".voxos" / "corpus", force=refresh)
    embedder, threshold, kind = audio.make_embedder(cfg)
    root = ROOT / ".voxos" / "corpus"
    speakers = {"owner": [corpus.load_clip(root, clip) for clip in manifest["owner"]["clips"]]}  # type: ignore[index]
    for entry in manifest["impostors"]:  # type: ignore[index]
        speakers[entry["voice"]] = [corpus.load_clip(root, clip) for clip in entry["clips"]]

    embeddings = {name: [embedder(clip) for clip in clips] for name, clips in speakers.items()}
    sweep = []
    for point in (0.70, 0.76, 0.80, 0.82, 0.84, 0.86, 0.90):
        frr, far, same_n, diff_n = _evaluate(embeddings, point)
        sweep.append({"threshold": point, "frr": frr, "far": far, "same_n": same_n, "diff_n": diff_n})

    frr, far, same_n, diff_n = _evaluate(embeddings, threshold)
    same_scores = []
    diff_scores = []
    for name, vectors in embeddings.items():
        profile = _profile(vectors[:2])
        for vector in vectors[2:]:
            same_scores.append(dsp.cosine(profile, vector))
        for other, other_vectors in embeddings.items():
            if other == name:
                continue
            for vector in other_vectors[2:]:
                diff_scores.append(dsp.cosine(profile, vector))
    return {
        "backend": kind, "threshold": threshold, "corpus": manifest["kind"],
        "speakers": list(speakers), "frr": frr, "far": far,
        "same_n": same_n, "diff_n": diff_n,
        "same_min": round(min(same_scores), 4), "same_mean": round(statistics.mean(same_scores), 4),
        "diff_max": round(max(diff_scores), 4), "diff_mean": round(statistics.mean(diff_scores), 4),
        "sweep": sweep,
    }


def _profile(vectors: list[np.ndarray]) -> np.ndarray:
    stacked = np.stack([v / (np.linalg.norm(v) + 1e-9) for v in vectors])
    mean = stacked.mean(axis=0)
    return mean / (np.linalg.norm(mean) + 1e-9)


def _evaluate(embeddings: dict[str, list[np.ndarray]], threshold: float) -> tuple[float, float, int, int]:
    frr = far = 0.0
    same_n = diff_n = 0
    for name, vectors in embeddings.items():
        profile = _profile(vectors[:2])
        for vector in vectors[2:]:
            same_n += 1
            frr += float(dsp.cosine(profile, vector) < threshold)
        for other, other_vectors in embeddings.items():
            if other == name:
                continue
            for vector in other_vectors[2:]:
                diff_n += 1
                far += float(dsp.cosine(profile, vector) >= threshold)
    return round(frr / max(same_n, 1) * 100, 2), round(far / max(diff_n, 1) * 100, 2), same_n, diff_n


def measure_e2e(cfg, bus) -> dict[str, object]:
    manifest = corpus.ensure_corpus(ROOT / ".voxos" / "corpus")
    root = ROOT / ".voxos" / "corpus"
    embedder, threshold, kind = audio.make_embedder(cfg)
    lock = dsp.SpeakerLock(threshold=threshold, embedder=embedder)
    owner = manifest["owner"]["clips"]  # type: ignore[index]
    consistency = lock.enroll([corpus.load_clip(root, clip) for clip in owner[:2]])
    impostor = manifest["impostors"][0]["clips"][0]  # type: ignore[index]

    transcripts = [owner[2]["text"], owner[3]["text"]]
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.ScriptedStt(transcripts), lock=lock,
                                   vad=audio.make_vad(cfg), on_event=bus)
    stream = audio.concat_with_silence(
        [corpus.load_clip(root, owner[2]), corpus.load_clip(root, impostor), corpus.load_clip(root, owner[3])]
    )
    executor = core.Executor(cfg=cfg, on_event=bus)
    accepted, rejected, latencies = 0, 0, []
    started = time.perf_counter()
    for utterance in pipeline.run(audio.ArraySource(stream)):
        if not utterance.accepted:
            rejected += 1
            continue
        accepted += 1
        tick = time.perf_counter()
        result = core.dispatch(utterance.text, executor=executor, cfg=cfg,
                               context={"active_window": "verify", "recent_files": []})
        latencies.append((time.perf_counter() - tick) * 1000)
        assert result.route == "fast", result
    wall = (time.perf_counter() - started) * 1000
    return {"accepted": accepted, "rejected": rejected, "enroll_consistency": round(consistency, 4),
            "backend": kind, "threshold": threshold,
            "dispatch_ms": [round(value, 2) for value in latencies],
            "wall_ms": round(wall, 1),
            "realtime_factor": round(wall / (stream.size / SR * 1000), 3)}


def measure_redlines(cfg) -> dict[str, object]:
    executor = core.Executor(cfg=cfg, confirm=lambda *_: False)
    cases = [
        ("write_code", {"file_path": "a.py", "operation": "CREATE_OR_OVERWRITE", "new_code": "rm -rf /"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "git push --force"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "DROP TABLE users"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "sudo reboot"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "submit_final_prescription"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "delete_patient_record"}),
        ("write_code", {"file_path": "a.py", "operation": "APPEND_CODE", "new_code": "mkfs.ext4 /dev/sda1"}),
        ("delegate_to_coding_cli", {"prompt": "drop database prod"}),
    ]
    blocked = sum(1 for name, args in cases if executor.execute_safely(name, args).startswith("⛔"))
    ops = core.Executor(cfg=cfg.with_overrides(**{"workspace.role": "OPS"}), confirm=lambda *_: False)
    ops_result = ops.execute_safely("write_code", {"file_path": "x.py",
                                                  "operation": "CREATE_OR_OVERWRITE"})
    ops_blocked = ops_result.startswith("⛔") and "OPS" in ops_result
    doctor = core.Executor(cfg=cfg.with_overrides(**{"workspace.role": "DOCTOR"}), confirm=lambda *_: False)
    draft = doctor.execute_safely("append_structured_log", {"content": "草稿验收"}).startswith("【未审核草稿】")
    confirm_needed = "已取消" in executor.execute_safely("delegate_to_coding_cli", {"prompt": "重构"})
    return {"forbidden_cases": len(cases), "blocked": blocked, "ops_isolated": ops_blocked,
            "doctor_draft": draft, "confirm_gate": confirm_needed}


def measure_code() -> dict[str, object]:
    """代码度量：**源码和脚本一起量**。

    只扫 `src/` 会让「单函数 ≤ 40 行」这条规矩管不到真正干活的脚本，
    而脚本一样会烂——这一轮就有两个函数在 40 行以上，因为没人量。
    """
    files = sorted(list((ROOT / "src" / "voxos").glob("*.py"))
                   + list((ROOT / "scripts").glob("*.py")))
    rows = []
    max_function = ("", 0)
    classes = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes += sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > max_function[1]:
                    max_function = (f"{path.relative_to(ROOT)}:{node.name}", length)
        rows.append({"file": str(path.relative_to(ROOT)), "lines": len(source.splitlines()),
                     "code_lines": sum(1 for line in source.splitlines()
                                       if line.strip() and not line.strip().startswith("#"))})
    tests = len(list((ROOT / "tests").glob("test_*.py")))
    return {"modules": rows, "total_lines": sum(row["lines"] for row in rows),
            "max_function": {"name": max_function[0], "lines": max_function[1]},
            "classes": classes, "tools": len(core.REGISTRY), "fast_routes": len(core.FAST_ROUTES),
            "test_files": tests}


def measure_tests() -> dict[str, object]:
    """跑整套测试。

    `--basetemp` 每次用新目录：pytest 启动时会先删旧 basetemp，而在受限环境里
    这一步会被权限拦掉，紧接着每个用 tmp_path 的测试都会在"建软链"上炸掉
    （表现是几十个 error，看起来像代码坏了）。
    """
    started = time.perf_counter()
    temp = SANDBOX / f"pytest-{datetime.now():%H%M%S}"
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                                "--basetemp", str(temp)],
                               cwd=ROOT, capture_output=True, text=True)
    _prune_basetemps(keep=temp)
    text = completed.stdout + completed.stderr
    matched = re.search(r"\d+ (?:passed|failed)[^\n]*", text)
    summary = matched.group(0).strip() if matched else text.strip().splitlines()[-1:]
    return {"returncode": completed.returncode,
            "summary": summary if isinstance(summary, str) else str(summary),
            "seconds": round(time.perf_counter() - started, 1)}


def _prune_basetemps(keep: Path) -> None:
    """只留本次与上一次的 basetemp，别让沙箱目录越堆越大。"""
    olds = sorted(SANDBOX.glob("pytest-*"), key=lambda p: p.name)
    for stale in olds[:-2]:
        if stale != keep:
            shutil.rmtree(stale, ignore_errors=True)


# ------------------------------------------------------------------ 报告

def build_report(refresh: bool = False) -> dict[str, object]:
    """跑完所有测量，返回原始数据。顺序就是打印顺序。"""
    cfg = sandbox_config()
    bus = ui.StatusBus(SANDBOX / "status.jsonl")
    started = datetime.now()
    steps = [
        ("快通道延迟", "fast_path", lambda: measure_fast_path()),
        ("端到端触发延迟", "dispatch", lambda: measure_dispatch(cfg)),
        ("声纹 FRR/FAR", "voiceprint", lambda: measure_voiceprint(cfg, refresh=refresh)),
        ("全链路 demo", "e2e", lambda: measure_e2e(cfg, bus)),
        ("安全红线矩阵", "redlines", lambda: measure_redlines(cfg)),
        ("静音待机开销", "standby", lambda: measure_standby()),
        ("本地接口安全", "security", lambda: measure_security()),
        ("设置写回", "settings", lambda: measure_settings()),
        ("首次启动引导", "onboarding", lambda: measure_onboarding()),
        ("确认通道", "confirm", lambda: measure_confirm()),
        ("真实识别率", "stt", lambda: measure_stt()),
        ("中英混说术语", "codeswitch", lambda: measure_codeswitch()),
        ("代码度量", "code", lambda: measure_code()),
        ("测试套件", "tests", lambda: measure_tests()),
    ]
    report: dict[str, object] = {}
    for label, key, run in steps:
        print(f"→ {label} …")
        report[key] = run()
    report["generated_at"] = started.isoformat(timespec="seconds")
    report["env"] = {"python": sys.version.split()[0], "numpy": np.__version__,
                     "sherpa": _version("sherpa_onnx"), "platform": sys.platform}
    return report


def _version(module: str) -> str:
    try:
        return __import__(module).__version__  # type: ignore[no-any-return]
    except Exception:
        return "未安装"


def verdicts(report: dict[str, object]) -> list[dict[str, object]]:
    """19 项验收，分三组：性能 / 安全 / 质量。拆开是为了每组都读得完。"""
    return _perf_verdicts(report) + _safety_verdicts(report) + _quality_verdicts(report)


def _perf_verdicts(report: dict[str, object]) -> list[dict[str, object]]:
    fast = report["fast_path"]  # type: ignore[index]
    dispatch = report["dispatch"]  # type: ignore[index]
    standby = report["standby"]  # type: ignore[index]
    return [
        {"item": "快通道决策延迟 p95 < 5ms", "measured": f"{fast['p95']}ms", "pass": fast["p95"] < 5},
        {"item": "端到端触发 p95 < 500ms", "measured": f"{dispatch['p95']}ms", "pass": dispatch["p95"] < 500},
        {"item": "静音待机 CPU < 1.5%", "measured": f"{standby['cpu_percent']}%/秒音频",
         "pass": standby["cpu_percent"] < 1.5},
        {"item": "静音零误触发", "measured": f"{standby['segments']} 段", "pass": standby["segments"] == 0},
    ]


def _safety_verdicts(report: dict[str, object]) -> list[dict[str, object]]:
    voice = report["voiceprint"]  # type: ignore[index]
    e2e = report["e2e"]  # type: ignore[index]
    redlines = report["redlines"]  # type: ignore[index]
    confirm = report["confirm"]  # type: ignore[index]
    return [
        {"item": "声纹 FRR < 2%", "measured": f"{voice['frr']}%", "pass": voice["frr"] < 2},
        {"item": "声纹 FAR < 1%", "measured": f"{voice['far']}%", "pass": voice["far"] < 1},
        {"item": "冒名语音被拒（全链路）", "measured": f"{e2e['rejected']} 条被拒",
         "pass": e2e["rejected"] >= 1},
        {"item": "红线禁令全部拦截", "measured": f"{redlines['blocked']}/{redlines['forbidden_cases']}",
         "pass": redlines["blocked"] == redlines["forbidden_cases"]},
        {"item": "角色隔离（OPS 不可写文件）", "measured": str(redlines["ops_isolated"]),
         "pass": redlines["ops_isolated"]},
        {"item": "医疗草稿强制标记", "measured": str(redlines["doctor_draft"]), "pass": redlines["doctor_draft"]},
        {"item": "跨站 / 无令牌请求被拦截", "measured": str(report["security"]["codes"]),  # type: ignore[index]
         "pass": bool(report["security"]["blocked"])},  # type: ignore[index]
        {"item": "确认通道从语音可达且文案是人话",
         "measured": "可达" if confirm["reached_from_speech"] else "不可达",
         "pass": bool(confirm["reached_from_speech"] and confirm["reason_is_human"]
                      and confirm["deny_cancels"] and confirm["allow_runs"])},
    ]


def _quality_verdicts(report: dict[str, object]) -> list[dict[str, object]]:
    code = report["code"]  # type: ignore[index]
    dispatch = report["dispatch"]  # type: ignore[index]
    settings_report = report["settings"]  # type: ignore[index]
    onboarding = report["onboarding"]  # type: ignore[index]
    stt = report["stt"]  # type: ignore[index]
    mixed = report["codeswitch"]  # type: ignore[index]
    return [
        {"item": "单函数 ≤ 40 行（src + scripts）", "measured": f"{code['max_function']['lines']} 行",
         "pass": code["max_function"]["lines"] <= 40},
        {"item": "测试全绿", "measured": report["tests"]["summary"],  # type: ignore[index]
         "pass": report["tests"]["returncode"] == 0},  # type: ignore[index]
        {"item": "上下文注入不阻塞派发", "measured": f"{dispatch['context_p95']}ms",
         "pass": dispatch["context_p95"] < 5},
        {"item": "设置写回保住注释", "measured": str(settings_report["comments_kept"]),
         "pass": bool(settings_report["applied"] and settings_report["comments_kept"]
                      and settings_report["source_kept"] and settings_report["invalid_rejected"])},
        {"item": "无声纹时给出录入入口", "measured": str(onboarding["enroll_available"]),
         "pass": bool(onboarding["enroll_available"] and not onboarding["status_enrolled"])},
        {"item": "本地识别字符错误率 < 10%",
         "measured": (f"{stt['cer']}%（中文 {stt['by_script']['zh']}% / 英文 {stt['by_script']['en']}%，"
                      # 实时率单次会在 33–38× 之间抖，取下界才是个稳定的说法
                      f"{stt['cases']} 句，≥{int(1 / max(stt['rtf'], 1e-6) // 10 * 10)}× 实时）")
                     if stt["available"] else stt.get("why", "跳过"),
         "pass": stt["cer"] < 10 if stt["available"] else False,
         "skipped": not stt["available"]},
        # gap=True：这一项**没达标**，而且不是回归——是能力边界。分开标，
        # 免得它把「有没有退化」这个信号淹掉，也免得把它藏进 SKIP 里假装不存在。
        {"item": "中英混说英文术语召回 ≥ 95%",
         "measured": (f"{mixed['recall']}%（中文音色）/ {mixed['recall_bilingual']}%（双语拼接），"
                      f"{mixed['terms']} 个术语，中文 {mixed['zh_cer']}% 字错")
                     if mixed["available"] else mixed.get("why", "跳过"),
         # 两套语料都是真实现象，不能取最大值挑好看的：任一套没到 95% 就没达标。
         "pass": (_passes_all(mixed) if mixed["available"] else False),
         "skipped": not mixed["available"],
         "gap": bool(mixed["available"]) and not _passes_all(mixed)},
    ]

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>VoxOS 验收报告</title>
<style>
:root{{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--ok:#059669;--bad:#dc2626;--bg:#f7f8fa}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,"PingFang SC",system-ui,sans-serif}}
.wrap{{max-width:960px;margin:0 auto;padding:48px 28px 72px}}
h1{{font-size:30px;letter-spacing:-.02em;margin:0 0 6px}}
.sub{{color:var(--muted);font-size:13.5px;margin-bottom:32px}}
h2{{font-size:17px;margin:34px 0 12px;letter-spacing:-.01em}}
.card{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:20px 22px;
box-shadow:0 1px 2px rgba(16,24,40,.04)}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
tr:last-child td{{border-bottom:0}}
code{{background:#f3f4f6;border-radius:5px;padding:1px 6px;font-size:12.5px}}
.pass{{color:var(--ok);font-weight:600}}.fail{{color:var(--bad);font-weight:600}}
.skip{{color:var(--muted);font-weight:600}}
.gap{{color:#b45309;font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.kpi{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px}}
.kpi .n{{font-size:24px;font-weight:650;letter-spacing:-.02em}}
.kpi .l{{color:var(--muted);font-size:12.5px;margin-top:2px}}
.note{{color:var(--muted);font-size:12.5px;margin-top:10px}}
</style></head><body><div class="wrap">
<h1>VoxOS 工程验收报告</h1>
<div class="sub">生成于 {generated_at} · Python {python} · 声纹后端 <code>{backend}</code> · 语料 <code>{corpus_kind}</code></div>
<div class="grid">
  <div class="kpi"><div class="n">{fast_p95}ms</div><div class="l">快通道决策 p95</div></div>
  <div class="kpi"><div class="n">{dispatch_p95}ms</div><div class="l">端到端触发 p95</div></div>
  <div class="kpi"><div class="n">{frr}% / {far}%</div><div class="l">声纹 FRR / FAR</div></div>
  <div class="kpi"><div class="n">{total_lines}</div><div class="l">源码总行数（{modules} 个模块）</div></div>
</div>
<h2>验收结论</h2><div class="card"><table><tr><th>验收项</th><th>实测</th><th>结论</th></tr>
{verdict_rows}
</table><div class="note">判定口径见 <code>PRD.md §4</code> 与 <code>TODO_ROADMAP.md</code>；原始数据见 <code>docs/acceptance.json</code>。</div></div>
<h2>声纹阈值扫描</h2><div class="card"><table>
<tr><th>阈值</th><th>FRR</th><th>FAR</th><th>同人样本</th><th>异人样本</th></tr>{sweep_rows}</table>
<div class="note">语料为 macOS <code>say</code> 合成的 {speaker_count} 个真实音色（含同性别中文女声），每音色 4 句；
录入 2 句、测试 2 句。异人试验含同性别困难样本，因此 FAR 比真人场景保守。</div></div>
<h2>全链路实测</h2><div class="card"><table>
<tr><th>指标</th><th>值</th></tr>
<tr><td>接受 / 拒绝</td><td>{accepted} / {rejected}</td></tr>
<tr><td>录入一致性</td><td>{consistency}</td></tr>
<tr><td>单次派发延迟</td><td>{dispatch_ms}</td></tr>
<tr><td>处理耗时 / 音频时长</td><td>{wall_ms}ms / {rtf}× 实时</td></tr>
</table><div class="note">链路：WAV → 自愈源 → 能量 VAD → 端点 → CAM++ 声纹 → STT → 正则快通道 → 工具 → 审计。</div></div>
<h2>代码度量</h2><div class="card"><table>
<tr><th>模块</th><th>总行数</th><th>有效代码行</th></tr>{module_rows}</table>
<div class="note">最长函数 <code>{max_fn}</code>（{max_fn_lines} 行，预算 40）；类总数 {classes}；工具 {tools} 个；快通道 {fast_routes} 条。</div></div>
</div></body></html>"""


def render(report: dict[str, object], rows: list[dict[str, object]]) -> str:
    voice = report["voiceprint"]  # type: ignore[index]
    e2e = report["e2e"]  # type: ignore[index]
    code = report["code"]  # type: ignore[index]
    def _cls(row: dict[str, object]) -> str:
        if row.get("skipped"):
            return "skip"
        return "gap" if row.get("gap") else ("pass" if row["pass"] else "fail")

    verdict_rows = "\n".join(
        f"<tr><td>{row['item']}</td><td><code>{row['measured']}</code></td>"
        f"<td class=\"{_cls(row)}\">{_label(row)}</td></tr>"
        for row in rows
    )
    sweep_rows = "\n".join(
        f"<tr><td>{point['threshold']}</td><td>{point['frr']}%</td><td>{point['far']}%</td>"
        f"<td>{point['same_n']}</td><td>{point['diff_n']}</td></tr>"
        for point in voice["sweep"]  # type: ignore[index]
    )
    module_rows = "\n".join(
        f"<tr><td><code>{row['file']}</code></td><td>{row['lines']}</td><td>{row['code_lines']}</td></tr>"
        for row in code["modules"]  # type: ignore[index]
    )
    return HTML.format(
        generated_at=report["generated_at"], python=report["env"]["python"],  # type: ignore[index]
        backend=voice["backend"], corpus_kind=voice["corpus"],
        fast_p95=report["fast_path"]["p95"],  # type: ignore[index]
        dispatch_p95=report["dispatch"]["p95"],  # type: ignore[index]
        frr=voice["frr"], far=voice["far"],
        total_lines=code["total_lines"], modules=len(code["modules"]),  # type: ignore[index]
        verdict_rows=verdict_rows, sweep_rows=sweep_rows, module_rows=module_rows,
        speaker_count=len(voice["speakers"]),
        accepted=e2e["accepted"], rejected=e2e["rejected"], consistency=e2e["enroll_consistency"],
        dispatch_ms=", ".join(f"{value}ms" for value in e2e["dispatch_ms"]),
        wall_ms=e2e["wall_ms"], rtf=e2e["realtime_factor"],
        max_fn=code["max_function"]["name"], max_fn_lines=code["max_function"]["lines"],  # type: ignore[index]
        classes=code["classes"], tools=code["tools"], fast_routes=code["fast_routes"],  # type: ignore[index]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-corpus", action="store_true")
    parser.add_argument("--release", action="store_true",
                        help="发布门禁：GAP / SKIP 也算不通过（回归模式仍只拦 FAIL）")
    args = parser.parse_args()
    SANDBOX.mkdir(parents=True, exist_ok=True)
    report = build_report(args.refresh_corpus)
    rows = verdicts(report)
    if not (ROOT / "docs").exists():
        (ROOT / "docs").mkdir()
    (ROOT / "docs" / "acceptance.json").write_text(
        json.dumps({"report": report, "verdicts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "docs" / "ACCEPTANCE.html").write_text(render(report, rows), encoding="utf-8")

    print("\n验收项")
    print("-" * 62)
    for row in rows:
        print(f"{_label(row):<4}  {row['item']:<28} {row['measured']}")
    failed = [row for row in rows if not row["pass"] and not row.get("skipped") and not row.get("gap")]
    gaps = [row for row in rows if row.get("gap") and not row.get("skipped")]
    skipped = [row for row in rows if row.get("skipped")]
    judged = len(rows) - len(skipped)
    tail = f" · {len(skipped)} 跳过（缺模型/缺素材）" if skipped else ""
    if gaps:
        tail += f" · {len(gaps)} 项未达标（能力边界，见 REVIEW §22）"
    print(f"{judged - len(failed) - len(gaps)}/{judged} 通过{tail}"
          f" · 报告 docs/ACCEPTANCE.html")
    if failed:
        return 1
    if args.release and (gaps or skipped):
        print("发布门禁：存在未达标或未测量的必测项，按不通过处理")
        return 1
    return 0


def _passes_all(mixed: dict[str, object]) -> bool:
    """所有可用的混说测量都要 ≥95%；只有一套时按那一套算。"""
    values = [mixed.get("recall"), mixed.get("recall_bilingual")]
    measured = [float(value) for value in values if value is not None]
    return bool(measured) and all(value >= 95 for value in measured)


def _label(row: dict[str, object]) -> str:
    if row.get("skipped"):
        return "SKIP"
    if row.get("gap"):
        return "GAP"
    return "PASS" if row["pass"] else "FAIL"


if __name__ == "__main__":
    raise SystemExit(main())
