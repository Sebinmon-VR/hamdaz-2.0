"""The assignment engine (§5.4).

This is the piece that answers *"a new joiner should get less work"*, *"busy people should
get less work"* and *"assign in ratios"* — without any of those being special cases in code.

Like the evaluator, this module is pure: candidates in, a ranked decision out. No database,
no clock of its own. That is what makes the preview honest — the simulation and the real
assignment run the exact same function over the same inputs, so what the admin sees in the
preview is what will happen.

The mechanism, in one paragraph. Each candidate has a **capacity multiplier** from their
labels: a new joiner's 0.4 means they carry 40% of a normal load. Their **effective load** is
``open_tasks / capacity``, so a new joiner holding 2 tasks scores like someone holding 5, and
the engine stops feeding them work sooner. No branch anywhere says "if new joiner".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models.rules import DistributionMode

DEFAULT_CAPACITY = 1.0
#: Below this a capacity multiplier means "effectively unassignable"; guard against /0.
MIN_CAPACITY = 0.01

DEFAULT_FACTORS: dict[str, dict[str, Any]] = {
    "load_vs_capacity": {"weight": 0.45, "direction": "lower_is_better"},
    "open_task_count": {"weight": 0.30, "direction": "lower_is_better"},
    "days_since_last_assign": {"weight": 0.25, "direction": "higher_is_better"},
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A potential assignee, with everything the policy needs to judge them."""

    user_id: uuid.UUID
    display_name: str
    labels: frozenset[str] = frozenset()
    open_task_count: int = 0
    last_assigned_at: datetime | None = None
    on_leave: bool = False
    #: Assignments in the ratio window, used by ``mode: ratio`` drift correction.
    recent_assignment_count: int = 0

    def days_since_last_assign(self, now: datetime) -> float:
        if self.last_assigned_at is None:
            # Never assigned: maximally "idle", so a genuinely new member is reachable.
            return 3650.0
        last = self.last_assigned_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return max(0.0, (now - last).total_seconds() / 86400.0)


@dataclass(frozen=True, slots=True)
class ExclusionReason:
    user_id: uuid.UUID
    display_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    user_id: uuid.UUID
    display_name: str
    score: float
    capacity: float
    effective_load: float
    #: Per-factor contribution, so the preview can show *why* someone ranked where they did.
    breakdown: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssignmentDecision:
    assignee_id: uuid.UUID | None
    mode: str
    ranked: tuple[ScoredCandidate, ...] = ()
    excluded: tuple[ExclusionReason, ...] = ()
    #: Set when nobody was eligible; names the configured fallback.
    fallback: str | None = None
    explanation: str = ""

    @property
    def assigned(self) -> bool:
        return self.assignee_id is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "assignee_id": str(self.assignee_id) if self.assignee_id else None,
            "mode": self.mode,
            "fallback": self.fallback,
            "explanation": self.explanation,
            "ranked": [
                {
                    "user_id": str(c.user_id),
                    "display_name": c.display_name,
                    "score": round(c.score, 4),
                    "capacity": c.capacity,
                    "effective_load": round(c.effective_load, 3),
                    "breakdown": {k: round(v, 4) for k, v in c.breakdown.items()},
                }
                for c in self.ranked
            ],
            "excluded": [
                {"user_id": str(e.user_id), "display_name": e.display_name, "reason": e.reason}
                for e in self.excluded
            ],
        }


# ──────────────────────────────────────────────────────────────────────────
# Capacity
# ──────────────────────────────────────────────────────────────────────────


def capacity_for(candidate: Candidate, capacity_config: Mapping[str, Any]) -> float:
    """The candidate's capacity multiplier.

    When someone holds several labels with different multipliers the **lowest** wins. A
    part-time new joiner should be treated as the more constrained of the two, not the
    average — erring toward giving someone less work is recoverable; overloading them is
    what the legacy rotation kept doing.
    """
    default = _as_float(capacity_config.get("default"), DEFAULT_CAPACITY)
    by_label = capacity_config.get("by_label") or {}
    if not isinstance(by_label, Mapping):
        return max(default, MIN_CAPACITY)

    applicable = [
        _as_float(by_label[label], default)
        for label in candidate.labels
        if label in by_label
    ]
    value = min(applicable) if applicable else default
    return max(value, MIN_CAPACITY)


def effective_load(candidate: Candidate, capacity: float) -> float:
    """Open tasks scaled by capacity — the number that makes 'new joiner' work."""
    return candidate.open_task_count / max(capacity, MIN_CAPACITY)


# ──────────────────────────────────────────────────────────────────────────
# Eligibility
# ──────────────────────────────────────────────────────────────────────────


def filter_eligible(
    candidates: Sequence[Candidate],
    eligibility: Mapping[str, Any],
    capacity_config: Mapping[str, Any],
    *,
    required_labels: frozenset[str] = frozenset(),
) -> tuple[list[Candidate], list[ExclusionReason]]:
    """Apply the hard filters. Everyone rejected is recorded with a reason.

    Recording the reason is not a nicety: "why didn't Sara get this?" is exactly as common a
    question as "why did Rahul?", and the legacy system could answer neither.
    """
    eligible: list[Candidate] = []
    excluded: list[ExclusionReason] = []

    not_labelled = frozenset(str(x) for x in (eligibility.get("not_labelled") or ()))
    requires = (
        frozenset(str(x) for x in (eligibility.get("requires_labels") or ())) | required_labels
    )
    max_open = eligibility.get("max_open_tasks") or {}
    respect_leave = eligibility.get("not_on_leave", True)

    for candidate in candidates:
        if respect_leave and candidate.on_leave:
            excluded.append(ExclusionReason(candidate.user_id, candidate.display_name, "on leave"))
            continue

        blocking = candidate.labels & not_labelled
        if blocking:
            excluded.append(
                ExclusionReason(
                    candidate.user_id,
                    candidate.display_name,
                    f"holds excluded label: {', '.join(sorted(blocking))}",
                )
            )
            continue

        missing = requires - candidate.labels
        if missing:
            excluded.append(
                ExclusionReason(
                    candidate.user_id,
                    candidate.display_name,
                    f"missing required label: {', '.join(sorted(missing))}",
                )
            )
            continue

        ceiling = _max_open_for(candidate, max_open)
        if ceiling is not None and candidate.open_task_count >= ceiling:
            excluded.append(
                ExclusionReason(
                    candidate.user_id,
                    candidate.display_name,
                    f"at their limit ({candidate.open_task_count}/{ceiling} open)",
                )
            )
            continue

        eligible.append(candidate)

    return eligible, excluded


def _max_open_for(candidate: Candidate, config: Mapping[str, Any] | Any) -> int | None:
    """The candidate's hard ceiling on open tasks — the tightest applicable one."""
    if isinstance(config, int | float):
        return int(config)
    if not isinstance(config, Mapping):
        return None

    by_label = config.get("by_label") or {}
    limits = [
        int(_as_float(by_label[label], 0))
        for label in candidate.labels
        if isinstance(by_label, Mapping) and label in by_label
    ]
    if limits:
        return min(limits)

    default = config.get("default")
    return int(default) if default is not None else None


# ──────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────


def _normalise(values: Sequence[float], *, lower_is_better: bool) -> list[float]:
    """Map raw values onto 0..1 where 1 is always "more deserving of the next task".

    When every candidate has the same value the factor is neutral (all 1.0) rather than
    arbitrary — otherwise a tied factor would silently decide the outcome.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)

    span = hi - lo
    if lower_is_better:
        return [(hi - v) / span for v in values]
    return [(v - lo) / span for v in values]


def score_candidates(
    candidates: Sequence[Candidate],
    capacity_config: Mapping[str, Any],
    factors: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> list[ScoredCandidate]:
    """Rank candidates by the configured weighted factors. Highest score wins."""
    if not candidates:
        return []

    config = factors if isinstance(factors, Mapping) and factors else DEFAULT_FACTORS

    capacities = [capacity_for(c, capacity_config) for c in candidates]
    loads = [effective_load(c, cap) for c, cap in zip(candidates, capacities, strict=True)]

    raw: dict[str, list[float]] = {
        "load_vs_capacity": loads,
        "open_task_count": [float(c.open_task_count) for c in candidates],
        "days_since_last_assign": [c.days_since_last_assign(now) for c in candidates],
        "capacity": capacities,
    }

    contributions: dict[str, list[float]] = {}
    total_weight = 0.0

    for name, spec in config.items():
        if name not in raw:
            continue
        weight = _as_float(spec.get("weight") if isinstance(spec, Mapping) else spec, 0.0)
        if weight <= 0:
            continue
        direction = (
            str(spec.get("direction", "lower_is_better"))
            if isinstance(spec, Mapping)
            else "lower_is_better"
        )
        normalised = _normalise(raw[name], lower_is_better=direction == "lower_is_better")
        contributions[name] = [n * weight for n in normalised]
        total_weight += weight

    scored: list[ScoredCandidate] = []
    for index, candidate in enumerate(candidates):
        breakdown = {name: values[index] for name, values in contributions.items()}
        total = sum(breakdown.values())
        # Normalise by total weight so scores stay comparable across differently-configured
        # policies — a preview showing 0.82 should mean the same thing in every team.
        score = total / total_weight if total_weight > 0 else 0.0
        scored.append(
            ScoredCandidate(
                user_id=candidate.user_id,
                display_name=candidate.display_name,
                score=score,
                capacity=capacities[index],
                effective_load=loads[index],
                breakdown=breakdown,
            )
        )

    return scored


# ──────────────────────────────────────────────────────────────────────────
# Distribution modes
# ──────────────────────────────────────────────────────────────────────────


def _ratio_target_label(
    candidates: Sequence[Candidate], ratio_config: Mapping[str, Any]
) -> str | None:
    """Which label group is furthest below its target share right now.

    Drift is measured over a rolling window, so a run of luck corrects itself instead of
    compounding — which is the whole point of asking for ratios rather than randomness.
    """
    targets = ratio_config.get("targets") or {}
    if not isinstance(targets, Mapping) or not targets:
        return None

    total_target = sum(_as_float(v, 0.0) for v in targets.values())
    if total_target <= 0:
        return None

    counts = dict.fromkeys(targets, 0)
    total_recent = 0
    for candidate in candidates:
        for label in candidate.labels:
            if label in counts:
                counts[label] += candidate.recent_assignment_count
                total_recent += candidate.recent_assignment_count

    # Nothing assigned yet: seed with the largest target share.
    if total_recent == 0:
        return str(max(targets, key=lambda k: _as_float(targets[k], 0.0)))

    worst_label: str | None = None
    worst_deficit = 0.0
    for label, target in targets.items():
        desired = _as_float(target, 0.0) / total_target
        actual = counts[label] / total_recent
        deficit = desired - actual
        if deficit > worst_deficit:
            worst_deficit, worst_label = deficit, label

    return worst_label


def _apply_tie_break(
    tied: Sequence[ScoredCandidate],
    candidates_by_id: Mapping[uuid.UUID, Candidate],
    tie_break: str,
    now: datetime,
) -> ScoredCandidate:
    if tie_break == "longest_idle":
        return max(
            tied,
            key=lambda s: candidates_by_id[s.user_id].days_since_last_assign(now),
        )
    if tie_break == "lowest_load":
        return min(tied, key=lambda s: s.effective_load)
    if tie_break == "highest_capacity":
        return max(tied, key=lambda s: s.capacity)
    # Deterministic fallback: never random, so a preview matches the real run.
    return min(tied, key=lambda s: str(s.user_id))


def decide(
    *,
    candidates: Sequence[Candidate],
    eligibility: Mapping[str, Any],
    capacity_config: Mapping[str, Any],
    distribution: Mapping[str, Any],
    tie_break: str = "longest_idle",
    fallback: str = "notify_manager",
    required_labels: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> AssignmentDecision:
    """Pick an assignee. Pure — the same inputs always give the same answer.

    That determinism is what lets the admin panel show a trustworthy preview: the simulation
    calls this function with the same arguments the real assignment will.
    """
    now = now or datetime.now(UTC)
    mode = str(distribution.get("mode") or DistributionMode.WEIGHTED_LEAST_LOADED)

    eligible, excluded = filter_eligible(
        candidates, eligibility, capacity_config, required_labels=required_labels
    )

    if mode == DistributionMode.MANUAL:
        return AssignmentDecision(
            assignee_id=None,
            mode=mode,
            excluded=tuple(excluded),
            fallback="manual",
            explanation="This policy assigns manually; a manager will choose.",
        )

    if not eligible:
        return AssignmentDecision(
            assignee_id=None,
            mode=mode,
            excluded=tuple(excluded),
            fallback=fallback,
            explanation=(
                f"No eligible assignee among {len(candidates)} candidate(s). "
                f"Falling back to: {fallback}."
            ),
        )

    pool = list(eligible)
    ratio_note = ""

    if mode == DistributionMode.RATIO:
        ratio_config = distribution.get("ratio") or {}
        target_label = (
            _ratio_target_label(pool, ratio_config) if isinstance(ratio_config, Mapping) else None
        )
        if target_label:
            narrowed = [c for c in pool if target_label in c.labels]
            if narrowed:
                pool = narrowed
                ratio_note = f" Ratio targeting '{target_label}', which is furthest below share."
            else:
                ratio_note = (
                    f" Ratio wanted '{target_label}' but nobody eligible holds it; "
                    "fell back to the whole eligible pool."
                )

    if mode == DistributionMode.ROUND_ROBIN:
        factors: Mapping[str, Any] = {
            "days_since_last_assign": {"weight": 1.0, "direction": "higher_is_better"}
        }
    elif mode == DistributionMode.LEAST_LOADED:
        factors = {"open_task_count": {"weight": 1.0, "direction": "lower_is_better"}}
    else:
        factors = distribution.get("factors") or DEFAULT_FACTORS

    scored = score_candidates(pool, capacity_config, factors, now=now)
    scored.sort(key=lambda s: (-s.score, str(s.user_id)))

    best_score = scored[0].score
    tied = [s for s in scored if abs(s.score - best_score) < 1e-9]
    by_id = {c.user_id: c for c in pool}
    winner = _apply_tie_break(tied, by_id, tie_break, now) if len(tied) > 1 else scored[0]

    explanation = (
        f"{winner.display_name} scored {winner.score:.3f} using {mode}. "
        f"Effective load {winner.effective_load:.2f} "
        f"({by_id[winner.user_id].open_task_count} open ÷ capacity {winner.capacity:g})."
        + (f" Tie broken by {tie_break}." if len(tied) > 1 else "")
        + ratio_note
    )

    return AssignmentDecision(
        assignee_id=winner.user_id,
        mode=mode,
        ranked=tuple(scored),
        excluded=tuple(excluded),
        explanation=explanation,
    )


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


DEFAULT_POLICY: dict[str, Any] = {
    "eligibility": {
        "not_on_leave": True,
        "not_labelled": ["excluded-from-rotation", "on-notice"],
        "requires_labels": [],
        "max_open_tasks": {"default": 8, "by_label": {"new-joiner": 3, "part-time": 4}},
    },
    "capacity": {
        "default": 1.0,
        "by_label": {
            "new-joiner": 0.4,
            "on-probation": 0.3,
            "part-time": 0.5,
            "team-lead": 0.6,
            "senior": 1.0,
        },
    },
    "distribution": {
        "mode": DistributionMode.WEIGHTED_LEAST_LOADED.value,
        "factors": DEFAULT_FACTORS,
        "ratio": {"by": "label", "targets": {}, "window": "rolling_30d"},
    },
    "tie_break": "longest_idle",
    "fallback": "notify_manager",
}
