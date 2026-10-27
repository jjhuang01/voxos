"""端到端测试：音频 → 声纹门禁 → STT → 快通道 → 工具 → 日志文件。"""

from __future__ import annotations

import time

import numpy as np

from voxos import audio, core, dsp, tools


def test_voice_to_log_end_to_end(cfg, bus, make_voice, stub_embedder, silence):
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, 1.0, seed=1), make_voice(118, 1.0, seed=2)])

    transcripts = [
        "Siri，我要写日志，今天把登录接口的空值校验补上了",
        "Siri，检查一下服务器现在的负载",
    ]
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.ScriptedStt(transcripts), lock=lock, on_event=bus)
    stream = audio.concat_with_silence(
        [make_voice(118, 1.2, seed=3), make_voice(206, 1.2, seed=4), make_voice(118, 1.2, seed=5)],
        gap_ms=1400,
    )

    executor = core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: True)
    results = []
    for utterance in pipeline.run(audio.ArraySource(stream)):
        if not utterance.accepted:
            continue
        results.append(core.dispatch(utterance.text, executor=executor, cfg=cfg))

    assert len(results) == 2
    assert results[0].route == "fast" and results[0].tool == "append_structured_log"
    assert results[1].tool == "inspect_server_ops"

    log_file = cfg.path("workspace.daily_log_dir") / f"{time.strftime('%Y-%m-%d')}.md"
    content = log_file.read_text(encoding="utf-8")
    assert "空值校验" in content
    assert "服务器" not in content, "只读巡检不应写进日志"

    audit = cfg.path("audit.path").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit) == 2
    assert all('"ok": true' in line for line in audit)


def test_redline_blocks_even_when_spoken(cfg, bus, make_voice, stub_embedder):
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, 1.0)])
    executor = core.Executor(cfg=cfg, on_event=bus, confirm=lambda *_: True)

    # 语音层能识别出"改代码"，但模型/启发式给出的危险内容仍会被网关拦下
    reply = executor.execute_safely(
        "write_code",
        {"file_path": "danger.py", "operation": "CREATE_OR_OVERWRITE", "new_code": "rm -rf /"},
        text="Siri 帮我写代码 删掉所有东西",
    )
    assert reply.startswith("⛔")
    assert not (cfg.path("workspace.default_code_dir") / "danger.py").exists()


def test_doctor_profile_end_to_end(cfg, bus):
    doctor = cfg.with_overrides(**{"workspace.role": "DOCTOR"})
    tools.configure(doctor)
    executor = core.Executor(cfg=doctor, on_event=bus)
    reply = core.dispatch("Siri，我要写日志，患者主诉头晕三天", executor=executor, cfg=doctor).reply
    assert reply.startswith("【未审核草稿】")
    log_file = doctor.path("workspace.daily_log_dir") / f"{time.strftime('%Y-%m-%d')}.md"
    assert "头晕三天" in log_file.read_text(encoding="utf-8")


def test_demo_pipeline_statistics(cfg, bus, make_voice, stub_embedder):
    lock = dsp.SpeakerLock(threshold=0.84, embedder=stub_embedder)
    lock.enroll([make_voice(118, 1.0)])
    pipeline = audio.AudioPipeline(cfg=cfg, stt=audio.ScriptedStt(["我要写日志，统计"]), lock=lock, on_event=bus)
    stream = np.concatenate([make_voice(118, 1.0), np.zeros(24000, dtype=np.float32)])
    list(pipeline.run(audio.ArraySource(stream)))
    assert pipeline.stats["chunks"] > 0
    assert pipeline.stats["utterances"] == 1
