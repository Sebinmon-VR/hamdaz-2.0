"""The assignment engine (§5.4).

The requirements in the user's own words were: a new joiner should get less work, people
already holding several jobs should get less, and it should be possible to assign in ratios.
Each has a test class here asserting the behaviour end to end, not just the helper.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.rules.assignment import (
    DEFAULT_POLICY,
    Candidate,
    capacity_for,
    decide,
    effective_load,
    filter_eligible,
    score_candidates,
)
from app.models.rules import DistributionMode

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

CAPACITY = DEFAULT_POLICY["capacity"]
ELIGIBILITY = DEFAULT_POLICY["eligibility"]
DISTRIBUTION = DEFAULT_POLICY["distribution"]


def _c(
    name: str,
    *,
    labels: set[str] | None = None,
    open_tasks: int = 0,
    days_idle: float = 1.0,
    on_leave: bool = False,
    recent: int = 0,
) -> Candidate:
    return Candidate(
        user_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        display_name=name,
        labels=frozenset(labels or set()),
        open_task_count=open_tasks,
        last_assigned_at=NOW - timedelta(days=days_idle),
        on_leave=on_leave,
        recent_assignment_count=recent,
    )


class TestCapacity:
    def test_default_applies_without_labels(self) -> None:
        assert capacity_for(_c("A"), CAPACITY) == 1.0

    def test_label_multiplier_applies(self) -> None:
        assert capacity_for(_c("A", labels={"new-joiner"}), CAPACITY) == 0.4

    def test_lowest_multiplier_wins_when_labels_overlap(self) -> None:
        """A part-time new joiner is the more constrained of the two, not the average."""
        candidate = _c("A", labels={"new-joiner", "part-time"})
        assert capacity_for(candidate, CAPACITY) == 0.4

    def test_unknown_labels_are_ignored(self) -> None:
        assert capacity_for(_c("A", labels={"likes-tea"}), CAPACITY) == 1.0

    def test_capacity_is_floored_to_avoid_division_by_zero(self) -> None:
        assert capacity_for(_c("A", labels={"z"}), {"default": 0.0}) > 0

    def test_effective_load_scales_by_capacity(self) -> None:
        """The mechanism behind 'a new joiner gets less work'."""
        new_joiner = _c("N", labels={"new-joiner"}, open_tasks=2)
        assert effective_load(new_joiner, capacity_for(new_joiner, CAPACITY)) == 5.0


class TestNewJoinerGetsLessWork:
    """The user's first requirement, asserted as behaviour rather than configuration."""

    def test_new_joiner_loses_to_a_senior_at_equal_raw_load(self) -> None:
        senior = _c("Senior", labels={"senior"}, open_tasks=2)
        joiner = _c("Joiner", labels={"new-joiner"}, open_tasks=2)

        decision = decide(
            candidates=[senior, joiner],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        assert decision.assignee_id == senior.user_id

    def test_a_new_joiner_can_still_win_when_genuinely_free(self) -> None:
        """Reduced capacity must not become a permanent exclusion."""
        senior = _c("Senior", labels={"senior"}, open_tasks=6)
        joiner = _c("Joiner", labels={"new-joiner"}, open_tasks=0)

        decision = decide(
            candidates=[senior, joiner],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        assert decision.assignee_id == joiner.user_id

    def test_new_joiner_hits_a_lower_ceiling(self) -> None:
        joiner = _c("Joiner", labels={"new-joiner"}, open_tasks=3)
        eligible, excluded = filter_eligible([joiner], ELIGIBILITY, CAPACITY)
        assert eligible == []
        assert "at their limit" in excluded[0].reason

    def test_a_senior_at_the_same_count_is_still_eligible(self) -> None:
        senior = _c("Senior", labels={"senior"}, open_tasks=3)
        eligible, _ = filter_eligible([senior], ELIGIBILITY, CAPACITY)
        assert len(eligible) == 1


class TestBusyPeopleGetLessWork:
    """The user's second requirement."""

    def test_the_least_loaded_of_equals_wins(self) -> None:
        busy = _c("Busy", labels={"senior"}, open_tasks=5)
        free = _c("Free", labels={"senior"}, open_tasks=1)

        decision = decide(
            candidates=[busy, free],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        assert decision.assignee_id == free.user_id

    def test_the_hard_ceiling_cannot_be_exceeded(self) -> None:
        at_limit = _c("AtLimit", labels={"senior"}, open_tasks=8)
        eligible, excluded = filter_eligible([at_limit], ELIGIBILITY, CAPACITY)
        assert eligible == []
        assert excluded[0].reason.startswith("at their limit")

    def test_load_ordering_holds_across_a_realistic_team(self) -> None:
        team = [
            _c("A", labels={"senior"}, open_tasks=7),
            _c("B", labels={"senior"}, open_tasks=4),
            _c("C", labels={"senior"}, open_tasks=1),
            _c("D", labels={"senior"}, open_tasks=6),
        ]
        decision = decide(
            candidates=team,
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        assert decision.ranked[0].display_name == "C"


class TestRatioMode:
    """The user's third requirement: 'assign like in ratios'."""

    def test_the_group_furthest_below_its_share_is_targeted(self) -> None:
        distribution: dict[str, Any] = {
            "mode": DistributionMode.RATIO.value,
            "ratio": {"by": "label", "targets": {"senior": 3, "junior": 1}},
        }
        # Juniors are far below their 25% target, so the next one should go to a junior.
        candidates = [
            _c("S1", labels={"senior"}, open_tasks=1, recent=9),
            _c("J1", labels={"junior"}, open_tasks=1, recent=0),
        ]
        decision = decide(
            candidates=candidates,
            eligibility={"not_on_leave": True},
            capacity_config={"default": 1.0},
            distribution=distribution,
            now=NOW,
        )
        assert decision.assignee_id == candidates[1].user_id
        assert "junior" in decision.explanation

    def test_the_largest_target_seeds_an_empty_window(self) -> None:
        distribution: dict[str, Any] = {
            "mode": DistributionMode.RATIO.value,
            "ratio": {"targets": {"senior": 3, "junior": 1}},
        }
        candidates = [_c("S1", labels={"senior"}), _c("J1", labels={"junior"})]
        decision = decide(
            candidates=candidates,
            eligibility={"not_on_leave": True},
            capacity_config={"default": 1.0},
            distribution=distribution,
            now=NOW,
        )
        assert decision.assignee_id == candidates[0].user_id

    def test_falls_back_to_the_whole_pool_when_the_target_group_is_unavailable(self) -> None:
        """Ratio targeting must never strand work because a group is all on leave."""
        distribution: dict[str, Any] = {
            "mode": DistributionMode.RATIO.value,
            "ratio": {"targets": {"junior": 1}},
        }
        decision = decide(
            candidates=[_c("S1", labels={"senior"})],
            eligibility={"not_on_leave": True},
            capacity_config={"default": 1.0},
            distribution=distribution,
            now=NOW,
        )
        assert decision.assigned
        assert "fell back" in decision.explanation


class TestEligibility:
    def test_people_on_leave_are_excluded(self) -> None:
        eligible, excluded = filter_eligible([_c("A", on_leave=True)], ELIGIBILITY, CAPACITY)
        assert eligible == []
        assert excluded[0].reason == "on leave"

    def test_the_legacy_exclude_list_is_now_a_label(self) -> None:
        candidate = _c("A", labels={"excluded-from-rotation"})
        eligible, excluded = filter_eligible([candidate], ELIGIBILITY, CAPACITY)
        assert eligible == []
        assert "excluded label" in excluded[0].reason

    def test_required_skill_labels_gate_specialist_work(self) -> None:
        generalist = _c("Gen", labels={"senior"})
        specialist = _c("Spec", labels={"senior", "security"})

        eligible, excluded = filter_eligible(
            [generalist, specialist],
            ELIGIBILITY,
            CAPACITY,
            required_labels=frozenset({"security"}),
        )
        assert [c.display_name for c in eligible] == ["Spec"]
        assert "missing required label" in excluded[0].reason

    def test_every_exclusion_carries_a_reason(self) -> None:
        """'Why didn't Sara get this?' is as common a question as 'why did Rahul?'."""
        _, excluded = filter_eligible(
            [_c("A", on_leave=True), _c("B", labels={"on-notice"}), _c("C", open_tasks=99)],
            ELIGIBILITY,
            CAPACITY,
        )
        assert len(excluded) == 3
        assert all(e.reason for e in excluded)


class TestScoringMechanics:
    def test_identical_candidates_score_identically(self) -> None:
        scored = score_candidates(
            [_c("A", open_tasks=2), _c("B", open_tasks=2)], CAPACITY, None, now=NOW
        )
        assert scored[0].score == pytest.approx(scored[1].score)

    def test_a_tied_factor_stays_neutral(self) -> None:
        """A factor where everyone is equal must not silently decide the outcome."""
        scored = score_candidates(
            [_c("A", open_tasks=3, days_idle=1), _c("B", open_tasks=3, days_idle=1)],
            CAPACITY,
            None,
            now=NOW,
        )
        assert scored[0].score == pytest.approx(scored[1].score)

    def test_breakdown_explains_the_ranking(self) -> None:
        scored = score_candidates(
            [_c("A", open_tasks=1), _c("B", open_tasks=9)], CAPACITY, None, now=NOW
        )
        assert scored[0].breakdown
        assert "load_vs_capacity" in scored[0].breakdown

    def test_never_assigned_ranks_as_maximally_idle(self) -> None:
        never = Candidate(uuid.uuid4(), "Never", last_assigned_at=None)
        assert never.days_since_last_assign(NOW) > 1000

    def test_scores_are_comparable_across_policies(self) -> None:
        """Normalising by total weight keeps 0.82 meaning the same thing everywhere."""
        candidates = [_c("A", open_tasks=1), _c("B", open_tasks=9)]
        one = score_candidates(candidates, CAPACITY, {"open_task_count": {"weight": 1.0}}, now=NOW)
        ten = score_candidates(candidates, CAPACITY, {"open_task_count": {"weight": 10.0}}, now=NOW)
        assert one[0].score == pytest.approx(ten[0].score)

    def test_zero_weight_factors_are_ignored(self) -> None:
        scored = score_candidates(
            [_c("A", open_tasks=1)], CAPACITY, {"open_task_count": {"weight": 0}}, now=NOW
        )
        assert scored[0].score == 0.0


class TestDistributionModes:
    def test_round_robin_picks_the_longest_idle(self) -> None:
        recent = _c("Recent", days_idle=1)
        stale = _c("Stale", days_idle=30)
        decision = decide(
            candidates=[recent, stale],
            eligibility={"not_on_leave": True},
            capacity_config={"default": 1.0},
            distribution={"mode": DistributionMode.ROUND_ROBIN.value},
            now=NOW,
        )
        assert decision.assignee_id == stale.user_id

    def test_least_loaded_ignores_capacity(self) -> None:
        joiner = _c("Joiner", labels={"new-joiner"}, open_tasks=0)
        senior = _c("Senior", labels={"senior"}, open_tasks=2)
        decision = decide(
            candidates=[joiner, senior],
            eligibility={"not_on_leave": True},
            capacity_config=CAPACITY,
            distribution={"mode": DistributionMode.LEAST_LOADED.value},
            now=NOW,
        )
        assert decision.assignee_id == joiner.user_id

    def test_manual_mode_assigns_nobody(self) -> None:
        decision = decide(
            candidates=[_c("A")],
            eligibility={},
            capacity_config=CAPACITY,
            distribution={"mode": DistributionMode.MANUAL.value},
            now=NOW,
        )
        assert decision.assignee_id is None
        assert decision.fallback == "manual"


class TestFallbackAndDeterminism:
    def test_work_is_never_silently_dropped(self) -> None:
        decision = decide(
            candidates=[_c("A", on_leave=True)],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            fallback="notify_manager",
            now=NOW,
        )
        assert decision.assignee_id is None
        assert decision.fallback == "notify_manager"
        assert "No eligible assignee" in decision.explanation

    def test_an_empty_team_does_not_crash(self) -> None:
        decision = decide(
            candidates=[],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        assert decision.assigned is False

    def test_the_same_inputs_always_give_the_same_answer(self) -> None:
        """Determinism is what makes the admin preview trustworthy."""
        candidates = [_c("A", open_tasks=3), _c("B", open_tasks=3), _c("C", open_tasks=3)]
        results = {
            decide(
                candidates=candidates,
                eligibility=ELIGIBILITY,
                capacity_config=CAPACITY,
                distribution=DISTRIBUTION,
                now=NOW,
            ).assignee_id
            for _ in range(20)
        }
        assert len(results) == 1

    def test_ties_break_on_longest_idle(self) -> None:
        recent = _c("Recent", open_tasks=3, days_idle=1)
        stale = _c("Stale", open_tasks=3, days_idle=40)
        decision = decide(
            candidates=[recent, stale],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution={"mode": DistributionMode.WEIGHTED_LEAST_LOADED.value,
                          "factors": {"open_task_count": {"weight": 1.0}}},
            tie_break="longest_idle",
            now=NOW,
        )
        assert decision.assignee_id == stale.user_id

    def test_decision_serialises_for_the_preview(self) -> None:
        decision = decide(
            candidates=[_c("A", open_tasks=1), _c("B", on_leave=True)],
            eligibility=ELIGIBILITY,
            capacity_config=CAPACITY,
            distribution=DISTRIBUTION,
            now=NOW,
        )
        payload = decision.to_json()
        assert payload["ranked"][0]["display_name"] == "A"
        assert payload["excluded"][0]["reason"] == "on leave"
        assert payload["explanation"]
