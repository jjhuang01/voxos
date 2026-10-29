"""打包与安装的边界：装出来的包，数据不能落在安装目录里。

为什么单开一个文件：这一组问题只有在「装完」之后才出现，跑源码的时候
一切正常——所以它们必须有独立的、不依赖源码布局的守卫。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from voxos import __version__, config, main, models


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


def test_installed_layout_uses_a_writable_config_copy(monkeypatch, tmp_path):
    """装出来的包，配置不能在 `<venv>/share/voxos/config/settings.toml` 里改。

    那份是安装资源：只读、且 `pipx upgrade` 会整份换掉。用户在设置页切的
    `stt.backend = sense_voice` 如果写在那儿，升级一次就回退成 scripted——
    268MB 模型白下，而且没人会想到「我的设置被升级吃掉了」。
    """
    root = tmp_path / "share" / "voxos"
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.toml").write_text('[stt]\nbackend = "scripted"\n', encoding="utf-8")
    data = tmp_path / "home"
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "DATA", data)

    target = config._settings_path()
    assert target == data / ".voxos" / "settings.toml"
    assert target.exists() and "scripted" in target.read_text(encoding="utf-8")

    # 复制只发生一次：用户改过之后不能被下一次启动重置回出厂值。
    target.write_text('[stt]\nbackend = "sense_voice"\n', encoding="utf-8")
    assert config._settings_path() == target
    assert "sense_voice" in target.read_text(encoding="utf-8")


def test_source_layout_keeps_using_the_repo_config(monkeypatch, tmp_path):
    """源码布局（数据根就是仓库根）不能凭空多出一份 `.voxos/settings.toml`，
    否则仓库里那份 config 就不是当前配置了，所有手工验证都会对不上。"""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA", tmp_path)
    assert config._settings_path() == tmp_path / "config" / "settings.toml"


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
    """清单缺字段会在下载到一半才炸，不如现在炸。

    `sha256` 允许缺席，但**必须写明为什么**：要么有摘要，要么有一句话交代
    「算不出来」。否则「忘了填」和「故意不填」长得一模一样，而前者意味着
    用户下到一个被中间人换过的模型还浑然不知。
    """
    for name, spec in models.MODELS.items():
        for key in ("url", "min_bytes", "desc"):
            assert spec.get(key), f"{name} 缺 {key}"
        assert str(spec["url"]).startswith("https://"), f"{name} 不是 https"
        digest = spec.get("sha256")
        if digest:
            assert len(str(digest)) == 64, f"{name} 的 sha256 长度不对"
        else:
            assert spec.get("no_sha256_reason"), f"{name} 既没 sha256 也没写原因"


@pytest.mark.parametrize("name", sorted(models.OPTIONAL))
def test_optional_models_are_known(name: str):
    """OPTIONAL 里写的名字必须真的在清单里，否则 `--no-asr` 会静默不生效。"""
    assert name in models.MODELS


def test_no_asr_still_fetches_the_turn_model(monkeypatch, tmp_path):
    """`--no-asr` 是「跳过 239MB 的识别模型」，不是「跳过所有可选模型」。

    判停模型也在 OPTIONAL 里，但它只有 8.7MB，而「先不下识别模型、先把判停跑
    起来」是个正常需求。跟着一起跳掉的话，用户会得到一台「听得见但总抢话」的
    机器，而且没有任何地方提示这是因为少下了一个 8.7MB 的文件。
    """
    wanted: list[str] = []

    def fake_download(url, path, proxy, spec):
        wanted.append(path.name)
        return False

    monkeypatch.setattr(models, "download", fake_download)
    models.fetch(tmp_path, with_asr=False)
    assert "smart-turn-v3.2-cpu.onnx" in wanted
    assert "model.int8.onnx" not in wanted


def test_huggingface_files_get_a_mirror_on_the_same_route_first():
    """HuggingFace 原站在国内常常不通，镜像要排在**同一条通路**上的原站之后。

    实测（这台机器）：原站经代理立刻 502，直连却要等满超时。如果把所有直连
    排在所有代理之前，镜像就被挡在两次超时后面——用户看到的只是「卡了两分钟
    然后失败」。
    """
    spec = {"url": "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/x.onnx"}
    attempts = models._attempts(spec, "http://127.0.0.1:1")

    assert [via for _, via in attempts] == ["http://127.0.0.1:1"] * 2 + [None] * 2
    assert attempts[0][0] == spec["url"]
    assert attempts[1][0] == "https://hf-mirror.com/pipecat-ai/smart-turn-v3/resolve/main/x.onnx"


def test_a_non_huggingface_url_gets_no_mirror():
    """GitHub 上的模型没有镜像，硬拼一个 hf-mirror 地址只会多一次 404。"""
    spec = {"url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/x.onnx"}
    assert models._mirrors(spec["url"]) == []
    assert models._attempts(spec, None) == [(spec["url"], None)]


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


def test_version_literal_matches_pyproject():
    """`__version__` 和 `pyproject.toml` 的 version 必须是同一个数。

    这不是洁癖：0.1.11、0.1.12 两个 tag 发出去的时候，`__init__.py` 里还写着
    **0.1.10**，而没有任何地方会因此报错——直到装上 `--version` 才看见。
    """
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml")
                              .read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__, (
        f"pyproject 写 {pyproject['project']['version']}，包里是 {__version__}")


def test_version_flag_prints_and_exits(capsys):
    """`voxos --version` 曾经直接报「缺少 command」——pipx 装完第一件事就是敲它。"""
    with pytest.raises(SystemExit) as exit_info:
        main.build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"voxos {__version__}"


def test_doctor_says_where_the_config_lives(tmp_path, capsys):
    """doctor 必须报出配置路径本身，而不是一个「正常」。

    0.1.12 那个 bug 难发现，就是因为 doctor 里看不出自己改的是哪一份文件。
    """
    target = tmp_path / "settings.toml"
    target.write_text('[stt]\nbackend = "scripted"\n', encoding="utf-8")
    main.cmd_doctor(None, config.Config({}, source=target))  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "配置文件" in out and str(target) in out, out
    assert "可写" in out, out


def test_doctor_warns_when_config_is_inside_the_install(tmp_path, capsys):
    """配置落在安装目录里 = 升级一次就被整份替换，必须在 doctor 里说破。"""
    installed = Path(sys.prefix) / "share" / "voxos" / "config" / "settings.toml"
    main.cmd_doctor(None, config.Config({}, source=installed))  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "升级会被整份替换" in out, out


def test_ci_runs_the_install_smoke_not_a_weaker_one():
    """CI 的安装冒烟必须真的跑 `scripts/install_smoke.sh`。

    原来的 package job 只跑 `voxos tools` + `config.load()`，而 0.1.11 和 0.1.12
    两个「装完就坏」的 bug 都能过那一关——守的是一个不会红的绿灯。
    """
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "install_smoke.sh"
    assert script.exists(), "安装冒烟脚本不见了"
    assert script.stat().st_mode & 0o111, "install_smoke.sh 没有执行权限"
    workflow = (root / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "scripts/install_smoke.sh" in workflow, "CI 没跑安装冒烟"



# ---------------------------------------------------------------- App 启动链路

def test_port_command_prints_the_configured_port(capsys):
    """App 启动器靠 `voxos port` 判断服务就绪。写死 8765 会在端口被占时永远等不到。"""
    cfg = config.Config({"ui": {"port": 9123}})
    assert main.cmd_port(None, cfg) == 0  # type: ignore[arg-type]
    assert capsys.readouterr().out.strip() == "9123"


def test_busy_port_says_who_holds_it_instead_of_a_traceback(monkeypatch):
    """端口被别的程序占用时，用户看到的不该是一屏 traceback。

    本机实测：8765 被一个 Xcode 自带 Python 起的目录服务占着，
    原始报错只有 `OSError: [Errno 48] Address already in use`，
    既不说谁占的，也不说该改哪个配置。
    """
    import errno as _errno
    from types import SimpleNamespace

    def boom(*_args, **_kwargs):
        raise OSError(_errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(main.ui, "serve", boom)
    monkeypatch.setattr(main, "_port_holder", lambda port: "Python")
    monkeypatch.setattr(main, "_ui_context", lambda *a, **k: None)
    runtime = SimpleNamespace(cfg=config.Config({"ui": {"port": 8766}}))
    with pytest.raises(SystemExit) as caught:
        main._serve(runtime)  # type: ignore[arg-type]
    message = str(caught.value)
    assert "8766" in message and "ui.port" in message, message
    assert "Python" in message, "没说清楚是谁占着"


def test_enroll_warns_when_only_one_sentence_was_captured(monkeypatch):
    """只切出 1 句时模板不稳，必须当场说，而不是等用户被自己拒之门外。

    实测：单句模板下同人跨句子只有 0.751，阈值 0.82 时 100% 误拒。
    """
    import numpy as np

    monkeypatch.setattr(main, "_record_segments", lambda *a, **k: [np.zeros(3200, dtype="float32")])

    class FakeLock:
        profiles: dict = {}
        threshold = 0.75

        def enroll(self, samples):
            self.profiles["normal"] = np.ones(192, dtype="float32")
            return 1.0

        def save(self, path):
            return None

    result = main._enroll_now(config.Config({"audio": {}}), FakeLock(), 10.0)  # type: ignore[arg-type]
    assert result["ok"] is True
    assert result["samples"] == 1
    assert "再说 2 句" in str(result["warning"]), result


def test_stale_singleton_lock_is_cleared_only_when_nobody_is_using_it(tmp_path, monkeypatch):
    """强杀胶囊窗口会留下指向死 pid 的 `SingletonLock`，Chrome 之后就不再弹窗——
    `open -na` 还返回 0，于是「点了图标什么都没发生」。

    但**只有在确认没人在用**时才清：反过来的话会同时起两个实例，
    两个都以为自己独占，配置目录就真的坏了。
    """
    profile = tmp_path / "pill-profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).write_text("x")

    monkeypatch.setattr(main, "_window_alive", lambda p, timeout=4.0: True)
    main._drop_stale_lock(profile)
    assert (profile / "SingletonLock").exists(), "有人在用的时候一个都不许动"

    monkeypatch.setattr(main, "_window_alive", lambda p, timeout=4.0: False)
    main._drop_stale_lock(profile)
    assert not list(profile.iterdir()), "没人在用就该清干净"


def test_pill_window_reports_failure_when_no_window_appears(monkeypatch):
    """启动成功 ≠ 窗口出现。等不到窗口必须返回 False，让调用方回落到默认浏览器，
    而不是让 `open` 的退出码 0 冒充成功。"""
    monkeypatch.setattr(main, "_window_alive", lambda p, timeout=4.0: False)
    monkeypatch.setattr(main, "_drop_stale_lock", lambda p: None)
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(main.Path, "exists", lambda self: True)
    assert main._open_app_window("http://127.0.0.1:1/") is False
