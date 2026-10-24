"""Every log call's format string must work with its arguments. A bad one (v5.0.0: "$%,.0f") raises inside
logging, so the line is never printed, and it failed the dashboard self-test, which runs pytest with its
logging plugin on."""
from __future__ import annotations

import ast
import pathlib

LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
ROOT = pathlib.Path(__file__).resolve().parent.parent / "northstar"


def _calls():
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LEVELS
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("log", "logger", "logging")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and not any(isinstance(a, ast.Starred) for a in node.args)):
                yield path, node.lineno, node.args[0].value, len(node.args) - 1


def test_every_log_format_matches_its_arguments():
    bad = []
    for path, line, fmt, n in _calls():
        try:
            if n:
                fmt % tuple([1] * n)
            elif "%" in fmt.replace("%%", ""):
                pass                      # no arguments: logging prints the text as it is
        except (TypeError, ValueError) as e:
            bad.append(f"{path.name}:{line}: {fmt!r} with {n} argument(s): {e}")
    assert not bad, "\n".join(bad)
