"""Response shapes for proposal tasks."""

from __future__ import annotations

from pydantic import BaseModel

from app.proposals.sharepoint import ProposalTask


class TaskOut(BaseModel):
    id: str
    title: str
    status: str | None
    priority: str | None
    #: Display name from SharePoint. Shown to humans; never used to authorise.
    assigned_to_name: str | None
    start_date: str | None
    due_date: str | None
    bid_closing_date: str | None
    end_user: str | None
    submission_status: str | None
    current_type: str | None
    order_status: str | None
    negotiation: str | None
    quote_no: str | None
    remarks: str | None
    working_notes: str | None
    created_at: str | None
    modified_at: str | None
    #: Deep link into SharePoint, where the work actually happens.
    web_url: str | None
    is_open: bool
    #: The date that actually matters — BCD, falling back to DueDate.
    deadline: str | None

    @classmethod
    def from_domain(cls, task: ProposalTask) -> TaskOut:
        return cls(
            id=task.id,
            title=task.title,
            status=task.status,
            priority=task.priority,
            assigned_to_name=task.assigned_to_name,
            start_date=task.start_date,
            due_date=task.due_date,
            bid_closing_date=task.bid_closing_date,
            end_user=task.end_user,
            submission_status=task.submission_status,
            current_type=task.current_type,
            order_status=task.order_status,
            negotiation=task.negotiation,
            quote_no=task.quote_no,
            remarks=task.remarks,
            working_notes=task.working_notes,
            created_at=task.created_at,
            modified_at=task.modified_at,
            web_url=task.web_url,
            is_open=task.is_open,
            deadline=task.deadline,
        )


class MyTasksOut(BaseModel):
    email: str
    #: The caller's id within the SharePoint site, for support and debugging.
    sharepoint_user_id: str | None
    #: False when they have no presence on that site at all.
    in_sharepoint: bool
    #: Everything assigned to them, before the open_only filter.
    total: int
    open_count: int
    tasks: list[TaskOut]


class ColumnOut(BaseModel):
    name: str | None
    display_name: str | None
    choices: list[str] | None = None


class PersonWorkloadOut(BaseModel):
    #: None for rows assigned to nobody.
    lookup_id: str | None
    name: str
    email: str | None
    total: int
    completed: int
    open: int
    #: Bid closing date already past.
    overdue: int
    due_soon: int
    later: int
    no_deadline: int
    #: Counted in `open` as well; surfaced so the backlog is not overstated silently.
    no_status: int
    by_status: dict[str, int]
    next_deadline: str | None


class ExcludedOut(BaseModel):
    """What a team scope left out. Never silently dropped."""

    rows: int
    people: int
    names: list[str]


class ScopeOut(BaseModel):
    team_slug: str
    team_name: str
    member_count: int
    #: Members found in the SharePoint site user list.
    matched_in_sharepoint: int
    #: Members with no SharePoint presence — the usual reason a count looks low.
    members_without_sharepoint: list[str]


class WorkloadOut(BaseModel):
    #: Totals across everyone, computed in the same pass.
    organisation: PersonWorkloadOut
    people: list[PersonWorkloadOut]
    person_count: int
    #: The "due soon" horizon, in days.
    soon_days: int
    generated_at: str
    #: Rows swept to produce this.
    row_count: int | None = None
    #: Present when the numbers were scoped to one team's members.
    scope: ScopeOut | None = None
    #: Rows and people the scope excluded.
    excluded: ExcludedOut | None = None
    #: True when served from the shared cache rather than recomputed.
    cached: bool = False
    age_seconds: int = 0
    elapsed_ms: int | None = None
    #: Time spent fetching from SharePoint; 0 when the sweep was cached.
    fetch_ms: int | None = None
