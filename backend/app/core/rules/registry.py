"""The decision-point registry.

Same pattern as the permission registry, for the same reason: one declaration drives the
engine, the admin UI's rule builder, and the docs, so they cannot drift.

A **decision point** is a moment where the system makes a choice. Each declares the *facts*
available in its context and the *actions* it accepts. The builder renders only what appears
here, which is what stops an admin composing a rule the engine cannot honour.

Risk #6 in the plan is that this grows into a general-purpose programming language. The
defence is structural and lives here: decision points and their fact schemas are a **fixed
registry extended in code**. Conditions and actions are declarative data — never executable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FactType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    #: A set of strings — labels, team slugs. Uses the ``contains`` family of operators.
    STRING_SET = "string_set"
    UUID = "uuid"


class Operator(StrEnum):
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    CONTAINS_ANY = "contains_any"
    CONTAINS_ALL = "contains_all"
    IS_EMPTY = "is_empty"
    IS_NOT_EMPTY = "is_not_empty"


#: Which operators make sense for which fact type. The builder uses this to populate its
#: operator dropdown, and the validator uses it to reject nonsense like ``total contains x``.
OPERATORS_BY_TYPE: dict[FactType, tuple[Operator, ...]] = {
    FactType.STRING: (
        Operator.EQ,
        Operator.NE,
        Operator.IN,
        Operator.NOT_IN,
        Operator.CONTAINS,
        Operator.NOT_CONTAINS,
        Operator.IS_EMPTY,
        Operator.IS_NOT_EMPTY,
    ),
    FactType.NUMBER: (
        Operator.EQ,
        Operator.NE,
        Operator.GT,
        Operator.GTE,
        Operator.LT,
        Operator.LTE,
        Operator.IN,
        Operator.NOT_IN,
    ),
    FactType.BOOLEAN: (Operator.EQ, Operator.NE),
    FactType.DATE: (
        Operator.EQ,
        Operator.NE,
        Operator.GT,
        Operator.GTE,
        Operator.LT,
        Operator.LTE,
    ),
    FactType.STRING_SET: (
        Operator.CONTAINS,
        Operator.NOT_CONTAINS,
        Operator.CONTAINS_ANY,
        Operator.CONTAINS_ALL,
        Operator.IS_EMPTY,
        Operator.IS_NOT_EMPTY,
    ),
    FactType.UUID: (Operator.EQ, Operator.NE, Operator.IN, Operator.NOT_IN),
}


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    type: FactType
    description: str
    #: Fixed choices, where the builder should offer a dropdown rather than free text.
    choices: tuple[str, ...] = ()

    @property
    def operators(self) -> tuple[Operator, ...]:
        return OPERATORS_BY_TYPE[self.type]


@dataclass(frozen=True, slots=True)
class ActionParam:
    key: str
    type: FactType
    description: str
    required: bool = True
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Action:
    type: str
    description: str
    params: tuple[ActionParam, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisionPoint:
    key: str
    name: str
    description: str
    #: What fires it, in words an admin recognises.
    fires_when: str
    facts: tuple[Fact, ...]
    actions: tuple[Action, ...]
    entity_type: str
    #: Permission required to edit rules here, beyond the generic ``rules.edit_*``.
    module: str = "rules"
    fact_map: dict[str, Fact] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_map", {f.key: f for f in self.facts})

    def action_types(self) -> frozenset[str]:
        return frozenset(a.type for a in self.actions)

    def get_action(self, action_type: str) -> Action | None:
        return next((a for a in self.actions if a.type == action_type), None)


# ──────────────────────────────────────────────────────────────────────────
# Shared fact groups
# ──────────────────────────────────────────────────────────────────────────

_ACTOR_FACTS: tuple[Fact, ...] = (
    Fact("actor.labels", FactType.STRING_SET, "Labels held by the acting user"),
    Fact("actor.role", FactType.STRING, "The acting user's role key in this team"),
    Fact("actor.days_since_joining", FactType.NUMBER, "Days since the user joined"),
)

_TEAM_FACTS: tuple[Fact, ...] = (
    Fact("team.slug", FactType.STRING, "Team slug"),
    Fact("team.member_count", FactType.NUMBER, "Active members in the team"),
)

_NOTIFY = Action(
    "notify",
    "Send a notification",
    (
        ActionParam(
            "target",
            FactType.STRING,
            "Who to notify",
            choices=("assignee", "team_lead", "team_manager", "org_manager", "creator"),
        ),
        ActionParam("message", FactType.STRING, "Message template", required=False),
    ),
)


# ──────────────────────────────────────────────────────────────────────────
# The registry. Mirrors docs/PROJECT_PLAN.md §5.2.
# ──────────────────────────────────────────────────────────────────────────

DECISION_POINTS: tuple[DecisionPoint, ...] = (
    DecisionPoint(
        key="proposal.assign",
        name="Proposal assignment",
        description=(
            "Chooses who a proposal goes to. The detailed capacity and ratio settings live "
            "in the dedicated assignment policy builder; rules here handle exceptions."
        ),
        fires_when="A proposal arrives, or is reassigned",
        entity_type="proposal",
        facts=(
            Fact("proposal.title", FactType.STRING, "Proposal title"),
            Fact("proposal.status", FactType.STRING, "Current status"),
            Fact("proposal.value", FactType.NUMBER, "Estimated value"),
            Fact("proposal.days_to_bcd", FactType.NUMBER, "Days until bid closing date"),
            Fact("proposal.customer", FactType.STRING, "Customer name"),
            Fact("proposal.required_labels", FactType.STRING_SET, "Skills the work needs"),
            *_TEAM_FACTS,
        ),
        actions=(
            Action(
                "assign_to_label",
                "Restrict candidates to holders of a label",
                (ActionParam("label", FactType.STRING, "Required label key"),),
            ),
            Action(
                "exclude_label",
                "Exclude holders of a label from consideration",
                (ActionParam("label", FactType.STRING, "Label key to exclude"),),
            ),
            Action(
                "set_priority",
                "Set the proposal's priority",
                (ActionParam("priority", FactType.NUMBER, "Priority score"),),
            ),
            Action(
                "require_manual_assignment",
                "Stop automatic assignment and hand it to a manager",
            ),
            _NOTIFY,
        ),
    ),
    DecisionPoint(
        key="proposal.escalate",
        name="Proposal escalation",
        description="Reacts to a proposal approaching or breaching its deadline.",
        fires_when="The bid closing date approaches, or an SLA breaches",
        entity_type="proposal",
        facts=(
            Fact("proposal.status", FactType.STRING, "Current status"),
            Fact("proposal.days_to_bcd", FactType.NUMBER, "Days until bid closing date"),
            Fact("proposal.days_since_assigned", FactType.NUMBER, "Days since assignment"),
            Fact("proposal.value", FactType.NUMBER, "Estimated value"),
            Fact("assignee.labels", FactType.STRING_SET, "Labels held by the assignee"),
            Fact("assignee.open_task_count", FactType.NUMBER, "Assignee's open proposals"),
            *_TEAM_FACTS,
        ),
        actions=(
            Action(
                "raise_priority",
                "Raise the proposal's priority",
                (ActionParam("by", FactType.NUMBER, "Amount to add"),),
            ),
            Action("reassign", "Return the proposal to the assignment engine"),
            Action("flag_to_manager", "Flag the proposal on the manager's dashboard"),
            _NOTIFY,
        ),
    ),
    DecisionPoint(
        key="quote.approval_route",
        name="Quote approval routing",
        description=(
            "Chooses the approver chain for a submitted quote. Replaces the legacy "
            "hardcoded approvers list."
        ),
        fires_when="A quote is submitted for approval",
        entity_type="quote",
        facts=(
            Fact("quote.total", FactType.NUMBER, "Quote total"),
            Fact("quote.currency", FactType.STRING, "Currency", choices=("AED", "USD", "EUR")),
            Fact("quote.margin_pct", FactType.NUMBER, "Margin percentage"),
            Fact("quote.item_count", FactType.NUMBER, "Number of line items"),
            Fact("quote.customer", FactType.STRING, "Customer name"),
            Fact("quote.has_discount", FactType.BOOLEAN, "Any line carries a discount"),
            *_ACTOR_FACTS,
            *_TEAM_FACTS,
        ),
        actions=(
            Action(
                "require_approval",
                "Require approval from a role",
                (
                    ActionParam(
                        "approver_role",
                        FactType.STRING,
                        "Role that must approve",
                        choices=("team_manager", "org_manager", "super_admin"),
                    ),
                    ActionParam("step", FactType.NUMBER, "Position in the chain", required=False),
                ),
            ),
            Action("auto_approve", "Approve without human review"),
            Action(
                "block",
                "Refuse submission",
                (ActionParam("reason", FactType.STRING, "Why it was blocked"),),
            ),
            _NOTIFY,
        ),
    ),
    DecisionPoint(
        key="quote.validate",
        name="Quote validation",
        description="Checks a quote before it may be submitted.",
        fires_when="A quote is about to be submitted",
        entity_type="quote",
        facts=(
            Fact("quote.total", FactType.NUMBER, "Quote total"),
            Fact("quote.margin_pct", FactType.NUMBER, "Margin percentage"),
            Fact("quote.item_count", FactType.NUMBER, "Number of line items"),
            Fact("quote.has_attachments", FactType.BOOLEAN, "Any file attached"),
            *_ACTOR_FACTS,
        ),
        actions=(
            Action(
                "block",
                "Refuse submission",
                (ActionParam("reason", FactType.STRING, "Message shown to the user"),),
            ),
            Action(
                "warn",
                "Allow, with a warning",
                (ActionParam("message", FactType.STRING, "Warning text"),),
            ),
            Action(
                "require_field",
                "Require a field to be filled",
                (ActionParam("field", FactType.STRING, "Field name"),),
            ),
        ),
    ),
    DecisionPoint(
        key="leave.eligibility",
        name="Leave eligibility",
        description="Decides how a leave request is handled.",
        fires_when="A member requests leave",
        entity_type="leave_request",
        facts=(
            Fact("leave.days", FactType.NUMBER, "Length in days"),
            Fact("leave.type", FactType.STRING, "Leave type"),
            Fact("leave.notice_days", FactType.NUMBER, "Days of notice given"),
            Fact("leave.concurrent_count", FactType.NUMBER, "Others already away then"),
            Fact("leave.overlaps_blackout", FactType.BOOLEAN, "Falls in a blackout period"),
            Fact("requester.open_task_count", FactType.NUMBER, "Requester's open proposals"),
            *_ACTOR_FACTS,
            *_TEAM_FACTS,
        ),
        actions=(
            Action("auto_approve", "Approve automatically"),
            Action(
                "require_approval",
                "Require approval from a role",
                (
                    ActionParam(
                        "approver_role",
                        FactType.STRING,
                        "Role that must approve",
                        choices=("team_manager", "org_manager"),
                    ),
                ),
            ),
            Action(
                "block",
                "Refuse the request",
                (ActionParam("reason", FactType.STRING, "Reason shown to the requester"),),
            ),
            Action("require_handoff", "Require ongoing proposals be handed off first"),
            _NOTIFY,
        ),
    ),
    DecisionPoint(
        key="user.label",
        name="Automatic labelling",
        description=(
            "Grants and revokes labels from user attributes — for example, "
            "joined within 90 days becomes New Joiner."
        ),
        fires_when="A user joins, or their attributes change",
        entity_type="user",
        facts=(
            Fact("user.days_since_joining", FactType.NUMBER, "Days since joining"),
            Fact("user.role", FactType.STRING, "Role key in this team"),
            Fact("user.open_task_count", FactType.NUMBER, "Open proposals"),
            Fact("user.labels", FactType.STRING_SET, "Labels currently held"),
            Fact("user.status", FactType.STRING, "Account status"),
            *_TEAM_FACTS,
        ),
        actions=(
            Action(
                "grant_label",
                "Grant a label",
                (
                    ActionParam("label", FactType.STRING, "Label key"),
                    ActionParam(
                        "expires_in_days",
                        FactType.NUMBER,
                        "Auto-expiry, so the label falls off by itself",
                        required=False,
                    ),
                ),
            ),
            Action(
                "revoke_label",
                "Revoke a label",
                (ActionParam("label", FactType.STRING, "Label key"),),
            ),
        ),
    ),
    DecisionPoint(
        key="notification.route",
        name="Notification routing",
        description="Decides who hears about an event, and how loudly.",
        fires_when="Any domain event is raised",
        entity_type="notification",
        facts=(
            Fact("event.type", FactType.STRING, "Event type"),
            Fact("event.entity_type", FactType.STRING, "Entity involved"),
            Fact("event.value", FactType.NUMBER, "Associated value, where relevant"),
            *_ACTOR_FACTS,
            *_TEAM_FACTS,
        ),
        actions=(
            _NOTIFY,
            Action("suppress", "Send nothing"),
            Action(
                "set_urgency",
                "Set urgency",
                (
                    ActionParam(
                        "urgency",
                        FactType.STRING,
                        "Urgency level",
                        choices=("low", "normal", "high", "critical"),
                    ),
                ),
            ),
        ),
    ),
    DecisionPoint(
        key="visibility.field",
        name="Field visibility",
        description="Masks or hides fields for particular audiences.",
        fires_when="A record is rendered",
        entity_type="field",
        facts=(
            Fact("record.type", FactType.STRING, "Record type"),
            Fact("field.name", FactType.STRING, "Field name"),
            *_ACTOR_FACTS,
            *_TEAM_FACTS,
        ),
        actions=(
            Action("hide", "Hide the field entirely"),
            Action("mask", "Show the field masked"),
            Action("read_only", "Show the field but prevent editing"),
        ),
    ),
)

DECISION_POINTS_BY_KEY: dict[str, DecisionPoint] = {d.key: d for d in DECISION_POINTS}

if len(DECISION_POINTS_BY_KEY) != len(DECISION_POINTS):  # pragma: no cover
    raise RuntimeError("duplicate decision point key")


class UnknownDecisionPointError(KeyError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        known = ", ".join(sorted(DECISION_POINTS_BY_KEY))
        return f"unknown decision point {self.key!r}. Known: {known}"


def get_decision_point(key: str) -> DecisionPoint:
    try:
        return DECISION_POINTS_BY_KEY[key]
    except KeyError:
        raise UnknownDecisionPointError(key) from None
