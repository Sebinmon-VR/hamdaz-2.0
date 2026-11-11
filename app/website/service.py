"""Website enquiries: reading the form, keeping the row, telling sales.

The form is read here rather than declared as endpoint parameters for the
reason the careers form is: multipart is what a browser's ``FormData`` posts,
it is CORS-safelisted so the website needs no preflight, and a repeated key
(the ticked topics) has to be read as a list rather than as its last value.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Iterable

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.mail import GraphMailer, MailError
from app.models.access import TeamModuleAccess
from app.models.notification import NotificationKind
from app.models.role import Role, UserRole
from app.models.team import TeamMembership
from app.models.user import User
from app.models.website import WebsiteEnquiry, WebsiteEnquiryStatus
from app.notifications.service import notify, send_to_teams
from app.roles.catalogue import ROLE_MODULES, SUPER_ADMIN
from app.website import MODULE_KEY

logger = logging.getLogger("hamdaz.website")

#: Deliberately loose. The point is to catch a typo'd field, not to rule on
#: what an address may look like — a stricter pattern refuses real addresses.
_EMAIL: Final = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
#: The campaign tags the website forwards from the landing page's address.
ATTRIBUTION_KEYS: Final = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid")
MAX_TOPICS: Final = 12


class EnquiryInputError(Exception):
    """The form is not something we can store. The message is shown as is."""


class EnquiryError(Exception):
    """A colleague asked for something that cannot be done."""


class NotFoundError(EnquiryError):
    pass


@dataclass(slots=True)
class CleanEnquiry:
    name: str
    message: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    topics: list[str] = field(default_factory=list)
    page: str | None = None
    landing: str | None = None
    referrer: str | None = None
    attribution: dict[str, str] = field(default_factory=dict)
    #: The honeypot was filled: answer as if it worked, keep nothing.
    trapped: bool = False

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name


# ── reading the form ───────────────────────────────────────────────────


def clean(items: Iterable[tuple[str, Any]]) -> CleanEnquiry:
    """The form, checked. Raises ``EnquiryInputError`` with a sentence to show.

    What the visitor typed is refused when it is wrong, so they can fix it.
    What the page added on their behalf — where they were, the campaign tags —
    is cut to length instead: a long referrer is not a reason to lose a
    customer's message.
    """
    single: dict[str, str] = {}
    topics: list[str] = []
    for key, value in items:
        if not isinstance(value, str):
            continue  # a file nobody asked for
        if key == "topics":
            topics.append(value)
        else:
            # The first value wins: a repeated single field is a malformed post,
            # and the first is what a browser would have shown.
            single.setdefault(key, value)

    def text(key: str) -> str:
        return " ".join(single.get(key, "").split())

    def typed(key: str, label: str, limit: int) -> str | None:
        value = text(key)
        if len(value) > limit:
            raise EnquiryInputError(f"Please keep the {label} under {limit} characters.")
        return value or None

    def added(key: str, limit: int) -> str | None:
        return single.get(key, "").strip()[:limit] or None

    trapped = bool(single.get("website", "").strip())

    name = text("name")
    if len(name) < 2:
        raise EnquiryInputError("Please tell us your name.")
    if len(name) > 200:
        raise EnquiryInputError("Please keep your name under 200 characters.")

    email = typed("email", "email address", 320)
    if email and not _EMAIL.match(email):
        raise EnquiryInputError("Please check your email address.")
    phone = typed("phone", "phone number", 40)
    if not email and not phone:
        raise EnquiryInputError("Please give us an email address or a phone number, so we can reply.")
    company = typed("company", "company name", 200)

    # The message keeps its line breaks: it is read as written.
    message = single.get("message", "").strip()
    if len(message) < 5:
        raise EnquiryInputError("Please tell us a little about what you need.")
    if len(message) > 5000:
        raise EnquiryInputError("Please keep your message under 5,000 characters.")

    chosen = list(dict.fromkeys(" ".join(t.split()) for t in topics if t.strip()))
    if len(chosen) > MAX_TOPICS or any(len(t) > 80 for t in chosen):
        raise EnquiryInputError("Please choose fewer topics.")

    return CleanEnquiry(
        name=name,
        message=message,
        email=email,
        phone=phone,
        company=company,
        topics=chosen,
        page=added("page", 500),
        landing=added("landing", 500),
        referrer=added("referrer", 500),
        attribution={k: v for k in ATTRIBUTION_KEYS if (v := added(k, 300))},
        trapped=trapped,
    )


async def create(session: AsyncSession, found: CleanEnquiry) -> WebsiteEnquiry:
    row = WebsiteEnquiry(
        name=found.name,
        email=found.email,
        phone=found.phone,
        company=found.company,
        topics=found.topics,
        message=found.message,
        page=found.page,
        landing=found.landing,
        referrer=found.referrer,
        attribution=found.attribution,
        status=WebsiteEnquiryStatus.NEW.value,
    )
    session.add(row)
    await session.flush()
    return row


# ── who may see them ───────────────────────────────────────────────────


async def people_who_can_see(session: AsyncSession) -> list[User]:
    """Everyone who can open the module: who to tell, and who to assign to.

    Mirrors ``app.access.service.effective_access`` from the other side — the
    super admins, the global roles that open the module by themselves (see
    ``ROLE_MODULES``), and the members of any team granted it. Asking that
    function once per user would be the same answer, a query per person slower.
    """
    role_keys = {SUPER_ADMIN} | {r for r, modules in ROLE_MODULES.items() if MODULE_KEY in modules}
    by_role = (
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.key.in_(role_keys))
    )
    by_team = (
        select(TeamMembership.user_id)
        .join(TeamModuleAccess, TeamModuleAccess.team_id == TeamMembership.team_id)
        .where(TeamModuleAccess.module_key == MODULE_KEY)
    )
    rows = await session.scalars(
        select(User)
        .where(User.is_active.is_(True), or_(User.id.in_(by_role), User.id.in_(by_team)))
        .order_by(User.display_name)
    )
    return list(rows.unique().all())


# ── the sales side ─────────────────────────────────────────────────────


async def listing(
    session: AsyncSession,
    *,
    status: WebsiteEnquiryStatus | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WebsiteEnquiry], int, dict[str, int]]:
    query = select(WebsiteEnquiry)
    if status is not None:
        query = query.where(WebsiteEnquiry.status == status.value)
    words = (search or "").strip()
    if words:
        like = f"%{words}%"
        query = query.where(
            or_(
                WebsiteEnquiry.name.ilike(like),
                WebsiteEnquiry.email.ilike(like),
                WebsiteEnquiry.company.ilike(like),
                WebsiteEnquiry.message.ilike(like),
            )
        )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.scalars(
            query.order_by(WebsiteEnquiry.created_at.desc()).limit(limit).offset(offset)
        )
    ).unique().all()
    counts = {s.value: 0 for s in WebsiteEnquiryStatus}
    for key, n in (
        await session.execute(
            select(WebsiteEnquiry.status, func.count()).group_by(WebsiteEnquiry.status)
        )
    ).all():
        counts[str(key)] = int(n)
    return list(rows), total, counts


async def get(session: AsyncSession, enquiry_id: uuid.UUID) -> WebsiteEnquiry:
    row = await session.get(WebsiteEnquiry, enquiry_id)
    if row is None:
        raise NotFoundError("There is no such enquiry.")
    return row


async def update(
    session: AsyncSession,
    row: WebsiteEnquiry,
    *,
    changes: dict[str, Any],
    by: User,
) -> WebsiteEnquiry:
    """Apply what was sent. Only keys present in ``changes`` are touched."""
    if "assigned_to_id" in changes:
        assignee = changes["assigned_to_id"]
        if assignee is not None and assignee not in {u.id for u in await people_who_can_see(session)}:
            # Handing it to somebody who cannot open it would leave it with
            # nobody, while looking taken.
            raise EnquiryError("That person cannot see website enquiries, so it cannot be theirs.")
        row.assigned_to_id = assignee
    if "status" in changes and changes["status"] is not None:
        new = WebsiteEnquiryStatus(changes["status"]).value
        if new != WebsiteEnquiryStatus.NEW.value and row.handled_at is None:
            row.handled_at = datetime.now(UTC)
            row.handled_by_id = by.id
        row.status = new
    if "notes" in changes:
        row.notes = (changes["notes"] or "").strip() or None
    await session.flush()
    await session.refresh(row)
    return row


# ── telling people ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Teller:
    """What telling needs, gathered from app state while the request is alive."""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient | None
    mailer: GraphMailer | None


def _came_from(row: WebsiteEnquiry) -> str | None:
    tags = row.attribution or {}
    if tags.get("gclid") or tags.get("utm_source"):
        bits = [tags.get("utm_source") or "google ads", tags.get("utm_medium"), tags.get("utm_campaign")]
        return " / ".join(b for b in bits if b)
    return row.referrer or None


def _mail_html(row: WebsiteEnquiry, link: str) -> str:
    e = html.escape
    # Every value is escaped here, once, as it is turned into markup: the whole
    # message is typed by a stranger on the internet.
    facts = [
        ("Name", e(row.name)),
        ("Company", e(row.company) if row.company else None),
        ("Email", f'<a href="mailto:{e(row.email)}">{e(row.email)}</a>' if row.email else None),
        ("Phone", f'<a href="tel:{e(row.phone)}">{e(row.phone)}</a>' if row.phone else None),
        ("Interested in", e(", ".join(row.topics)) if row.topics else None),
        ("Sent from", e(row.page) if row.page else None),
        ("Landed on", e(row.landing) if row.landing else None),
        ("Came from", e(_came_from(row)) if _came_from(row) else None),
    ]
    rows = "".join(
        f"<tr><td style='padding:4px 16px 4px 0;color:#667;vertical-align:top'>{label}</td>"
        f"<td style='padding:4px 0'>{value}</td></tr>"
        for label, value in facts
        if value
    )
    message = e(row.message).replace("\n", "<br>")
    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#123'>"
        f"<p style='margin:0 0 12px'>A new enquiry came in through the contact form on hamdaz.com.</p>"
        f"<table style='border-collapse:collapse;margin:0 0 16px'>{rows}</table>"
        f"<p style='margin:0 0 6px;color:#667'>Message</p>"
        f"<p style='margin:0 0 20px;padding:12px 14px;background:#f4f6f8;border-radius:6px'>{message}</p>"
        f"<p style='margin:0'><a href='{e(link)}'>Open it in the ERP</a> to take it on and note what happened.</p>"
        "</div>"
    )


async def tell(teller: Teller, enquiry_id: uuid.UUID) -> None:
    """Raise it in the app, copy it to Teams, mail it. Runs after the response.

    Never raises: the visitor has been thanked and the row is stored, so a mail
    server or a webhook that is down costs a notice, not the enquiry. Each
    channel is tried whatever happened to the one before it.
    """
    settings = teller.settings
    try:
        async with teller.factory() as session:
            row = await session.get(WebsiteEnquiry, enquiry_id)
            if row is None:
                return
            path = f"/website-enquiries/{row.id}"
            link = f"{settings.frontend_url.rstrip('/')}{path}"
            who = f"{row.name}{f', {row.company}' if row.company else ''}"
            title = f"Website enquiry from {who}"
            body = row.message[:500]
            people = await people_who_can_see(session)
            await notify(
                session,
                users=people,
                kind=NotificationKind.WEBSITE_ENQUIRY,
                title=title,
                body=body,
                link=path,
                source="website_enquiry",
                source_id=str(row.id),
                payload={"email": row.email, "phone": row.phone, "topics": row.topics},
            )
            await session.commit()
            mail_html = _mail_html(row, link)
            reply_to = [row.email] if row.email else None
            facts = {
                "Email": row.email, "Phone": row.phone,
                "Interested in": ", ".join(row.topics), "Came from": _came_from(row),
            }
    except Exception:  # noqa: BLE001 - the enquiry is stored; this is only the telling
        logger.exception("website enquiry %s: could not raise the notification", enquiry_id)
        return

    if teller.http is not None and settings.website_enquiry_teams_webhook_url:
        await send_to_teams(
            teller.http, settings.website_enquiry_teams_webhook_url,
            title=title, body=body, facts=facts, link=link,
        )

    recipients = settings.website_enquiry_recipient_list
    if not settings.website_enquiry_sender or teller.mailer is None or not recipients:
        logger.info(
            "website enquiry %s: no email sent (%s)", enquiry_id,
            "WEBSITE_ENQUIRY_SENDER is not set" if not settings.website_enquiry_sender
            else "no mailer" if teller.mailer is None else "no recipients",
        )
        return
    try:
        await teller.mailer.send(
            sender=settings.website_enquiry_sender,
            recipients=recipients,
            subject=title,
            html=mail_html,
            reply_to=reply_to,
        )
    except (MailError, httpx.HTTPError) as exc:
        logger.warning("website enquiry %s: email failed: %s", enquiry_id, exc)
