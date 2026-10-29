"""函数里用到的懒导入模块，必须在函数自己里导入。

main.py 为了让 `--version` 不拖 numpy，把 audio/dsp 放进函数内导入。
0.1.39 有人把四处 `from . import audio, dsp` 改成只导 audio，
单测全靠 monkeypatch 绕过了这些函数，结果 `voxos run` 起不来、`voxos enroll` 读完一句就崩。
这里静态扫一遍，不依赖麦克风。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "voxos"


def _annotation_ids(func: ast.AST) -> set[int]:
    ids: set[int] = set()
    args = func.args  # type: ignore[attr-defined]
    for arg in args.args + args.kwonlyargs + args.posonlyargs:
        if arg.annotation:
            ids |= {id(x) for x in ast.walk(arg.annotation)}
    if func.returns:  # type: ignore[attr-defined]
        ids |= {id(x) for x in ast.walk(func.returns)}  # type: ignore[attr-defined]
    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign):
            ids |= {id(x) for x in ast.walk(node.annotation)}
    return ids


def _misses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
           for a in n.names}
    lazy = {a.asname or a.name for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n not in tree.body for a in n.names} - top
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        skip = _annotation_ids(func)
        local = {a.asname or a.name for n in ast.walk(func)
                 if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        local |= {t.id for n in ast.walk(func) if isinstance(n, (ast.Assign, ast.For))
                  for t in ast.walk(n.targets[0] if isinstance(n, ast.Assign) else n.target)
                  if isinstance(t, ast.Name)}
        for node in ast.walk(func):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in lazy and node.id not in local and id(node) not in skip):
                found.append(f"{path.name}:{node.lineno} {func.name} 用了 {node.id} 却没导入")
    return found


def test_every_lazy_module_is_imported_where_used():
    misses = [m for path in sorted(SRC.glob("*.py")) for m in _misses(path)]
    assert not misses, "\n".join(misses)
