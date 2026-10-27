"""工具层测试：日志模板、代码操作、只读巡检、dry-run、热词扫描。"""

from __future__ import annotations

import time

from voxos import audio, core, tools


def test_append_log_uses_template(cfg):
    reply = tools.append_structured_log("修好了空指针", category="BUG_FIX", project="auth")
    assert "日志已记录" in reply
    log_file = cfg.path("workspace.daily_log_dir") / f"{time.strftime('%Y-%m-%d')}.md"
    text = log_file.read_text(encoding="utf-8")
    assert "# " in text                                   # 首次写入带标题
    assert "[BUG_FIX]" in text and "(auth)" in text and "修好了空指针" in text


def test_log_appends_without_losing_history(cfg):
    tools.append_structured_log("第一条")
    tools.append_structured_log("第二条")
    log_file = cfg.path("workspace.daily_log_dir") / f"{time.strftime('%Y-%m-%d')}.md"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert sum("第一条" in line for line in lines) == 1
    assert sum("第二条" in line for line in lines) == 1


def test_write_code_create_and_replace(cfg):
    assert "已写入" in tools.write_code("pkg/a.py", "CREATE_OR_OVERWRITE", new_code="def f():\n    return 1\n")
    target = cfg.path("workspace.default_code_dir") / "pkg" / "a.py"
    assert target.exists()

    tools.write_code("pkg/a.py", "REPLACE_SNIPPET", target_snippet="return 1", new_code="return 2")
    assert "return 2" in target.read_text(encoding="utf-8")

    tools.write_code("pkg/a.py", "APPEND_CODE", new_code="\ndef g():\n    return 3")
    assert "def g()" in target.read_text(encoding="utf-8")


def test_replace_snippet_must_be_unique(cfg):
    tools.write_code("dup.py", "CREATE_OR_OVERWRITE", new_code="x = 1\nx = 1\n")
    reply = tools.write_code("dup.py", "REPLACE_SNIPPET", target_snippet="x = 1", new_code="x = 2")
    assert "必须恰好 1 处" in reply
    assert "x = 1" in (cfg.path("workspace.default_code_dir") / "dup.py").read_text(encoding="utf-8")


def test_replace_missing_file_is_refused(cfg):
    assert "不存在" in tools.write_code("ghost.py", "REPLACE_SNIPPET", target_snippet="a", new_code="b")


def test_read_file_head_and_search(cfg):
    tools.write_code("readme.md", "CREATE_OR_OVERWRITE", new_code="# 标题\n正文内容\n")
    assert "标题" in tools.read_file_head("readme.md", lines=5)
    assert "文件不存在" in tools.read_file_head("nope.md")
    tools.append_structured_log("排查了 token 过期问题")
    assert "token" in tools.search_notes("token", root=str(cfg.path("workspace.daily_log_dir")))


def test_inspect_ops_is_read_only(cfg):
    reply = tools.inspect_server_ops("看看负载")
    assert "负载" in reply and "磁盘" in reply
    meta = core.REGISTRY["inspect_server_ops"]
    assert meta.risk is core.RiskLevel.GREEN


def test_code_session_dry_run(cfg):
    reply = tools.dispatch_code_session("给登录加验证码")
    assert "dry-run" in reply and "claude" in reply


def test_open_in_ide_dry_run(cfg):
    tools.write_code("open_me.py", "CREATE_OR_OVERWRITE", new_code="pass\n")
    assert "dry-run" in tools.open_in_ide("open_me.py", line=3)


def test_list_tools_mentions_all(cfg):
    reply = tools.list_tools()
    for name in core.REGISTRY:
        assert name in reply


def test_hotwords_scan(cfg):
    tools.write_code("user_auth.py", "CREATE_OR_OVERWRITE", new_code="pass\n")
    tools.write_code("payment_gateway.ts", "CREATE_OR_OVERWRITE", new_code="export {}\n")
    words = audio.refresh_hotwords(cfg, cfg.path("workspace.default_code_dir"))
    assert "user_auth" in words
    assert "payment_gateway" in words
    assert cfg.path("audio.hotwords_generated").exists()
