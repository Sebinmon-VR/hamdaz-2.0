"""Filling a block's configuration from what the run has learned so far.

A step's config is written by a super admin in the builder and holds
placeholders — ``{{ task.title }}``, ``{{ suppliers.verified | json }}`` — that
are resolved against the run's context when the step runs. Deliberately a very
small language: dotted paths, a handful of filters, and a ``when`` condition.
Anything richer belongs in an ``agent`` step, where a model reads the context
and writes what is needed, rather than in a template engine nobody can debug
from the builder screen.

Pure functions, so the whole thing is testable against a dict.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

_PLACEHOLDER: Final = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]-]+)\s*(?:\|\s*([a-z_]+)\s*)?\}\}")
_INDEX: Final = re.compile(r"^([a-zA-Z0-9_-]+)\[(\d+)\]$")


class TemplateError(Exception):
    """A placeholder or condition could not be resolved."""


def lookup(context: dict[str, Any], path: str, default: Any = None) -> Any:
    """``suppliers.verified[0].email`` → the value, or ``default``.

    Missing keys are not errors. A step that asks for something an earlier
    step did not produce should see nothing and decide what that means, not
    crash the run on a typo that the builder could not have caught.
    """
    current: Any = context
    for raw in path.split("."):
        if not raw:
            continue
        name, index = raw, None
        m = _INDEX.match(raw)
        if m:
            name, index = m.group(1), int(m.group(2))
        if isinstance(current, dict):
            current = current.get(name)
        elif isinstance(current, list) and name.isdigit():
            i = int(name)
            current = current[i] if 0 <= i < len(current) else None
        else:
            return default
        if index is not None:
            if isinstance(current, list) and 0 <= index < len(current):
                current = current[index]
            else:
                return default
        if current is None:
            return default
    return current


def _apply_filter(value: Any, name: str | None) -> str:
    if name is None or name == "text":
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)
    if name == "json":
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if name == "count":
        return str(len(value)) if isinstance(value, (list, dict, str)) else "0"
    if name == "lines":
        if isinstance(value, list):
            return "\n".join(_line(v) for v in value)
        return _apply_filter(value, None)
    if name == "bullets":
        if isinstance(value, list):
            return "\n".join(f"- {_line(v)}" for v in value)
        return _apply_filter(value, None)
    if name == "upper":
        return _apply_filter(value, None).upper()
    raise TemplateError(f"Unknown filter {name!r}")


def _line(value: Any) -> str:
    """One list entry as a person would write it on a line."""
    if isinstance(value, dict):
        parts = []
        for key in ("description", "name", "title", "part_number", "brand", "quantity", "unit"):
            if value.get(key) not in (None, ""):
                parts.append(f"{value[key]}" if key in ("description", "name", "title") else f"{key}: {value[key]}")
        return ", ".join(parts) if parts else json.dumps(value, default=str)
    return str(value)


def render(template: str, context: dict[str, Any]) -> str:
    """Replace every ``{{ path | filter }}`` in a string."""
    if not template:
        return template

    def sub(match: re.Match[str]) -> str:
        path, filt = match.group(1), match.group(2)
        return _apply_filter(lookup(context, path), filt)

    return _PLACEHOLDER.sub(sub, template)


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Render placeholders anywhere inside a JSON-ish value.

    A string that is *exactly* one placeholder keeps the value's own type —
    ``"{{ items }}"`` gives the list, not its text — which is what lets a
    block's arguments carry a whole table from one step to the next.
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole and whole.group(2) is None:
            found = lookup(context, whole.group(1))
            return found
        return render(value, context)
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    return value


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none", "null")
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return bool(value)


def condition_holds(when: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    """Whether a step's ``when`` is satisfied.

    ``{"path": "docs.found", "is": false}`` — the value at the path, compared
    to ``is``. A boolean ``is`` compares truthiness; anything else compares
    equality as text. No ``when`` means always.
    """
    if not when:
        return True
    path = str(when.get("path") or "")
    if not path:
        return True
    found = lookup(context, path)
    expected = when.get("is", True)
    if isinstance(expected, bool):
        return truthy(found) is expected
    if expected is None:
        return found is None
    if isinstance(expected, list):
        return found in expected or str(found) in [str(e) for e in expected]
    return str(found) == str(expected)
