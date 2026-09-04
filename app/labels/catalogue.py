"""The labels the product ships with.

Seeded like the module catalogue: these are code, not user data, because the
assignment policy refers to some of them by key. Admins may add any others they
like and rename these; deleting a system label is refused, because the policy
would then point at nothing.

The seniority ladder is deliberately short. Five rungs is enough to express
"gets less work" and "gets more", and every extra one is another number somebody
has to justify to the person on the rung below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.models.labels import (
    LABEL_EXCLUDED,
    LABEL_NEW_JOINER,
    LABEL_ON_LEAVE,
    LabelKind,
)


@dataclass(frozen=True, slots=True)
class LabelSpec:
    key: str
    name: str
    kind: LabelKind
    description: str
    color: str | None = None
    #: Suggested capacity multiplier, used to seed a new policy. Not the
    #: authority — the policy row is, and an admin changes it there.
    capacity: float | None = None
    #: Computed at read time rather than assigned. See app.models.labels.
    derived: bool = False


LABELS: Final[tuple[LabelSpec, ...]] = (
    # ── seniority: how much work ───────────────────────────────────────
    LabelSpec(
        key=LABEL_NEW_JOINER,
        name="New Joiner",
        kind=LabelKind.CATEGORY,
        description=(
            "Joined recently. Applied automatically from their joining date and "
            "lapses on its own once the policy's window passes."
        ),
        color="#2b6cb0",
        capacity=0.5,
        derived=True,
    ),
    LabelSpec(
        key="junior",
        name="Junior",
        kind=LabelKind.CATEGORY,
        description="Working independently on routine items, still building depth.",
        color="#3182ce",
        capacity=0.7,
    ),
    LabelSpec(
        key="mid",
        name="Mid-level",
        kind=LabelKind.CATEGORY,
        description="The baseline. Carries a full share of the work.",
        color="#4a5568",
        capacity=1.0,
    ),
    LabelSpec(
        key="senior",
        name="Senior",
        kind=LabelKind.CATEGORY,
        description="Handles the difficult items and takes a larger share.",
        color="#1a7f47",
        capacity=1.4,
    ),
    LabelSpec(
        key="lead",
        name="Lead",
        kind=LabelKind.CATEGORY,
        description=(
            "Runs the team as well as doing the work, so carries less of it than "
            "a senior despite being more experienced."
        ),
        color="#6b46c1",
        capacity=0.8,
    ),
    # ── status: whether any work at all ────────────────────────────────
    LabelSpec(
        key=LABEL_ON_LEAVE,
        name="On Leave",
        kind=LabelKind.STATUS,
        description=(
            "Away today on approved leave. Read live from the leave module, so it "
            "appears when the leave starts and is gone the day it ends."
        ),
        color="#8a5a00",
        capacity=0.0,
        derived=True,
    ),
    LabelSpec(
        key="training",
        name="In Training",
        kind=LabelKind.STATUS,
        description="On a course or onboarding. Assign sparingly for now.",
        color="#8a5a00",
        capacity=0.4,
    ),
    LabelSpec(
        key=LABEL_EXCLUDED,
        name="Excluded from Rotation",
        kind=LabelKind.STATUS,
        description=(
            "Takes no new work at all, for any reason — a secondment, a notice "
            "period, a temporary reassignment."
        ),
        color="#b42318",
        capacity=0.0,
    ),
    # ── skill: which work ──────────────────────────────────────────────
    # Deliberately none shipped. Competencies are specific to what a business
    # actually sells, and a guessed list would be wrong everywhere and still get
    # used because it was there.
)

BY_KEY: Final[dict[str, LabelSpec]] = {spec.key: spec for spec in LABELS}

#: Keys the assignment policy refers to by name and may not lose.
SYSTEM_KEYS: Final[frozenset[str]] = frozenset(
    {LABEL_NEW_JOINER, LABEL_ON_LEAVE, LABEL_EXCLUDED}
)

#: Seeds a fresh policy's capacity map.
DEFAULT_CAPACITY_BY_LABEL: Final[dict[str, float]] = {
    spec.key: spec.capacity for spec in LABELS if spec.capacity is not None
}

#: Seeds a fresh policy's exclusions: nobody who is away or off rotation.
DEFAULT_EXCLUDED: Final[tuple[str, ...]] = (LABEL_ON_LEAVE, LABEL_EXCLUDED)
