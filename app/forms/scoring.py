"""Turning filled-in answers into a score, per tag.

A template field may carry a ``scoring`` block. That is what makes a form an
*assessment* rather than a questionnaire: the same template machinery that
records a candidate's answers can also say how good those answers were, without
a second form system for the scored ones.

    {
      "key": "communication",
      "type": "select",
      "options": ["Poor", "Adequate", "Strong"],
      "scoring": {
        "tags": ["communication", "client_facing"],
        "max": 5,
        "weight": 1.0,
        "option_scores": {"Poor": 1, "Adequate": 3, "Strong": 5}
      }
    }

**Tags are the point, not the total.** One overall number tells you somebody
scored 71%; the tags tell you they are strong technically and weak on delivery,
which is the thing anyone actually acts on. A field feeds every tag it lists, at
its full weight — a question about explaining a design to a client genuinely is
evidence about both communication and client-facing work, and splitting the
credit between them would understate both.

**An unanswered field is not a zero.** It is left out of the total *and* out of
the maximum, so an optional question nobody filled in cannot drag a score down.
The alternative punishes people for questions the reviewer skipped, which makes
scores from different reviewers incomparable — and comparing them is the only
reason to compute a number at all.

Only four field types can be scored: ``select`` (by option), ``number`` and
``percent`` (the value itself), and ``checkbox`` (all or nothing). Attaching
scoring to free text would mean scoring prose, which is a judgement, not a sum.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Final

from app.models.templates import FieldType

#: The field types a ``scoring`` block may be attached to.
SCORABLE: Final[frozenset[str]] = frozenset(
    {
        FieldType.SELECT.value,
        FieldType.NUMBER.value,
        FieldType.PERCENT.value,
        FieldType.CHECKBOX.value,
    }
)

#: Used when a scoring block names no ceiling of its own.
DEFAULT_MAX: Final[float] = 5.0


class ScoringError(ValueError):
    """A scoring block is not usable. Safe to show whoever wrote the template."""


# ── the definition side ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Rule:
    """One field's scoring block, after validation."""

    key: str
    type: str
    tags: tuple[str, ...]
    max: float
    weight: float
    option_scores: dict[str, float]

    @property
    def ceiling(self) -> float:
        """What this field contributes to the maximum when it is answered."""
        return self.max * self.weight


def validate_block(field_key: str, field_type: str, block: Any) -> dict[str, Any]:
    """Check one ``scoring`` block and return it normalised.

    Raised errors are shown to the super admin editing the template, so they
    name the field and say what is wrong with it rather than where it failed.
    """
    if not isinstance(block, dict):
        raise ScoringError(f"Field {field_key!r}: scoring must be an object")
    if field_type not in SCORABLE:
        raise ScoringError(
            f"Field {field_key!r} is a {field_type} and cannot be scored. "
            f"Scoring works on {', '.join(sorted(SCORABLE))}."
        )

    raw_tags = block.get("tags") or []
    if not isinstance(raw_tags, list) or not raw_tags:
        raise ScoringError(
            f"Field {field_key!r}: scoring needs at least one tag, or the score "
            f"it produces belongs to nothing."
        )
    tags: list[str] = []
    for tag in raw_tags:
        slug = str(tag).strip().casefold().replace(" ", "_")
        if not slug:
            raise ScoringError(f"Field {field_key!r}: a tag is blank")
        if slug not in tags:
            tags.append(slug)

    try:
        ceiling = float(block.get("max", DEFAULT_MAX))
        weight = float(block.get("weight", 1.0))
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"Field {field_key!r}: max and weight must be numbers") from exc
    if ceiling <= 0:
        raise ScoringError(f"Field {field_key!r}: max must be greater than zero")
    if weight <= 0:
        raise ScoringError(f"Field {field_key!r}: weight must be greater than zero")

    options: dict[str, float] = {}
    raw_options = block.get("option_scores") or {}
    if raw_options:
        if not isinstance(raw_options, dict):
            raise ScoringError(f"Field {field_key!r}: option_scores must be an object")
        for option, points in raw_options.items():
            try:
                options[str(option)] = float(points)
            except (TypeError, ValueError) as exc:
                raise ScoringError(
                    f"Field {field_key!r}: option {option!r} has a non-numeric score"
                ) from exc
    elif field_type == FieldType.SELECT.value:
        raise ScoringError(
            f"Field {field_key!r} is a select, so scoring needs option_scores — "
            f"otherwise there is no way to know what an answer is worth."
        )

    normalised: dict[str, Any] = {"tags": tags, "max": ceiling, "weight": weight}
    if options:
        normalised["option_scores"] = options
    return normalised


def rules_for(fields: list[dict[str, Any]]) -> list[Rule]:
    """Every scoring rule on a template, in field order.

    Blocks that do not validate are skipped rather than raised on: a template
    saved before a validation rule tightened must still produce a score for the
    records already made from it.
    """
    rules: list[Rule] = []
    for spec in fields:
        if not isinstance(spec, dict) or not spec.get("scoring"):
            continue
        key, kind = str(spec.get("key") or ""), str(spec.get("type") or "")
        try:
            block = validate_block(key, kind, spec["scoring"])
        except ScoringError:
            continue
        rules.append(
            Rule(
                key=key,
                type=kind,
                tags=tuple(block["tags"]),
                max=block["max"],
                weight=block["weight"],
                option_scores=block.get("option_scores", {}),
            )
        )
    return rules


def tags_of(fields: list[dict[str, Any]]) -> list[str]:
    """Every tag a template can score against, in first-appearance order."""
    seen: list[str] = []
    for rule in rules_for(fields):
        for tag in rule.tags:
            if tag not in seen:
                seen.append(tag)
    return seen


def is_scored(fields: list[dict[str, Any]]) -> bool:
    return bool(rules_for(fields))


# ── the answer side ────────────────────────────────────────────────────


def _points(rule: Rule, answer: Any) -> float | None:
    """What one answer is worth, or None if it was not answered."""
    if answer is None or (isinstance(answer, str) and not answer.strip()):
        return None

    if rule.type == FieldType.CHECKBOX.value:
        # An explicit false is an answer — "no" is information — and it is worth
        # nothing, which is different from having been skipped.
        return rule.max if answer is True else 0.0

    if rule.type == FieldType.SELECT.value:
        chosen = str(answer)
        if chosen not in rule.option_scores:
            # An answer the template no longer offers. Counting it as zero would
            # invent a bad score out of an edited template.
            return None
        return rule.option_scores[chosen]

    try:
        value = float(answer)
    except (TypeError, ValueError):
        return None
    # Clamped: a reviewer typing 50 into a field scored out of 5 should not make
    # the percentage nonsense.
    return min(max(value, 0.0), rule.max)


@dataclass(slots=True)
class TagScore:
    tag: str
    points: float = 0.0
    max: float = 0.0
    #: How many fields fed this tag. Shown so a 100% off one question is not
    #: mistaken for a 100% off twelve.
    answered: int = 0

    @property
    def percent(self) -> float | None:
        return round(100 * self.points / self.max, 1) if self.max else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "points": round(self.points, 2),
            "max": round(self.max, 2),
            "answered": self.answered,
            "percent": self.percent,
        }


@dataclass(slots=True)
class Score:
    """What a set of answers came to."""

    points: float = 0.0
    max: float = 0.0
    answered: int = 0
    #: How many scorable fields were left blank, so a near-empty review is
    #: visibly near-empty rather than just a high percentage.
    skipped: int = 0
    tags: dict[str, TagScore] = dc_field(default_factory=dict)

    @property
    def percent(self) -> float | None:
        """None when nothing scorable was answered — not zero.

        Zero would read as "scored badly"; the truth is "not scored".
        """
        return round(100 * self.points / self.max, 1) if self.max else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "points": round(self.points, 2),
            "max": round(self.max, 2),
            "percent": self.percent,
            "answered": self.answered,
            "skipped": self.skipped,
            "tags": [self.tags[t].as_dict() for t in self.tags],
        }


def score(fields: list[dict[str, Any]], answers: dict[str, Any]) -> Score:
    """Score ``answers`` against a template's fields.

    The result is stored on the record alongside the answers, so a score stays
    what it was when it was given even if the template is edited afterwards.
    """
    result = Score()
    for rule in rules_for(fields):
        earned = _points(rule, answers.get(rule.key))
        if earned is None:
            result.skipped += 1
            continue

        result.answered += 1
        result.points += earned * rule.weight
        result.max += rule.ceiling
        for tag in rule.tags:
            bucket = result.tags.setdefault(tag, TagScore(tag=tag))
            bucket.points += earned * rule.weight
            bucket.max += rule.ceiling
            bucket.answered += 1
    return result


def combine(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll several stored scores into one, for an employee over a cycle.

    Takes the ``as_dict`` form back, because that is what is on the rows. Two
    reviewers who answered different numbers of questions are combined by
    points and maximums rather than by averaging their percentages — averaging
    percentages would give a reviewer who answered two questions the same say
    as one who answered twenty.
    """
    total = Score()
    for stored in scores:
        if not isinstance(stored, dict):
            continue
        total.points += float(stored.get("points") or 0)
        total.max += float(stored.get("max") or 0)
        total.answered += int(stored.get("answered") or 0)
        total.skipped += int(stored.get("skipped") or 0)
        for entry in stored.get("tags") or []:
            tag = str(entry.get("tag") or "")
            if not tag:
                continue
            bucket = total.tags.setdefault(tag, TagScore(tag=tag))
            bucket.points += float(entry.get("points") or 0)
            bucket.max += float(entry.get("max") or 0)
            bucket.answered += int(entry.get("answered") or 0)
    return total.as_dict()
