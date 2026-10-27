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


def measure_voiceprint(cfg, refresh: bool) -> dict[str, object]:
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
    ops_blocked = "无权调用" in ops.execute_safely("write_code", {"file_path": "x.py",
                                                                "operation": "CREATE_OR_OVERWRITE"})
    doctor = core.Executor(cfg=cfg.with_overrides(**{"workspace.role": "DOCTOR"}), confirm=lambda *_: False)
    draft = doctor.execute_safely("append_structured_log", {"content": "草稿验收"}).startswith("【未审核草稿】")
    confirm_needed = "已取消" in executor.execute_safely("delegate_to_coding_cli", {"prompt": "重构"})
    return {"forbidden_cases": len(cases), "blocked": blocked, "ops_isolated": ops_blocked,
            "doctor_draft": draft, "confirm_gate": confirm_needed}


def measure_code() -> dict[str, object]:
    files = sorted((ROOT / "src" / "voxos").glob("*.py"))
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
                    max_function = (f"{path.name}:{node.name}", length)
        rows.append({"file": path.name, "lines": len(source.splitlines()),
                     "code_lines": sum(1 for line in source.splitlines()
                                       if line.strip() and not line.strip().startswith("#"))})
    tests = len(list((ROOT / "tests").glob("test_*.py")))
    return {"modules": rows, "total_lines": sum(row["lines"] for row in rows),
            "max_function": {"name": max_function[0], "lines": max_function[1]},
            "classes": classes, "tools": len(core.REGISTRY), "fast_routes": len(core.FAST_ROUTES),
            "test_files": tests}


def measure_tests() -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                                "--basetemp", str(SANDBOX / "pytest")],
                               cwd=ROOT, capture_output=True, text=True)
    import re

    text = completed.stdout + completed.stderr
    matched = re.search(r"\d+ (?:passed|failed)[^\n]*", text)
    summary = matched.group(0).strip() if matched else text.strip().splitlines()[-1:]
    return {"returncode": completed.returncode,
            "summary": summary if isinstance(summary, str) else str(summary),
            "seconds": round(time.perf_counter() - started, 1)}


# ------------------------------------------------------------------ 报告

def build_report(refresh: bool = False) -> dict[str, object]:
    cfg = sandbox_config()
    bus = ui.StatusBus(SANDBOX / "status.jsonl")
    started = datetime.now()
    print("→ 快通道延迟 …")
    fast = measure_fast_path()
    print("→ 端到端触发延迟 …")
    dispatch = measure_dispatch(cfg)
    print("→ 声纹 FRR/FAR …")
    voiceprint = measure_voiceprint(cfg, refresh=refresh)
    print("→ 全链路 demo …")
    e2e = measure_e2e(cfg, bus)
    print("→ 安全红线矩阵 …")
    redlines = measure_redlines(cfg)
    print("→ 本地接口安全 …")
    security = measure_security()
    print("→ 设置写回 …")
    settings_report = measure_settings()
    print("→ 首次启动引导 …")
    onboarding = measure_onboarding()
    print("→ 代码度量 …")
    code = measure_code()
    print("→ 测试套件 …")
    tests = measure_tests()
    return {
        "generated_at": started.isoformat(timespec="seconds"),
        "env": {"python": sys.version.split()[0], "numpy": np.__version__,
                "sherpa": _version("sherpa_onnx"), "platform": sys.platform},
        "fast_path": fast, "dispatch": dispatch, "voiceprint": voiceprint,
        "e2e": e2e, "redlines": redlines, "security": security,
        "settings": settings_report, "onboarding": onboarding,
        "code": code, "tests": tests,
    }


def _version(module: str) -> str:
    try:
        return __import__(module).__version__  # type: ignore[no-any-return]
    except Exception:
        return "未安装"


def verdicts(report: dict[str, object]) -> list[dict[str, object]]:
    fast = report["fast_path"]  # type: ignore[index]
    dispatch = report["dispatch"]  # type: ignore[index]
    voice = report["voiceprint"]  # type: ignore[index]
    redlines = report["redlines"]  # type: ignore[index]
    code = report["code"]  # type: ignore[index]
    e2e = report["e2e"]  # type: ignore[index]
    return [
        {"item": "快通道决策延迟 p95 < 5ms", "measured": f"{fast['p95']}ms", "pass": fast["p95"] < 5},
        {"item": "端到端触发 p95 < 500ms", "measured": f"{dispatch['p95']}ms", "pass": dispatch["p95"] < 500},
        {"item": "声纹 FRR < 2%", "measured": f"{voice['frr']}%", "pass": voice["frr"] < 2},
        {"item": "声纹 FAR < 1%", "measured": f"{voice['far']}%", "pass": voice["far"] < 1},
        {"item": "冒名语音被拒（全链路）", "measured": f"{e2e['rejected']} 条被拒", "pass": e2e["rejected"] >= 1},
        {"item": "红线禁令全部拦截", "measured": f"{redlines['blocked']}/{redlines['forbidden_cases']}",
         "pass": redlines["blocked"] == redlines["forbidden_cases"]},
        {"item": "角色隔离（OPS 不可写文件）", "measured": str(redlines["ops_isolated"]), "pass": redlines["ops_isolated"]},
        {"item": "医疗草稿强制标记", "measured": str(redlines["doctor_draft"]), "pass": redlines["doctor_draft"]},
        {"item": "单函数 ≤ 40 行", "measured": f"{code['max_function']['lines']} 行",
         "pass": code["max_function"]["lines"] <= 40},
        {"item": "测试全绿", "measured": report["tests"]["summary"], "pass": report["tests"]["returncode"] == 0},  # type: ignore[index]
        {"item": "跨站 / 无令牌请求被拦截", "measured": str(report["security"]["codes"]),  # type: ignore[index]
         "pass": bool(report["security"]["blocked"])},  # type: ignore[index]
        {"item": "上下文注入不阻塞派发", "measured": f"{report['dispatch']['context_p95']}ms",  # type: ignore[index]
         "pass": report["dispatch"]["context_p95"] < 5},  # type: ignore[index]
        {"item": "设置写回保住注释", "measured": str(report["settings"]["comments_kept"]),  # type: ignore[index]
         "pass": bool(report["settings"]["applied"] and report["settings"]["comments_kept"]
                      and report["settings"]["source_kept"]
                      and report["settings"]["invalid_rejected"])},  # type: ignore[index]
        {"item": "无声纹时给出录入入口", "measured": str(report["onboarding"]["enroll_available"]),  # type: ignore[index]
         "pass": bool(report["onboarding"]["enroll_available"]
                      and not report["onboarding"]["status_enrolled"])},  # type: ignore[index]
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
    verdict_rows = "\n".join(
        f"<tr><td>{row['item']}</td><td><code>{row['measured']}</code></td>"
        f"<td class=\"{'pass' if row['pass'] else 'fail'}\">{'PASS' if row['pass'] else 'FAIL'}</td></tr>"
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
        print(f"{'PASS' if row['pass'] else 'FAIL'}  {row['item']:<28} {row['measured']}")
    failed = [row for row in rows if not row["pass"]]
    print("-" * 62)
    print(f"{len(rows) - len(failed)}/{len(rows)} 通过 · 报告 docs/ACCEPTANCE.html")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
