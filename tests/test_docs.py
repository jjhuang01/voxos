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
    claims = _claims(r"(\d+)\s*(?:项测试|passed)")
    assert claims, "一个测试数都没找到，说明措辞变了，改这条测试"
    assert len(claims) == 1, _explain("测试数", claims)


def test_test_count_matches_pytest_collection():
    """文档说多少，就真的有多少——上面那条只保证「到处一样」。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, (result.stdout[-500:] + result.stderr[-500:])
    claimed = next(iter(_claims(r"(\d+)\s*(?:项测试|passed)")))
    assert int(match.group(1)) == claimed, f"文档写 {claimed} 项，实际收集到 {match.group(1)} 项"


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
