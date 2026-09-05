"""Request and response shapes for HR.

Two families of response, kept rigorously apart:

* the ``*Out`` models, which HR and employees see, and
* the ``Public*`` models in the second half, which candidates see.

The separation is the security boundary made visible. A public model is not an
``Out`` model with fields hidden — it is a different class with nothing in it
that a candidate has no business knowing, so adding a column to an opening
cannot leak it by default.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.hr import (
    ApplicationStage,
    CycleStatus,
    DocumentKind,
    EmploymentType,
    OpeningStatus,
    ReviewerRelation,
    ReviewStatus,
)

# ── openings ───────────────────────────────────────────────────────────


class OpeningIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    #: Leave unset to use the canonical job application form.
    template_id: uuid.UUID | None = None
    #: The posting form HR is filling in. Unset uses the canonical one.
    posting_template_id: uuid.UUID | None = None
    #: The answers to it — the advert itself. Required fields are enforced when
    #: the opening is posted, not while it is a draft.
    details: dict[str, Any] | None = None
    reference: str | None = Field(default=None, max_length=64)
    team_id: uuid.UUID | None = None
    department: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=160)
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    headcount: int = Field(default=1, ge=1, le=999)
    salary_range: str | None = Field(default=None, max_length=120)
    summary: str | None = None
    description: str | None = None
    requirements: str | None = None
    closes_on: date | None = None
    #: Off by default — see the column comment on ``JobOpening``.
    publicly_listed: bool = False
    hosted_form: bool = True


class OpeningUpdateIn(BaseModel):
    """A patch. Anything left out is untouched."""

    title: str | None = Field(default=None, max_length=200)
    template_id: uuid.UUID | None = None
    posting_template_id: uuid.UUID | None = None
    details: dict[str, Any] | None = None
    reference: str | None = Field(default=None, max_length=64)
    team_id: uuid.UUID | None = None
    department: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=160)
    employment_type: EmploymentType | None = None
    headcount: int | None = Field(default=None, ge=1, le=999)
    salary_range: str | None = Field(default=None, max_length=120)
    summary: str | None = None
    description: str | None = None
    requirements: str | None = None
    closes_on: date | None = None
    publicly_listed: bool | None = None
    hosted_form: bool | None = None


class OpeningOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    reference: str | None
    team_id: uuid.UUID | None
    team_name: str | None = None
    department: str | None
    location: str | None
    employment_type: EmploymentType
    headcount: int
    salary_range: str | None
    summary: str | None
    description: str | None
    requirements: str | None

    template_id: uuid.UUID
    template_name: str | None = None
    template_version: int

    #: The posting form and what HR put in it.
    posting_template_id: uuid.UUID | None = None
    posting_template_name: str | None = None
    posting_template_version: int = 1
    details: dict[str, Any] = Field(default_factory=dict)
    #: The posting form's own fields, so the HR screen renders the form it is
    #: editing without a second request for it.
    posting_fields: list[dict[str, Any]] = Field(default_factory=list)
    posting_sections: list[dict[str, Any]] = Field(default_factory=list)

    status: OpeningStatus
    publicly_listed: bool
    hosted_form: bool
    accepts_applications: bool = False
    posted_at: datetime | None
    closes_on: date | None
    closed_at: datetime | None
    created_by_name: str | None = None
    created_at: datetime

    #: The link HR copies. Present only while the opening is posted — a link to
    #: a draft goes nowhere, and showing one invites it being sent anyway.
    share_url: str | None = None
    #: The same token as JSON, for an organisation with its own careers site.
    share_api_url: str | None = None

    application_count: int = 0
    new_application_count: int = 0


class OpeningSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    status: OpeningStatus
    department: str | None
    location: str | None
    employment_type: EmploymentType
    publicly_listed: bool
    posted_at: datetime | None
    closes_on: date | None
    application_count: int = 0
    new_application_count: int = 0


# ── applications ───────────────────────────────────────────────────────


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    field_key: str | None
    file_name: str
    content_type: str | None
    size_bytes: int


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opening_id: uuid.UUID
    opening_title: str | None = None
    candidate_name: str
    candidate_email: str
    candidate_phone: str | None
    answers: dict[str, Any]
    template_version: int
    #: ``app.forms.scoring.Score.as_dict()`` — points, maximum, and the per-tag
    #: breakdown. Empty when the form scores nothing.
    score: dict[str, Any]
    score_percent: Decimal | None
    stage: ApplicationStage
    stage_note: str | None
    internal_notes: str | None
    submitted_at: datetime
    decided_at: datetime | None
    decided_by_name: str | None = None
    hired_user_id: uuid.UUID | None
    attachments: list[AttachmentOut] = Field(default_factory=list)


class StageIn(BaseModel):
    stage: ApplicationStage
    note: str | None = None


class NotesIn(BaseModel):
    internal_notes: str | None = None


class RemovedOut(BaseModel):
    """What a deletion destroyed.

    Returned rather than a bare 204 so a super admin sees the blast radius —
    deleting one opening can take forty applications and their CVs with it, and
    an empty response cannot say so.
    """

    openings: int = 0
    applications: int = 0
    files: int = 0
    documents: int = 0
    cycles: int = 0
    reviews: int = 0
    #: A sentence naming what went, for a confirmation the user actually reads.
    summary: str = ""


class HireIn(BaseModel):
    #: The employee record the candidate became. They appear here after their
    #: Microsoft account signs in for the first time.
    user_id: uuid.UUID
    #: Also mark the opening filled.
    close_opening: bool = False


# ── employee documents ─────────────────────────────────────────────────


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    kind: DocumentKind
    title: str
    note: str | None
    file_name: str
    content_type: str | None
    size_bytes: int
    issued_on: date | None
    expires_on: date | None
    #: True once ``expires_on`` is in the past. Computed, not stored: a stored
    #: flag would be wrong every morning until something rewrote it.
    expired: bool = False
    visible_to_employee: bool
    uploaded_by_name: str | None = None
    created_at: datetime


class DocumentUpdateIn(BaseModel):
    kind: DocumentKind | None = None
    title: str | None = Field(default=None, max_length=200)
    note: str | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    visible_to_employee: bool | None = None


# ── review cycles ──────────────────────────────────────────────────────


class CycleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Leave unset to use the newest active performance review form.
    template_id: uuid.UUID | None = None
    description: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    due_on: date | None = None
    shared_with_subjects: bool = False


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    template_id: uuid.UUID
    template_name: str | None = None
    template_version: int
    period_start: date | None
    period_end: date | None
    due_on: date | None
    status: CycleStatus
    shared_with_subjects: bool
    opened_at: datetime | None
    closed_at: datetime | None
    created_by_name: str | None = None
    created_at: datetime

    nominated: int = 0
    submitted: int = 0
    #: Everyone being reviewed in this cycle, so HR can see coverage without
    #: pulling every review.
    subjects: int = 0


class NominateIn(BaseModel):
    subject_id: uuid.UUID
    reviewer_id: uuid.UUID
    #: How the reviewer knows them. Recorded for context; never a permission.
    #: Forced to ``self`` when the two ids match.
    relation: ReviewerRelation = ReviewerRelation.OTHER
    due_on: date | None = None


class BulkNominateIn(BaseModel):
    """Nominate several reviewers at once — the usual way a cycle is set up."""

    nominations: list[NominateIn] = Field(min_length=1, max_length=500)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    cycle_name: str | None = None
    subject_id: uuid.UUID
    subject_name: str | None = None
    reviewer_id: uuid.UUID
    reviewer_name: str | None = None
    relation: ReviewerRelation
    status: ReviewStatus
    #: Omitted unless the caller may read the content — a subject can see that
    #: a review of them exists long before they may read what it says.
    answers: dict[str, Any] | None = None
    score: dict[str, Any] | None = None
    score_percent: Decimal | None = None
    comment: str | None = None
    declined_reason: str | None = None
    due_on: date | None
    submitted_at: datetime | None
    #: What the reviewer is being asked to fill in. Sent with their own review
    #: so the form and the answers arrive together.
    fields: list[dict[str, Any]] | None = None
    sections: list[dict[str, Any]] | None = None


class ReviewAnswersIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    comment: str | None = None
    #: False saves a draft; true submits and freezes the score.
    submit: bool = False


class DeclineIn(BaseModel):
    reason: str | None = None


class PerformanceOut(BaseModel):
    """Somebody's combined score across every submitted review of them."""

    user_id: uuid.UUID
    user_name: str | None = None
    reviews: int
    self_reviews: int
    cycles: list[str]
    points: float
    max: float
    percent: float | None
    answered: int
    skipped: int
    tags: list[dict[str, Any]]


# ── the public side ────────────────────────────────────────────────────
#
# Everything below is served without authentication, at a path outside the API
# prefix. Nothing here carries an internal id, a colleague's name, a template
# id, a headcount, or a score. A candidate learns the job and the questions.


class PublicFieldOut(BaseModel):
    """One question, stripped to what a form needs to render it.

    Note what is absent: ``scoring``. Telling a candidate which answer is worth
    five points would turn the form into a multiple-choice exam with the answer
    key attached.
    """

    key: str
    label: str
    type: str
    section: str | None = None
    required: bool = False
    help: str | None = None
    options: list[str] | None = None
    default: Any = None


class PublicSectionOut(BaseModel):
    key: str
    name: str
    help: str | None = None


class PublicDetailOut(BaseModel):
    """One line of the advert, as the posting form labelled it."""

    key: str
    label: str
    value: Any


class PublicOpeningOut(BaseModel):
    """What a candidate is told about the role."""

    title: str
    department: str | None = None
    location: str | None = None
    employment_type: str
    salary_range: str | None = None
    summary: str | None = None
    description: str | None = None
    requirements: str | None = None
    closes_on: date | None = None
    #: The rest of the advert, in the posting form's own order, with the fields
    #: that form marks ``internal`` removed and the four mirrored onto the
    #: fields above left out so nothing is said twice.
    details: list[PublicDetailOut] = Field(default_factory=list)
    #: False when the opening has closed. The page then says so rather than
    #: presenting a form that will be refused.
    accepting: bool = True
    closed_message: str | None = None
    sections: list[PublicSectionOut] = Field(default_factory=list)
    fields: list[PublicFieldOut] = Field(default_factory=list)
    #: Where to POST. Given explicitly so a careers site never has to build it.
    submit_url: str


class PublicListingOut(BaseModel):
    """One row of the public careers list."""

    title: str
    department: str | None = None
    location: str | None = None
    employment_type: str
    summary: str | None = None
    closes_on: date | None = None
    #: The application link for this opening. The token, not the slug.
    apply_url: str


class PublicSubmitOut(BaseModel):
    """All a candidate is told back. Deliberately almost nothing.

    No application id, no score, no position in a queue. An id would be a handle
    on a record the candidate cannot be allowed to read, and a score would tell
    them how the form is marked.
    """

    received: bool = True
    message: str
    candidate_email: str
