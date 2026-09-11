"""The pipeline: one email in, one decision out.

Read the mail, decide what it is, look for it in the Proposals list, and then
do the one thing that follows:

* a **tender or proposal** that matches nothing is new work — raise a task and
  give it to whoever the live ranking says is next;
* a **tender or proposal** that matches something is that thing again. If the
  mail says it has reopened, tell whoever holds it; otherwise it is a duplicate
  and nobody needs waking;
* a **negotiation or order** always concerns work that exists. Never raise a
  task; find the row, find who holds it, tell them;
* **general** is a circular. Record it and leave it.

Two things are worth stating plainly.

**Nothing reaches SharePoint unless a super admin switched it on.** With
``create_in_sharepoint`` off — which is how it ships — a message that would
raise a task instead records the exact field-for-field payload it would have
posted, and stops. The decision is complete and inspectable; only the last step
is missing. That is not a test mode, it is how this runs until its judgement
has been watched.

**Every message gets a row, including the ignored ones.** The question people
ask of a pipeline like this is never "what did you do" — it is "why did
nothing happen when I sent that", and only the ignored rows answer it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import live as live_scores
from app.core.config import Settings
from app.intake.classifier import Classification, Classifier, ClassifierError
from app.intake.graph_mail import MailReader, sender_allowed, summarise_message
from app.intake.matcher import Match, Matcher
from app.models.intake import (
    IntakeAction,
    IntakeMessage,
    IntakeSettings,
    IntakeStatus,
    MailCategory,
)
from app.models.notification import NotificationKind
from app.models.proposal_index import ProposalIndexItem
from app.models.team import Team
from app.models.user import User
from app.notifications import service as notifications
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.intake")


class IntakeError(Exception):
    """Refused. The message is safe to show a super admin."""


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> IntakeSettings:
    row = await session.get(IntakeSettings, 1)
    if row is None:
        row = IntakeSettings(id=1)
        session.add(row)
        await session.flush()
    return row


def _clean_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        value = str(raw).strip().lower().lstrip("@")
        if value and value not in out:
            out.append(value)
    return out


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> IntakeSettings:
    """Change what is watched and what may happen. Only the keys given change."""
    row = await get_settings(session)

    for field in (
        "enabled", "create_in_sharepoint", "update_negotiation",
        "notify_in_app", "notify_teams",
    ):
        if changes.get(field) is not None:
            setattr(row, field, bool(changes[field]))

    if (mailbox := changes.get("mailbox")) is not None:
        row.mailbox = str(mailbox).strip().lower()
        # A new mailbox starts from now. Replaying somebody's inbox from the
        # beginning would, with creation on, mean a year of tasks.
        row.delta_link = None
        row.watch_from = datetime.now(UTC)
    if "allowed_senders" in changes:
        row.allowed_senders = _clean_list(changes["allowed_senders"])
    if "allowed_domains" in changes:
        wanted = _clean_list(changes["allowed_domains"])
        # A bare label can never match: the part after the "@" always carries a
        # dot, so "adnoc" would sit in the settings looking configured and
        # admit nobody. Refused loudly rather than saved silently — a filter
        # that quietly matches nothing is the worst way for this to be wrong.
        if bad := [d for d in wanted if "." not in d]:
            raise IntakeError(
                "A domain needs its full name, e.g. 'adnoc.ae' rather than "
                f"'{bad[0]}' — otherwise it matches no address at all."
            )
        row.allowed_domains = wanted
    if (value := changes.get("negotiation_value")) is not None:
        row.negotiation_value = str(value).strip() or "Yes"
    if "teams_webhook_url" in changes:
        url = (changes["teams_webhook_url"] or "").strip()
        row.teams_webhook_url = url or None
    if (team_id := changes.get("assign_team_id")) is not None:
        if await session.get(Team, team_id) is None:
            raise IntakeError("No team with that id")
        row.assign_team_id = team_id
    for field in ("match_threshold", "classify_threshold"):
        if changes.get(field) is not None:
            value = float(changes[field])
            if not 0 <= value <= 1:
                raise IntakeError(f"{field} must be between 0 and 1")
            setattr(row, field, value)
    if changes.get("poll_seconds") is not None:
        row.poll_seconds = max(15, int(changes["poll_seconds"]))

    row.updated_by_id = actor_id
    await session.flush()
    return row


# ── recording what arrived ─────────────────────────────────────────────


async def record(session: AsyncSession, raw: dict[str, Any]) -> IntakeMessage | None:
    """Store one message if it is new. ``None`` when we have seen it.

    Graph will deliver the same message twice — a notification and a poll
    racing, or a webhook retried. Returning None on the second is what keeps
    that from becoming two tasks.
    """
    summary = summarise_message(raw)
    if not summary["graph_message_id"]:
        return None
    seen = await session.scalar(
        select(IntakeMessage).where(
            IntakeMessage.graph_message_id == summary["graph_message_id"]
        )
    )
    if seen is not None:
        return None
    row = IntakeMessage(**summary, status=IntakeStatus.RECEIVED)
    session.add(row)
    await session.flush()
    return row


async def pending(session: AsyncSession, *, limit: int = 20) -> list[IntakeMessage]:
    return list(
        (
            await session.scalars(
                select(IntakeMessage)
                .where(IntakeMessage.status == IntakeStatus.RECEIVED)
                .order_by(IntakeMessage.received_at.asc().nulls_last())
                .limit(limit)
            )
        ).all()
    )


async def listing(
    session: AsyncSession,
    *,
    status: str | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[IntakeMessage], int]:
    query = select(IntakeMessage)
    if status:
        query = query.where(IntakeMessage.status == status)
    if category:
        query = query.where(IntakeMessage.category == category)
    total = int(
        await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    )
    rows = (
        await session.scalars(
            query.order_by(IntakeMessage.received_at.desc().nulls_last())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


# ── the decision ───────────────────────────────────────────────────────


def _ignore(row: IntakeMessage, why: str) -> IntakeMessage:
    row.status = IntakeStatus.IGNORED
    row.action = IntakeAction.NONE
    row.reasoning = why
    row.processed_at = datetime.now(UTC)
    return row


async def _holder(session: AsyncSession, item: ProposalIndexItem) -> User | None:
    """The user who holds a Proposals row, by the email SharePoint has for them.

    Email is the only identifier the two systems agree on — display names
    collide and change, and lookup ids are local to the site.
    """
    if not item.assigned_email:
        return None
    return await session.scalar(
        select(User).where(func.lower(User.email) == item.assigned_email.lower())
    )


def _task_fields(
    classification: Classification, *, assignee_lookup_id: str | None
) -> dict[str, Any]:
    """Exactly what would be posted to the Proposals list.

    Built whether or not it is going to be sent, because with writing switched
    off this *is* the output: a super admin reading the row should see the row
    that would exist, not a description of one.
    """
    fields: dict[str, Any] = {
        "Title": classification.title[:255] or "(from email)",
        "Status": "Not Started",
    }
    if classification.customer:
        fields["EndUser"] = classification.customer[:255]
    if classification.deadline:
        fields["BCD"] = classification.deadline
    if classification.references:
        fields["Remarks"] = "Ref: " + ", ".join(classification.references[:5])
    if classification.summary:
        fields["WorkingNotes"] = classification.summary[:2000]
    if assignee_lookup_id:
        fields["AssignedToLookupId"] = assignee_lookup_id
    return fields


async def process(
    session: AsyncSession,
    row: IntakeMessage,
    *,
    settings: Settings,
    intake: IntakeSettings,
    classifier: Classifier,
    matcher: Matcher,
    sharepoint: SharePointProposals,
    http: httpx.AsyncClient,
) -> IntakeMessage:
    """Take one recorded message all the way to a decision.

    Never raises. Anything that goes wrong lands on the row as ``failed`` with
    the reason, because a pipeline that drops a message on an exception is a
    pipeline nobody can audit — and the message can be retried once the cause
    is fixed.
    """
    try:
        return await _process(
            session, row,
            settings=settings, intake=intake, classifier=classifier,
            matcher=matcher, sharepoint=sharepoint, http=http,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        logger.exception("intake %s failed", row.id)
        row.status = IntakeStatus.FAILED
        row.error = f"{type(exc).__name__}: {exc}"
        row.processed_at = datetime.now(UTC)
        await session.flush()
        return row


async def _process(
    session: AsyncSession,
    row: IntakeMessage,
    *,
    settings: Settings,
    intake: IntakeSettings,
    classifier: Classifier,
    matcher: Matcher,
    sharepoint: SharePointProposals,
    http: httpx.AsyncClient,
) -> IntakeMessage:
    if not sender_allowed(
        row.sender_email,
        addresses=list(intake.allowed_senders or []),
        domains=list(intake.allowed_domains or []),
    ):
        await session.flush()
        return _ignore(row, f"Not from a watched sender ({row.sender_email}).")

    try:
        found = await classifier.classify(
            subject=row.subject, body=row.body, sender=row.sender_email
        )
    except ClassifierError as exc:
        row.status = IntakeStatus.FAILED
        row.error = str(exc)
        row.processed_at = datetime.now(UTC)
        await session.flush()
        return row

    row.category = found.category
    row.confidence = found.confidence
    row.is_reopened = found.is_reopened
    row.extracted = found.as_dict()
    row.reasoning = found.reasoning
    row.cost_usd = found.cost_usd
    row.status = IntakeStatus.CLASSIFIED
    await session.flush()

    if found.confidence < float(intake.classify_threshold):
        return _ignore(
            row,
            f"{found.reasoning} (too unsure to act on: "
            f"{found.confidence:.2f} < {float(intake.classify_threshold):.2f})",
        )
    if found.category in (MailCategory.GENERAL, MailCategory.UNKNOWN):
        return _ignore(row, found.reasoning or "Nothing to do.")

    match = await matcher.find(
        session, found, subject=row.subject, threshold=float(intake.match_threshold)
    )
    row.candidates = match.considered
    row.match_confidence = match.confidence
    row.match_reason = match.reason
    row.matched_item_id = match.item.item_id if match.found else None
    await session.flush()

    if match.found:
        return await _known_work(
            session, row, found, match,
            intake=intake, sharepoint=sharepoint, http=http,
        )
    if found.creates_work:
        return await _new_work(
            session, row, found,
            settings=settings, intake=intake, sharepoint=sharepoint, http=http,
        )
    # A negotiation or an order about something we cannot find. Not ignorable —
    # somebody is waiting on a reply — but there is nobody to route it to, so
    # it is left for a person with the reason recorded.
    return _ignore(
        row,
        f"A {found.category} that matches nothing in the list: {match.reason}",
    )


async def _mark_negotiation(
    session: AsyncSession,
    row: IntakeMessage,
    item: ProposalIndexItem,
    *,
    intake: IntakeSettings,
    sharepoint: SharePointProposals,
) -> str:
    """Set ``Negotiation`` on the matched task, so a flow watching the list fires.

    This is the one place a negotiation reaches anything outside this system.
    A negotiation never creates a task — the work already exists — so nothing
    changes in SharePoint on its own, and a flow triggered by "an item was
    created or modified" has nothing to react to. Marking the column gives it
    one.

    **Skipped when the column already says so.** A negotiation is usually a
    thread, and this runs per message: re-writing the same value on every reply
    would re-fire the flow each time and fill the row's history with changes
    that changed nothing. The cost is that a second round of negotiation on the
    same bid does not raise a second trigger — worth knowing, and the setting
    to change if that turns out to be the wrong trade.

    Returns the action to record. Never raises: failing to mark the column must
    not lose the notification that goes with it.
    """
    wanted = (intake.negotiation_value or "Yes").strip()
    fields = {"Negotiation": wanted}
    row.would_update = {"item_id": item.item_id, "fields": fields}

    if (item.negotiation or "").strip().casefold() == wanted.casefold():
        row.match_reason = (
            f"{row.match_reason or ''} "
            f"(Negotiation already {wanted}; not written again)"
        ).strip()
        return IntakeAction.NEGOTIATION_NOTICE

    if not intake.update_negotiation:
        # The switch, off. Everything is decided and the payload is on the row;
        # only the write is missing.
        row.status = IntakeStatus.SIMULATED
        return IntakeAction.NEGOTIATION_NOTICE

    try:
        await sharepoint.update_task(item.item_id, fields)
    except Exception as exc:  # noqa: BLE001 - the notice still goes out
        logger.warning("could not mark %s as negotiation: %s", item.item_id, exc)
        row.error = f"Could not set Negotiation: {exc}"
        return IntakeAction.NEGOTIATION_NOTICE

    # Kept locally too, so a second email in the same thread sees the new value
    # without waiting for the next mirror sync.
    item.negotiation = wanted
    await session.flush()
    return IntakeAction.MARKED_NEGOTIATION


async def _known_work(
    session: AsyncSession,
    row: IntakeMessage,
    found: Classification,
    match: Match,
    *,
    intake: IntakeSettings,
    sharepoint: SharePointProposals,
    http: httpx.AsyncClient,
) -> IntakeMessage:
    """The work exists. Tell whoever holds it — and never raise a second task."""
    item = match.item
    assert item is not None

    reopened = found.is_reopened
    if found.category == MailCategory.NEGOTIATION:
        kind, action = NotificationKind.NEGOTIATION, IntakeAction.NEGOTIATION_NOTICE
        headline = f"Negotiation update — {item.title}"
        action = await _mark_negotiation(
            session, row, item, intake=intake, sharepoint=sharepoint
        )
    elif found.category == MailCategory.ORDER:
        kind, action = NotificationKind.ORDER, IntakeAction.ORDER_NOTICE
        headline = f"Order received — {item.title}"
    elif reopened:
        kind, action = NotificationKind.TASK_REOPENED, IntakeAction.REOPENED_NOTICE
        headline = f"Reopened — {item.title}"
    else:
        # Already in the list and nothing new said about it. Recorded, and
        # nobody woken: an intake that pings the team for every acknowledgement
        # is one they mute within a week.
        row.action = IntakeAction.DUPLICATE
        row.status = IntakeStatus.IGNORED
        row.processed_at = datetime.now(UTC)
        await session.flush()
        return row

    holder = await _holder(session, item)
    facts = {
        "Customer": item.end_user or found.customer,
        "Quote": item.quote_no,
        "Deadline": item.deadline.isoformat() if item.deadline else found.deadline,
        "Holder": item.assigned_name,
        "From": row.sender_email,
    }
    made = await notifications.notify_and_post(
        session,
        http,
        users=[holder] if holder else [],
        webhook_url=intake.teams_webhook_url,
        kind=kind,
        title=headline,
        body=found.summary or row.subject,
        facts=facts,
        link=row.web_link,
        source="intake",
        source_id=str(row.id),
        in_app=intake.notify_in_app and holder is not None,
        to_teams=intake.notify_teams,
    )

    row.action = action
    # Not overwritten when the negotiation branch already marked this as a
    # withheld write: the notice went out, but a write did not, and "simulated"
    # is the more honest of the two things that happened.
    if row.status != IntakeStatus.SIMULATED:
        row.status = IntakeStatus.ACTIONED
    row.assigned_user_id = holder.id if holder else None
    row.assigned_reason = "Holds the matched task" if holder else None
    row.notified_user_ids = [str(n.user_id) for n in made]
    row.notified_teams = bool(intake.notify_teams and intake.teams_webhook_url)
    row.processed_at = datetime.now(UTC)
    await session.flush()
    return row


async def _new_work(
    session: AsyncSession,
    row: IntakeMessage,
    found: Classification,
    *,
    settings: Settings,
    intake: IntakeSettings,
    sharepoint: SharePointProposals,
    http: httpx.AsyncClient,
) -> IntakeMessage:
    """Nothing in the list is this. Raise it, and give it to whoever is next."""
    team = (
        await session.get(Team, intake.assign_team_id)
        if intake.assign_team_id
        else None
    )
    nominated = await live_scores.next_up(session, team=team, limit=1)
    winner = nominated[0] if nominated else None

    fields = _task_fields(found, assignee_lookup_id=winner.sharepoint_lookup_id if winner else None)
    row.would_create = fields
    row.assigned_user_id = winner.user_id if winner else None
    row.assigned_reason = (
        f"Ranked 1 of the {team.name if team else 'organisation'} by workload"
        if winner
        else "Nobody is currently assignable"
    )

    if not intake.create_in_sharepoint:
        # The switch that ships off. Everything above is decided and recorded;
        # only the write is missing, and the row shows exactly what it would be.
        row.status = IntakeStatus.SIMULATED
        row.action = IntakeAction.NONE
        row.processed_at = datetime.now(UTC)
        await session.flush()
        return row

    created = await sharepoint.create_task(fields)
    row.created_item_id = str(created.id)
    row.action = IntakeAction.CREATED_TASK
    row.status = IntakeStatus.ACTIONED

    holder = (
        await session.get(User, winner.user_id) if winner and winner.user_id else None
    )
    made = await notifications.notify_and_post(
        session,
        http,
        users=[holder] if holder else [],
        webhook_url=intake.teams_webhook_url,
        kind=NotificationKind.TASK_ASSIGNED,
        title=f"New {found.category} — {found.title}",
        body=found.summary,
        facts={
            "Customer": found.customer,
            "Deadline": found.deadline,
            "Reference": ", ".join(found.references[:3]),
            "Assigned to": winner.display_name if winner else "nobody",
            "Why": row.assigned_reason,
        },
        link=created.web_url,
        source="intake",
        source_id=str(row.id),
        in_app=intake.notify_in_app and holder is not None,
        to_teams=intake.notify_teams,
    )
    row.notified_user_ids = [str(n.user_id) for n in made]
    row.notified_teams = bool(intake.notify_teams and intake.teams_webhook_url)
    row.processed_at = datetime.now(UTC)
    await session.flush()

    # The list just changed in a way that changes who is next, and the whole
    # point of the live ranking is that it is not stale by the time the next
    # tender arrives — which may be a minute from now.
    await live_scores.recompute(session, team=team, reason="intake")
    await session.flush()
    return row
