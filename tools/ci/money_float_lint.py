#!/usr/bin/env python3
"""ED-8 money-float lint — Money is BIGINT KES cents, end to end.

Section 0, ED-8: "Never floats for money anywhere in the platform — CI lint
rule bans `float` in any signature typed `Money`." This checker parses every
Python source under `platform/`, `agents/`, and `packs/` and fails if:

  * a function whose signature (any parameter annotation or the return
    annotation) mentions `Money` also mentions `float`, or
  * `Money` is aliased to `float` (e.g. `Money = float`).

Exit code 1 on any violation, printing `path:line: ...`. No third-party deps.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOTS = ("platform", "agents", "packs")


def _annotation_names(node: ast.AST | None) -> set[str]:
    """Every identifier / forward-ref string appearing inside an annotation."""
    names: set[str] = set()
    if node is None:
        return names
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            names.add(n.value)  # string / forward-ref annotation
    return names


def check_source(src: str, filename: str) -> list[tuple[int, str]]:
    """Return (lineno, message) violations for one source string."""
    violations: list[tuple[int, str]] = []
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as e:
        return [(e.lineno or 0, f"syntax error: {e.msg}")]

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "Money" in targets and isinstance(node.value, ast.Name) and node.value.id == "float":
                violations.append((node.lineno, "`Money` aliased to `float`"))

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            names: set[str] = set()
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None:
                    names |= _annotation_names(arg.annotation)
            names |= _annotation_names(node.returns)
            if "Money" in names and "float" in names:
                violations.append(
                    (node.lineno, f"`float` in Money-typed signature of `{node.name}`")
                )
    return violations


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parents[2]
    failed = False
    for root in ROOTS:
        base = repo / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(repo)
            for lineno, msg in check_source(path.read_text(encoding="utf-8"), str(rel)):
                failed = True
                print(f"{rel}:{lineno}: ED-8 money-float: {msg}")

    if failed:
        print("\nED-8 violated: Money is BIGINT KES cents; no floats in Money signatures.")
        return 1
    print("money-float lint: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
