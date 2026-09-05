"""What the HR module does.

Three areas, one shape. An opening and a review cycle both name a form template
and then collect answers against it, so both go through ``fill`` below — one
place that checks the required fields, drops the ones the template does not ask
for, and computes the score. Two copies of that would drift, and the way they
would drift is that one of them would stop validating something.

Two rules are load-bearing and worth stating once:

**Answers are validated against the template version in force when the form was
handed out**, not the newest one. A candidate who submits five minutes after a
super admin edits the form should not be told they missed a question that did
not exist when their browser loaded the page.

**Scores are computed once, at submission.** Never on read. See the module
docstring in ``app.models.hr``.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.forms import scoring
from app.forms.catalogue import JOB_APPLICATION, JOB_POSTING, PERFORMANCE_REVIEW
from app.hr.documents import Upload
from app.models.hr import (
    ApplicationFile,
    ApplicationStage,
    CycleStatus,
    DocumentKind,
    EmployeeDocument,
    JobApplication,
    JobOpening,
    OpeningStatus,
    PerformanceReview,
    ReviewCycle,
    ReviewerRelation,
    ReviewStatus,
)
from app.models.templates import FieldType, FormTemplate, TemplateStatus
from app.models.user import User

logger = logging.getLogger("hamdaz.hr")

#: Bytes of entropy in a share token. 32 is not a guess: the token is the only
#: thing standing between the public internet and an opening's application form,
#: and it appears in links that get forwarded.
TOKEN_BYTES: Final[int] = 32

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class HRError(Exception):
    """Refused, with a reason safe to show whoever asked."""


class NotFoundError(HRError):
    pass


# ── shared helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def make_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def slugify(text: str) -> str:
    return _SLUG_STRIP.sub("-", (text or "").casefold()).strip("-")[:100] or "opening"


async def _unique_slug(session: AsyncSession, wanted: str) -> str:
    """``senior-estimator``, then ``senior-estimator-2``, and so on.

    Two openings for the same title in the same year is normal, and failing the
    request over it would make HR invent titles to get past the error.
    """
    base = slugify(wanted)
    taken = set(
        (
            await session.scalars(
                select(JobOpening.slug).where(JobOpening.slug.like(f"{base}%"))
            )
        ).all()
    )
    if base not in taken:
        return base
    for n in range(2, 500):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    return f"{base}-{secrets.token_hex(3)}"


async def _template_for(session: AsyncSession, template_id: uuid.UUID, kind: str) -> FormTemplate:
    template = await session.get(FormTemplate, template_id)
    if template is None:
        raise NotFoundError("That form template does not exist")
    if template.kind != kind:
        raise HRError(
            f"{template.name!r} is a {template.kind!r} form. This needs a {kind!r} "
            f"one — a super admin creates it in the templates section."
        )
    if template.status != TemplateStatus.ACTIVE:
        raise HRError(
            f"{template.name!r} is {template.status}. Only an active template can "
            f"be used; ask a super admin to publish it."
        )
    return template


async def templates_of(session: AsyncSession, kind: str) -> list[FormTemplate]:
    """Every active template of a kind, for HR to choose between."""
    return list(
        (
            await session.scalars(
                select(FormTemplate)
                .where(
                    FormTemplate.kind == kind,
                    FormTemplate.status == TemplateStatus.ACTIVE,
                )
                .order_by(FormTemplate.name)
            )
        ).all()
    )


async def default_template(session: AsyncSession, kind: str) -> FormTemplate | None:
    """The template used when HR names none.

    **The canonical template is the one whose key equals its kind.** With
    several forms of a kind — a short application, a technical one — "newest
    active" is not a default, it is whichever happened to be saved last, and it
    changes under HR without anybody choosing. This convention makes the
    fallback a decision instead.
    """
    active = await templates_of(session, kind)
    if not active:
        return None
    for template in active:
        if template.key == kind:
            return template
    # No canonical one. Newest wins, and HR should really pick.
    return max(active, key=lambda t: (t.version, t.created_at))


def fill(
    template: FormTemplate,
    answers: dict[str, Any],
    *,
    uploaded_keys: frozenset[str] = frozenset(),
    require_required: bool = True,
) -> tuple[dict[str, Any], scoring.Score]:
    """Check answers against a template and score them.

    Unknown keys are dropped rather than rejected: a stale browser tab posting a
    field that has since been removed should still record everything else it
    sent. Required ``file`` fields are satisfied by ``uploaded_keys``, because a
    file does not arrive in the answers at all.
    """
    fields = list(template.fields or [])
    known = {str(f.get("key")): f for f in fields if isinstance(f, dict) and f.get("key")}

    missing: list[str] = []
    clean: dict[str, Any] = {}
    for key, spec in known.items():
        value = answers.get(key)
        if spec.get("type") == FieldType.FILE.value:
            if require_required and spec.get("required") and key not in uploaded_keys:
                missing.append(str(spec.get("label") or key))
            continue
        blank = value is None or (isinstance(value, str) and not value.strip())
        if blank:
            if require_required and spec.get("required"):
                missing.append(str(spec.get("label") or key))
            continue
        clean[key] = value.strip() if isinstance(value, str) else value

    if missing:
        raise HRError("Please fill in: " + ", ".join(missing))
    return clean, scoring.score(fields, clean)


def _percent(score: scoring.Score) -> Decimal | None:
    return None if score.percent is None else Decimal(str(score.percent))


# ── openings ───────────────────────────────────────────────────────────


async def create_opening(
    session: AsyncSession,
    *,
    actor: User,
    title: str,
    template_id: uuid.UUID | None = None,
    posting_template_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    **fields: Any,
) -> JobOpening:
    title = (title or "").strip()
    if not title:
        raise HRError("An opening needs a title")

    # Only when HR is actually using the posting form. Attaching the canonical
    # one to every opening would mean an opening written straight onto the
    # columns could never be posted, because it would have to satisfy a form
    # nobody filled in.
    posting = (
        await _posting_template(session, posting_template_id)
        if posting_template_id is not None or details is not None
        else None
    )
    if template_id is None:
        template = await default_template(session, JOB_APPLICATION)
        if template is None:
            raise HRError(
                "There is no active job application form. A super admin creates "
                "one in the templates section before an opening can be posted."
            )
    else:
        template = await _template_for(session, template_id, JOB_APPLICATION)

    opening = JobOpening(
        title=title,
        slug=await _unique_slug(session, fields.pop("slug", None) or title),
        posting_template=posting,
        posting_template_id=None if posting is None else posting.id,
        posting_template_version=1 if posting is None else posting.version,
        # The object as well as the id: the response names the form, and an
        # unloaded relationship read during serialisation is a MissingGreenlet
        # rather than a name.
        template=template,
        template_id=template.id,
        template_version=template.version,
        public_token=make_token(),
        status=OpeningStatus.DRAFT,
        created_by=actor,
        updated_by_id=actor.id,
    )
    _apply_opening_fields(opening, fields)
    # Not validated for required fields yet: an opening is created as a draft,
    # and refusing to save a half-written advert is how a draft stops being a
    # draft. Posting it is where the form has to be complete.
    _apply_details(opening, posting, details, require_required=False)
    session.add(opening)
    await session.flush()
    await _load_opening_relations(session, opening)
    return opening


#: Posting answers that also live in a column of their own. See the comment on
#: ``JobOpening.details`` for why they are duplicated rather than read out of
#: the blob wherever they are needed.
MIRRORED: Final[tuple[str, ...]] = ("summary", "description", "requirements", "salary_range")


async def _posting_template(
    session: AsyncSession, template_id: uuid.UUID | None
) -> FormTemplate | None:
    """The posting form, or None to write the advert straight onto the columns.

    Unset means the default one where there is one — an organisation that has a
    posting form should not have to name it on every opening — and None where
    there is not, which keeps the module usable before anybody has set one up.
    """
    if template_id is not None:
        return await _template_for(session, template_id, JOB_POSTING)
    return await default_template(session, JOB_POSTING)


def _apply_details(
    opening: JobOpening,
    posting: FormTemplate | None,
    details: dict[str, Any] | None,
    *,
    require_required: bool,
) -> None:
    """Validate the advert against its form and mirror the four known keys."""
    if details is None:
        return
    if posting is None:
        raise HRError(
            "There is no job posting form, so there is nothing to fill in. A "
            "super admin creates one in the templates section."
        )
    clean, _ = fill(posting, details, require_required=require_required)
    opening.details = clean
    for key in MIRRORED:
        if key not in clean:
            continue
        value = str(clean[key])
        # salary_range has a 120-character column behind it; the other three are
        # Text. A posting form is free text and a column is not, so the one that
        # can overflow is truncated rather than left to fail the insert.
        setattr(opening, key, value[:120] if key == "salary_range" else value)


async def _load_opening_relations(session: AsyncSession, opening: JobOpening) -> None:
    """Make the joined relationships safe to read.

    Setting ``team_id`` does not update ``team``, so anything that changed an
    id has to say so before the row is serialised — otherwise the read happens
    lazily from inside the serialiser, which inside async is a MissingGreenlet
    rather than a value.
    """
    await session.refresh(
        opening, ["team", "template", "posting_template", "created_by"]
    )


def _apply_opening_fields(opening: JobOpening, changes: dict[str, Any]) -> None:
    for name in (
        "reference", "team_id", "department", "location", "employment_type",
        "salary_range", "summary", "description", "requirements", "closes_on",
        "publicly_listed", "hosted_form",
    ):
        if name in changes and changes[name] is not None:
            setattr(opening, name, changes[name])
    if (headcount := changes.get("headcount")) is not None:
        if headcount < 1:
            raise HRError("An opening is for at least one person")
        opening.headcount = headcount


async def update_opening(
    session: AsyncSession, opening: JobOpening, *, actor: User, **changes: Any
) -> JobOpening:
    if (posting_id := changes.pop("posting_template_id", None)) is not None:
        posting = await _template_for(session, posting_id, JOB_POSTING)
        opening.posting_template = posting
        opening.posting_template_id = posting.id
        opening.posting_template_version = posting.version
    if (details := changes.pop("details", None)) is not None:
        form = opening.posting_template or await _posting_template(session, None)
        if form is not None and opening.posting_template_id is None:
            # Filling the advert in is what opts an opening into the form, so
            # this is where it gets attached.
            opening.posting_template = form
            opening.posting_template_id = form.id
            opening.posting_template_version = form.version
        # A posted opening's advert is live, so it may not be edited into an
        # incomplete state. A draft may: that is what a draft is.
        _apply_details(
            opening, form, details,
            require_required=opening.status != OpeningStatus.DRAFT,
        )
    if (title := changes.pop("title", None)):
        opening.title = str(title).strip()[:200]
    if (template_id := changes.pop("template_id", None)) and template_id != opening.template_id:
        if opening.status != OpeningStatus.DRAFT:
            # Applications already in hand were answered against the old form.
            # Swapping it under them would leave their answers unreadable.
            raise HRError(
                "The application form can only be changed while the opening is a "
                "draft. Close this one and post a new opening instead."
            )
        template = await _template_for(session, template_id, JOB_APPLICATION)
        opening.template_id = template.id
        opening.template_version = template.version
    _apply_opening_fields(opening, changes)
    opening.updated_by_id = actor.id
    await session.flush()
    await _load_opening_relations(session, opening)
    return opening


async def post_opening(session: AsyncSession, opening: JobOpening, *, actor: User) -> JobOpening:
    """Make the share link live."""
    if opening.status == OpeningStatus.OPEN:
        return opening
    if opening.closes_on and opening.closes_on < date.today():
        raise HRError("The closing date has already passed. Change it before posting.")

    template = await session.get(FormTemplate, opening.template_id)
    if template is None or template.status != TemplateStatus.ACTIVE:
        raise HRError(
            "The application form is not active any more. Choose another before posting."
        )
    if opening.posting_template_id is not None:
        posting = await session.get(FormTemplate, opening.posting_template_id)
        if posting is None:
            raise HRError("The job posting form is unavailable.")
        # Now it has to be complete: this is the moment the advert goes out.
        fill(posting, dict(opening.details or {}), require_required=True)
        opening.posting_template_version = posting.version

    # Re-snapshotted here rather than at creation: the version that matters is
    # the one candidates are actually shown.
    opening.template_version = template.version
    opening.status = OpeningStatus.OPEN
    opening.posted_at = opening.posted_at or _now()
    opening.closed_at = None
    opening.updated_by_id = actor.id
    await session.flush()
    return opening


async def close_opening(
    session: AsyncSession, opening: JobOpening, *, actor: User, filled: bool = False
) -> JobOpening:
    opening.status = OpeningStatus.FILLED if filled else OpeningStatus.CLOSED
    opening.closed_at = _now()
    opening.updated_by_id = actor.id
    await session.flush()
    return opening


async def rotate_token(
    session: AsyncSession, opening: JobOpening, *, actor: User
) -> JobOpening:
    """Issue a new share link and kill the old one.

    The only way to revoke a link that went somewhere it should not have. Every
    copy of the old URL stops working immediately, which is the point and is
    worth warning HR about in the UI.
    """
    opening.public_token = make_token()
    opening.updated_by_id = actor.id
    await session.flush()
    return opening


async def get_opening(session: AsyncSession, ref: str | uuid.UUID) -> JobOpening:
    """By id or slug — both appear in URLs."""
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(str(ref))
    except (ValueError, AttributeError):
        as_uuid = None

    opening = await session.get(JobOpening, as_uuid) if as_uuid else None
    if opening is None:
        opening = await session.scalar(select(JobOpening).where(JobOpening.slug == str(ref)))
    if opening is None:
        raise NotFoundError("No such opening")
    return opening


async def list_openings(
    session: AsyncSession, *, status: OpeningStatus | None = None
) -> list[tuple[JobOpening, int, int]]:
    """Every opening, with how many applications it has and how many are new.

    Counted in SQL rather than by loading the applications: an opening that has
    been advertised widely can have hundreds, and the list only needs two
    numbers from them.
    """
    query = select(JobOpening).order_by(
        JobOpening.status, JobOpening.posted_at.desc().nulls_last(), JobOpening.created_at.desc()
    )
    if status is not None:
        query = query.where(JobOpening.status == status)
    openings = list((await session.scalars(query)).all())
    if not openings:
        return []

    ids = [o.id for o in openings]
    rows = await session.execute(
        select(
            JobApplication.opening_id,
            func.count(JobApplication.id),
            func.count(JobApplication.id).filter(
                JobApplication.stage == ApplicationStage.NEW
            ),
        )
        .where(JobApplication.opening_id.in_(ids))
        .group_by(JobApplication.opening_id)
    )
    counts = {oid: (total, new) for oid, total, new in rows}
    return [(o, *counts.get(o.id, (0, 0))) for o in openings]


async def by_token(session: AsyncSession, token: str) -> JobOpening:
    """The public lookup. Never falls back to id or slug — that is the point.

    A share token is the credential. Allowing the same endpoint to accept a slug
    would mean anybody who could guess ``senior-estimator`` had the link.
    """
    if not token or len(token) < 20:
        raise NotFoundError("No such opening")
    opening = await session.scalar(
        select(JobOpening).where(JobOpening.public_token == token)
    )
    if opening is None or opening.status == OpeningStatus.DRAFT:
        # A draft is indistinguishable from a wrong token on purpose: an
        # unposted opening should not be confirmable by anybody outside.
        raise NotFoundError("No such opening")
    return opening


async def listed_openings(session: AsyncSession) -> list[JobOpening]:
    """What the public careers list shows: posted, listed, and still accepting."""
    openings = await session.scalars(
        select(JobOpening)
        .where(
            JobOpening.status == OpeningStatus.OPEN,
            JobOpening.publicly_listed.is_(True),
        )
        .order_by(JobOpening.posted_at.desc().nulls_last())
    )
    return [o for o in openings.all() if o.accepts_applications]


# ── applications ───────────────────────────────────────────────────────


def _contact(answers: dict[str, Any]) -> tuple[str, str, str | None]:
    """Pull the three columns that get their own home off the answers."""
    name = str(answers.get("candidate_name") or "").strip()
    email = str(answers.get("candidate_email") or "").strip().casefold()
    phone = str(answers.get("candidate_phone") or "").strip() or None

    if not name:
        raise HRError("Please give your name")
    if not _EMAIL.match(email):
        raise HRError("Please give an email address we can reply to")
    return name[:200], email[:320], (phone[:40] if phone else None)


async def submit_application(
    session: AsyncSession,
    opening: JobOpening,
    *,
    answers: dict[str, Any],
    uploads: list[tuple[str | None, Upload]] | None = None,
) -> JobApplication:
    """Record a candidate's application. Called from the public endpoint.

    Resubmission by the same email replaces what was sent, but only while
    nobody has looked at it. Once HR has moved a candidate along, a silent
    overwrite would change the thing a decision was made on.
    """
    if not opening.accepts_applications:
        raise HRError(
            "This opening is no longer accepting applications."
            if opening.status != OpeningStatus.OPEN
            else "The closing date for this opening has passed."
        )

    uploads = uploads or []
    template = await session.get(FormTemplate, opening.template_id)
    if template is None:
        raise HRError("This application form is unavailable. Please try again later.")

    name, email, phone = _contact(answers)
    clean, score = fill(
        template,
        answers,
        uploaded_keys=frozenset(key for key, _ in uploads if key),
    )

    existing = await session.scalar(
        select(JobApplication).where(
            JobApplication.opening_id == opening.id,
            JobApplication.candidate_email == email,
        )
    )
    if existing is not None and existing.stage != ApplicationStage.NEW:
        raise HRError(
            "We already have an application from this address for this role, and "
            "it is being looked at. Please contact us rather than reapplying."
        )

    application = existing or JobApplication(opening_id=opening.id)
    application.candidate_name = name
    application.candidate_email = email
    application.candidate_phone = phone
    application.answers = clean
    application.template_version = opening.template_version
    application.score = score.as_dict()
    application.score_percent = _percent(score)
    application.stage = ApplicationStage.NEW
    application.submitted_at = _now()
    # A resubmission replaces the documents too: the second CV is the one they
    # meant to send.
    application.attachments = [
        ApplicationFile(
            field_key=key,
            file_name=upload.file_name,
            content_type=upload.content_type,
            size_bytes=upload.size,
            file_bytes=upload.content,
        )
        for key, upload in uploads
    ]
    if existing is None:
        session.add(application)
    await session.flush()
    logger.info("application %s for opening %s", application.id, opening.slug)
    return application


async def list_applications(
    session: AsyncSession,
    *,
    opening_id: uuid.UUID | None = None,
    stage: ApplicationStage | None = None,
) -> list[JobApplication]:
    query = (
        select(JobApplication)
        .order_by(
            JobApplication.score_percent.desc().nulls_last(),
            JobApplication.submitted_at.desc(),
        )
        .options(selectinload(JobApplication.opening))
    )
    if opening_id is not None:
        query = query.where(JobApplication.opening_id == opening_id)
    if stage is not None:
        query = query.where(JobApplication.stage == stage)
    return list((await session.scalars(query)).all())


async def get_application(session: AsyncSession, application_id: uuid.UUID) -> JobApplication:
    application = await session.get(JobApplication, application_id)
    if application is None:
        raise NotFoundError("No such application")
    return application


async def move_stage(
    session: AsyncSession,
    application: JobApplication,
    *,
    actor: User,
    stage: ApplicationStage,
    note: str | None = None,
) -> JobApplication:
    application.stage = stage
    if note is not None:
        application.stage_note = note
    application.decided_at = _now()
    application.decided_by = actor
    await session.flush()
    return application


async def hire(
    session: AsyncSession,
    application: JobApplication,
    *,
    actor: User,
    user_id: uuid.UUID,
    close_opening_too: bool = False,
) -> JobApplication:
    """Link a hired candidate to the employee record they became.

    The user is not created here. People arrive from Entra when they first sign
    in, and inventing a row before that would produce a second account for the
    same person the day they do.
    """
    employee = await session.get(User, user_id)
    if employee is None:
        raise NotFoundError(
            "That employee record does not exist yet. They appear here once "
            "their Microsoft account signs in for the first time."
        )
    application.hired_user_id = employee.id
    await move_stage(session, application, actor=actor, stage=ApplicationStage.HIRED)

    if close_opening_too:
        opening = await session.get(JobOpening, application.opening_id)
        if opening is not None:
            await close_opening(session, opening, actor=actor, filled=True)
    return application


# ── employee documents ─────────────────────────────────────────────────


async def add_document(
    session: AsyncSession,
    *,
    actor: User,
    user_id: uuid.UUID,
    upload: Upload,
    kind: DocumentKind = DocumentKind.OTHER,
    title: str | None = None,
    note: str | None = None,
    issued_on: date | None = None,
    expires_on: date | None = None,
    visible_to_employee: bool = True,
    source_application_id: uuid.UUID | None = None,
) -> EmployeeDocument:
    if await session.get(User, user_id) is None:
        raise NotFoundError("No such person")
    if issued_on and expires_on and expires_on < issued_on:
        raise HRError("The expiry date is before the issue date")

    document = EmployeeDocument(
        user_id=user_id,
        kind=kind,
        title=(title or "").strip()[:200] or upload.file_name,
        note=note,
        file_name=upload.file_name,
        content_type=upload.content_type,
        size_bytes=upload.size,
        file_bytes=upload.content,
        issued_on=issued_on,
        expires_on=expires_on,
        visible_to_employee=visible_to_employee,
        uploaded_by_id=actor.id,
        source_application_id=source_application_id,
    )
    session.add(document)
    await session.flush()
    return document


async def list_documents(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    expiring_within_days: int | None = None,
) -> list[EmployeeDocument]:
    query = select(EmployeeDocument).order_by(
        EmployeeDocument.kind, EmployeeDocument.created_at.desc()
    )
    if user_id is not None:
        query = query.where(EmployeeDocument.user_id == user_id)
    if expiring_within_days is not None:
        cutoff = date.today() + timedelta(days=expiring_within_days)
        query = query.where(
            EmployeeDocument.expires_on.is_not(None), EmployeeDocument.expires_on <= cutoff
        )
    return list((await session.scalars(query)).all())


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> EmployeeDocument:
    document = await session.get(EmployeeDocument, document_id)
    if document is None:
        raise NotFoundError("No such document")
    return document


async def update_document(
    session: AsyncSession, document: EmployeeDocument, **changes: Any
) -> EmployeeDocument:
    for name in ("title", "note", "kind", "issued_on", "expires_on", "visible_to_employee"):
        if name in changes and changes[name] is not None:
            setattr(document, name, changes[name])
    await session.flush()
    return document


async def delete_document(
    session: AsyncSession, document: EmployeeDocument, *, actor: User
) -> Removed:
    title, owner = document.title, document.user_id
    await session.delete(document)
    await session.flush()
    logger.warning(
        "super admin %s deleted document %r of user %s", actor.email, title, owner
    )
    return Removed(documents=1)


# ── review cycles ──────────────────────────────────────────────────────


async def create_cycle(
    session: AsyncSession,
    *,
    actor: User,
    name: str,
    template_id: uuid.UUID | None = None,
    **fields: Any,
) -> ReviewCycle:
    name = (name or "").strip()
    if not name:
        raise HRError("A review cycle needs a name")

    if template_id is None:
        template = await default_template(session, PERFORMANCE_REVIEW)
        if template is None:
            raise HRError(
                "There is no active performance review form. A super admin "
                "creates one in the templates section first."
            )
    else:
        template = await _template_for(session, template_id, PERFORMANCE_REVIEW)

    period_start, period_end = fields.get("period_start"), fields.get("period_end")
    if period_start and period_end and period_end < period_start:
        raise HRError("The period ends before it starts")

    cycle = ReviewCycle(
        name=name,
        description=fields.get("description"),
        template=template,
        template_id=template.id,
        template_version=template.version,
        period_start=period_start,
        period_end=period_end,
        due_on=fields.get("due_on"),
        shared_with_subjects=bool(fields.get("shared_with_subjects", False)),
        status=CycleStatus.DRAFT,
        created_by=actor,
    )
    session.add(cycle)
    await session.flush()
    return cycle


async def get_cycle(session: AsyncSession, cycle_id: uuid.UUID) -> ReviewCycle:
    cycle = await session.get(ReviewCycle, cycle_id)
    if cycle is None:
        raise NotFoundError("No such review cycle")
    return cycle


async def list_cycles(session: AsyncSession) -> list[ReviewCycle]:
    return list(
        (
            await session.scalars(
                select(ReviewCycle).order_by(
                    ReviewCycle.status, ReviewCycle.created_at.desc()
                )
            )
        ).all()
    )


async def open_cycle(session: AsyncSession, cycle: ReviewCycle) -> ReviewCycle:
    if cycle.status == CycleStatus.CLOSED:
        raise HRError("A closed cycle cannot be reopened. Start a new one.")
    nominations = await session.scalar(
        select(func.count(PerformanceReview.id)).where(PerformanceReview.cycle_id == cycle.id)
    )
    if not nominations:
        raise HRError(
            "Nobody has been nominated to review anybody. Add nominations before "
            "opening the cycle, or it opens to an empty room."
        )
    cycle.status = CycleStatus.OPEN
    cycle.opened_at = cycle.opened_at or _now()
    await session.flush()
    return cycle


async def close_cycle(session: AsyncSession, cycle: ReviewCycle) -> ReviewCycle:
    cycle.status = CycleStatus.CLOSED
    cycle.closed_at = _now()
    await session.flush()
    return cycle


async def set_cycle_sharing(
    session: AsyncSession, cycle: ReviewCycle, *, shared: bool
) -> ReviewCycle:
    cycle.shared_with_subjects = shared
    await session.flush()
    return cycle


async def nominate(
    session: AsyncSession,
    cycle: ReviewCycle,
    *,
    subject_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    relation: ReviewerRelation = ReviewerRelation.OTHER,
    due_on: date | None = None,
) -> PerformanceReview:
    """Ask one person to review another. Self-review is a nomination like any other."""
    if cycle.status == CycleStatus.CLOSED:
        raise HRError("This cycle has closed")

    subject, reviewer = await session.get(User, subject_id), await session.get(User, reviewer_id)
    if subject is None:
        raise NotFoundError("No such person to review")
    if reviewer is None:
        raise NotFoundError("No such reviewer")
    if not reviewer.is_active:
        raise HRError(f"{reviewer.display_name} is deactivated and cannot be nominated")

    existing = await session.scalar(
        select(PerformanceReview).where(
            PerformanceReview.cycle_id == cycle.id,
            PerformanceReview.subject_id == subject_id,
            PerformanceReview.reviewer_id == reviewer_id,
        )
    )
    if existing is not None:
        raise HRError(
            f"{reviewer.display_name} is already reviewing {subject.display_name} "
            f"in this cycle."
        )

    review = PerformanceReview(
        # All three as objects. A nomination is serialised straight back to the
        # caller, and every one of these is read to build that response.
        cycle=cycle,
        cycle_id=cycle.id,
        subject=subject,
        subject_id=subject_id,
        reviewer=reviewer,
        reviewer_id=reviewer_id,
        relation=(
            ReviewerRelation.SELF if subject_id == reviewer_id else relation
        ),
        due_on=due_on or cycle.due_on,
        status=ReviewStatus.PENDING,
    )
    session.add(review)
    await session.flush()
    return review


async def withdraw_nomination(session: AsyncSession, review: PerformanceReview) -> None:
    """Un-ask somebody. Kept separate from deleting a written review.

    This is the ordinary HR correction — the wrong reviewer was nominated — and
    it destroys nothing, because it refuses once anything has been written. What
    to do with a review that *has* content is a super admin's decision, through
    ``delete_review``.
    """
    if review.status in (ReviewStatus.SUBMITTED, ReviewStatus.DRAFT):
        raise HRError(
            "That reviewer has already started writing. Withdrawing it now would "
            "throw their work away — a super admin can delete it if it really "
            "should not exist."
        )
    await session.delete(review)
    await session.flush()


# ── deletion ───────────────────────────────────────────────────────────
#
# Every function below destroys records and is reachable only by a super admin
# — see ``app.hr.access.PURGE_ADMINS`` for why that is narrower than HR itself.
#
# Each one counts what it is about to remove *before* removing it and hands the
# counts back. A super admin pressing delete on an opening should be told it
# takes forty applications with it, and a database that only reports success
# cannot tell them that. The counts are also what goes in the log line, which is
# the only trace left once the rows are gone.


@dataclass(frozen=True, slots=True)
class Removed:
    """What a deletion actually destroyed."""

    openings: int = 0
    applications: int = 0
    files: int = 0
    documents: int = 0
    cycles: int = 0
    reviews: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "openings": self.openings,
            "applications": self.applications,
            "files": self.files,
            "documents": self.documents,
            "cycles": self.cycles,
            "reviews": self.reviews,
        }


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count(model.id)).where(*where)) or 0)


async def delete_opening(
    session: AsyncSession, opening: JobOpening, *, actor: User
) -> Removed:
    """An opening and every application to it.

    The candidates' files go with them, by the cascade on ``application_files``.
    Offer letters already filed against an employee do **not**: their
    ``source_application_id`` is SET NULL, so deleting the opening somebody was
    hired through never destroys their contract.
    """
    applications = await _count(
        session, JobApplication, JobApplication.opening_id == opening.id
    )
    files = int(
        await session.scalar(
            select(func.count(ApplicationFile.id))
            .join(JobApplication, ApplicationFile.application_id == JobApplication.id)
            .where(JobApplication.opening_id == opening.id)
        )
        or 0
    )
    slug = opening.slug
    await session.delete(opening)
    await session.flush()
    removed = Removed(openings=1, applications=applications, files=files)
    logger.warning(
        "super admin %s deleted opening %s: %s", actor.email, slug, removed.as_dict()
    )
    return removed


async def delete_application(
    session: AsyncSession, application: JobApplication, *, actor: User
) -> Removed:
    files = len(application.attachments or [])
    email = application.candidate_email
    await session.delete(application)
    await session.flush()
    removed = Removed(applications=1, files=files)
    logger.warning(
        "super admin %s deleted application from %s: %s",
        actor.email, email, removed.as_dict(),
    )
    return removed


async def delete_cycle(
    session: AsyncSession, cycle: ReviewCycle, *, actor: User
) -> Removed:
    """A cycle and every review in it, submitted ones included."""
    reviews = await _count(
        session, PerformanceReview, PerformanceReview.cycle_id == cycle.id
    )
    name = cycle.name
    await session.delete(cycle)
    await session.flush()
    removed = Removed(cycles=1, reviews=reviews)
    logger.warning(
        "super admin %s deleted review cycle %r: %s", actor.email, name, removed.as_dict()
    )
    return removed


async def delete_review(
    session: AsyncSession, review: PerformanceReview, *, actor: User
) -> Removed:
    """One review, whatever state it is in.

    No guard on ``submitted`` here, unlike ``withdraw_nomination``. That guard
    exists to stop HR throwing away somebody's work by accident; a super admin
    deleting a submitted review is doing it on purpose, and the log line is what
    records that they did.
    """
    subject = review.subject_id
    await session.delete(review)
    await session.flush()
    logger.warning(
        "super admin %s deleted a %s review of user %s",
        actor.email, review.status, subject,
    )
    return Removed(reviews=1)


async def purge_person(
    session: AsyncSession, user_id: uuid.UUID, *, actor: User
) -> Removed:
    """Everything HR holds *about* one person.

    Their documents, and every review written about them. Reviews they *wrote*
    about other people are deliberately left: those are records about somebody
    else, and deleting a leaver's account should not quietly remove half the
    evidence behind a colleague's appraisal. Their applications as a candidate
    are keyed on an email address rather than on this row and are not touched
    either — delete those through the opening or the application.
    """
    person = await session.get(User, user_id)
    if person is None:
        raise NotFoundError("No such person")

    documents = await _count(
        session, EmployeeDocument, EmployeeDocument.user_id == user_id
    )
    reviews = await _count(
        session, PerformanceReview, PerformanceReview.subject_id == user_id
    )
    for document in await session.scalars(
        select(EmployeeDocument).where(EmployeeDocument.user_id == user_id)
    ):
        await session.delete(document)
    for review in await session.scalars(
        select(PerformanceReview).where(PerformanceReview.subject_id == user_id)
    ):
        await session.delete(review)
    await session.flush()

    removed = Removed(documents=documents, reviews=reviews)
    logger.warning(
        "super admin %s purged HR data for %s: %s",
        actor.email, person.email, removed.as_dict(),
    )
    return removed


async def get_review(session: AsyncSession, review_id: uuid.UUID) -> PerformanceReview:
    review = await session.get(
        PerformanceReview, review_id, options=[selectinload(PerformanceReview.cycle)]
    )
    if review is None:
        raise NotFoundError("No such review")
    return review


async def list_reviews(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    reviewer_id: uuid.UUID | None = None,
    status: ReviewStatus | None = None,
) -> list[PerformanceReview]:
    query = (
        select(PerformanceReview)
        .order_by(PerformanceReview.created_at.desc())
        .options(selectinload(PerformanceReview.cycle))
    )
    if cycle_id is not None:
        query = query.where(PerformanceReview.cycle_id == cycle_id)
    if subject_id is not None:
        query = query.where(PerformanceReview.subject_id == subject_id)
    if reviewer_id is not None:
        query = query.where(PerformanceReview.reviewer_id == reviewer_id)
    if status is not None:
        query = query.where(PerformanceReview.status == status)
    return list((await session.scalars(query)).all())


async def save_review(
    session: AsyncSession,
    review: PerformanceReview,
    *,
    answers: dict[str, Any],
    comment: str | None = None,
    submit: bool = False,
) -> PerformanceReview:
    """Save progress, or submit.

    A draft is not validated — half an answer is the normal state of a form
    somebody is still filling in. Submitting is where the required fields are
    enforced and where the score is computed and frozen.
    """
    template = await session.get(FormTemplate, review.cycle.template_id)
    if template is None:
        raise HRError("The review form is unavailable")

    clean, score = fill(template, answers, require_required=submit)
    review.answers = clean
    if comment is not None:
        review.comment = comment

    if submit:
        review.score = score.as_dict()
        review.score_percent = _percent(score)
        review.status = ReviewStatus.SUBMITTED
        review.submitted_at = _now()
    else:
        # A draft carries a provisional score so the reviewer can see where they
        # are, but it is not the record until they submit.
        review.score = score.as_dict()
        review.score_percent = _percent(score)
        review.status = ReviewStatus.DRAFT
    await session.flush()
    return review


async def decline_review(
    session: AsyncSession, review: PerformanceReview, *, reason: str | None = None
) -> PerformanceReview:
    review.status = ReviewStatus.DECLINED
    review.declined_reason = reason
    await session.flush()
    return review


async def reopen_review(session: AsyncSession, review: PerformanceReview) -> PerformanceReview:
    """HR hands a submitted review back to its author.

    The score is cleared with it. Leaving the old number on a review that is
    being rewritten would mean a stale figure feeding the combined score for as
    long as it takes the reviewer to get round to it.
    """
    if review.cycle.status != CycleStatus.OPEN:
        raise HRError("Reopen the cycle first — the reviewer cannot submit into a closed one.")
    review.status = ReviewStatus.DRAFT
    review.submitted_at = None
    review.score = {}
    review.score_percent = None
    await session.flush()
    return review


# ── the performance picture ────────────────────────────────────────────


async def performance_for(
    session: AsyncSession, user_id: uuid.UUID, *, cycle_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Somebody's combined score across every submitted review of them.

    Combined by points and maximums rather than by averaging percentages — see
    ``app.forms.scoring.combine`` for why that distinction matters.
    """
    reviews = await list_reviews(
        session, subject_id=user_id, cycle_id=cycle_id, status=ReviewStatus.SUBMITTED
    )
    combined = scoring.combine([r.score for r in reviews if r.score])
    return {
        "user_id": user_id,
        "reviews": len(reviews),
        "self_reviews": sum(1 for r in reviews if r.is_self_review),
        "cycles": sorted({str(r.cycle_id) for r in reviews}),
        **combined,
    }
