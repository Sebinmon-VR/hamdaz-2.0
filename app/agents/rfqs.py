"""After the RFQs go: the supplier's reply, their quote, the report, approval.

The user's wish (2026-10-15): once an agent has sent the RFQs it watches for
the suppliers' replies; when one comes it tells its owner in Teams, and when
they say take it, it does what presales does by hand in Quote requests — the
supplier's quote onto the task's quote request, read into lines, priced, the
selling & costing report made — and when they say send it, it goes for
approval.

Each sent RFQ is a row (``AgentRfq``), not a step of a run that would have to
stay alive for days: the worker looks for replies every few minutes, and each
answer — in Teams or in the app — moves one row on. Nothing here happens
without its owner's yes, and it acts as them: their mailbox, their quote
request, their roles, checked as the Quote requests pages check them.

**Test mode** (AGENT_MAIL_REDIRECT, or any server but the live one): the
RFQs went to the test inbox, so a "reply" is a reply in that inbox to one of
those test emails; and approval is never sent — the card says who it would
have gone to instead. The quote request itself is made, as a draft, and says
it was made in a test.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import engine
from app.models.agent import AgentRfq, AgentRun, UserAgent
from app.models.notification import NotificationKind
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User
from app.notifications.service import notify

logger = logging.getLogger("hamdaz.agents.rfqs")

#: Replies are looked for this long after an RFQ goes.
WATCH_FOR: Final = timedelta(days=21)
#: The margin a taken quote is priced at, as a share of the selling price,
#: until its owner changes it on the quote request.
DEFAULT_MARGIN: Final = Decimal(15)
#: Files on a reply worth reading as a supplier quote.
_QUOTE_FILES: Final = (".pdf", ".xlsx", ".xls", ".xlsm", ".csv", ".docx", ".png", ".jpg", ".jpeg")
_PREFIX: Final = re.compile(r"^\s*((re|aw|sv|fw|fwd|antw)\s*:\s*)+", re.IGNORECASE)

#: Follow-ups running in this process, kept so they are not collected mid-way.
_JOBS: set[asyncio.Task[None]] = set()


def _core(subject: str) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", subject or "")).strip().casefold()


def test_mode(settings: Any) -> bool:
    return not engine.sends_for_real(settings)


# ── recording what was sent ────────────────────────────────────────────


def record(session: AsyncSession, run: AgentRun, sent: list[dict[str, Any]]) -> int:
    """A row for every RFQ that went, to watch for its reply."""
    now = datetime.now(UTC)
    count = 0
    for entry in sent:
        if entry.get("status") != "sent":
            continue
        session.add(AgentRfq(
            run_id=run.id, agent_id=run.agent_id, owner_id=run.owner_id, task_id=run.task_id,
            task_title=run.task_title, supplier_name=entry.get("to_name"), supplier_email=entry["to_email"],
            sent_to=entry.get("redirected_to") or entry["to_email"], subject=entry.get("sent_subject") or entry["subject"],
            sent_at=now, status="waiting",
        ))
        count += 1
    return count


# ── noticing replies ───────────────────────────────────────────────────


def _is_reply(rfq: AgentRfq, message: dict[str, Any]) -> bool:
    sender = str(((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
    if sender != rfq.sent_to.lower():
        return False
    received = engine.parse_time(message.get("receivedDateTime"))
    if received is None or received <= rfq.sent_at:
        return False
    subject = str(message.get("subject") or "")
    if rfq.sent_to.lower() != rfq.supplier_email.lower():
        # Test mode: the test inbox sent it and gets it. Only a reply to that
        # very email counts — not the copy of it that landed in the inbox.
        return _core(subject) == _core(rfq.subject) and subject.strip() != rfq.subject.strip()
    # A supplier answering often starts a new thread; anything from them after the RFQ counts.
    return True


async def watch(session: AsyncSession, deps: engine.Deps, *, run_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Look in each owner's inbox for replies to their waiting RFQs. The rows that got one."""
    reader = getattr(deps.state, "mail_reader", None)
    if reader is None:
        return []
    query = select(AgentRfq).where(AgentRfq.status == "waiting", AgentRfq.sent_at >= datetime.now(UTC) - WATCH_FOR)
    if run_id is not None:
        query = query.where(AgentRfq.run_id == run_id)
    rows = list((await session.scalars(query)).all())
    found: list[uuid.UUID] = []
    by_owner: dict[uuid.UUID, list[AgentRfq]] = {}
    for row in rows:
        by_owner.setdefault(row.owner_id, []).append(row)
    taken = set((await session.scalars(
        select(AgentRfq.reply["message_id"].astext).where(AgentRfq.reply.is_not(None))
    )).all())
    for owner_id, waiting in by_owner.items():
        owner = await session.get(User, owner_id)
        mailbox = owner and (owner.entra_object_id or owner.email)
        if not mailbox:
            continue
        since = min(r.sent_at for r in waiting).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            messages = await reader.inbox_since(mailbox, since, top=100)
        except Exception:  # noqa: BLE001 — one mailbox that cannot be read must not stop the rest
            logger.warning("could not read %s's inbox for RFQ replies", owner.email, exc_info=True)
            continue
        for rfq in waiting:
            # Oldest reply first: the first answer is the answer.
            for message in sorted(messages, key=lambda m: str(m.get("receivedDateTime") or "")):
                if message.get("id") in taken or not _is_reply(rfq, message):
                    continue
                taken.add(message["id"])
                rfq.status = "replied"
                rfq.reply = {
                    "message_id": message["id"],
                    "from": ((message.get("from") or {}).get("emailAddress") or {}),
                    "subject": message.get("subject"),
                    "received": message.get("receivedDateTime"),
                    "preview": str(message.get("bodyPreview") or "")[:600],
                    "has_attachments": bool(message.get("hasAttachments")),
                    "attachments": [],
                }
                if message.get("hasAttachments"):
                    rfq.reply["attachments"] = await _names(reader, mailbox, message["id"])
                rfq.note = "Replied — waiting for you to say whether to take their quote."
                found.append(rfq.id)
                break
    await session.flush()
    return found


async def _names(reader: Any, mailbox: str, message_id: str) -> list[str]:
    try:
        return [name for name, _type, _content in await reader.attachments(mailbox, message_id)]
    except Exception:  # noqa: BLE001 — the names are a nicety
        return []


async def tell_replied(deps: engine.Deps, rfq_ids: list[uuid.UUID]) -> None:
    """The bell and a Teams card for each new reply: take their quote, or not."""
    for rfq_id in rfq_ids:
        async with deps.factory() as session:
            rfq = await session.get(AgentRfq, rfq_id)
            if rfq is None:
                continue
            run, user, agent = await _context(session, rfq)
            if run is None or user is None:
                continue
            who = rfq.supplier_name or rfq.supplier_email
            await notify(
                session, users=[user.id], kind=NotificationKind.GENERAL,
                title=f"{who} replied to your RFQ", body=(rfq.reply or {}).get("preview") or rfq.task_title or "",
                link=engine.run_link(run), source="agent_rfq_reply", source_id=str(rfq.id),
            )
            await session.commit()
            name = agent.name if agent else "Your agent"
            face = engine.face_url(deps.settings, agent)
            await engine.post_card(
                deps, user, run, step_id=f"rfq-{rfq.id}", title=f"{who} replied", agent=name, task=rfq.task_title,
                card=reply_card(agent=name, rfq=rfq, face=face, link=_app_link(deps, run),
                                test=test_mode(deps.settings)),
            )


# ── their quote, onto the quote request ────────────────────────────────


def schedule(coro: Any) -> None:
    """Run a follow-up in the background, so Teams and the app get their answer at once."""
    job = asyncio.create_task(coro)
    _JOBS.add(job)
    job.add_done_callback(_JOBS.discard)


async def take(deps: engine.Deps, rfq_id: uuid.UUID, go: bool) -> None:
    """Their quote, taken — or not. Taken: onto the task's quote request, priced, the report made."""
    async with deps.factory() as session:
        rfq = await session.get(AgentRfq, rfq_id)
        if rfq is None or rfq.status not in ("replied", "failed"):
            return
        run, user, agent = await _context(session, rfq)
        if run is None or user is None:
            return
        who = rfq.supplier_name or rfq.supplier_email
        if not go:
            rfq.status, rfq.note = "declined", "You said not to take this quote."
            await session.commit()
            return
        rfq.status, rfq.note = "taking", "Putting their quote on the quote request…"
        await session.commit()
        try:
            await _take(session, deps, rfq, user)
        except _Halt as why:
            await session.rollback()
            rfq = await session.get(AgentRfq, rfq_id)
            rfq.status, rfq.note = "failed", str(why)
            await session.commit()
            await _tell(deps, rfq, f"Could not take {who}'s quote", str(why))
            return
        except Exception as exc:  # noqa: BLE001 — the row must say what happened
            logger.exception("taking RFQ reply %s failed", rfq_id)
            await session.rollback()
            rfq = await session.get(AgentRfq, rfq_id)
            rfq.status, rfq.note = "failed", f"Something went wrong taking their quote: {type(exc).__name__}: {exc}"[:500]
            await session.commit()
            await _tell(deps, rfq, f"Could not take {who}'s quote", rfq.note)
            return
        await session.commit()
        run, user, agent = await _context(session, rfq)
        name = agent.name if agent else "Your agent"
        await engine.post_card(
            deps, user, run, step_id=f"qa-{rfq.id}", title=f"{who}'s quote is ready to send for approval",
            agent=name, task=rfq.task_title,
            card=approval_card(agent=name, rfq=rfq, face=engine.face_url(deps.settings, agent),
                               link=_quote_link(deps, rfq), test=test_mode(deps.settings)),
        )


class _Halt(Exception):
    """A follow-up cannot go on; the message says why, for its owner."""


async def _take(session: AsyncSession, deps: engine.Deps, rfq: AgentRfq, user: User) -> None:
    from fastapi import HTTPException
    from starlette.datastructures import Headers, UploadFile

    from app.access import service as access_service
    from app.proposals.mirror import to_task as proposal_task
    from app.quoting import router as quote_router
    from app.quoting import service as quote_service
    from app.quoting.fx import zoho_rate
    from app.quoting.schemas import QuoteRequestIn, SupplierEmailIn
    from app.roles.service import global_role_keys

    state, settings = deps.state, deps.settings
    roles = await global_role_keys(session, user.id)
    if not await access_service.can_reach(session, user_id=user.id, global_roles=roles, module_key="quote_requests"):
        raise _Halt("You do not have Quote requests, so I cannot raise one for you.")
    if not rfq.task_id:
        raise _Halt("This run had no Proposals task, so there is no quote request to put it on.")

    # 1. The task's quote request: the one already raised, or a new one.
    existing = (await quote_service.quotes_for_tasks(session, [rfq.task_id])).get(rfq.task_id)
    if existing is not None and existing.status.value not in ("draft", "changes_requested", "in_negotiation"):
        raise _Halt(f"The quote request for this task is already {existing.status.value.replace('_', ' ')}.")
    if existing is None:
        row = await session.get(ProposalIndexItem, rfq.task_id)
        if row is None or row.deleted:
            raise _Halt("The task is no longer in the Proposals list.")
        task = proposal_task(row)
        payload = quote_service.payload_from_task(task)
        if test_mode(settings):
            payload["notes"] = "Made by the agent in a test — no supplier was written to.\n\n" + (payload.get("notes") or "")
        try:
            made = await quote_router._raise_quote(
                session, QuoteRequestIn(**payload), user=user, roles=roles, team=settings.nova_team_slug,
                zoho=state.zoho, rates=state.win_rates, task=task,
            )
        except HTTPException as exc:
            raise _Halt(f"The quote request could not be raised: {exc.detail}") from exc
        request = await quote_service.get(session, made.id)
    else:
        request = existing
    rfq.quote_request_id = request.id

    # 2. What the supplier sent.
    owner_box = user.entra_object_id or user.email
    reply = rfq.reply or {}
    files = [
        f for f in await state.mail_reader.attachments(owner_box, reply["message_id"])
        if f[0].lower().endswith(_QUOTE_FILES)
    ]
    if not files:
        raise _Halt(
            "Their reply has no quote file I can read (PDF, Excel, Word or an image). "
            "Open the quote request and add their prices yourself."
        )
    uploads = [
        UploadFile(file=io.BytesIO(content), filename=name, headers=Headers({"content-type": ctype or "application/octet-stream"}))
        for name, ctype, content in files[:12]
    ]
    try:
        await quote_router.attach_suppliers(
            request.id, user, roles, session, state.quote_extractor, state.quote_drive, state.zoho,
            files=uploads, currency=None,
        )
    except HTTPException as exc:
        raise _Halt(f"Their quote could not be read: {exc.detail}") from exc
    # Their email too, so the approvers see what they said. A nicety: it may not file.
    try:
        await quote_router.attach_supplier_email(
            request.id, SupplierEmailIn(message_id=reply["message_id"], notes="The supplier's reply, taken by the agent."),
            user, roles, session, state.mail_reader, state.quote_drive,
        )
    except Exception:  # noqa: BLE001
        logger.info("RFQ %s: the reply email was not filed with the quote", rfq.id, exc_info=True)

    # 3. Priced from their offer, when nobody has chosen a supplier yet.
    request = await quote_service.get(session, request.id)
    names = {f[0] for f in files}
    offers = [q for q in (request.comparison.quotes if request.comparison else []) if getattr(q, "file_name", None) in names]
    offer = next((q for q in offers if q.items), None)
    chose = False
    if offer is not None and request.selected_supplier_quote_id is None:
        try:
            await quote_service.select_supplier(
                session, request, user=user, supplier_quote_id=offer.id, margin_percent=DEFAULT_MARGIN,
                rates=lambda ours, theirs: zoho_rate(state.zoho, quote_currency=ours, supplier_currency=theirs),
                roles=roles,
            )
            await quote_router._comparison_follows(request, state.zoho, state.quote_extractor)
            chose = True
        except Exception as exc:  # noqa: BLE001 — the quote is on; pricing can be done by hand
            logger.warning("RFQ %s: could not price from the offer: %s", rfq.id, exc)

    # 4. The selling & costing report.
    report = await quote_router._report(request, state.zoho)
    rfq.report = {
        "currency": report.currency,
        "quoted": str(report.quoted_price.amount),
        "landed": str(report.landed_total.amount),
        "margin": str(report.gross_margin.amount),
        "margin_percent": str(report.gross_margin_percent) if report.gross_margin_percent is not None else None,
        "lines": len(report.lines),
        "supplier": report.supplier.name,
        "recommendation": report.recommendation,
        "warnings": list(report.warnings)[:5],
        "priced_from_this": chose,
        "ready": quote_service.why_not_submit(request) is None,
        "missing": quote_service.why_not_submit(request),
    }
    rfq.status = "taken"
    rfq.note = (
        f"Their quote is on the quote request, priced at {DEFAULT_MARGIN}% margin, and the report is made."
        if chose else "Their quote is on the quote request beside the others; the quote is still priced from the supplier chosen before."
    )
    await session.flush()


# ── approval ───────────────────────────────────────────────────────────


async def submit(deps: engine.Deps, rfq_id: uuid.UUID, go: bool) -> None:
    """Send the quote request for approval — or, in test mode, say who it would have gone to."""
    from fastapi import HTTPException

    from app.quoting import router as quote_router
    from app.quoting import service as quote_service
    from app.roles.service import global_role_keys

    async with deps.factory() as session:
        rfq = await session.get(AgentRfq, rfq_id)
        if rfq is None or rfq.status != "taken" or rfq.quote_request_id is None:
            return
        run, user, agent = await _context(session, rfq)
        if run is None or user is None:
            return
        if not go:
            rfq.note = "Not sent for approval yet, as you said. Send it from the quote request when you are ready."
            await session.commit()
            return
        request = await quote_service.get(session, rfq.quote_request_id)
        missing = quote_service.why_not_submit(request)
        if missing:
            rfq.note = f"Not sent for approval: {missing}"
            await session.commit()
            await _tell(deps, rfq, "Not sent for approval yet", rfq.note)
            return
        if test_mode(deps.settings):
            people = await quote_service.approvers_to_notify(session, request.team_id)
            names = ", ".join(getattr(p, "email", "") for p in people) or "nobody (no approvers are set)"
            rfq.note = f"Test mode — not sent for approval. It would have gone to: {names}."
            await session.commit()
            await _tell(deps, rfq, "Test mode: approval not sent", rfq.note)
            return
        roles = await global_role_keys(session, user.id)
        state = deps.state
        try:
            await quote_router.submit(
                request.id, user, roles, session, state.quote_mailer, state.zoho, state.quote_drive, state.mail_reader,
            )
        except HTTPException as exc:
            await session.rollback()
            rfq = await session.get(AgentRfq, rfq_id)
            rfq.note = f"Could not send it for approval: {exc.detail}"
            await session.commit()
            await _tell(deps, rfq, "Could not send for approval", rfq.note)
            return
        rfq.status, rfq.note = "submitted", "Sent for approval, with the costing report."
        await session.commit()
        await _tell(deps, rfq, "Sent for approval", "The approvers have it, with the selling & costing report.")


# ── small things ───────────────────────────────────────────────────────


async def _context(session: AsyncSession, rfq: AgentRfq) -> tuple[AgentRun | None, User | None, UserAgent | None]:
    return (
        await session.get(AgentRun, rfq.run_id),
        await session.get(User, rfq.owner_id),
        await session.get(UserAgent, rfq.agent_id),
    )


async def _tell(deps: engine.Deps, rfq: AgentRfq, title: str, lead: str) -> None:
    async with deps.factory() as session:
        run, user, agent = await _context(session, rfq)
        if run is None or user is None:
            return
        await notify(session, users=[user.id], kind=NotificationKind.GENERAL, title=title, body=lead[:1000],
                     link=engine.run_link(run), source="agent_rfq", source_id=f"{rfq.id}:{title}"[:120])
        await session.commit()
        await engine.tell_in_teams(
            deps, user, run, agent=agent.name if agent else "Your agent", title=title, lead=lead,
            points=[f"{rfq.supplier_name or rfq.supplier_email} — {rfq.task_title or ''}"], button="Open in the app",
            face=engine.face_url(deps.settings, agent),
        )


def _app_link(deps: engine.Deps, run: AgentRun) -> str:
    return f"{deps.settings.frontend_url.rstrip('/')}{engine.run_link(run)}"


def _quote_link(deps: engine.Deps, rfq: AgentRfq) -> str:
    return f"{deps.settings.frontend_url.rstrip('/')}/quote-requests/{rfq.quote_request_id}"


def _test_banner(test: bool) -> list[dict[str, Any]]:
    if not test:
        return []
    return [{"type": "Container", "style": "warning", "items": [engine._text(
        "TEST MODE — nothing goes to a supplier or an approver.", weight="Bolder", size="Small")]}]


def reply_card(*, agent: str, rfq: AgentRfq, face: str | None, link: str, test: bool) -> dict[str, Any]:
    """A supplier replied: what they said, what they sent, and "Take their quote" / "Not this one"."""
    who = rfq.supplier_name or rfq.supplier_email
    reply = rfq.reply or {}
    body = engine._header(f"{who} replied to your RFQ", rfq.task_title, face) + _test_banner(test)
    body.append(engine._text(f"Subject: {reply.get('subject') or ''}", size="Small", isSubtle=True, spacing="Medium"))
    if reply.get("preview"):
        body.append(engine._text(str(reply["preview"])))
    names = reply.get("attachments") or []
    body.append(engine._text(
        ("Attached: " + ", ".join(names)) if names else "No files attached — I can only take a quote that comes as a file.",
        size="Small", weight="Bolder" if names else "Default", spacing="Medium",
    ))
    body.append(engine._text(
        "Take their quote and I'll put it on the task's quote request, read their prices, and make the costing report.",
        size="Small", isSubtle=True,
    ))
    return engine._card(body, [
        {"type": "Action.Submit", "title": "Take their quote", "style": "positive", "data": {"go": True}},
        {"type": "Action.Submit", "title": "Not this one", "data": {"go": False}},
        {"type": "Action.OpenUrl", "title": "Open in the app", "url": link},
    ])


def approval_card(*, agent: str, rfq: AgentRfq, face: str | None, link: str, test: bool) -> dict[str, Any]:
    """The quote request is priced and the report made: the figures, and "Send for approval" / "Not yet"."""
    who = rfq.supplier_name or rfq.supplier_email
    report = rfq.report or {}
    body = engine._header(f"{who}'s quote is on the quote request", rfq.task_title, face) + _test_banner(test)
    facts = [
        {"title": "Lines", "value": str(report.get("lines", 0))},
        {"title": "We sell at", "value": f"{report.get('quoted')} {report.get('currency')}"},
        {"title": "Landed cost", "value": f"{report.get('landed')} {report.get('currency')}"},
        {"title": "Margin", "value": f"{report.get('margin')} {report.get('currency')}"
                                     + (f" ({report['margin_percent']}%)" if report.get("margin_percent") else "")},
    ]
    body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})
    if report.get("recommendation"):
        body.append(engine._text(str(report["recommendation"]), size="Small"))
    for warning in report.get("warnings") or []:
        body.append(engine._text(f"⚠ {warning}", size="Small", color="Warning"))
    if rfq.note:
        body.append(engine._text(rfq.note, size="Small", isSubtle=True))
    if report.get("missing"):
        body.append(engine._text(f"Before it can go: {report['missing']}", size="Small", color="Attention"))
    return engine._card(body, [
        {"type": "Action.Submit", "title": "Test: who would approve?" if test else "Send for approval",
         "style": "positive", "data": {"go": True}},
        {"type": "Action.Submit", "title": "Not yet", "data": {"go": False}},
        {"type": "Action.OpenUrl", "title": "Open the quote request", "url": link},
    ])
