"""HR: hiring, employee documents, and performance reviews.

Three things that look separate and are not. All three are a *form template*
plus the record somebody filled in from it — which is why none of them define
their own field lists. A super admin writes the form in the templates section;
HR chooses which template an opening or a review cycle uses; the answers land
here as JSONB against the template version they were given against.

**Scores are stored, not derived on read.** ``app.forms.scoring`` turns answers
into per-tag points at the moment of submission, and the result is written to
the row. A template edited next month must not silently restate what somebody
scored last month — a performance record that changes when a form is reworded
is not a record.

**Candidates are not users.** A job application carries the candidate's own
contact details and nothing else; there is no account, no session, and no row
in ``users`` until somebody is actually hired. The public endpoints that write
these rows are mounted outside the API prefix and hold no authentication at all
— see ``app.hr.public`` for why that separation is the whole point.

Documents are bytes in Postgres, following the supplier quotes in
``app.models.comparison``: offer letters are small, they are read rarely, and
one storage story is easier to back up and to reason about than two.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.templates import FormTemplate
from app.models.user import User

# ── hiring ─────────────────────────────────────────────────────────────


class OpeningStatus(StrEnum):
    #: Being written. No share link works, because there is nothing to apply to.
    DRAFT = "draft"
    #: Posted. The share link accepts applications.
    OPEN = "open"
    #: Deliberately stopped. The link stops accepting and says so, rather than
    #: 404ing — a candidate who was sent it deserves to know it closed.
    CLOSED = "closed"
    #: Closed because somebody was hired.
    FILLED = "filled"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class JobOpening(Base, UUIDPrimaryKey, Timestamped):
    """A role being recruited for, and the form its candidates fill in."""

    __tablename__ = "job_openings"
    __table_args__ = (
        Index("ix_job_openings_status_posted", "status", "posted_at"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Appears in the public link, so it is readable rather than a bare id.
    #: Unique, because two openings sharing a slug make one of the links wrong.
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    #: HR's own reference, e.g. a requisition number. Never shown publicly.
    reference: Mapped[str | None] = mapped_column(String(64))

    #: The team the hire joins, where there is one. Nullable because a company
    #: hires for roles that do not belong to a team yet.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    department: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str | None] = mapped_column(String(160))
    employment_type: Mapped[EmploymentType] = mapped_column(
        String(20), default=EmploymentType.FULL_TIME, nullable=False
    )
    #: How many people are being hired into this opening.
    headcount: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    #: Free text on purpose. "Competitive", "AED 8–10k", and "DOE" are all
    #: things HR writes, and none of them are a number.
    salary_range: Mapped[str | None] = mapped_column(String(120))

    #: Shown to candidates on the public page.
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)

    #: The posting form — what HR filled in to describe this job. Optional:
    #: an opening can be written straight onto the columns below without one,
    #: which is what happens before an organisation has set a posting form up.
    posting_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="RESTRICT")
    )
    posting_template_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    #: Everything the posting form asked for, keyed by field. Fields the
    #: template marks ``internal`` live here too and are never published — see
    #: ``app.hr.public`` for where that is enforced.
    #:
    #: Four of these keys are also mirrored onto the columns above, exactly as
    #: a candidate's contact details are mirrored off their answers: the list,
    #: the careers page and any future search need real columns, and reaching
    #: into a JSONB blob for the summary under whichever key this version of
    #: the template used is how that breaks on the next edit.
    details: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    #: The application form. A super admin writes it; HR only chooses it.
    #: RESTRICT rather than CASCADE: deleting a template that applications were
    #: submitted against would leave those answers unreadable.
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False
    )
    #: The version in force when the opening was posted, recorded so an
    #: application can be read against the form the candidate actually saw.
    template_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    status: Mapped[OpeningStatus] = mapped_column(
        String(16), default=OpeningStatus.DRAFT, nullable=False, index=True
    )
    #: The whole of the public link's security. Long, random, and rotatable —
    #: rotating it is how HR revokes a link that went further than intended.
    public_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    #: Whether this appears on the public careers list. Off by default: a link
    #: HR shares deliberately is not the same as a job advertised to the world,
    #: and confusing the two is how a confidential replacement hire leaks.
    publicly_listed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Whether the standalone HTML form is served. Off means the token answers
    #: JSON only, for an organisation that has built its own careers site.
    hosted_form: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closes_on: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    template: Mapped[FormTemplate] = relationship(
        foreign_keys=[template_id], lazy="joined"
    )
    posting_template: Mapped[FormTemplate | None] = relationship(
        foreign_keys=[posting_template_id], lazy="joined"
    )
    team: Mapped[Team | None] = relationship(lazy="joined")
    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    applications: Mapped[list[JobApplication]] = relationship(
        back_populates="opening", cascade="all, delete-orphan"
    )

    @property
    def accepts_applications(self) -> bool:
        """Whether the share link should take a submission right now.

        ``closes_on`` is inclusive: a closing date of the 30th means the 30th
        is still a day somebody can apply on.
        """
        if self.status != OpeningStatus.OPEN:
            return False
        return self.closes_on is None or self.closes_on >= date.today()

    def __repr__(self) -> str:
        return f"<JobOpening {self.slug} {self.status}>"


class ApplicationStage(StrEnum):
    """Where a candidate is. HR moves them; nothing moves them automatically."""

    NEW = "new"
    SHORTLISTED = "shortlisted"
    INTERVIEWED = "interviewed"
    OFFERED = "offered"
    HIRED = "hired"
    REJECTED = "rejected"
    #: They pulled out. Kept apart from rejected: the distinction matters when
    #: the same person applies again.
    WITHDRAWN = "withdrawn"


#: Stages that mean the candidate is no longer being considered.
CLOSED_STAGES = frozenset({ApplicationStage.HIRED, ApplicationStage.REJECTED,
                           ApplicationStage.WITHDRAWN})


class JobApplication(Base, UUIDPrimaryKey, Timestamped):
    """One candidate's answers to one opening's form.

    The three contact columns are duplicated out of ``answers`` deliberately.
    They are what every list, search and mail-merge needs, and reaching into a
    JSONB blob for the applicant's email — under whichever key this version of
    the template happened to use — is how that breaks the next time somebody
    edits the form.
    """

    __tablename__ = "job_applications"
    __table_args__ = (
        Index("ix_job_applications_opening_stage", "opening_id", "stage"),
        # One application per person per opening. A candidate who resubmits is
        # updating what they sent, not queueing behind themselves.
        UniqueConstraint("opening_id", "candidate_email", name="uq_application_opening_email"),
    )

    opening_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_openings.id", ondelete="CASCADE"), nullable=False
    )

    candidate_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Lowercased on the way in, so the uniqueness constraint means what it
    #: looks like it means.
    candidate_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    candidate_phone: Mapped[str | None] = mapped_column(String(40))

    #: Everything the template asked for, keyed by field.
    answers: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: The template version these answers were given against, so they can still
    #: be rendered after the form is edited.
    template_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    #: ``app.forms.scoring.Score.as_dict()`` — the per-tag breakdown, frozen at
    #: submission. Empty for a form that scores nothing.
    score: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: Lifted out of ``score`` so the list can sort on it in SQL. Null means the
    #: form was not scored — which is not the same as scoring zero.
    score_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))

    stage: Mapped[ApplicationStage] = mapped_column(
        String(16), default=ApplicationStage.NEW, nullable=False, index=True
    )
    stage_note: Mapped[str | None] = mapped_column(Text)
    #: HR's own notes. Never returned by any public endpoint.
    internal_notes: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Set when a hire becomes an employee here, linking the application to the
    #: person it produced.
    hired_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    opening: Mapped[JobOpening] = relationship(back_populates="applications")
    decided_by: Mapped[User | None] = relationship(
        foreign_keys=[decided_by_id], lazy="joined"
    )
    attachments: Mapped[list[ApplicationFile]] = relationship(
        back_populates="application", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<JobApplication {self.candidate_email} {self.stage}>"


class ApplicationFile(Base, UUIDPrimaryKey, Timestamped):
    """A CV or covering letter, as the candidate uploaded it.

    Separate from ``EmployeeDocument`` rather than one polymorphic table: a
    candidate is not an employee, and a foreign key that sometimes points at a
    user and sometimes does not is the kind of thing that ends with somebody's
    CV attached to the wrong person.
    """

    __tablename__ = "application_files"

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: Which template field the upload answers, so several uploads on one form
    #: stay distinguishable. Null for the implicit CV field.
    field_key: Mapped[str | None] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    application: Mapped[JobApplication] = relationship(back_populates="attachments")

    def __repr__(self) -> str:
        return f"<ApplicationFile {self.file_name}>"


# ── employee documents ─────────────────────────────────────────────────


class DocumentKind(StrEnum):
    """What a document is. Extended by adding a member — it is not user data.

    ``OTHER`` exists so HR is never blocked on a deploy to file something.
    """

    OFFER_LETTER = "offer_letter"
    CONTRACT = "contract"
    AMENDMENT = "amendment"
    ID_DOCUMENT = "id_document"
    VISA = "visa"
    CERTIFICATE = "certificate"
    PAYSLIP = "payslip"
    APPRAISAL = "appraisal"
    WARNING = "warning"
    RESIGNATION = "resignation"
    OTHER = "other"


class EmployeeDocument(Base, UUIDPrimaryKey, Timestamped):
    """A file held against a person: their offer letter, contract, visa, anything.

    ``visible_to_employee`` decides whether the person it is about can see it.
    It defaults to **true**, because most of what HR files about somebody is
    something they were given a copy of anyway, and a system where an employee
    cannot retrieve their own offer letter is a system people mail HR about. HR
    turns it off for the things that genuinely are not theirs to read — a
    disciplinary note being written, an appraisal not yet shared.
    """

    __tablename__ = "employee_documents"
    __table_args__ = (
        Index("ix_employee_documents_user_kind", "user_id", "kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[DocumentKind] = mapped_column(
        String(24), default=DocumentKind.OTHER, nullable=False
    )
    #: What to call it in a list. Defaults to the file name when HR gives none.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: When the document takes effect and when it stops — a visa expiring is
    #: something HR has to act on, so it is a column rather than a note.
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date, index=True)

    visible_to_employee: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The application this came from, when HR files an offer letter straight
    #: off a hire. Null for everything uploaded directly.
    source_application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_applications.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")
    uploaded_by: Mapped[User | None] = relationship(
        foreign_keys=[uploaded_by_id], lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<EmployeeDocument {self.kind} {self.title!r}>"


# ── performance ────────────────────────────────────────────────────────


class CycleStatus(StrEnum):
    DRAFT = "draft"
    #: Nominated reviewers can fill their form in.
    OPEN = "open"
    #: Nobody may submit any more. Scores stay readable.
    CLOSED = "closed"


class ReviewStatus(StrEnum):
    #: Nominated, not yet filled in.
    PENDING = "pending"
    #: Started and saved, not submitted. Still editable by the reviewer.
    DRAFT = "draft"
    SUBMITTED = "submitted"
    #: The reviewer said they are not the right person. Keeps an unanswered
    #: nomination distinguishable from one nobody got round to.
    DECLINED = "declined"


class ReviewCycle(Base, UUIDPrimaryKey, Timestamped):
    """A round of reviews: one form, one period, and the nominations HR makes.

    HR nominates every reviewer explicitly. Nothing is inferred from the org
    chart — who is best placed to judge somebody's year is a decision, and the
    reporting line is at best a guess at it.
    """

    __tablename__ = "review_cycles"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)

    status: Mapped[CycleStatus] = mapped_column(
        String(16), default=CycleStatus.DRAFT, nullable=False, index=True
    )
    #: Whether a subject may read the reviews written about them. Off by
    #: default: HR usually wants to read a cycle before the people in it do.
    shared_with_subjects: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    template: Mapped[FormTemplate] = relationship(lazy="joined")
    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    reviews: Mapped[list[PerformanceReview]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ReviewCycle {self.name!r} {self.status}>"


class ReviewerRelation(StrEnum):
    """How the reviewer knows the subject. Recorded, never used as a permission.

    It exists so a score can be read in context — a self-review and a manager's
    review of the same person are not the same evidence — and so HR can see at a
    glance that somebody has been reviewed only by themselves.
    """

    SELF = "self"
    MANAGER = "manager"
    PEER = "peer"
    REPORT = "report"
    HR = "hr"
    OTHER = "other"


class PerformanceReview(Base, UUIDPrimaryKey, Timestamped):
    """One nominated reviewer's assessment of one person, in one cycle.

    The nomination and the answers are one row rather than two tables. A
    nomination that is never filled in is not a missing record — it is a
    pending one, and that is a status, not an absence.
    """

    __tablename__ = "performance_reviews"
    __table_args__ = (
        # HR nominates a given reviewer for a given subject once per cycle.
        # Nominating twice is a mistake, and it would double that reviewer's
        # weight in the combined score.
        UniqueConstraint(
            "cycle_id", "subject_id", "reviewer_id", name="uq_review_cycle_subject_reviewer"
        ),
        Index("ix_performance_reviews_reviewer_status", "reviewer_id", "status"),
        Index("ix_performance_reviews_subject", "subject_id"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_cycles.id", ondelete="CASCADE"), nullable=False
    )
    #: Who is being reviewed.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Who was nominated to review them. Equal to ``subject_id`` for a
    #: self-review, which is allowed and is why there is no check forbidding it.
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[ReviewerRelation] = mapped_column(
        String(16), default=ReviewerRelation.OTHER, nullable=False
    )

    status: Mapped[ReviewStatus] = mapped_column(
        String(16), default=ReviewStatus.PENDING, nullable=False, index=True
    )
    answers: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: Frozen at submission, like an application's. See the module docstring.
    score: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    score_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    #: The reviewer's own summary, outside the scored fields.
    comment: Mapped[str | None] = mapped_column(Text)
    #: Why they declined, when they did.
    declined_reason: Mapped[str | None] = mapped_column(Text)

    due_on: Mapped[date | None] = mapped_column(Date)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cycle: Mapped[ReviewCycle] = relationship(back_populates="reviews")
    subject: Mapped[User] = relationship(foreign_keys=[subject_id], lazy="joined")
    reviewer: Mapped[User] = relationship(foreign_keys=[reviewer_id], lazy="joined")

    @property
    def is_self_review(self) -> bool:
        return self.subject_id == self.reviewer_id

    def __repr__(self) -> str:
        return f"<PerformanceReview subject={self.subject_id} {self.status}>"
