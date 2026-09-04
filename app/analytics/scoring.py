"""Turning workload numbers into a ranked priority score.

Pure functions, no I/O and no clock beyond what is passed in. Everything the
score depends on arrives as an argument, which is what makes a ranking
reproducible: the same inputs give the same answer next month, when the live
counts have moved on and somebody is asking why a decision was made.

**Three factors**, weighted by the policy:

* ``load_vs_capacity`` — open tasks divided by capacity. This is where a ratio
  becomes real: a new joiner on 0.5 holding 2 tasks scores like someone holding
  4, so they reach the front of the queue at half the rate without being a
  special case anywhere in this file.
* ``open_task_count`` — raw active work. Kept alongside the capacity-adjusted
  figure because somebody genuinely holding twenty things is under pressure
  whatever their multiplier says.
* ``days_since_last_assign`` — how long they have waited. Without it the two
  fastest closers absorb everything, because finishing work is what makes you
  look available.

Each is normalised across the candidates to 0..1 where **1 always means "most
deserving of the next task"**, then weighted and summed. Normalising within the
group rather than against absolute thresholds is deliberate: "busy" only means
anything relative to the people you are choosing between.

A factor where everybody ties contributes 1.0 to all of them rather than 0. A
tie is not evidence, and scoring it as zero would let an irrelevant factor
quietly reorder the ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

#: Below this a capacity multiplier means "effectively unassignable", and it
#: also guards the division.
MIN_CAPACITY: Final = Decimal("0.01")

#: Somebody who has never been given anything should rank as having waited a
#: long time, not as having waited zero days.
NEVER_ASSIGNED_DAYS: Final = 365


@dataclass(slots=True)
class Candidate:
    """One person's inputs. Everything the score sees about them."""

    key: str
    display_name: str
    user_id: str | None = None
    email: str | None = None
    lookup_id: str | None = None

    total_tasks: int = 0
    open_tasks: int = 0
    completed_tasks: int = 0
    overdue_tasks: int = 0
    due_soon_tasks: int = 0
    no_status_tasks: int = 0
    expired_tasks: int = 0
    bid_closed_tasks: int = 0
    active_tasks: int = 0
    last_assigned_at: datetime | None = None

    labels: set[str] = field(default_factory=set)
    capacity: Decimal = Decimal(1)
    max_open: int | None = None
    excluded_reason: str | None = None

    def days_since_last_assign(self, now: datetime) -> float:
        if self.last_assigned_at is None:
            return float(NEVER_ASSIGNED_DAYS)
        delta = now - self.last_assigned_at
        return max(delta.total_seconds() / 86400.0, 0.0)

    @property
    def effective_load(self) -> Decimal:
        """Open work scaled by capacity — the number that makes 'new joiner' work."""
        return Decimal(self.active_tasks) / max(self.capacity, MIN_CAPACITY)


@dataclass(slots=True)
class Scored:
    candidate: Candidate

    #: **The priority score**: 1, 2, 3, 4 ... where **1 is the highest priority**
    #: — the person who should get the next task. Consecutive over the people who
    #: can actually be assigned, with no gaps and no shared numbers. ``None`` for
    #: anyone excluded, who has no position in the queue at all.
    priority: int | None

    #: The sum of the weighted factor contributions, 0..1. **Not a score** —
    #: the score is ``priority``. This exists only to show how far apart two
    #: positions are, because first and second can be separated by a hair or by
    #: a mile and 1 and 2 look identical either way.
    factor_total: Decimal | None

    factors: dict[str, Any]
    excluded: bool
    excluded_reason: str | None


def _normalise(values: Sequence[float], *, lower_is_better: bool) -> list[float]:
    """Map raw values onto 0..1 where 1 is always "more deserving of the next task".

    When everybody ties the factor is neutral (all 1.0) rather than arbitrary —
    otherwise a factor carrying no information would silently decide the order.
    """
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [1.0] * len(values)

    span = high - low
    if lower_is_better:
        return [(high - v) / span for v in values]
    return [(v - low) / span for v in values]


#: Which direction is better for each factor. Fewer tasks is better; more days
#: waited is better.
_DIRECTION: Final = {
    "load_vs_capacity": True,
    "open_task_count": True,
    "days_since_last_assign": False,
}


def score(
    candidates: Sequence[Candidate],
    *,
    weights: dict[str, Decimal],
    now: datetime | None = None,
) -> list[Scored]:
    """Rank the assignable candidates. **Priority 1 gets the next task.**

    Excluded people are returned too, with a ``None`` score. Dropping them would
    make the response silently disagree with the team roster, and "why is Priya
    not in this list" is a worse question than "why is Priya excluded".
    """
    now = now or datetime.now(UTC)
    if not candidates:
        return []

    eligible = [c for c in candidates if c.excluded_reason is None]
    excluded = [c for c in candidates if c.excluded_reason is not None]

    results: list[Scored] = []

    if eligible:
        raw: dict[str, list[float]] = {
            "load_vs_capacity": [float(c.effective_load) for c in eligible],
            "open_task_count": [float(c.active_tasks) for c in eligible],
            "days_since_last_assign": [c.days_since_last_assign(now) for c in eligible],
        }

        total_weight = sum(
            float(weights.get(name, 0)) for name in raw if float(weights.get(name, 0)) > 0
        )
        breakdown: dict[str, list[dict[str, float]]] = {}

        for name, values in raw.items():
            weight = float(weights.get(name, 0))
            normalised = _normalise(values, lower_is_better=_DIRECTION[name])
            breakdown[name] = [
                {
                    "raw": round(value, 4),
                    "normalised": round(norm, 4),
                    "weight": round(weight, 4),
                    # Divided by the total so the score lands in 0..1 whatever
                    # weights the policy uses — they need not sum to 1.
                    "contribution": round(norm * weight / total_weight, 6)
                    if total_weight
                    else 0.0,
                }
                for value, norm in zip(values, normalised, strict=True)
            ]

        totals = [
            sum(breakdown[name][index]["contribution"] for name in breakdown)
            for index in range(len(eligible))
        ]

        # 1, 2, 3, ... with no gaps and no shared numbers, over the people who
        # can actually be assigned. 1 is the highest priority. Anyone excluded is
        # left out of the numbering entirely rather than given a place at the
        # bottom, so the sequence always reads 1..N over the real candidates.
        # Ties fall back to the name so two identical people do not swap places
        # between runs.
        ranked = sorted(
            range(len(eligible)), key=lambda i: (-totals[i], eligible[i].display_name.casefold())
        )
        rank_of = {index: position + 1 for position, index in enumerate(ranked)}

        for index, candidate in enumerate(eligible):
            results.append(
                Scored(
                    candidate=candidate,
                    priority=rank_of[index],
                    factor_total=Decimal(str(round(totals[index], 6))),
                    factors={name: breakdown[name][index] for name in breakdown},
                    excluded=False,
                    excluded_reason=None,
                )
            )

    for candidate in excluded:
        results.append(
            Scored(
                candidate=candidate,
                # No position at all, rather than last: a number invites somebody
                # to sort by it and hand them the work anyway.
                priority=None,
                factor_total=None,
                factors={},
                excluded=True,
                excluded_reason=candidate.excluded_reason,
            )
        )

    # Ranked first, then the excluded, each alphabetically.
    results.sort(
        key=lambda r: (r.priority is None, r.priority or 0, r.candidate.display_name.casefold())
    )
    return results
