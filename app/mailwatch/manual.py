"""Tasks added to the Proposals list by hand → a card in Teams, like the mail
watch's own.

The mail watch posts a card for each new tender it assigns. A tender somebody
assigns themselves — a task added straight to the Proposals list — got none,
so the chat that follows new tenders (Quote Alerts) never heard of it. This
posts one for those, from the app's copy of the list (``proposal_index``).

**Which tasks.** New ones only: created since the switch went on
(``MailWatchTeams.manual_from``), still open, and not made by the mail watch —
those have their own card. A task is posted once (``mailwatch_manual_tasks``):
reassigning, renaming or editing it later posts nothing. The card waits
``SETTLE`` after the task is created, so a task the mail watch is still
recording is never taken for one added by hand.

**Where.** The test channel while ``manual_test_only`` (the default), else
wherever the other cards go — the chosen group chat or channel.

**What is written.** The card and the row recording it. The Proposals list is
only read.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.mailwatch import teams
from app.mailwatch.teams import (
    CARD_BYTES,
    FILE_ROWS,
    MAX_ATTEMPTS,
    NARROW,
    WIDE,
    TaskFiles,
    TeamsError,
    TeamsPoster,
    _badge,
    _caption,
    _countdown,
    _facts,
    _file_grid,
    _footer,
    _heading,
    _persona,
    _tabs,
    _testing_note,
    _text,
    _v,
)
from app.models.mailwatch import MailWatchManualTask, MailWatchTestTask
from app.models.proposal_index import ProposalIndexItem

logger = logging.getLogger("hamdaz.mailwatch.manual")

#: Beside the others (812_401 – 812_409), used by nothing else: one instance
#: posts at a time, so a task is never posted twice.
MANUAL_LOCK: Final = 812_410

#: How long after a task is created before it is taken as added by hand. The
#: mail watch creates its task in SharePoint and then records it here; the
#: list's copy can see the task in between.
SETTLE: Final = timedelta(minutes=3)

#: Cards per pass — a backlog goes out a few at a time, never as a flood.
PER_PASS: Final = 5

TEST, CHOSEN = "test", "chosen"


def item_url(settings: Settings, item_id: str) -> str:
    return f"{settings.sharepoint_proposals_list_url.rstrip('/')}/DispForm.aspx?ID={item_id}"


def _by_mail_watch(settings: Settings) -> Any:
    """The mail watch made this task (in the real Proposals list)."""
    return exists().where(
        MailWatchTestTask.list_item_id == ProposalIndexItem.item_id,
        MailWatchTestTask.list_id == settings.sharepoint_proposals_list_id,
    )


def added_by_hand(settings: Settings, *, since: datetime, now: datetime) -> Any:
    """The tasks that are due a card: new, open, and nobody's but a person's."""
    return and_(
        ProposalIndexItem.sp_created_at >= since,
        ProposalIndexItem.sp_created_at <= now - SETTLE,
        ProposalIndexItem.deleted.is_(False),
        ProposalIndexItem.is_open.is_(True),
        ~_by_mail_watch(settings),
    )


# ── the card ─────────────────────────────────────────────────────────


def manual_task_card(
    item: ProposalIndexItem,
    *,
    url: str,
    files: TaskFiles | None = None,
    people: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The card, with as many of the task's files as fit."""
    shown = FILE_ROWS
    while True:
        card = _card(item, url, files or TaskFiles(), people or {}, shown)
        if shown == 0 or len(json.dumps(card, ensure_ascii=False)) <= CARD_BYTES:
            return card
        shown -= 1


def _card(
    item: ProposalIndexItem,
    url: str,
    files: TaskFiles,
    people: dict[str, dict[str, str]],
    shown: int,
) -> dict[str, Any]:
    stage = (item.current_type or "").upper() or "Tender"
    badge = _badge("New task", icon="AddCircle", style="Good")
    created = (
        item.sp_created_at.astimezone(ZoneInfo("Asia/Dubai")).strftime("%d %b, %H:%M")
        if item.sp_created_at else ""
    )
    body: list[dict[str, Any]] = [
        *_testing_note("Posted to the test channel. A task added to the Proposals list by hand."),
        {
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [
                        _text(_v(item.title), size="Large", weight="Bolder"),
                        _caption(
                            f"{_v(item.end_user)} · added to the Proposals list"
                            + (f" {created} UAE" if created else ""),
                            spacing="None",
                        ),
                    ],
                },
                {"type": "Column", "width": "auto", "targetWidth": WIDE, "items": [badge]},
            ],
        },
        {**badge, "targetWidth": NARROW},
    ]

    bcd_day = item.bid_closing_date or item.deadline
    countdown = _countdown(bcd_day.isoformat() if bcd_day else None)
    bcd = (
        [_text(countdown[0], weight="Bolder", color=countdown[1], spacing="None"),
         _caption(bcd_day.strftime("%d %b %Y"), size="Small", spacing="None")]
        if countdown and bcd_day else [_text("Not given", isSubtle=True, spacing="None")]
    )
    body += _facts([
        ("Assigned to", [_persona(people.get((item.assigned_email or "").lower()),
                                  item.assigned_name)]),
        ("Assigned by", [_text("Hand — on the list", weight="Bolder", spacing="None")]),
        ("Type", [_text(stage, weight="Bolder", spacing="None")]),
        ("BCD", bcd),
    ])

    def day(value: Any) -> str:
        return _v(value and value.strftime("%d %b %Y"))

    tabs: list[tuple[str, str, str, list[dict[str, Any]]]] = [(
        "task", "Task", "TaskListLtr", [
            _caption(f"On the Proposals list (item {item.item_id}):", size="Small"),
            {
                "type": "FactSet", "spacing": "Small",
                "facts": [
                    {"title": "Title", "value": _v(item.title)},
                    {"title": "Assigned To", "value": _v(item.assigned_name)},
                    {"title": "Status", "value": _v(item.status)},
                    {"title": "CurrentType", "value": _v(item.current_type)},
                    {"title": "End User", "value": _v(item.end_user)},
                    {"title": "Start Date", "value": day(item.start_date)},
                    {"title": "BCD", "value": day(bcd_day)},
                    {"title": "Quote no.", "value": _v(item.quote_no)},
                    {"title": "Remarks", "value": _v(item.remarks)},
                ],
            },
        ],
    )]
    if files.files:
        tabs.append(("files", f"Files ({len(files.files)})", "Attach",
                     [_heading(f"Its files ({len(files.files)})"),
                      *_file_grid(files.files, shown)]))
    elif files.attachments_hidden:
        tabs.append(("files", "Files", "Attach",
                     [_caption("It has attachments — open the task to see them.", size="Small")]))
    body += _tabs(tabs)
    body += _footer(ai=False)

    actions = [{"type": "Action.OpenUrl", "title": "Open in Proposals", "url": url,
                "iconUrl": "icon:Open"}]
    if files.folder_url:
        actions.append({"type": "Action.OpenUrl", "title": "Open folder",
                        "url": files.folder_url, "iconUrl": "icon:Folder"})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": actions,
    }


# ── posting ──────────────────────────────────────────────────────────


async def _card_for(
    item: ProposalIndexItem, *, poster: TeamsPoster, settings: Settings
) -> dict[str, Any]:
    files = await poster.task_files({"item_id": item.item_id, "title": item.title})
    people = await poster.people([item.assigned_email or ""])
    return manual_task_card(item, url=item_url(settings, item.item_id), files=files, people=people)


async def post_pending(session: AsyncSession, *, poster: TeamsPoster, settings: Settings) -> int:
    """Post the hand-added tasks that are due a card; how many were posted.

    Each is recorded before it is posted and counted as an attempt, so a card
    that fails is tried at most ``MAX_ATTEMPTS`` times and one that succeeds
    is never posted again.
    """
    row = await teams.get_connection(session)
    if not row.post_manual_tasks or row.manual_from is None or not poster.can_read(row):
        return 0
    if not await session.scalar(select(func.pg_try_advisory_xact_lock(MANUAL_LOCK))):
        return 0
    now = datetime.now(UTC)
    tried = select(MailWatchManualTask.item_id).where(
        (MailWatchManualTask.posted_at.is_not(None))
        | (MailWatchManualTask.attempts >= MAX_ATTEMPTS)
    )
    items = (
        await session.scalars(
            select(ProposalIndexItem)
            .where(
                added_by_hand(settings, since=row.manual_from, now=now),
                ProposalIndexItem.item_id.not_in(tried),
            )
            .order_by(ProposalIndexItem.sp_created_at.asc())
            .limit(PER_PASS)
        )
    ).all()
    posted = 0
    for item in items:
        record = await session.get(MailWatchManualTask, item.item_id)
        if record is None:
            record = MailWatchManualTask(item_id=item.item_id, title=item.title or "", attempts=0)
            session.add(record)
        record.title = item.title or ""
        record.assigned_email = item.assigned_email
        record.attempts += 1
        try:
            card = await _card_for(item, poster=poster, settings=settings)
            record.message_id = await poster.post(
                row, "", card=card, test_only=row.manual_test_only
            )
        except TeamsError as exc:
            record.error = str(exc)[:500]
            row.last_error = str(exc)[:500]
            logger.warning("hand-added task %s: no Teams card: %s", item.item_id, exc)
            if "Connect again" in str(exc):
                break
            continue
        record.posted_at = datetime.now(UTC)
        record.posted_to = TEST if row.manual_test_only else CHOSEN
        record.error = None
        posted += 1
    return posted


async def latest(session: AsyncSession, settings: Settings) -> ProposalIndexItem | None:
    """The newest task added by hand, posted or not — for a sample card."""
    return await session.scalar(
        select(ProposalIndexItem)
        .where(
            ProposalIndexItem.deleted.is_(False),
            ProposalIndexItem.sp_created_at.is_not(None),
            ~_by_mail_watch(settings),
        )
        .order_by(ProposalIndexItem.sp_created_at.desc())
        .limit(1)
    )


async def post_sample(session: AsyncSession, *, poster: TeamsPoster, settings: Settings) -> str:
    """The newest hand-added task's card, to the test channel — always,
    whatever is chosen for real cards. Nothing is recorded: the task still
    gets its real card when it is due one."""
    row = await teams.get_connection(session)
    if not teams.is_connected(row):
        raise TeamsError("No Teams account is connected.")
    item = await latest(session, settings)
    if item is None:
        raise TeamsError("No task has been added to the Proposals list by hand.")
    card = await _card_for(item, poster=poster, settings=settings)
    return await poster.post(row, "", card=card, test_only=True)


async def counts(session: AsyncSession) -> int:
    """How many hand-added tasks have had their card."""
    return int(
        await session.scalar(
            select(func.count()).select_from(MailWatchManualTask)
            .where(MailWatchManualTask.posted_at.is_not(None))
        )
        or 0
    )
