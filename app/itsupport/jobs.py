"""The messages IT Support sends unasked, each behind its feature switch.

The user's shortlist (2026-11-18). Every job here:

* runs only while its feature is on (all ship off: ``features``);
* sends each thing once, ever (``sent.Ledger``, in the database), and logs
  what it sent (``activity``);
* never messages the CEO (the user, 2026-11-18: "do not add ceo now");
* with "Send check-ins to" set on the settings page (a trial), sends into that
  one chat instead, marked with whose it was;
* messages only the people the agent answers.

Rules write every message (no model): they are lists and facts, and must be
exact. The jobs:

* ``daily_asks`` — the status reminder's and the follow-up's questions;
* ``team_briefs`` — for a super admin or manager, the team's tasks due today;
* ``bcd_asks`` — a placeholder BCD, with the link to fix it on the list;
* ``work_changes`` — a new task assigned, a BCD moved (from the task reads);
* ``approval_pings`` — a costing approved, rejected or sent back;
* ``website_enquiries`` — a hamdaz.com enquiry assigned to somebody;
* ``leave_handovers`` — someone away with tasks closing today or tomorrow;
* ``delivery_watch`` — a customer PO due for delivery and not delivered.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select

from app.followups import service as fu
from app.itsupport import activity, asks, features, talk, tasklist, when
from app.itsupport.due import in_office_hours, short
from app.itsupport.lookups import link_base
from app.itsupport.status import Asked, Item
from app.models.user import User

if TYPE_CHECKING:
    from app.itsupport.work import Work
    from app.itsupport.worker import Chat, ITSupportWorker

logger = logging.getLogger("hamdaz.itsupport")

#: A heads-up older than this is not news any more.
FRESH: Final = timedelta(hours=24)


async def _ceo(w: ITSupportWorker) -> set[str]:
    assert w._factory is not None
    async with w._factory() as session:
        return {e.casefold() for e in await fu.ceo_emails(session)}


def _target(w: ITSupportWorker, chat: Chat, chats: list[Chat]) -> Chat | None:
    test_to = w._settings.it_support_due_test_to.strip().lower()
    if not test_to:
        return chat
    return next((c for c in chats if c.email == test_to), None)


async def send(w: ITSupportWorker, chat: Chat, chats: list[Chat], text: str, *, key: str,
               kind: str, detail: dict[str, Any] | None = None,
               asked: Asked | None = None, html_body: str | None = None) -> bool:
    """One message, once: to the person (or the trial chat), logged. With
    ``html_body`` that is what's sent (a list as tables) and ``text`` is its
    first line, for the log."""
    if await w._sent.seen(key):
        return False
    target = _target(w, chat, chats)
    if target is None:
        logger.warning("it support: the trial address is not a chat it answers; %s held", key)
        return False
    body = html_body if html_body is not None else talk.to_html(text)
    if target is not chat:
        text = f"(test: this is for {chat.first_name})\n{text}"
        body = talk.to_html(f"(test: this is for {chat.first_name})") + "<br>" + body
    await w._say(target, body)
    if asked is not None:
        target.asked = asked
    await w._sent.mark(key, email=chat.email, kind=kind, detail=detail)
    await activity.record(w._factory, email=chat.email, kind=activity.SENT,
                          summary=f"{kind}: {text.splitlines()[0][:200]}",
                          detail={"key": key, **(detail or {}),
                                  **({"sent_to": target.email} if target is not chat else {})})
    logger.info("it support: %s to %s%s", kind, chat.email,
                f" (sent to {target.email})" if target is not chat else "")
    return True


async def _known(w: ITSupportWorker, chat: Chat) -> None:
    """Their teams and what they oversee, read now if no chat turn has yet
    (a brief due before they've said anything still goes)."""
    if not chat.teams and not chat.oversees and w._factory is not None:
        try:
            await w._person(chat)
        except Exception:  # noqa: BLE001 - the next round tries again
            logger.exception("it support: reading %s's teams failed", chat.email)


async def _users(w: ITSupportWorker, chats: list[Chat]) -> dict[str, Any]:
    assert w._factory is not None
    async with w._factory() as session:
        found = (await session.execute(
            select(func.lower(User.email), User.id)
            .where(func.lower(User.email).in_([c.email for c in chats])))).all()
    return dict(found)


# ── the daily asks ─────────────────────────────────────────────────────


async def daily_asks(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "daily_asks") or w._factory is None or not chats:
        return
    now = datetime.now(UTC)
    if not in_office_hours(s, now):
        return
    from app.mailwatch.availability import away_reasons
    from app.reminders import service as reminders

    ceo = await _ceo(w)
    users = await _users(w, chats)
    plans: list[tuple[Chat, str, list[asks.Row], str]] = []
    async with w._factory() as session:
        fs = await fu.get_settings(session)
        rs = await reminders.get_settings(session)
        kinds = [k for k, ready in ((asks.STATUS, asks.status_ready(fs, rs, now)),
                                    (asks.REASON, asks.reason_ready(fs, now))) if ready]
        if not kinds:
            return
        day = asks.today(fs, now)
        away = await away_reasons(session, list(users.values()))
        for chat in chats:
            uid = users.get(chat.email)
            if uid is None or chat.email in ceo:
                continue
            for kind in kinds:
                key = f"{kind}-ask:{chat.email}:{day.isoformat()}"
                if await w._sent.seen(key):
                    continue
                if uid in away:
                    await w._sent.mark(key, email=chat.email, kind=f"{kind}-ask",
                                       detail={"skipped": away[uid]})
                    continue
                found = await asks.rows(session, kind, uid, fs, day,
                                        trials=features.trial(s))
                if found:
                    plans.append((chat, kind, found, key))
                else:
                    await w._sent.mark(key, email=chat.email, kind=f"{kind}-ask",
                                       detail={"skipped": "nothing to ask"})
        risks: dict[str, str] = {}
        if features.on(s, "risk_notes"):
            from app.itsupport.risks import notes

            ids = {r.task_id: r.due_at for _, kind, found, _ in plans if kind == asks.STATUS
                   for r in found}
            risks = await notes(session, ids, now)
    for chat, kind, found, key in plans:
        noted = risks if kind == asks.STATUS else None
        text, asked = asks.message(kind, chat.first_name, found, now, risks=noted)
        await send(w, chat, chats, text, key=key, kind=f"{kind}-ask",
                   detail={"tasks": [r.task_id for r in found]}, asked=asked,
                   html_body=asks.html_message(kind, chat.first_name, found, now, risks=noted))


# ── the team's day, for a super admin or manager ───────────────────────


async def team_briefs(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "team_brief") or w._factory is None:
        return
    from app.itsupport import brief as brief_mod

    now = datetime.now(UTC)
    day = brief_mod.due_now(s, now)
    if day is None:
        return
    ceo = await _ceo(w)
    for chat in chats:
        await _known(w, chat)
        if not chat.team_first or not chat.oversees or chat.email in ceo:
            continue
        key = f"team-brief:{chat.email}:{day.isoformat()}"
        if await w._sent.seen(key):
            continue
        teams: list[tuple[str, list[tasklist.Row]]] = []
        for team_id, name in chat.oversees[:3]:
            data = await w._team_data(team_id)
            if data is None:
                continue
            members, held = data
            teams.append((name, tasklist.collect(members, held, now)))
        shown = tasklist.brief(chat.first_name, teams, now)
        if shown is None:
            await w._sent.mark(key, email=chat.email, kind="team-brief",
                               detail={"skipped": "nothing due"})
            continue
        await send(w, chat, chats, shown.line, key=key, kind="team-brief",
                   detail={"tasks": [i.task_id for i in shown.items]},
                   asked=Asked("team", shown.items, now), html_body=shown.html)


# ── placeholder BCDs ───────────────────────────────────────────────────


async def bcd_asks(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "bcd_asks") or w._factory is None:
        return
    now = datetime.now(UTC)
    if not in_office_hours(s, now):
        return
    from app.bcd.service import edit_link
    from app.models.bcd_check import BcdCheck, BcdCheckStatus

    by_email = {c.email: c for c in chats}
    ceo = await _ceo(w)
    async with w._factory() as session:
        pending = (await session.scalars(select(BcdCheck).where(
            BcdCheck.status == BcdCheckStatus.PENDING, BcdCheck.asked_at.is_not(None),
            BcdCheck.asked_at >= now - FRESH))).all()
        found = [(r.id, r.task_id, r.task_title, r.task_url, r.placeholder_bcd,
                  r.assignee_email.lower()) for r in pending]
    for check_id, task_id, title, url, placeholder, email in found:
        chat = by_email.get(email)
        if chat is None or email in ceo:
            continue
        shown = f" ({when.day_and_time(placeholder, now)})" if placeholder else ""
        fix = edit_link(url)
        text = (f"Hi {chat.first_name}, {task_id} · {short(title, 55)} still has its BCD at "
                f"the time it was assigned{shown}. Could you set the real closing date on the "
                f"list?" + (f" Edit it here: {fix}" if fix else "") +
                "\nIf it's actually right, just say \"BCD correct\".")
        await send(w, chat, chats, text, key=f"bcd-ask:{check_id}", kind="bcd-ask",
                   detail={"task": task_id},
                   asked=Asked("bcd", [Item(1, task_id, title, str(check_id))], now))


# ── a new task, a BCD moved ────────────────────────────────────────────


async def work_changes(w: ITSupportWorker, chat: Chat, chats: list[Chat], old: Work,
                       new: Work) -> None:
    """From two reads of the person's tasks: what's new, whose BCD moved."""
    s = w._settings
    new_on, moved_on = features.on(s, "new_task"), features.on(s, "bcd_moved")
    if not (new_on or moved_on) or old.read_at is None:
        return
    from app.reminders.service import is_done

    now = datetime.now(UTC)
    before = {t.id: t for t in old.tasks}
    base = link_base(s)
    for task in new.tasks:
        if is_done(task):
            continue
        due = fu.due_of(task)
        was = before.get(task.id)
        if was is None and new_on:
            created = when.parse_when(task.created_at)
            if created is None or now - created > FRESH:
                continue
            files = ("It has files on the list." if task.has_attachments else
                     "No files on it yet: send me the RFQ and I'll pull out the items.")
            if task.id in new.held:
                closes = (", BCD not set yet (it shows the assignment time): set the real "
                          "closing date on the list")
            else:
                closes = f", closes {when.day_and_time(due, now)}" if due else ""
            text = (f"New one for you: {task.id} · {short((task.title or '').strip(), 60)}"
                    f"{closes}" + (f" · {task.end_user}" if task.end_user else "") +
                    f". {files} Your tasks: {base}/proposals/my-tasks")
            await send(w, chat, chats, text, key=f"new-task:{task.id}", kind="new-task",
                       detail={"task": task.id})
        elif was is not None and moved_on and due is not None and task.id not in old.held \
                and task.id not in new.held:
            before_due = fu.due_of(was)
            if before_due is None or abs((due - before_due).total_seconds()) <= 60 \
                    or due < now:
                continue
            text = (f"Heads-up: {task.id} · {short((task.title or '').strip(), 55)} now closes "
                    f"{when.day_and_time(due, now)} (was {when.day_and_time(before_due, now)}).")
            await send(w, chat, chats, text, key=f"bcd-moved:{task.id}:{due.isoformat()}",
                       kind="bcd-moved", detail={"task": task.id})


# ── costings decided ───────────────────────────────────────────────────

_DECIDED: Final = {"approve": "approved", "reject": "rejected", "rework": "sent back for changes"}


async def approval_pings(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "approval_pings") or w._factory is None:
        return
    from app.models.quoting import QuoteRequest, QuoteReview

    now = datetime.now(UTC)
    since = w._watermarks.setdefault("reviews", now)
    by_email = {c.email: c for c in chats}
    ceo = await _ceo(w)
    async with w._factory() as session:
        found = (await session.execute(
            select(QuoteReview, QuoteRequest)
            .join(QuoteRequest, QuoteReview.request_id == QuoteRequest.id)
            .where(QuoteReview.created_at > since,
                   QuoteReview.action.in_(list(_DECIDED)))
            .order_by(QuoteReview.created_at))).all()
        rows = []
        for review, request in found:
            owners = {u.email.lower() for u in (request.created_by, request.assigned_to)
                      if u is not None and u.id != review.reviewer_id and u.email}
            rows.append((review.id, review.action, review.note,
                         review.reviewer.display_name if review.reviewer else "an approver",
                         request.id, request.reference or "", request.title, owners,
                         review.created_at))
    base = link_base(s)
    for rid, action, note, by, request_id, reference, title, owners, at in rows:
        w._watermarks["reviews"] = max(w._watermarks["reviews"], at)
        for email in owners:
            chat = by_email.get(email)
            if chat is None or email in ceo:
                continue
            said = _DECIDED.get(str(action), str(action))
            text = (f"Your costing {reference} · {short(title, 50)} was {said} by {by}"
                    + (f": \"{note.strip()[:300]}\"" if note and note.strip() else "") +
                    f". {base}/quote-requests/{request_id}")
            await send(w, chat, chats, text, key=f"review:{rid}:{email}", kind="approval",
                       detail={"costing": reference, "action": str(action)})


# ── website enquiries ──────────────────────────────────────────────────


async def website_enquiries(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "website_enquiries") or w._factory is None:
        return
    from app.models.website import WebsiteEnquiry

    now = datetime.now(UTC)
    by_email = {c.email: c for c in chats}
    ceo = await _ceo(w)
    async with w._factory() as session:
        found = (await session.scalars(select(WebsiteEnquiry).where(
            WebsiteEnquiry.assigned_to_id.is_not(None),
            WebsiteEnquiry.updated_at >= now - FRESH))).all()
        rows = [(e.id, e.assigned_to_id, (e.assigned_to.email or "").lower()
                 if e.assigned_to else "", e.name, e.company, e.topics, e.message, e.status)
                for e in found]
    base = link_base(s)
    for eid, assignee, email, name, company, topics, message, state in rows:
        chat = by_email.get(email)
        if chat is None or email in ceo or state in ("spam", "won", "lost"):
            continue
        about = ", ".join(topics or []) if isinstance(topics, list) else str(topics or "")
        text = (f"A website enquiry is with you: {name}" + (f" from {company}" if company else "")
                + (f", about {about}" if about else "") +
                f": \"{' '.join((message or '').split())[:240]}\" {base}/website-enquiries/{eid}")
        await send(w, chat, chats, text, key=f"enquiry:{eid}:{assignee}", kind="enquiry",
                   detail={"enquiry": str(eid)})


# ── someone away with work closing ─────────────────────────────────────


async def leave_handovers(w: ITSupportWorker, chats: list[Chat]) -> None:
    s = w._settings
    if not features.on(s, "leave_handover") or w._factory is None:
        return
    from app.itsupport import brief as brief_mod
    from app.reminders.service import is_done

    now = datetime.now(UTC)
    day = brief_mod.due_now(s, now)
    if day is None:
        return
    ceo = await _ceo(w)
    today = now.astimezone(when.UAE).date()
    by_team: dict[Any, tuple[str, list[Chat]]] = {}
    for chat in chats:
        if chat.email in ceo:
            continue
        await _known(w, chat)
        if chat.team_first and features.on(s, "team_brief"):
            continue  # their team brief marks who's away
        for team_id, name in chat.oversees[:3]:
            by_team.setdefault(team_id, (name, []))[1].append(chat)
    for team_id, (_name, leads) in by_team.items():
        data = await w._team_data(team_id)
        if data is None:
            continue
        members, held = data
        for member in members:
            holder, away = member.name, member.away
            if not away:
                continue
            soon = []
            for task in member.tasks:
                due = fu.due_of(task)
                if due is None or is_done(task) or task.id in held or due < now:
                    continue
                if (due.astimezone(when.UAE).date() - today).days <= 1:
                    soon.append((due, task))
            if not soon:
                continue
            soon.sort(key=lambda x: x[0])
            listed = "; ".join(f"{t.id} · {short((t.title or '').strip(), 45)}, "
                               f"{when.day_and_time(d, now)}" for d, t in soon)
            for lead in leads:
                if lead.email == member.email.lower() or lead.first_name == holder:
                    continue
                text = (f"{holder} is away today ({away}). Closing soon: {listed}. "
                        "Who's picking these up?")
                await send(w, lead, chats, text,
                           key=f"handover:{team_id}:{holder}:{day.isoformat()}:{lead.email}",
                           kind="handover", detail={"away": holder,
                                                    "tasks": [t.id for _, t in soon]})


# ── 👎 on a reply ──────────────────────────────────────────────────────

_THUMBS_DOWN: Final = ("dislike", "👎", "thumbsdown", "thumbs down")


async def feedback(w: ITSupportWorker, chat: Chat, newest_first: list[dict[str, Any]]) -> None:
    """A 👎 on one of its replies, kept with what was asked, once."""
    if not features.on(w._settings, "feedback"):
        return
    for n, message in enumerate(newest_first):
        sender = ((message.get("from") or {}).get("user") or {}).get("id")
        if sender != w.me_id:
            continue
        downs = [r for r in message.get("reactions") or []
                 if any(t in f"{r.get('reactionType', '')} {r.get('displayName', '')}".casefold()
                        for t in _THUMBS_DOWN)]
        if not downs:
            continue
        key = f"feedback:{message.get('id')}"
        if await w._sent.seen(key):
            continue
        asked = next((talk.plain(m) for m in newest_first[n + 1:]
                      if ((m.get("from") or {}).get("user") or {}).get("id") != w.me_id), "")
        await w._sent.mark(key, email=chat.email, kind="feedback")
        await activity.record(w._factory, email=chat.email, kind=activity.FEEDBACK,
                              summary=f"👎 on: {talk.plain(message)[:300]}",
                              detail={"reply": talk.plain(message)[:2000],
                                      "asked": asked[:2000],
                                      "at": message.get("createdDateTime")}, ok=False)


# ── customer POs due for delivery ──────────────────────────────────────


async def delivery_watch(w: ITSupportWorker, chats: list[Chat]) -> None:
    """A PO whose delivery date is near (or just past) and not marked
    delivered, to its sales person, once per date; checked in office hours."""
    s = w._settings
    if not features.on(s, "delivery_watch") or w._sharepoint is None:
        return
    now = datetime.now(UTC)
    if not in_office_hours(s, now):
        return
    from app.itsupport import tracker

    ceo = await _ceo(w) if w._factory is not None else set()
    by_name: dict[str, Chat] = {}
    for c in chats:
        for name in {c.first_name, (c.display_name or "").split(" ")[0], c.display_name or ""}:
            if name:
                by_name[name.casefold()] = c
    today = now.astimezone(when.UAE).date()
    for po, day in tracker.due_deliveries(await tracker.rows(w._sharepoint, s), today):
        owner = str(po.get("SalesPerson") or "").strip().casefold()
        chat = by_name.get(owner) or by_name.get(owner.split(" ")[0] if owner else "")
        if chat is None or chat.email in ceo:
            continue
        gap = (day - today).days
        when_said = "today" if gap == 0 else f"{-gap} day(s) ago" if gap < 0 else \
            f"on {day:%a %d %b}"
        stage = po.get("Stage") or po.get("Status") or "no stage"
        text = (f"PO {po.ref} · {short(str(po.get('Title') or ''), 50)} for "
                f"{po.get('Customer') or 'the customer'} is due for delivery {when_said} and "
                f"isn't marked delivered (stage: {stage}).")
        await send(w, chat, chats, text, key=f"delivery:{po.ref}:{day.isoformat()}",
                   kind="delivery", detail={"po": po.ref})
