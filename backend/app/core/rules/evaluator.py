"""The rules evaluator.

Pure, synchronous and side-effect free: facts in, decision out. No database, no clock, no
network. That is deliberate — this code decides who gets work, so it must be exhaustively
testable without infrastructure, and its output must depend on nothing but its input.

Conditions are **data, not code**. There is no ``eval``, no expression parser, no lambdas.
A condition is a ``{fact, op, value}`` triple looked up against a registry-declared fact.
That is the structural answer to risk #6 — the engine cannot grow into a language, because
adding an operator means editing this file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.rules.registry import (
    DecisionPoint,
    FactType,
    Operator,
)


class RuleValidationError(ValueError):
    """A rule that cannot be evaluated. Raised at save time, never at evaluation time."""


@dataclass(frozen=True, slots=True)
class ConditionResult:
    fact: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    #: Set when the condition could not be evaluated — a missing fact, say.
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    name: str
    position: int
    matched: bool
    conditions: tuple[ConditionResult, ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    skipped_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What the engine concluded, plus everything needed to explain it."""

    decision_point: str
    matched_rule_ids: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    trace: tuple[RuleResult, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return bool(self.matched_rule_ids)

    def actions_of_type(self, action_type: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(a for a in self.actions if a.get("type") == action_type)

    def has_action(self, action_type: str) -> bool:
        return any(a.get("type") == action_type for a in self.actions)

    def to_trace_json(self) -> list[dict[str, Any]]:
        """Serialised for ``rule_evaluations.trace``."""
        return [
            {
                "rule_id": r.rule_id,
                "name": r.name,
                "position": r.position,
                "matched": r.matched,
                "skipped_reason": r.skipped_reason,
                "conditions": [
                    {
                        "fact": c.fact,
                        "op": c.op,
                        "expected": _jsonable(c.expected),
                        "actual": _jsonable(c.actual),
                        "passed": c.passed,
                        "note": c.note,
                    }
                    for c in r.conditions
                ],
                "actions": [dict(a) for a in r.actions],
            }
            for r in self.trace
        ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, set | frozenset):
        return sorted(str(v) for v in value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool | dict):
        return value
    return str(value)


# ──────────────────────────────────────────────────────────────────────────
# Operator implementations
# ──────────────────────────────────────────────────────────────────────────

_MISSING = object()


def _as_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, Sequence | set | frozenset):
        return frozenset(str(v) for v in value)
    return frozenset({str(value)})


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        # bool is an int subclass; comparing True > 0 is almost never intended.
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _compare_numeric(op: Operator, actual: Any, expected: Any) -> tuple[bool, str | None]:
    a, b = _as_number(actual), _as_number(expected)
    if a is None or b is None:
        return False, f"not comparable as numbers: {actual!r} vs {expected!r}"
    return {
        Operator.GT: a > b,
        Operator.GTE: a >= b,
        Operator.LT: a < b,
        Operator.LTE: a <= b,
    }[op], None


def _compare_dates(op: Operator, actual: Any, expected: Any) -> tuple[bool, str | None]:
    a, b = _as_datetime(actual), _as_datetime(expected)
    if a is None or b is None:
        return False, f"not comparable as dates: {actual!r} vs {expected!r}"
    # Mixing naive and aware datetimes raises; normalise by dropping tzinfo for comparison.
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return {
        Operator.EQ: a == b,
        Operator.NE: a != b,
        Operator.GT: a > b,
        Operator.GTE: a >= b,
        Operator.LT: a < b,
        Operator.LTE: a <= b,
    }[op], None


def evaluate_operator(
    op: Operator, actual: Any, expected: Any, fact_type: FactType
) -> tuple[bool, str | None]:
    """Apply one operator. Returns ``(passed, note)``."""
    if actual is _MISSING:
        return False, "fact not supplied"

    if fact_type is FactType.DATE and op in {
        Operator.EQ,
        Operator.NE,
        Operator.GT,
        Operator.GTE,
        Operator.LT,
        Operator.LTE,
    }:
        return _compare_dates(op, actual, expected)

    match op:
        case Operator.EQ:
            if fact_type is FactType.NUMBER:
                a, b = _as_number(actual), _as_number(expected)
                return (a is not None and b is not None and a == b), None
            return actual == expected, None

        case Operator.NE:
            if fact_type is FactType.NUMBER:
                a, b = _as_number(actual), _as_number(expected)
                return not (a is not None and b is not None and a == b), None
            return actual != expected, None

        case Operator.GT | Operator.GTE | Operator.LT | Operator.LTE:
            return _compare_numeric(op, actual, expected)

        case Operator.IN:
            return actual in _as_set(expected), None

        case Operator.NOT_IN:
            return actual not in _as_set(expected), None

        case Operator.CONTAINS:
            if fact_type is FactType.STRING_SET:
                return str(expected) in _as_set(actual), None
            return str(expected).lower() in str(actual or "").lower(), None

        case Operator.NOT_CONTAINS:
            if fact_type is FactType.STRING_SET:
                return str(expected) not in _as_set(actual), None
            return str(expected).lower() not in str(actual or "").lower(), None

        case Operator.CONTAINS_ANY:
            return bool(_as_set(actual) & _as_set(expected)), None

        case Operator.CONTAINS_ALL:
            return _as_set(expected) <= _as_set(actual), None

        case Operator.IS_EMPTY:
            if fact_type is FactType.STRING_SET:
                return not _as_set(actual), None
            return actual in (None, "", [], {}), None

        case Operator.IS_NOT_EMPTY:
            if fact_type is FactType.STRING_SET:
                return bool(_as_set(actual)), None
            return actual not in (None, "", [], {}), None

    return False, f"unsupported operator {op!r}"  # pragma: no cover - match is exhaustive


# ──────────────────────────────────────────────────────────────────────────
# Condition trees
# ──────────────────────────────────────────────────────────────────────────


def _evaluate_leaf(
    clause: Mapping[str, Any],
    facts: Mapping[str, Any],
    decision_point: DecisionPoint,
) -> ConditionResult:
    fact_key = str(clause.get("fact", ""))
    raw_op = str(clause.get("op", ""))
    expected = clause.get("value")

    fact = decision_point.fact_map.get(fact_key)
    if fact is None:
        return ConditionResult(
            fact=fact_key,
            op=raw_op,
            expected=expected,
            actual=None,
            passed=False,
            note=f"unknown fact for {decision_point.key}",
        )

    try:
        op = Operator(raw_op)
    except ValueError:
        return ConditionResult(
            fact_key, raw_op, expected, None, False, note=f"unknown operator {raw_op!r}"
        )

    actual = facts.get(fact_key, _MISSING)
    passed, note = evaluate_operator(op, actual, expected, fact.type)

    return ConditionResult(
        fact=fact_key,
        op=raw_op,
        expected=expected,
        actual=None if actual is _MISSING else actual,
        passed=passed,
        note=note,
    )


def evaluate_conditions(
    conditions: Mapping[str, Any],
    facts: Mapping[str, Any],
    decision_point: DecisionPoint,
) -> tuple[bool, tuple[ConditionResult, ...]]:
    """Evaluate a condition tree. Returns ``(matched, per-condition results)``.

    Supported shapes::

        {"always": true}
        {"all": [ {...}, {...} ]}    # every clause must pass
        {"any": [ {...}, {...} ]}    # at least one clause must pass
        {"none": [ {...} ]}          # no clause may pass
        {"fact": "x", "op": "=", "value": 1}   # a bare leaf

    ``all``/``any``/``none`` nest, so an admin can express "large deal AND (thin margin OR
    new customer)" without the engine needing an expression language.
    """
    results: list[ConditionResult] = []

    def walk(node: Mapping[str, Any]) -> bool:
        if not node:
            # An empty condition block matches nothing. Matching everything would make a
            # half-built rule silently live.
            return False

        if node.get("always") is True:
            return True

        for key, combinator in (("all", all), ("any", any)):
            if key in node:
                clauses = node[key]
                if not isinstance(clauses, list) or not clauses:
                    return False
                # Evaluate every branch rather than short-circuiting: the trace is the
                # product here, and a partial trace explains nothing.
                return combinator([walk(c) for c in clauses])

        if "none" in node:
            clauses = node["none"]
            if not isinstance(clauses, list) or not clauses:
                return False
            # The list is deliberate — it forces every branch to evaluate so
            # the trace is complete. A generator would short-circuit and explain nothing.
            return not any([walk(c) for c in clauses])  # noqa: C419

        if "fact" in node:
            result = _evaluate_leaf(node, facts, decision_point)
            results.append(result)
            return result.passed

        return False

    matched = walk(conditions)
    return matched, tuple(results)


# ──────────────────────────────────────────────────────────────────────────
# Rule sets
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A rule flattened out of the ORM, so the evaluator stays database-free."""

    id: str
    name: str
    position: int
    conditions: Mapping[str, Any]
    actions: Sequence[Mapping[str, Any]]
    enabled: bool = True


def evaluate_rule_set(
    *,
    decision_point: DecisionPoint,
    rules: Sequence[CompiledRule],
    facts: Mapping[str, Any],
    evaluate_all: bool = False,
) -> Decision:
    """Run an ordered rule set.

    First match wins unless ``evaluate_all``. Either way every rule is traced — including
    the ones that did not match and why — because "why did nothing happen?" is a question
    the developer panel has to answer just as often as "why did this happen?".
    """
    trace: list[RuleResult] = []
    matched_ids: list[str] = []
    collected: list[Mapping[str, Any]] = []
    stop = False

    for rule in sorted(rules, key=lambda r: r.position):
        if stop:
            trace.append(
                RuleResult(
                    rule_id=rule.id,
                    name=rule.name,
                    position=rule.position,
                    matched=False,
                    skipped_reason="an earlier rule already matched",
                )
            )
            continue

        if not rule.enabled:
            trace.append(
                RuleResult(rule.id, rule.name, rule.position, False, skipped_reason="disabled")
            )
            continue

        matched, conditions = evaluate_conditions(rule.conditions, facts, decision_point)
        actions = tuple(rule.actions) if matched else ()

        trace.append(
            RuleResult(
                rule_id=rule.id,
                name=rule.name,
                position=rule.position,
                matched=matched,
                conditions=conditions,
                actions=actions,
            )
        )

        if matched:
            matched_ids.append(rule.id)
            collected.extend(actions)
            if not evaluate_all:
                stop = True

    return Decision(
        decision_point=decision_point.key,
        matched_rule_ids=tuple(matched_ids),
        actions=tuple(collected),
        trace=tuple(trace),
        facts=dict(facts),
    )


# ──────────────────────────────────────────────────────────────────────────
# Validation — runs at save time so a broken rule never reaches evaluation
# ──────────────────────────────────────────────────────────────────────────


def validate_conditions(
    conditions: Mapping[str, Any], decision_point: DecisionPoint, *, _depth: int = 0
) -> None:
    if _depth > 10:
        raise RuleValidationError("Conditions are nested too deeply (limit 10).")

    if not isinstance(conditions, Mapping):
        raise RuleValidationError("Conditions must be an object.")

    if not conditions:
        raise RuleValidationError("Conditions cannot be empty. Use {'always': true} to match all.")

    if conditions.get("always") is True:
        return

    for key in ("all", "any", "none"):
        if key in conditions:
            clauses = conditions[key]
            if not isinstance(clauses, list) or not clauses:
                raise RuleValidationError(f"'{key}' must be a non-empty list of conditions.")
            for clause in clauses:
                validate_conditions(clause, decision_point, _depth=_depth + 1)
            return

    if "fact" in conditions:
        _validate_leaf(conditions, decision_point)
        return

    raise RuleValidationError(
        "A condition needs one of: 'always', 'all', 'any', 'none', or a 'fact' clause."
    )


def _validate_leaf(clause: Mapping[str, Any], decision_point: DecisionPoint) -> None:
    fact_key = clause.get("fact")
    fact = decision_point.fact_map.get(str(fact_key))
    if fact is None:
        available = ", ".join(sorted(decision_point.fact_map))
        raise RuleValidationError(
            f"Unknown fact {fact_key!r} for {decision_point.key}. Available: {available}"
        )

    raw_op = clause.get("op")
    try:
        op = Operator(str(raw_op))
    except ValueError:
        raise RuleValidationError(f"Unknown operator {raw_op!r}.") from None

    if op not in fact.operators:
        allowed = ", ".join(o.value for o in fact.operators)
        raise RuleValidationError(
            f"Operator {op.value!r} does not apply to {fact.key!r} "
            f"({fact.type.value}). Allowed: {allowed}"
        )

    needs_value = op not in {Operator.IS_EMPTY, Operator.IS_NOT_EMPTY}
    if needs_value and "value" not in clause:
        raise RuleValidationError(f"Condition on {fact.key!r} with {op.value!r} needs a value.")

    if fact.choices and op in {Operator.EQ, Operator.NE}:
        value = clause.get("value")
        if value is not None and str(value) not in fact.choices:
            allowed = ", ".join(fact.choices)
            raise RuleValidationError(
                f"{value!r} is not a valid value for {fact.key!r}. Allowed: {allowed}"
            )


def validate_actions(
    actions: Sequence[Mapping[str, Any]], decision_point: DecisionPoint
) -> None:
    if not isinstance(actions, Sequence) or isinstance(actions, str):
        raise RuleValidationError("Actions must be a list.")
    if not actions:
        raise RuleValidationError("A rule must do something — add at least one action.")

    for entry in actions:
        if not isinstance(entry, Mapping):
            raise RuleValidationError("Each action must be an object.")

        action_type = str(entry.get("type", ""))
        action = decision_point.get_action(action_type)
        if action is None:
            available = ", ".join(sorted(decision_point.action_types()))
            raise RuleValidationError(
                f"Unknown action {action_type!r} for {decision_point.key}. Available: {available}"
            )

        for param in action.params:
            if param.required and entry.get(param.key) in (None, ""):
                raise RuleValidationError(
                    f"Action {action_type!r} requires {param.key!r} ({param.description})."
                )
            value = entry.get(param.key)
            if value is not None and param.choices and str(value) not in param.choices:
                allowed = ", ".join(param.choices)
                raise RuleValidationError(
                    f"{value!r} is not valid for {action_type}.{param.key}. Allowed: {allowed}"
                )


def validate_rule(
    conditions: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    decision_point: DecisionPoint,
) -> None:
    validate_conditions(conditions, decision_point)
    validate_actions(actions, decision_point)
