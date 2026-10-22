"""Mail watch, step 1: receive the CEO's mails as they arrive, and keep them.

**Told, not polling.** Graph posts to the webhook when a mail lands in the
watched inbox; the notification only says "something arrived", so the answer
is one delta read from the stored bookmark, which returns just the new mail.
There is no timer reading the inbox. Two things still run without being told:
the subscription is renewed before Graph drops it (about three days), and one
catch-up runs when the server starts, for mail that arrived while it was down.

**Only the watched senders are kept.** Everything else in the mailbox is
looked at for its sender and nothing more — no body, no row.

Read only: nothing is marked read, moved, or written anywhere outside this
app's own tables.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.model import AgentModel
from app.core.config import Settings
from app.core.mail import MailError
from app.followups.service import ceo_emails
from app.intake.graph_mail import MailReader, sender_allowed, summarise_message
from app.mailwatch import matching
from app.mailwatch.classify import CONFIDENTIAL, Reading, classify
from app.models.mailwatch import MailWatchSettings, WatchedMail
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.mailwatch")

#: The mailbox watched until somebody changes it in the settings.
DEFAULT_MAILBOX: Final = "sebin@hamdaz.com"

#: A subscription this close to expiry is renewed.
RENEW_WITHIN: Final = timedelta(hours=24)


class MailWatchError(Exception):
    """Something a super admin can be shown as it is."""


def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return None


def _clean_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if email and ("@" not in email or " " in email):
        raise MailWatchError(f"{raw!r} is not an email address.")
    return email


def notification_url(settings: Settings) -> str:
    """Where Graph posts. Graph only posts to an https address it can reach."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base.startswith("https://"):
        raise MailWatchError(
            "Graph only posts to a public https address. Set PUBLIC_BASE_URL to "
            "this app's public URL — on a laptop there is no subscription, use "
            "catch-up instead."
        )
    return f"{base}{settings.api_prefix}/mailwatch/notifications"


# ── settings ────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> MailWatchSettings:
    row = await session.get(MailWatchSettings, 1)
    if row is None:
        row = MailWatchSettings(
            id=1, mailbox=DEFAULT_MAILBOX, senders=[], watch_from=datetime.now(UTC)
        )
        session.add(row)
        await session.flush()
    return row


async def watched_senders(session: AsyncSession, row: MailWatchSettings) -> list[str]:
    """The settings' own list, or whoever holds the CEO role."""
    if row.senders:
        return list(row.senders)
    return sorted(await ceo_emails(session))


_EDITABLE: Final = frozenset({"enabled", "mailbox", "senders", "use_model"})


async def update_settings(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    changes: dict[str, Any],
    reader: MailReader,
    settings: Settings,
) -> MailWatchSettings:
    """Apply a change, and move the subscription with it.

    A new mailbox starts from now: its bookmark is dropped and its history is
    not read. Switching on subscribes; switching off unsubscribes. A failure
    to subscribe does not undo the change — it is recorded as the last error,
    and the subscribe route retries it.
    """
    row = await get_settings(session)
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise MailWatchError(f"Not a setting: {', '.join(sorted(unknown))}.")

    if "mailbox" in changes:
        mailbox = _clean_email(changes["mailbox"] or "")
        if not mailbox:
            raise MailWatchError("The mailbox can't be blank.")
        if mailbox != row.mailbox:
            await _drop_subscription(row, reader)
            row.mailbox = mailbox
            row.delta_link = None
            row.watch_from = datetime.now(UTC)
    if "senders" in changes:
        row.senders = sorted({e for e in (_clean_email(s) for s in changes["senders"] or []) if e})
    if "use_model" in changes:
        row.use_model = bool(changes["use_model"])
    if "enabled" in changes:
        row.enabled = bool(changes["enabled"])
    row.updated_by_id = actor_id

    if not row.enabled:
        await _drop_subscription(row, reader)
    elif not row.subscription_id:
        try:
            await subscribe(session, reader=reader, settings=settings)
        except MailWatchError as exc:
            row.last_error = str(exc)
    return row


# ── the subscription ────────────────────────────────────────────────


async def subscribe(
    session: AsyncSession, *, reader: MailReader, settings: Settings
) -> MailWatchSettings:
    """Ask Graph to post when mail lands in the watched inbox. Replaces any old one."""
    row = await get_settings(session)
    url = notification_url(settings)
    await _drop_subscription(row, reader)
    secret = secrets.token_urlsafe(24)
    try:
        created = await reader.subscribe(row.mailbox, notification_url=url, secret=secret)
    except MailError as exc:
        raise MailWatchError(str(exc)) from exc
    row.subscription_id = created.get("id")
    row.subscription_secret = secret
    row.subscription_mailbox = row.mailbox
    row.subscription_expires_at = _parse(created.get("expirationDateTime"))
    row.last_error = None
    logger.info("mail watch subscribed to %s", row.mailbox)
    return row


async def unsubscribe(session: AsyncSession, *, reader: MailReader) -> MailWatchSettings:
    row = await get_settings(session)
    await _drop_subscription(row, reader)
    return row


async def _drop_subscription(row: MailWatchSettings, reader: MailReader) -> None:
    if row.subscription_id:
        # Best effort: one Graph has already dropped is gone either way.
        with contextlib.suppress(Exception):
            await reader.unsubscribe(row.subscription_id)
    row.subscription_id = None
    row.subscription_secret = None
    row.subscription_mailbox = None
    row.subscription_expires_at = None


async def keep_subscribed(
    session: AsyncSession, *, reader: MailReader, settings: Settings
) -> None:
    """Renew before Graph drops it; subscribe again if it is gone or missing.

    Only while switched on and only where Graph can reach us — on a laptop
    there is nothing to keep.
    """
    row = await get_settings(session)
    if not row.enabled:
        return
    try:
        notification_url(settings)
    except MailWatchError:
        return
    now = datetime.now(UTC)
    if row.subscription_id and row.subscription_expires_at and (
        row.subscription_expires_at - now > RENEW_WITHIN
    ):
        return
    if row.subscription_id and row.subscription_expires_at and row.subscription_expires_at > now:
        try:
            renewed = await reader.renew(row.subscription_id)
            row.subscription_expires_at = _parse(renewed.get("expirationDateTime"))
            return
        except MailError as exc:
            logger.warning("mail watch renewal failed, subscribing again: %s", exc)
    try:
        await subscribe(session, reader=reader, settings=settings)
    except MailWatchError as exc:
        row.last_error = str(exc)
        logger.warning("mail watch could not subscribe: %s", exc)


def accepts(row: MailWatchSettings, client_state: str | None) -> bool:
    """Whether a notification is ours: it carries the secret we gave Graph."""
    return bool(
        row.enabled
        and row.subscription_secret
        and client_state
        and secrets.compare_digest(client_state, row.subscription_secret)
    )


# ── reading ─────────────────────────────────────────────────────────


@dataclass
class ReadReport:
    mailbox: str = ""
    #: What the delta returned, and how many were from a watched sender.
    seen: int = 0
    kept: int = 0
    #: Already stored — the same mail reported twice.
    already: int = 0
    senders: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def admitted(raw: dict[str, Any], *, senders: list[str], since: datetime | None) -> bool:
    """Whether one delta entry is a mail we keep."""
    if "@removed" in raw or raw.get("isDraft"):
        return False
    summary = summarise_message(raw)
    if since is not None and summary["received_at"] is not None and summary["received_at"] < since:
        return False
    return sender_allowed(summary["sender_email"], addresses=senders, domains=[])


async def read_new(
    session: AsyncSession,
    *,
    reader: MailReader,
    model: AgentModel | None,
    sharepoint: SharePointProposals | None,
) -> ReadReport:
    """Read what arrived since the bookmark, keep the watched senders' mail.

    The bookmark only moves after the rows are added, in the same transaction,
    so a failure part-way reads the same mail again next time rather than
    skipping it — and the unique key makes that harmless.
    """
    row = await get_settings(session)
    report = ReadReport(mailbox=row.mailbox)
    report.senders = await watched_senders(session, row)
    if not row.mailbox:
        report.error = "No mailbox is set."
        return report
    if not report.senders:
        report.error = "Nobody to watch: no senders set and nobody holds the CEO role."
        row.last_error = report.error
        return report
    if row.watch_from is None:
        row.watch_from = datetime.now(UTC)

    try:
        messages, cursor = await reader.delta(
            row.mailbox, delta_link=row.delta_link, since=row.watch_from
        )
    except MailError as exc:
        report.error = str(exc)
        row.last_error = report.error
        return report

    report.seen = len(messages)
    for raw in messages:
        if not admitted(raw, senders=report.senders, since=row.watch_from):
            continue
        if await _keep(
            session, reader, row.mailbox, raw,
            model=model, use_model=row.use_model, sharepoint=sharepoint,
        ):
            report.kept += 1
        else:
            report.already += 1

    if cursor:
        row.delta_link = cursor
    row.last_read_at = datetime.now(UTC)
    row.last_error = None
    if report.kept:
        logger.info("mail watch kept %d new mail(s) from %s", report.kept, row.mailbox)
    return report


async def _read_whole(
    reader: MailReader, mailbox: str, raw: dict[str, Any]
) -> tuple[dict[str, Any], list[str], str | None]:
    """The mail with its body, and its attachment names. A failure keeps what
    the delta already said rather than losing the mail; the subject says a lot."""
    message_id = raw.get("id") or ""
    try:
        full = await reader.message(mailbox, message_id)
        names: list[str] = []
        if full.get("hasAttachments"):
            names = await reader.attachment_names(mailbox, message_id, immutable=False)
        return full, names, None
    except MailError as exc:
        logger.warning("mail watch could not read the whole of a mail: %s", exc)
        return raw, [], str(exc)


async def _reading(
    summary: dict[str, Any], names: list[str], *, model: AgentModel | None, use_model: bool
) -> Reading:
    return await classify(
        subject=summary["subject"], body=summary["body"], attachments=names,
        model=model, use_model=use_model,
    )


def _columns(summary: dict[str, Any], names: list[str], reading: Reading) -> dict[str, Any]:
    """What a row holds. A confidential mail keeps its label, date and sender —
    not its subject, body or attachment names."""
    secret = reading.category == CONFIDENTIAL
    return {
        "conversation_id": summary["conversation_id"],
        "internet_message_id": summary["internet_message_id"],
        "received_at": summary["received_at"],
        "sender_email": summary["sender_email"],
        "sender_name": summary["sender_name"],
        "subject": None if secret else summary["subject"],
        "body": None if secret else summary["body"],
        "has_attachments": summary["has_attachments"],
        "attachment_names": [] if secret else names,
        "web_link": summary["web_link"],
        "category": reading.category,
        "kind": reading.kind,
        "stage": reading.stage,
        "decided_by": reading.decided_by,
        "reason": reading.reason,
        "confidence": reading.confidence,
        "title": reading.title,
        "customer": (reading.customer or "")[:200] or None,
        "reference_numbers": reading.references,
        "ariba_doc": reading.ariba_doc,
        "closing_at": reading.closing_at,
        "original_sender": reading.original_sender,
        "model_cost_usd": reading.model_cost_usd,
        "classified_at": datetime.now(UTC),
        "classify_error": reading.error,
    }


async def _match(
    session: AsyncSession,
    summary: dict[str, Any],
    reading: Reading,
    *,
    mail_id: uuid.UUID | None,
    sharepoint: SharePointProposals | None,
    model: AgentModel | None,
    use_model: bool,
) -> matching.Match | None:
    try:
        return await matching.match(
            session,
            category=reading.category, stage=reading.stage, title=reading.title,
            subject=summary["subject"], customer=reading.customer,
            references=reading.references, ariba_doc=reading.ariba_doc,
            conversation_id=summary["conversation_id"], received_at=summary["received_at"],
            mail_id=mail_id, sharepoint=sharepoint, model=model, use_model=use_model,
        )
    except Exception as exc:  # noqa: BLE001 - a mail is kept even when matching fails
        logger.exception("mail watch matching failed")
        return matching.Match(verdict=matching.UNSURE, error=f"matching failed: {exc}"[:500])


def _match_columns(found: matching.Match | None) -> dict[str, Any]:
    if found is None:
        return {
            "verdict": None, "matched_item_id": None, "match_method": None,
            "match_score": None, "match_reason": None, "match_candidates": [],
            "task": None, "duplicate_of_id": None, "matched_at": None, "match_error": None,
        }
    return {
        "verdict": found.verdict,
        "matched_item_id": found.item_id,
        "match_method": found.method,
        "match_score": found.score,
        "match_reason": found.reason,
        "match_candidates": [c.as_dict() for c in found.candidates],
        "task": found.task,
        "duplicate_of_id": found.duplicate_of_id,
        "matched_at": datetime.now(UTC),
        "match_error": found.error,
    }


async def _keep(
    session: AsyncSession,
    reader: MailReader,
    mailbox: str,
    raw: dict[str, Any],
    *,
    model: AgentModel | None,
    use_model: bool,
    sharepoint: SharePointProposals | None,
) -> bool:
    """Store and classify one mail. False if it was already stored."""
    message_id = raw.get("id") or ""
    exists = await session.scalar(
        select(WatchedMail.id).where(
            WatchedMail.mailbox == mailbox, WatchedMail.graph_message_id == message_id
        )
    )
    if exists:
        return False
    full, names, _ = await _read_whole(reader, mailbox, raw)
    summary = summarise_message(full)
    reading = await _reading(summary, names, model=model, use_model=use_model)
    found = await _match(
        session, summary, reading, mail_id=None,
        sharepoint=sharepoint, model=model, use_model=use_model,
    )
    if found is not None:
        reading.model_cost_usd += Decimal(str(found.model_cost_usd))
    result = await session.execute(
        insert(WatchedMail)
        .values(
            mailbox=mailbox, graph_message_id=message_id,
            **_columns(summary, names, reading), **_match_columns(found),
        )
        .on_conflict_do_nothing(constraint="uq_watched_mails_message")
    )
    return bool(result.rowcount)


async def reclassify(
    session: AsyncSession,
    mail: WatchedMail,
    *,
    reader: MailReader,
    model: AgentModel | None,
    use_model: bool,
    sharepoint: SharePointProposals | None,
) -> Reading:
    """Read the mail from the mailbox again, classify it and match it afresh.

    From the mailbox rather than the row, because a mail filed as confidential
    kept nothing to re-read — and one that turns out not to be gets its
    subject and body back.
    """
    full, names, error = await _read_whole(reader, mail.mailbox, {"id": mail.graph_message_id})
    if error:
        raise MailWatchError(f"Could not read the mail again: {error}")
    summary = summarise_message(full)
    reading = await _reading(summary, names, model=model, use_model=use_model)
    found = await _match(
        session, summary, reading, mail_id=mail.id,
        sharepoint=sharepoint, model=model, use_model=use_model,
    )
    if found is not None:
        reading.model_cost_usd += Decimal(str(found.model_cost_usd))
    for key, value in {**_columns(summary, names, reading), **_match_columns(found)}.items():
        setattr(mail, key, value)
    return reading


@dataclass
class TrialRow:
    received_at: datetime | None
    #: Hidden for a confidential mail.
    subject: str | None
    web_link: str | None
    attachments: int
    reading: Reading
    match: matching.Match | None = None


async def trial(
    session: AsyncSession,
    *,
    reader: MailReader,
    model: AgentModel | None,
    days: int,
    limit: int,
    use_model: bool,
    sharepoint: SharePointProposals | None,
) -> tuple[MailWatchSettings, list[str], datetime, list[TrialRow]]:
    """Classify the watched senders' mail of the last ``days`` days, as if it
    had just arrived. Stores nothing."""
    row = await get_settings(session)
    senders = await watched_senders(session, row)
    if not senders:
        raise MailWatchError("Nobody to watch: no senders set and nobody holds the CEO role.")
    since = datetime.now(UTC) - timedelta(days=days)
    try:
        messages = await reader.from_senders(row.mailbox, senders, since=since, limit=limit)
    except MailError as exc:
        raise MailWatchError(str(exc)) from exc
    rows: list[TrialRow] = []
    #: Within the trial, the first mail seen for each event, oldest first — a
    #: later one with no task is a duplicate of it, as it would be live.
    first_seen: dict[str, TrialRow] = {}
    for raw in reversed(messages):
        names: list[str] = []
        if raw.get("hasAttachments"):
            with contextlib.suppress(MailError):
                names = await reader.attachment_names(row.mailbox, raw["id"], immutable=False)
        summary = summarise_message(raw)
        reading = await _reading(summary, names, model=model, use_model=use_model)
        found = await _match(
            session, summary, reading, mail_id=None,
            sharepoint=sharepoint, model=model, use_model=use_model,
        )
        trial_row = TrialRow(
            received_at=summary["received_at"],
            subject=None if reading.category == CONFIDENTIAL else summary["subject"],
            web_link=summary["web_link"],
            attachments=len(names),
            reading=reading,
            match=found,
        )
        if found is not None:
            reading.model_cost_usd += Decimal(str(found.model_cost_usd))
            keys = [k for k in (reading.ariba_doc, *matching._refs(reading.references)[0]) if k]
            earlier = next((first_seen[k] for k in keys if k in first_seen), None)
            if earlier is not None and found.verdict == matching.NEW:
                found.verdict = matching.DUPLICATE_MAIL
                found.reason = "no task yet, but an earlier mail in this trial brought the same tender"
            for k in keys:
                first_seen.setdefault(k, trial_row)
        rows.append(trial_row)
    rows.reverse()
    return row, senders, since, rows


async def recent(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    stage: str | None = None,
    verdict: str | None = None,
) -> list[WatchedMail]:
    query = select(WatchedMail)
    if verdict:
        query = query.where(WatchedMail.verdict == verdict)
    if category:
        query = query.where(WatchedMail.category == category)
    if stage:
        query = query.where(WatchedMail.stage == stage)
    rows = await session.scalars(
        query.order_by(WatchedMail.received_at.desc().nulls_last())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    return list(rows.all())
