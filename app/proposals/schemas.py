"""Response shapes for proposal tasks."""

from __future__ import annotations

import uuid

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
    #: Deep link into SharePoint, where the work actually happens — the
    #: display form, not Graph's internal item identifier. See ProposalTask.
    web_url: str | None
    #: Whether the item has files at all.
    has_attachments: bool = False
    #: Straight to the attachment folder, or null when there is nothing there.
    #: Opening it uses the viewer's own SharePoint access, not ours.
    attachments_url: str | None = None
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
            has_attachments=task.has_attachments,
            attachments_url=task.attachments_url,
            is_open=task.is_open,
            deadline=task.deadline,
        )


class TaskAttachmentOut(BaseModel):
    """One file on a task. The download URL is ours, not SharePoint's, so the
    caller's ERP session is what authorises the fetch."""

    file_name: str
    download_url: str


class TaskUpdateIn(BaseModel):
    """A patch. Anything left out is untouched.

    Deliberately not every column. ``AssignedTo`` is reassignment — a workflow
    decision with its own rules, not a field edit — and ``Attachments`` and
    ``ContentType`` are SharePoint's own. Dates are passed as SharePoint stores
    them (ISO 8601, e.g. ``2026-09-30T00:00:00Z``).
    """

    title: str | None = None
    status: str | None = None
    priority: str | None = None
    start_date: str | None = None
    due_date: str | None = None
    bid_closing_date: str | None = None
    end_user: str | None = None
    submission_status: str | None = None
    current_type: str | None = None
    order_status: str | None = None
    negotiation: str | None = None
    negotiation_notes: str | None = None
    quote_no: str | None = None
    remarks: str | None = None
    working_notes: str | None = None

    #: Our field names -> the list's internal column names.
    _COLUMNS = {
        "title": "Title",
        "status": "Status",
        "priority": "Priority",
        "start_date": "StartDate",
        "due_date": "DueDate",
        "bid_closing_date": "BCD",
        "end_user": "EndUser",
        "submission_status": "SubmissionStatus",
        "current_type": "CurrentType",
        "order_status": "OrderStatus",
        "negotiation": "Negotiation",
        "negotiation_notes": "NegotiationNotes",
        "quote_no": "zohpquoteno",
        "remarks": "Remarks",
        "working_notes": "WorkingNotes",
    }

    def as_sharepoint_fields(self) -> dict[str, str]:
        """Only what was actually sent, under SharePoint's own column names."""
        sent = self.model_dump(exclude_unset=True, exclude_none=True)
        return {self._COLUMNS[k]: v for k, v in sent.items() if k in self._COLUMNS}


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


class MemberTasksOut(BaseModel):
    """One team member's proposal rows, as their lead sees them.

    The identity is the ERP user, not the SharePoint row's display name: the
    join went the other way — this team's members were looked up in SharePoint,
    rather than SharePoint rows being attributed to whoever they name. That is
    what makes the set of people here exactly the team, and nobody else.
    """

    user_id: uuid.UUID
    name: str
    email: str
    #: Their roles *inside this team*, so a lead can be told apart from a member.
    role_keys: list[str]
    sharepoint_user_id: str | None
    #: False when they have no presence on the SharePoint site at all. Different
    #: from having nothing assigned, and it needs saying differently: one is
    #: "they are clear", the other is "nothing could ever reach them here".
    in_sharepoint: bool
    #: Everything assigned to them, before the open_only filter.
    total: int
    #: Not finished, whatever the deadline — includes bids that closed long ago.
    open_count: int
    #: Not finished *and* the bid is still open. The number to lead with; see
    #: ProposalTask.is_active for why `open_count` is mostly an archive.
    active_count: int
    #: Live rows whose deadline falls inside the `soon_days` horizon.
    due_soon_count: int
    #: The nearest deadline still ahead of them, or None.
    next_deadline: str | None
    tasks: list[TaskOut]


class TeamTasksOut(BaseModel):
    """A whole team's proposal work, member by member, with the rows attached."""

    scope: ScopeOut
    #: The "due soon" horizon, in days.
    soon_days: int
    generated_at: str
    member_count: int
    #: Totals across the team, summed from the members below rather than
    #: computed separately, so the header and the list cannot disagree.
    total: int
    open_count: int
    active_count: int
    #: Busiest first, on live work — see oversight.team_tasks for the ordering.
    members: list[MemberTasksOut]
    #: True when served from the per-team cache rather than re-swept.
    cached: bool = False
    age_seconds: int = 0
