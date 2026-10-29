"""文档自洽：同一个数字写在四个文件里，就必须是同一个数。

为什么需要：这一轮我手工同步了三遍（158 → 161），每次都漏一处——
验收表里还写着 157、路线图写着 160、README 写着 161。人眼对不齐四个文件。

它不判断「数字对不对」（那要靠跑），只判断两件更便宜的事：

1. 同一个说法在多处出现时，值必须一致；
2. 能直接数出来的（测试数、界面断言条数、源码行数），必须和代码一致。

`docs/REVIEW.md` 不在检查范围内：它是按轮次写的复盘，里面的数字是**当时**的，
把它也钉住等于不许记录历史。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "TODO_ROADMAP.md", "docs/ACCEPTANCE.md")


def _claims(pattern: str, files: tuple[str, ...] = DOCS) -> dict[int, list[str]]:
    """把「某个说法在哪些文件里被写成了几」收集起来。"""
    found: dict[int, list[str]] = {}
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in re.finditer(pattern, text):
            line = text.count("\n", 0, match.start()) + 1
            found.setdefault(int(match.group(1)), []).append(f"{name}:{line}")
    return found


def _merged(*patterns: str) -> dict[int, list[str]]:
    merged: dict[int, list[str]] = {}
    for pattern in patterns:
        for value, where in _claims(pattern).items():
            merged.setdefault(value, []).extend(where)
    return merged


def _explain(label: str, claims: dict[int, list[str]]) -> str:
    rows = [f"  {value} ← {', '.join(where)}" for value, where in sorted(claims.items())]
    return f"{label}在文档里出现了 {len(claims)} 个不同的值：\n" + "\n".join(rows)


def test_test_count_is_the_same_everywhere():
    claims = _claims(r"(\d+)\s*项测试")
    assert claims, "一个测试数都没找到，说明措辞变了，改这条测试"
    assert len(claims) == 1, _explain("测试数", claims)


def test_test_count_matches_pytest_collection():
    """文档说多少，就真的有多少——上面那条只保证「到处一样」。

    并且**跳过不等于通过**：以前这条只比「收集数」，于是「207 passed」写在
    「208 项」旁边也过得去，等于默许把 skip 算成 pass。现在拆开比。
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, (result.stdout[-500:] + result.stderr[-500:])
    collected = int(match.group(1))
    claimed = next(iter(_claims(r"(\d+)\s*项测试")))
    assert collected == claimed, f"文档写 {claimed} 项，实际收集到 {collected} 项"

    text = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
    if "当前全量结果：BLOCKED" in text:
        # 受阻状态要么写「未发布」，要么显式写「已知阻断下发布」并声明没全绿。
        # 两者都不许含糊带过：这个守卫防的不是「发版」，是「发了版还说全量跑过」。
        declared = "未发布" in text or (
            "已知阻断下发布" in text and "不得记成" in text)
        assert "阻断原因：" in text and declared, (
            "受阻状态必须写原因，并明确「未发布」或「已知阻断下发布」")
        return  # 历史 passed/skip 不是本次结果；不能为了守卫而编造本次全绿。
    run = re.search(r"(\d+) passed \+ (\d+) skipped", text)
    assert run, "验收表里没写「N passed + M skipped」，无法核对跳过数"
    passed, skipped = int(run.group(1)), int(run.group(2))
    assert passed + skipped == collected, (
        f"文档写 {passed} passed + {skipped} skipped = {passed + skipped}，收集到 {collected}")


def test_audit_count_matches_the_script():
    """界面断言条数是数得出来的：`len(audit_ui.CHECKS)`。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import audit_ui

    claims = _merged(r"界面回归\s*(\d+)\s*[条项]", r"(\d+)\s*条断言")
    assert claims, "没找到界面断言条数的说法"
    assert set(claims) == {len(audit_ui.CHECKS)}, (
        _explain("界面断言条数", claims) + f"\n  脚本里实际有 {len(audit_ui.CHECKS)} 条")


def test_acceptance_table_is_numbered_without_gaps():
    """验收项编号连续，且和「N 项」的说法对得上。"""
    text = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
    numbers = [int(m.group(1)) for m in re.finditer(r"(?m)^\| (\d+) \|", text)]
    assert numbers, "验收表不见了"
    assert numbers == list(range(1, len(numbers) + 1)), f"编号有断档：{numbers}"
    for value, where in _merged(r"(\d+) 项验收指标",
                                r"验收项 \d+ → \*\*(\d+) 项\*\*").items():
        assert value == len(numbers), f"{', '.join(where)} 说 {value} 项，表里有 {len(numbers)} 项"


def test_source_line_table_matches_reality():
    """代码度量表是手写的，最容易过期。"""
    text = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
    rows = re.findall(r"(?m)^\| `(\w+\.py)` \| (\d+) \|", text)
    assert rows, "代码度量表不见了"
    wrong = [f"  {name}: 文档 {claimed} / 实际 {actual}"
             for name, claimed in rows
             if (actual := len((ROOT / "src/voxos" / name).read_text(encoding="utf-8").splitlines()))
             != int(claimed)]
    assert not wrong, "代码度量表和实际行数不一致：\n" + "\n".join(wrong)

    total = re.search(r"\*\*合计\*\* \| \*\*(\d+)\*\*", text)
    assert total, "合计行不见了"
    actual_total = sum(len(p.read_text(encoding="utf-8").splitlines())
                       for p in (ROOT / "src/voxos").glob("*.py"))
    assert int(total.group(1)) == actual_total, f"合计写 {total.group(1)}，实际 {actual_total}"


def _function_lengths() -> list[tuple[str, int]]:
    """源码和脚本一起量：只扫 `src/` 会让这条规矩管不到真正干活的脚本。"""
    found: list[tuple[str, int]] = []
    for path in sorted(list((ROOT / "src/voxos").glob("*.py"))
                       + list((ROOT / "scripts").glob("*.py"))):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                found.append((f"{path.relative_to(ROOT)}:{node.name}", length))
    return found


def test_no_function_exceeds_the_line_budget():
    """「单函数 ≤ 40 行」是验收项 #9，但以前只有 `make verify` 管它。

    结果就是：审计修复把 `core.dispatch` 顶到 45 行时，`pytest` 一声不响，
    要等到整轮验收才发现。规矩应该在**最近的地方**拦，不是在最远的地方。
    """
    budget = 40
    offenders = [f"  {name} = {length} 行"
                 for name, length in _function_lengths() if length > budget]
    assert not offenders, f"超过 {budget} 行的函数：\n" + "\n".join(offenders)


def test_acceptance_names_the_real_longest_function():
    """验收表里「最长函数 N 行（`xxx`）」不能是上一轮的记忆。"""
    text = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
    match = re.search(r"最长函数 \*\*(\d+) 行\*\*（`([^`]+)`）", text)
    assert match, "验收表里没写最长函数"
    name, length = max(_function_lengths(), key=lambda pair: pair[1])
    assert (int(match.group(1)), match.group(2)) == (length, name), (
        f"文档写 {match.group(1)} 行（{match.group(2)}），"
        f"实际最长是 {length} 行（{name}）")


_HEREDOC = re.compile(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)")


def test_unquoted_heredocs_have_no_backticks():
    """未加引号的 heredoc 里，反引号会在**执行时**被当成命令替换。

    `scripts/build_app.sh` 用 `<<LAUNCHER`（没加引号）生成 App 启动器，于是
    注释里随手写的 `wait -n` 会在**构建机上真的被执行一遍**。0.1.23~0.1.27
    一直在刷「WINDOW_PID: unbound variable」和「wait: -n: invalid option」。

    讽刺的是同一段 heredoc 上方第 109 行就写着「注释里也不能写反引号」——
    规矩写在旁边，然后写规矩的人自己违反了它。所以这条必须是**能自动跑的**，
    不能只是注释。
    """
    text = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    bad: list[str] = []
    index = 0
    while index < len(lines):
        match = _HEREDOC.search(lines[index])
        index += 1
        if not match:
            continue
        terminator = match.group(1)
        while index < len(lines) and lines[index].strip() != terminator:
            if "`" in lines[index]:
                bad.append(f"  {index + 1}: {lines[index].strip()[:90]}")
            index += 1
        index += 1
    assert not bad, "未加引号的 heredoc 里不能有反引号（构建时会被执行）：\n" + "\n".join(bad)
