#!/usr/bin/env python
"""Guard #3 — CI enforcement of constraint C2.

Fails the build if any HTTP write call appears in the SharePoint connector package outside
the one module allowed to have them.

This parses the source rather than grepping it. A grep for ``.post(`` is defeated by a line
break, a variable holding the verb, or ``getattr(client, "post")``; walking the AST catches
the shapes that matter and does not fire on the word "post" inside a docstring.

Run locally:  uv run python scripts/check_sharepoint_readonly.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: HTTP verbs that mutate. ``request``/``send`` are included because they take the verb as an
#: argument and so could smuggle one past a name-based check.
WRITE_METHODS = frozenset({"post", "put", "patch", "delete", "request", "send", "stream"})
WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE", "MERGE"})

CONNECTOR_DIR = Path("app/connectors/sharepoint")

#: The only module permitted to write, and only ever to the sandbox site. See §8.1.1.
EXEMPT = {"sandbox.py"}

#: guard.py names the verbs in order to *refuse* them, so its string literals are expected.
LITERAL_EXEMPT = {"guard.py"}


class WriteCallFinder(ast.NodeVisitor):
    def __init__(self, path: Path, *, check_literals: bool) -> None:
        self.path = path
        self.check_literals = check_literals
        self.findings: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # client.post(...) / self._http.patch(...)
        if isinstance(func, ast.Attribute) and func.attr.lower() in WRITE_METHODS:
            self.findings.append((node.lineno, f"call to .{func.attr}()"))

        # getattr(client, "post")(...) — the obvious way around a name check
        if isinstance(func, ast.Name) and func.id == "getattr":
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and str(arg.value).lower() in WRITE_METHODS:
                    self.findings.append((node.lineno, f'getattr(..., "{arg.value}")'))

        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            self.check_literals
            and isinstance(node.value, str)
            and node.value.upper() in WRITE_VERBS
        ):
            self.findings.append((node.lineno, f'HTTP verb literal "{node.value}"'))
        self.generic_visit(node)


def scan(root: Path) -> list[str]:
    connector_dir = root / CONNECTOR_DIR
    if not connector_dir.is_dir():
        return [f"{CONNECTOR_DIR} does not exist — the guard has nothing to protect"]

    problems: list[str] = []

    for path in sorted(connector_dir.rglob("*.py")):
        if path.name in EXEMPT:
            continue

        finder = WriteCallFinder(path, check_literals=path.name not in LITERAL_EXEMPT)
        finder.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))

        problems.extend(
            f"{path.as_posix()}:{line}: {what}" for line, what in finder.findings
        )

    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    problems = scan(root)

    if not problems:
        print("SharePoint read-only guard: OK - no write calls outside the sandbox module.")
        return 0

    print("SharePoint read-only guard FAILED (constraint C2)\n", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(
        "\nLive SharePoint is in production and must never be written to.\n"
        "See docs/PROJECT_PLAN.md §8.1. If you need a write for sandbox testing, put it in\n"
        f"{CONNECTOR_DIR.as_posix()}/sandbox.py, which is bound to the sandbox site.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
