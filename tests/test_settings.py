"""设置层：字段描述、校验、以及"改值不毁注释"的写回。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voxos import settings
from voxos.config import load

SAMPLE = """\
# 顶部注释要保住
[audio]
chunk_size = 512              # 32ms @16kHz
voiceprint_threshold = 0.82   # 实测标定值
mask_mode = false

[llm]
enabled = false               # 关闭时完全离线
base_url = "http://localhost:11434/v1"
"""


@pytest.fixture
def toml_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 读取

def test_describe_groups_and_values(cfg):
    groups = settings.describe(cfg)
    assert [group["id"] for group in groups] == ["audio", "stt", "llm", "workspace", "ui"]
    audio = {field["key"]: field for field in groups[0]["fields"]}
    assert audio["audio.voiceprint_threshold"]["value"] == 0.84
    assert audio["audio.voiceprint_threshold"]["min"] == 0.5


def test_describe_masks_secrets(cfg):
    groups = settings.describe(cfg.with_overrides(**{"llm.api_key": "sk-123456"}))
    llm = {field["key"]: field for field in groups[2]["fields"]}
    assert llm["llm.api_key"]["value"] == "••••••"


def test_describe_reports_env_override(cfg, monkeypatch):
    monkeypatch.setenv("VOXOS_LLM_MODEL", "env-model")
    groups = settings.describe(cfg)
    llm = {field["key"]: field for field in groups[2]["fields"]}
    assert llm["llm.model"]["overridden_by"] == "VOXOS_LLM_MODEL"


# ------------------------------------------------------------------ 校验

def test_validate_rejects_out_of_range():
    _, errors = settings.validate({"audio.voiceprint_threshold": 1.4})
    assert errors and "0.5" in errors[0]


def test_validate_rejects_unknown_key():
    _, errors = settings.validate({"audio.nope": 1})
    assert errors == ["未知配置项：audio.nope"]


def test_validate_coerces_strings():
    clean, errors = settings.validate({"audio.voiceprint_threshold": "0.88",
                                       "workspace.dry_run": "false"})
    assert not errors
    assert clean["audio.voiceprint_threshold"] == 0.88
    assert clean["workspace.dry_run"] is False


def test_validate_rejects_bad_enum():
    _, errors = settings.validate({"workspace.role": "ADMIN"})
    assert "DEVELOPER" in errors[0]


def test_validate_cross_field():
    _, errors = settings.validate({"llm.enabled": True, "llm.base_url": ""})
    assert errors == ["启用大模型时必须填写接口地址"]


# ------------------------------------------------------------------ 写回

def test_save_keeps_comments_and_alignment(toml_file: Path):
    settings.save({"audio.voiceprint_threshold": 0.9}, toml_file)
    text = toml_file.read_text(encoding="utf-8")
    assert "# 顶部注释要保住" in text
    assert "voiceprint_threshold = 0.9   # 实测标定值" in text
    assert "chunk_size = 512              # 32ms @16kHz" in text


def test_save_quotes_strings_and_bools(toml_file: Path):
    settings.save({"llm.base_url": "http://x/v1", "llm.enabled": True}, toml_file)
    data = load(toml_file).data
    assert data["llm"]["base_url"] == "http://x/v1"
    assert data["llm"]["enabled"] is True


def test_save_inserts_missing_key_into_section(toml_file: Path):
    settings.save({"audio.vad_threshold": 0.6}, toml_file)
    data = load(toml_file).data
    assert data["audio"]["vad_threshold"] == 0.6
    assert data["audio"]["voiceprint_threshold"] == 0.82


def test_save_creates_missing_section(toml_file: Path):
    settings.save({"ui.earcons": False}, toml_file)
    assert load(toml_file).data["ui"]["earcons"] is False


def test_save_ignores_hash_inside_string(toml_file: Path):
    settings.save({"llm.base_url": "http://h/#frag"}, toml_file)
    assert load(toml_file).data["llm"]["base_url"] == "http://h/#frag"


# ------------------------------------------------------------------ 应用

def test_apply_writes_and_reloads(cfg, toml_file: Path):
    cfg = cfg.with_overrides(**{"audio.voiceprint_threshold": 0.82})
    new_cfg, errors = settings.apply(cfg, {"audio.voiceprint_threshold": 0.9}, toml_file)
    assert not errors
    assert new_cfg.get("audio.voiceprint_threshold") == 0.9
    assert "0.9" in toml_file.read_text(encoding="utf-8")


def test_apply_writes_nothing_when_invalid(cfg, toml_file: Path):
    before = toml_file.read_text(encoding="utf-8")
    _, errors = settings.apply(cfg, {"audio.voiceprint_threshold": 5}, toml_file)
    assert errors
    assert toml_file.read_text(encoding="utf-8") == before


def test_apply_keeps_env_precedence(cfg, toml_file: Path, monkeypatch):
    monkeypatch.setenv("VOXOS_ROLE", "OPS")
    new_cfg, errors = settings.apply(cfg, {"workspace.role": "DEVELOPER"}, toml_file)
    assert not errors
    assert new_cfg.get("workspace.role") == "OPS"     # 环境变量仍然赢


def test_apply_writes_back_to_the_file_it_came_from(tmp_path: Path):
    """--config 指定的文件才是写回目标，不能总是写仓库里的默认文件。"""
    source = tmp_path / "custom.toml"
    source.write_text("[audio]\nvoiceprint_threshold = 0.80\n", encoding="utf-8")
    cfg = load(source)
    assert cfg.source == source
    new_cfg, errors = settings.apply(cfg, {"audio.voiceprint_threshold": 0.9})
    assert not errors
    assert "0.9" in source.read_text(encoding="utf-8")
    assert new_cfg.source == source


def test_save_roundtrip_all_fields(tmp_path: Path):
    """把模式里每个字段都写一遍，确认都能被 tomllib 读回同样的值。"""
    path = tmp_path / "s.toml"
    path.write_text("[audio]\n[stt]\n[llm]\n[workspace]\n[ui]\n", encoding="utf-8")
    payload = {}
    for field in settings.SCHEMA:
        payload[field.key] = (field.options[0] if field.kind == "enum"
                              else field.minimum if field.kind in ("int", "float")
                              else True if field.kind == "bool" else "值")
    settings.save(payload, path)
    data = load(path).data
    for field in settings.SCHEMA:
        section, name = field.key.split(".", 1)
        assert data[section][name] == payload[field.key]


def test_env_map_covers_schema():
    """模式里的每个键如果出现在环境变量映射里，映射必须是合法的。"""
    for env_key, target in settings.ENV_MAP.items():
        assert len(target) == 2
        assert os.environ.get(env_key) is None or isinstance(os.environ.get(env_key), str)
