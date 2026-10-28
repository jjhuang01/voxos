"""打包与安装的边界：装出来的包，数据不能落在安装目录里。

为什么单开一个文件：这一组问题只有在「装完」之后才出现，跑源码的时候
一切正常——所以它们必须有独立的、不依赖源码布局的守卫。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voxos import config, main, models


def test_models_is_a_subcommand():
    """`pipx install voxos` 之后只有 `voxos` 命令，`scripts/` 不在 wheel 里。

    没有 `voxos models` 就等于「装好了但下不了模型」，只能回去 clone 源码。
    """
    parser = main.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "子命令解析器不见了"
    choices = actions[0].choices  # type: ignore[attr-defined]
    assert "models" in choices, f"缺 models 子命令：{sorted(choices)}"


def test_data_root_can_be_overridden(monkeypatch, tmp_path):
    """VOXOS_HOME 必须能改数据根，否则多实例和测试会互相踩。"""
    monkeypatch.setenv("VOXOS_HOME", str(tmp_path))
    assert config._default_data_root() == tmp_path


def test_installed_layout_puts_models_outside_the_venv(monkeypatch, tmp_path):
    """安装布局下，`.voxos/models` 不能解析到 site-packages / venv 里。

    pipx 升级会重建 venv，模型放那儿会一起消失——267MB 白下。
    """
    monkeypatch.setenv("VOXOS_HOME", str(tmp_path))
    data_root = config._default_data_root()
    cfg = config.Config({"audio": {"speaker_model": ".voxos/models/campplus.onnx"}},
                        data_root=data_root)
    model_dir = cfg.path("audio.speaker_model").parent
    assert model_dir == tmp_path / ".voxos" / "models"
    assert Path(sys.prefix) not in model_dir.parents, "模型落进了安装目录"


def test_resource_paths_stay_with_the_install():
    """只读资源（胶囊页面、热词表）跟着安装走，不跟着数据走。"""
    cfg = config.Config({"ui": {"pill_file": "web/pill.html"}},
                        root=Path("/opt/share/voxos"), data_root=Path("/home/u"))
    assert cfg.res_path("ui.pill_file") == Path("/opt/share/voxos/web/pill.html")
    assert cfg.path("ui.pill_file") == Path("/home/u/web/pill.html")


def test_absolute_paths_ignore_both_roots():
    """配置里写绝对路径就是绝对路径，不能被根目录再拼一次。"""
    cfg = config.Config({"stt": {"model_dir": "/data/models"}},
                        root=Path("/opt/share/voxos"), data_root=Path("/home/u"))
    assert cfg.path("stt.model_dir") == Path("/data/models")
    assert cfg.res_path("stt.model_dir") == Path("/data/models")


def test_model_manifest_entries_are_complete():
    """清单缺字段会在下载到一半才炸，不如现在炸。"""
    for name, spec in models.MODELS.items():
        for key in ("url", "min_bytes", "desc", "sha256"):
            assert spec.get(key), f"{name} 缺 {key}"
        assert str(spec["url"]).startswith("https://"), f"{name} 不是 https"
        assert len(str(spec["sha256"])) == 64, f"{name} 的 sha256 长度不对"


@pytest.mark.parametrize("name", sorted(models.OPTIONAL))
def test_optional_models_are_known(name: str):
    """OPTIONAL 里写的名字必须真的在清单里，否则 `--no-asr` 会静默不生效。"""
    assert name in models.MODELS


def test_models_command_actually_runs(monkeypatch, tmp_path):
    """`voxos models` 曾经能解析但一调就 NameError（main.py 里没有 import os）。

    「参数解析得出来」和「跑得起来」是两件事，所以这里真的调一次 cmd_models，
    只把下载动作换成假的——不然一条单测要拖 28MB 流量。
    """
    calls: list[dict] = []

    def fake_fetch(target, *, proxy=None, force=False, with_asr=True):
        calls.append({"target": target, "proxy": proxy, "force": force, "with_asr": with_asr})
        return 0

    monkeypatch.setattr(models, "fetch", fake_fetch)
    parser = main.build_parser()
    args = parser.parse_args(["models", "--dir", str(tmp_path), "--no-asr"])
    assert main.cmd_models(args, config.Config({})) == 0
    assert calls == [{"target": tmp_path, "proxy": None, "force": False, "with_asr": False}]


def test_fetch_reads_proxy_from_environment(monkeypatch, tmp_path):
    """代理兜底放在 fetch 里：调用方不该为一个默认值去 import os。"""
    seen: list[str | None] = []

    def fake_download(url, target, proxy, spec):
        seen.append(proxy)
        return False

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(models, "download", fake_download)
    monkeypatch.setattr(models, "verify", lambda *_: False)
    models.fetch(tmp_path, with_asr=False)
    assert seen and seen[0] == "http://127.0.0.1:7890", f"没读到代理：{seen[:3]}"
