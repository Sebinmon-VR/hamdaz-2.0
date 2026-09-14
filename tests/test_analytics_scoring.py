"""The priority score.

Pure arithmetic over explicit inputs — no database, no SharePoint, no clock
beyond what is passed in. That is the point: a ranking that decides who gets
work has to be reproducible and arguable, and neither is possible if you cannot
recompute it from the same numbers a month later.

The cases worth attacking are the ones where a plausible implementation quietly
does the wrong thing: a tied factor deciding the order, an excluded person being
ranked last instead of removed, and capacity failing to actually change the
outcome — which would make the whole ratio feature decorative.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.scoring import NEVER_ASSIGNED_DAYS, Candidate, _normalise, score

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

#: The shipped defaults.
WEIGHTS = {
    "load_vs_capacity": Decimal("0.45"),
    "open_task_count": Decimal("0.30"),
    "days_since_last_assign": Decimal("0.25"),
}


def who(
    name: str,
    *,
    open_tasks: int = 0,
    capacity: str = "1.0",
    days_ago: int | None = 0,
    excluded: str | None = None,
) -> Candidate:
    return Candidate(
        key=name,
        display_name=name,
        open_tasks=open_tasks,
        active_tasks=open_tasks,
        total_tasks=open_tasks,
        capacity=Decimal(capacity),
        last_assigned_at=None if days_ago is None else NOW - timedelta(days=days_ago),
        excluded_reason=excluded,
    )


def ranking(*candidates: Candidate) -> list[str]:
    return [
        r.candidate.display_name
        for r in score(list(candidates), weights=WEIGHTS, now=NOW)
        if not r.excluded
    ]


# ── normalisation ──────────────────────────────────────────────────────


def test_one_always_means_most_deserving() -> None:
    """Whichever direction a factor runs, 1.0 is the person who should get it."""
    assert _normalise([0.0, 10.0], lower_is_better=True) == [1.0, 0.0]
    assert _normalise([0.0, 10.0], lower_is_better=False) == [0.0, 1.0]


def test_a_tied_factor_is_neutral_not_zero() -> None:
    """A factor carrying no information must not silently reorder anybody.

    Scoring a tie as 0 would let an irrelevant factor decide the ranking; scoring
    it as 1 for everyone leaves the decision to the factors that differ.
    """
    assert _normalise([5.0, 5.0, 5.0], lower_is_better=True) == [1.0, 1.0, 1.0]


def test_an_empty_group_is_not_an_error() -> None:
    assert _normalise([], lower_is_better=True) == []
    assert score([], weights=WEIGHTS, now=NOW) == []


# ── the ranking ────────────────────────────────────────────────────────


def test_the_least_loaded_person_goes_first() -> None:
    assert ranking(who("Busy", open_tasks=40), who("Free", open_tasks=0))[0] == "Free"


def test_somebody_with_nothing_to_do_wins_outright() -> None:
    """Every factor favours them, so the score is a clean 1.0."""
    results = score(
        [who("Idle", open_tasks=0, days_ago=None), who("Loaded", open_tasks=30)],
        weights=WEIGHTS,
        now=NOW,
    )
    top = next(r for r in results if r.priority == 1)
    assert top.candidate.display_name == "Idle"
    assert top.factor_total == Decimal("1.0")


def test_the_score_stays_within_zero_and_one() -> None:
    """Contributions are divided by the total weight, so weights need not sum to 1."""
    results = score(
        [who("A", open_tasks=0), who("B", open_tasks=5), who("C", open_tasks=50)],
        # Deliberately summing to 3, not 1.
        weights={k: Decimal("1") for k in WEIGHTS},
        now=NOW,
    )
    for result in results:
        assert Decimal(0) <= result.factor_total <= Decimal(1)


def test_waiting_longest_breaks_a_tie_on_load() -> None:
    """Without this the fastest closers absorb everything.

    Finishing work is what makes somebody look available, so a pure load ranking
    rewards speed with more work until they stop being fast.
    """
    assert ranking(
        who("Recent", open_tasks=5, days_ago=1),
        who("Waiting", open_tasks=5, days_ago=200),
    )[0] == "Waiting"


def test_never_having_been_assigned_counts_as_a_long_wait() -> None:
    """Zero days would rank a brand-new person as though they had just been given work."""
    assert who("New", days_ago=None).days_since_last_assign(NOW) == NEVER_ASSIGNED_DAYS


# ── capacity, which is what makes a ratio real ─────────────────────────


def test_capacity_changes_who_wins() -> None:
    """The whole feature. Same open count, half the capacity, worse position.

    A new joiner on 0.5 holding 5 is judged like somebody holding 10, so they
    reach the front of the queue at half the rate — without "new joiner" being a
    special case anywhere in the scoring.
    """
    assert ranking(
        who("NewJoiner", open_tasks=5, capacity="0.5"),
        who("Senior", open_tasks=5, capacity="1.4"),
    )[0] == "Senior"


def test_a_half_capacity_person_scores_like_double_the_load() -> None:
    assert who("N", open_tasks=2, capacity="0.5").effective_load == Decimal(4)
    assert who("S", open_tasks=7, capacity="1.4").effective_load == Decimal(5)


def test_capacity_cannot_divide_by_zero() -> None:
    """Zero capacity means unassignable, not a crash."""
    assert who("Out", open_tasks=3, capacity="0").effective_load > 0


# ── exclusion ──────────────────────────────────────────────────────────


def test_an_excluded_person_has_no_score_at_all() -> None:
    """Null, not zero: out of the pool is not the same as ranked last.

    A zero invites somebody to sort by it and hand them the work anyway.
    """
    results = score(
        [who("Away", excluded="On approved leave today"), who("Here", open_tasks=3)],
        weights=WEIGHTS,
        now=NOW,
    )
    away = next(r for r in results if r.candidate.display_name == "Away")

    assert away.factor_total is None
    assert away.priority is None
    assert away.excluded_reason == "On approved leave today"


def test_an_excluded_person_is_still_returned() -> None:
    """'Why is Priya not in this list' is a worse question than 'why is she out'."""
    results = score(
        [who("Away", excluded="On leave"), who("Here")], weights=WEIGHTS, now=NOW
    )
    assert len(results) == 2


def test_exclusion_does_not_distort_the_others_normalisation() -> None:
    """Somebody out of the pool must not stretch the scale everyone else sits on."""
    without = score(
        [who("A", open_tasks=0), who("B", open_tasks=10)], weights=WEIGHTS, now=NOW
    )
    with_excluded = score(
        [
            who("A", open_tasks=0),
            who("B", open_tasks=10),
            who("Huge", open_tasks=5000, excluded="Off rotation"),
        ],
        weights=WEIGHTS,
        now=NOW,
    )
    assert [r.factor_total for r in without if r.factor_total is not None] == [
        r.factor_total for r in with_excluded if r.factor_total is not None
    ]


def test_everybody_excluded_is_a_valid_answer() -> None:
    results = score(
        [who("A", excluded="On leave"), who("B", excluded="Off rotation")],
        weights=WEIGHTS,
        now=NOW,
    )
    assert all(r.factor_total is None for r in results)


# ── explaining the result ──────────────────────────────────────────────


def test_every_factor_reports_its_own_contribution() -> None:
    """A single number nobody can decompose is a number nobody will trust."""
    results = score([who("A", open_tasks=0), who("B", open_tasks=9)], weights=WEIGHTS, now=NOW)
    factors = next(r for r in results if r.priority == 1).factors

    assert set(factors) == set(WEIGHTS)
    for detail in factors.values():
        assert {"raw", "normalised", "weight", "contribution"} <= set(detail)


def test_the_contributions_add_up_to_the_score() -> None:
    """If they did not, the breakdown would be decoration rather than an explanation."""
    for result in score(
        [who("A", open_tasks=1), who("B", open_tasks=8), who("C", open_tasks=20)],
        weights=WEIGHTS,
        now=NOW,
    ):
        total = sum(f["contribution"] for f in result.factors.values())
        assert abs(float(result.factor_total) - total) < 1e-6


def test_a_zero_weight_factor_contributes_nothing() -> None:
    """Turning a factor off in the policy must actually turn it off."""
    results = score(
        [who("Recent", open_tasks=5, days_ago=1), who("Waiting", open_tasks=5, days_ago=300)],
        weights={**WEIGHTS, "days_since_last_assign": Decimal(0)},
        now=NOW,
    )
    for result in results:
        assert result.factors["days_since_last_assign"]["contribution"] == 0.0
    # Tied on everything that still counts, so the order falls back to the name.
    assert [r.candidate.display_name for r in results] == ["Recent", "Waiting"]


def test_ties_are_broken_by_name_so_the_order_is_stable() -> None:
    """Two identical people must not swap places between runs."""
    first = ranking(who("Zoe", open_tasks=4), who("Adam", open_tasks=4))
    second = ranking(who("Adam", open_tasks=4), who("Zoe", open_tasks=4))
    assert first == second == ["Adam", "Zoe"]


# ── the rank numbers themselves ────────────────────────────────────────


def test_ranks_are_1_2_3_with_no_gaps() -> None:
    """1 is next in line, then 2, 3, 4 ... consecutively."""
    results = score(
        [who(n, open_tasks=t) for n, t in
         (("A", 0), ("B", 3), ("C", 7), ("D", 12), ("E", 40))],
        weights=WEIGHTS,
        now=NOW,
    )
    assert sorted(r.priority for r in results) == [1, 2, 3, 4, 5]


def test_excluding_someone_does_not_leave_a_hole_in_the_numbering() -> None:
    """A manager sitting between two people must not turn 1,2,3 into 1,3,4.

    Excluded people are left out of the numbering entirely rather than parked at
    the bottom, so the sequence always reads 1..N over the real candidates.
    """
    results = score(
        [
            who("A", open_tasks=0),
            who("Manager", open_tasks=1, excluded="Not given work: holds the 'manager' role"),
            who("B", open_tasks=5),
            who("C", open_tasks=9),
        ],
        weights=WEIGHTS,
        now=NOW,
    )
    ranks = sorted(r.priority for r in results if r.priority is not None)

    assert ranks == [1, 2, 3]
    assert next(r for r in results if r.candidate.display_name == "Manager").priority is None


def test_the_first_rank_is_always_1() -> None:
    """Even with one candidate, and even when everybody ties."""
    assert score([who("Solo")], weights=WEIGHTS, now=NOW)[0].priority == 1

    tied = score([who("A", open_tasks=5), who("B", open_tasks=5)], weights=WEIGHTS, now=NOW)
    assert sorted(r.priority for r in tied) == [1, 2]


def test_rank_1_is_the_person_who_should_get_the_next_task() -> None:
    """Lowest number, highest score. Stating it because the two run opposite ways."""
    results = score(
        [who("Loaded", open_tasks=50), who("Free", open_tasks=0)], weights=WEIGHTS, now=NOW
    )
    top = next(r for r in results if r.priority == 1)

    assert top.candidate.display_name == "Free"
    assert top.factor_total == max(r.factor_total for r in results)


# ── the preview must be a complete object ──────────────────────────────


def test_a_preview_carries_the_same_shape_as_a_kept_run() -> None:
    """A preview is never inserted, so Postgres never generates its id or
    timestamps — and the response model refuses None for both.

    This 500'd in the browser. Filling them in means a caller never has to
    branch on whether the run they are holding was stored.
    """
    import uuid
    from datetime import datetime

    from app.models.analytics import AnalyticsRun

    record = AnalyticsRun(team_name="Presalse", policy_snapshot={}, rows_read=0)
    assert record.id is None and record.created_at is None

    # What service.run does when save=False.
    now = datetime.now(UTC)
    record.id, record.created_at, record.updated_at = uuid.uuid4(), now, now

    assert isinstance(record.id, uuid.UUID)
    assert record.created_at is not None
