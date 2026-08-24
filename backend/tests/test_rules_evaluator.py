"""The rules evaluator.

This code decides who gets work and which quotes need approval, so it is tested against the
edge cases rather than the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.rules.evaluator import (
    CompiledRule,
    RuleValidationError,
    evaluate_conditions,
    evaluate_operator,
    evaluate_rule_set,
    validate_actions,
    validate_conditions,
    validate_rule,
)
from app.core.rules.registry import (
    DECISION_POINTS,
    FactType,
    Operator,
    UnknownDecisionPointError,
    get_decision_point,
)

QUOTE_DP = get_decision_point("quote.approval_route")
LABEL_DP = get_decision_point("user.label")


def _rule(
    position: int,
    conditions: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    name: str = "rule",
    enabled: bool = True,
) -> CompiledRule:
    return CompiledRule(
        id=f"rule-{position}",
        name=name,
        position=position,
        conditions=conditions,
        actions=actions,
        enabled=enabled,
    )


class TestRegistryIntegrity:
    def test_every_decision_point_declares_facts_and_actions(self) -> None:
        for dp in DECISION_POINTS:
            assert dp.facts, f"{dp.key} declares no facts"
            assert dp.actions, f"{dp.key} declares no actions"

    def test_fact_map_is_built(self) -> None:
        assert "quote.total" in QUOTE_DP.fact_map

    def test_every_fact_type_has_operators(self) -> None:
        for dp in DECISION_POINTS:
            for fact in dp.facts:
                assert fact.operators, f"{dp.key}.{fact.key} has no operators"

    def test_unknown_decision_point_lists_the_known_ones(self) -> None:
        with pytest.raises(UnknownDecisionPointError) as exc:
            get_decision_point("nope.nope")
        assert "quote.approval_route" in str(exc.value)


class TestOperators:
    @pytest.mark.parametrize(
        ("op", "actual", "expected", "result"),
        [
            (Operator.EQ, 100, 100, True),
            (Operator.EQ, 100, 200, False),
            (Operator.NE, 100, 200, True),
            (Operator.GT, 500_000, 100_000, True),
            (Operator.GT, 100, 100, False),
            (Operator.GTE, 100, 100, True),
            (Operator.LT, 5, 12, True),
            (Operator.LTE, 12, 12, True),
        ],
    )
    def test_numeric(self, op: Operator, actual: Any, expected: Any, result: bool) -> None:
        passed, _ = evaluate_operator(op, actual, expected, FactType.NUMBER)
        assert passed is result

    def test_numeric_strings_are_coerced(self) -> None:
        """Values arriving from JSON are often strings."""
        passed, note = evaluate_operator(Operator.GT, "500", 100, FactType.NUMBER)
        assert passed is True
        assert note is None

    def test_non_numeric_comparison_reports_why(self) -> None:
        passed, note = evaluate_operator(Operator.GT, "abc", 100, FactType.NUMBER)
        assert passed is False
        assert note is not None and "not comparable" in note

    def test_bool_is_not_treated_as_a_number(self) -> None:
        """True > 0 is almost never what a rule author meant."""
        passed, note = evaluate_operator(Operator.GT, True, 0, FactType.NUMBER)
        assert passed is False
        assert note is not None

    @pytest.mark.parametrize(
        ("op", "actual", "expected", "result"),
        [
            (Operator.CONTAINS, {"senior", "cctv"}, "senior", True),
            (Operator.CONTAINS, {"senior"}, "junior", False),
            (Operator.NOT_CONTAINS, {"senior"}, "junior", True),
            (Operator.CONTAINS_ANY, {"a", "b"}, ["b", "c"], True),
            (Operator.CONTAINS_ANY, {"a"}, ["b", "c"], False),
            (Operator.CONTAINS_ALL, {"a", "b", "c"}, ["a", "b"], True),
            (Operator.CONTAINS_ALL, {"a"}, ["a", "b"], False),
            (Operator.IS_EMPTY, set(), None, True),
            (Operator.IS_EMPTY, {"a"}, None, False),
            (Operator.IS_NOT_EMPTY, {"a"}, None, True),
        ],
    )
    def test_string_sets(self, op: Operator, actual: Any, expected: Any, result: bool) -> None:
        passed, _ = evaluate_operator(op, actual, expected, FactType.STRING_SET)
        assert passed is result

    def test_a_bare_string_counts_as_a_one_element_set(self) -> None:
        passed, _ = evaluate_operator(Operator.CONTAINS, "senior", "senior", FactType.STRING_SET)
        assert passed is True

    def test_string_contains_is_case_insensitive(self) -> None:
        passed, _ = evaluate_operator(Operator.CONTAINS, "ADNOC Group", "adnoc", FactType.STRING)
        assert passed is True

    def test_in_and_not_in(self) -> None:
        assert evaluate_operator(Operator.IN, "AED", ["AED", "USD"], FactType.STRING)[0]
        assert evaluate_operator(Operator.NOT_IN, "GBP", ["AED", "USD"], FactType.STRING)[0]

    def test_dates_compare_chronologically_not_lexically(self) -> None:
        passed, _ = evaluate_operator(
            Operator.GT, "2026-09-01T00:00:00", "2026-08-23T00:00:00", FactType.DATE
        )
        assert passed is True

    def test_mixed_naive_and_aware_datetimes_do_not_raise(self) -> None:
        """Real data has both. Raising here would take down an assignment."""
        passed, note = evaluate_operator(
            Operator.GT, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 8, 1), FactType.DATE
        )
        assert passed is True
        assert note is None

    def test_missing_fact_never_matches(self) -> None:
        from app.core.rules.evaluator import _MISSING

        passed, note = evaluate_operator(Operator.EQ, _MISSING, 1, FactType.NUMBER)
        assert passed is False
        assert note == "fact not supplied"


class TestConditionTrees:
    def test_always_matches(self) -> None:
        matched, _ = evaluate_conditions({"always": True}, {}, QUOTE_DP)
        assert matched is True

    def test_empty_conditions_match_nothing(self) -> None:
        """A half-built rule must not silently apply to everything."""
        matched, _ = evaluate_conditions({}, {"quote.total": 1}, QUOTE_DP)
        assert matched is False

    def test_all_requires_every_clause(self) -> None:
        conditions = {
            "all": [
                {"fact": "quote.total", "op": ">=", "value": 500_000},
                {"fact": "quote.currency", "op": "=", "value": "AED"},
            ]
        }
        assert evaluate_conditions(conditions, {"quote.total": 600_000, "quote.currency": "AED"}, QUOTE_DP)[0]
        assert not evaluate_conditions(conditions, {"quote.total": 600_000, "quote.currency": "USD"}, QUOTE_DP)[0]

    def test_any_requires_one_clause(self) -> None:
        conditions = {
            "any": [
                {"fact": "quote.total", "op": ">=", "value": 500_000},
                {"fact": "quote.margin_pct", "op": "<", "value": 12},
            ]
        }
        assert evaluate_conditions(conditions, {"quote.total": 10, "quote.margin_pct": 5}, QUOTE_DP)[0]
        assert not evaluate_conditions(conditions, {"quote.total": 10, "quote.margin_pct": 30}, QUOTE_DP)[0]

    def test_none_inverts(self) -> None:
        conditions = {"none": [{"fact": "quote.currency", "op": "=", "value": "GBP"}]}
        assert evaluate_conditions(conditions, {"quote.currency": "AED"}, QUOTE_DP)[0]
        assert not evaluate_conditions(conditions, {"quote.currency": "GBP"}, QUOTE_DP)[0]

    def test_nesting(self) -> None:
        """'large deal AND (thin margin OR discounted)' without an expression language."""
        conditions = {
            "all": [
                {"fact": "quote.total", "op": ">=", "value": 100_000},
                {
                    "any": [
                        {"fact": "quote.margin_pct", "op": "<", "value": 12},
                        {"fact": "quote.has_discount", "op": "=", "value": True},
                    ]
                },
            ]
        }
        facts = {"quote.total": 200_000, "quote.margin_pct": 25, "quote.has_discount": True}
        assert evaluate_conditions(conditions, facts, QUOTE_DP)[0]

        facts["quote.has_discount"] = False
        assert not evaluate_conditions(conditions, facts, QUOTE_DP)[0]

    def test_unknown_fact_fails_closed_and_says_so(self) -> None:
        """Failing open here would grant approval on a typo."""
        matched, results = evaluate_conditions(
            {"fact": "quote.totl", "op": ">=", "value": 1}, {"quote.total": 999}, QUOTE_DP
        )
        assert matched is False
        assert results[0].note is not None and "unknown fact" in results[0].note

    def test_unknown_operator_fails_closed(self) -> None:
        matched, results = evaluate_conditions(
            {"fact": "quote.total", "op": "~=", "value": 1}, {"quote.total": 1}, QUOTE_DP
        )
        assert matched is False
        assert results[0].note is not None and "unknown operator" in results[0].note

    def test_every_branch_is_traced_even_after_a_failure(self) -> None:
        """The trace is the product; short-circuiting would explain nothing."""
        conditions = {
            "all": [
                {"fact": "quote.total", "op": ">=", "value": 999_999},
                {"fact": "quote.currency", "op": "=", "value": "AED"},
            ]
        }
        _, results = evaluate_conditions(
            {"quote.total": 1, "quote.currency": "AED"} and conditions,
            {"quote.total": 1, "quote.currency": "AED"},
            QUOTE_DP,
        )
        assert len(results) == 2


class TestRuleSetEvaluation:
    def test_first_match_wins_by_default(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[
                _rule(1, {"fact": "quote.total", "op": ">=", "value": 500_000},
                      [{"type": "require_approval", "approver_role": "org_manager"}], name="big"),
                _rule(2, {"always": True}, [{"type": "auto_approve"}], name="default"),
            ],
            facts={"quote.total": 900_000},
        )
        assert decision.matched_rule_ids == ("rule-1",)
        assert decision.has_action("require_approval")
        assert not decision.has_action("auto_approve")

    def test_later_rules_are_traced_as_skipped(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[
                _rule(1, {"always": True}, [{"type": "auto_approve"}]),
                _rule(2, {"always": True}, [{"type": "block", "reason": "x"}]),
            ],
            facts={},
        )
        assert decision.trace[1].skipped_reason == "an earlier rule already matched"

    def test_evaluate_all_collects_every_match(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[
                _rule(1, {"always": True}, [{"type": "notify", "target": "team_lead"}]),
                _rule(2, {"always": True}, [{"type": "require_approval", "approver_role": "org_manager"}]),
            ],
            facts={},
            evaluate_all=True,
        )
        assert len(decision.matched_rule_ids) == 2
        assert len(decision.actions) == 2

    def test_rules_run_in_position_order_not_list_order(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[
                _rule(2, {"always": True}, [{"type": "auto_approve"}], name="second"),
                _rule(1, {"always": True}, [{"type": "block", "reason": "first"}], name="first"),
            ],
            facts={},
        )
        assert decision.matched_rule_ids == ("rule-1",)

    def test_disabled_rules_are_skipped_but_traced(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[
                _rule(1, {"always": True}, [{"type": "block", "reason": "x"}], enabled=False),
                _rule(2, {"always": True}, [{"type": "auto_approve"}]),
            ],
            facts={},
        )
        assert decision.matched_rule_ids == ("rule-2",)
        assert decision.trace[0].skipped_reason == "disabled"

    def test_no_match_is_a_valid_outcome(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[_rule(1, {"fact": "quote.total", "op": ">", "value": 10}, [{"type": "auto_approve"}])],
            facts={"quote.total": 5},
        )
        assert decision.matched is False
        assert decision.actions == ()
        # "Why did nothing happen?" must be answerable too.
        assert decision.trace[0].matched is False

    def test_empty_rule_set_does_not_crash(self) -> None:
        decision = evaluate_rule_set(decision_point=QUOTE_DP, rules=[], facts={})
        assert decision.matched is False

    def test_trace_serialises_for_storage(self) -> None:
        decision = evaluate_rule_set(
            decision_point=QUOTE_DP,
            rules=[_rule(1, {"fact": "quote.total", "op": ">", "value": 10}, [{"type": "auto_approve"}])],
            facts={"quote.total": 50},
        )
        payload = decision.to_trace_json()
        assert payload[0]["matched"] is True
        assert payload[0]["conditions"][0]["actual"] == 50


class TestValidation:
    """Validation runs at save time, so a broken rule never reaches evaluation."""

    def test_valid_rule_passes(self) -> None:
        validate_rule(
            {"fact": "quote.total", "op": ">=", "value": 1},
            [{"type": "require_approval", "approver_role": "org_manager"}],
            QUOTE_DP,
        )

    def test_unknown_fact_is_rejected_with_the_available_list(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_conditions({"fact": "quote.nope", "op": "=", "value": 1}, QUOTE_DP)
        assert "quote.total" in str(exc.value)

    def test_operator_must_suit_the_fact_type(self) -> None:
        """'total contains x' is nonsense and is caught before it can be saved."""
        with pytest.raises(RuleValidationError) as exc:
            validate_conditions({"fact": "quote.total", "op": "contains", "value": "x"}, QUOTE_DP)
        assert "does not apply" in str(exc.value)

    def test_value_is_required_except_for_emptiness_checks(self) -> None:
        with pytest.raises(RuleValidationError):
            validate_conditions({"fact": "quote.total", "op": ">="}, QUOTE_DP)
        validate_conditions({"fact": "actor.labels", "op": "is_not_empty"}, QUOTE_DP)

    def test_choice_facts_reject_values_outside_the_list(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_conditions({"fact": "quote.currency", "op": "=", "value": "GBP"}, QUOTE_DP)
        assert "AED" in str(exc.value)

    def test_empty_conditions_are_rejected_at_save_time(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_conditions({}, QUOTE_DP)
        assert "always" in str(exc.value)

    def test_deep_nesting_is_refused(self) -> None:
        node: dict[str, Any] = {"fact": "quote.total", "op": ">=", "value": 1}
        for _ in range(12):
            node = {"all": [node]}
        with pytest.raises(RuleValidationError) as exc:
            validate_conditions(node, QUOTE_DP)
        assert "too deeply" in str(exc.value)

    def test_empty_combinator_list_is_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            validate_conditions({"all": []}, QUOTE_DP)

    def test_unknown_action_is_rejected(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_actions([{"type": "delete_everything"}], QUOTE_DP)
        assert "Unknown action" in str(exc.value)

    def test_action_from_another_decision_point_is_rejected(self) -> None:
        """grant_label belongs to user.label, not quote.approval_route."""
        with pytest.raises(RuleValidationError):
            validate_actions([{"type": "grant_label", "label": "senior"}], QUOTE_DP)

    def test_missing_required_action_param_is_rejected(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_actions([{"type": "require_approval"}], QUOTE_DP)
        assert "approver_role" in str(exc.value)

    def test_action_param_choices_are_enforced(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_actions(
                [{"type": "require_approval", "approver_role": "janitor"}], QUOTE_DP
            )
        assert "Allowed" in str(exc.value)

    def test_a_rule_must_do_something(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            validate_actions([], QUOTE_DP)
        assert "at least one action" in str(exc.value)

    def test_optional_params_may_be_omitted(self) -> None:
        validate_actions([{"type": "grant_label", "label": "new-joiner"}], LABEL_DP)
