"""Mail watch → a task for each new tender, and who it goes to.

Two cases, from the matcher's verdict on a new-tender mail:

* **new** — no task on the Proposals list has this tender: it goes to whoever
  is priority 1 of the team (the live standing kept in ``live_scores``, the
  same ranking published to the useranalytics list).
* **stage change** — the tender is on the list at an earlier stage (an EOI
  that is now an RFP, say): it goes to whoever had it, as they know it.

Anything else — a task already at this stage, a second mail about the same
tender, a match the matcher was unsure of — gets no task.

**Not written to SharePoint.** While this is tested the task is kept in
``mailwatch_test_tasks``, with the Proposals list's own columns and the exact
payload a create would send; the list itself is never touched from here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import live
from app.analytics import publisher as publishing
from app.ariba.bcd import expected_bcd
from app.core.config import Settings
from app.intake.worker import MIRROR_LOCK
from app.mailwatch.classify import NEW_TENDER
from app.mailwatch.matching import NEW, STAGE_CHANGE
from app.models.mailwatch import MailWatchTeams, MailWatchTestTask, WatchedMail
from app.models.proposal_index import ProposalIndexItem
from app.models.team import Team
from app.proposals import mirror
from app.proposals.sharepoint import (
    SharePointConsentError,
    SharePointError,
    SharePointProposals,
)

logger = logging.getLogger("hamdaz.mailwatch.assign")

#: While this is tested every Teams card says so at the top
#: (app/mailwatch/teams.py, whose channel lock goes with it).
TESTING: Final = True

#: Where a new tender's task can be created (MailWatchTeams.task_list).
TEST_LIST, REAL_LIST, NO_LIST = "test", "real", "none"
TASK_LISTS: Final = (TEST_LIST, REAL_LIST, NO_LIST)


@dataclass(frozen=True)
class ListTarget:
    kind: str
    site_url: str
    site_id: str
    list_id: str
    #: Where an item is opened: ``<list_url>/DispForm.aspx?ID=…``.
    list_url: str
    name: str


def list_target(settings: Settings, choice: str) -> ListTarget | None:
    """The list a task goes to for the page's choice; None to keep it in the app."""
    if choice == REAL_LIST:
        list_url = settings.sharepoint_proposals_list_url.rstrip("/")
        return ListTarget(
            REAL_LIST, list_url.split("/Lists/")[0], settings.sharepoint_site_id,
            settings.sharepoint_proposals_list_id, list_url, "Proposals",
        )
    if choice == TEST_LIST and settings.mailwatch_task_list_id.strip():
        site_url = settings.mailwatch_task_site_url.rstrip("/")
        return ListTarget(
            TEST_LIST, site_url, settings.mailwatch_task_site_id.strip(),
            settings.mailwatch_task_list_id.strip(), f"{site_url}/Lists/ProposalsTest",
            "Proposals Test",
        )
    return None


async def task_list_choice(session: AsyncSession) -> str:
    row = await session.get(MailWatchTeams, 1)
    return row.task_list if row is not None and row.task_list in TASK_LISTS else TEST_LIST

#: The team a new tender is ranked in: the one whose assignment policy is on.
TEAM_SLUG: Final = "presales"

#: The list's CurrentType choices are EOI, RFP, RFI and RFT; an RFQ is filed
#: as RFP, as the team does.
CURRENT_TYPE: Final = {"eoi": "EOI", "rfi": "RFI", "rfq": "RFP", "rfp": "RFP", "rft": "RFT"}

#: Status of a task nobody has started.
NOT_STARTED: Final = "Not Started"

#: How many of the team's standing are kept with the task, to show why.
STANDING_KEPT: Final = 5


class AssignError(Exception):
    """Something a super admin can be shown as it is."""


def wanted(mail: WatchedMail) -> bool:
    """A mail that should have a task: a new tender, new or at a later stage."""
    return mail.category == NEW_TENDER and mail.verdict in (NEW, STAGE_CHANGE)


def task_title(mail: WatchedMail) -> str:
    """The list's titles start with the tender number ("6000153446 RFQ
    6000153446 - BELTS …"), which is how they are found again."""
    title = (mail.title or mail.subject or "").strip()
    number = next(
        (r for r in mail.reference_numbers if re.fullmatch(r"(?:Doc)?\d{6,12}", r)), None
    ) or mail.ariba_doc
    if number and not title.startswith(number):
        title = f"{number} {title}".strip()
    return title[:255] or "(new tender from the CEO's mail)"


async def _team(session: AsyncSession) -> Team:
    team = await session.scalar(select(Team).where(Team.slug == TEAM_SLUG))
    if team is None:
        raise AssignError(f"No team {TEAM_SLUG!r} to rank the new tender in.")
    return team


async def _standing(session: AsyncSession, team: Team) -> list[dict[str, Any]]:
    """The top of the team's ranking as it is this moment, from the Proposals
    list as the mirror holds it — a task just created there is in it already
    (:func:`refresh_ranking`). Tasks kept in the test list or the app are test
    work and never count. With what shapes each person's share: their labels,
    their capacity (1 is a full load) and the policy's ceiling on open tasks."""
    scored = await live.ranked(session, team=team)
    ranked = sorted(
        (
            r for r in scored
            # As the stored standing: eligible, placed, and somebody who can sign in.
            if not r.excluded and (r.priority or 0) > 0 and r.candidate.user_id is not None
        ),
        key=lambda r: r.priority,
    )[:STANDING_KEPT]
    out = []
    for result in ranked:
        person = result.candidate
        out.append({
            "rank": result.priority,
            "name": person.display_name,
            "email": (person.email or "").lower() or None,
            "lookup_id": person.lookup_id,
            "active": person.active_tasks,
            "open": person.open_tasks,
            "labels": sorted(str(label) for label in person.labels),
            "capacity": float(person.capacity) if person.capacity is not None else None,
            "limit": person.max_open,
        })
    return out


def _fields(task: MailWatchTestTask, settings: Settings) -> dict[str, Any]:
    """The create payload, by the list's internal names. Dates go as SharePoint
    keeps them: BCD is the UAE wall clock read in the site's zone."""
    fields: dict[str, Any] = {"Title": task.title, "Status": task.status}
    if task.end_user:
        fields["EndUser"] = task.end_user
    if task.current_type:
        fields["CurrentType"] = task.current_type
    if task.start_date:
        fields["StartDate"] = task.start_date.strftime("%Y-%m-%dT00:00:00Z")
    if task.bcd:
        fields["BCD"] = expected_bcd(
            task.bcd, uae=settings.sharepoint_meant_timezone,
            site=settings.sharepoint_site_timezone,
        )
    if task.remarks:
        fields["Remarks"] = task.remarks
    if task.working_notes:
        fields["WorkingNotes"] = task.working_notes
    if task.previous_owner:
        fields["PreviousOwner"] = task.previous_owner
    if task.assigned_to_lookup_id:
        fields["AssignedToLookupId"] = task.assigned_to_lookup_id
    return fields


async def create(
    session: AsyncSession,
    mail: WatchedMail,
    *,
    settings: Settings,
    sharepoint: SharePointProposals | None = None,
) -> MailWatchTestTask:
    """Assign the mail's tender and keep the task in the test list. Once per
    mail: a second call returns the task already made."""
    existing = await test_task_of(session, mail)
    if existing is not None:
        return existing
    choice = await task_list_choice(session)
    task = await plan(session, mail, settings=settings, choice=choice)
    session.add(task)
    await session.flush()
    if sharepoint is not None:
        target = list_target(settings, choice)
        await to_list(task, sharepoint=sharepoint, target=target)
        if target is not None and target.kind == REAL_LIST and task.list_item_id:
            await refresh_ranking(session, task, sharepoint=sharepoint, settings=settings)
    logger.info(
        "mail watch test task: %r to %s (%s)", task.title, task.assigned_to_name, task.action
    )
    return task


async def to_list(
    task: MailWatchTestTask, *, sharepoint: SharePointProposals, target: ListTarget | None
) -> None:
    """Create the task as an item in the chosen list, assigned there. A
    failure is kept on the task, not raised: the row in the app still stands."""
    if task.list_item_id or target is None:
        return
    try:
        fields = {k: v for k, v in task.fields.items() if k != "AssignedToLookupId"}
        if task.assigned_to_email:
            # Lookup ids are per site: the assignee's id on the list's own site.
            fields["AssignedToLookupId"] = await sharepoint.ensure_user_on(
                target.site_url, task.assigned_to_email
            )
        item = await sharepoint.create_item(target.site_id, target.list_id, fields)
    except (SharePointError, SharePointConsentError) as exc:
        task.list_error = str(exc)[:500]
        logger.warning("mail watch could not create %r in %s: %s", task.title, target.name, exc)
        return
    task.list_id = target.list_id
    task.list_item_id = str(item.get("id") or "")
    task.list_item_url = f"{target.list_url}/DispForm.aspx?ID={task.list_item_id}"
    task.list_error = None


async def refresh_ranking(
    session: AsyncSession,
    task: MailWatchTestTask,
    *,
    sharepoint: SharePointProposals,
    settings: Settings,
) -> None:
    """A task just created in the real Proposals list: put it in the mirror
    now, recompute the stored standing (everybody's and the team's), and
    publish it to the useranalytics list when publishing is on — so the next
    tender, and anyone reading the standing, sees it without waiting for the
    next sync. Never fails the assignment."""
    try:
        # After any sync in progress, and before the next: the same lock —
        # waited for briefly, as a sync takes seconds; past that the next sync
        # catches up instead.
        await session.execute(text("SET LOCAL lock_timeout = '10s'"))
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIRROR_LOCK})
        await session.execute(text("SET LOCAL lock_timeout = 0"))
        created = await sharepoint.task(str(task.list_item_id))
        people = {
            str(created.assigned_to_lookup_id or ""): {
                "name": task.assigned_to_name or "", "email": task.assigned_to_email or "",
            }
        }
        await mirror.upsert_one(session, created, people=people)
        team = await _team(session)
        counted = await live.counts(session)
        await live.recompute(session, team=None, reason="assign", counted=counted)
        await live.recompute(session, team=team, reason="assign", counted=counted)
        if settings.analytics_publish_enabled:
            await publishing.publish_team(
                session, publishing.Publisher(settings, sharepoint), team, reason="assign"
            )
    except Exception as exc:  # noqa: BLE001 - the next sync catches up
        logger.warning("mail watch could not refresh the standing after %r: %s", task.title, exc)


async def remove_from_list(
    task: MailWatchTestTask, *, sharepoint: SharePointProposals, settings: Settings
) -> bool:
    """Delete the task's item — from the Proposals Test list only. An item in
    the real list stays: removing a row here never deletes real work. True
    when an item was deleted."""
    test = list_target(settings, TEST_LIST)
    if test is None or not task.list_item_id or task.list_id != test.list_id:
        return False
    await sharepoint.delete_item(test.site_id, test.list_id, task.list_item_id)
    return True


async def plan(
    session: AsyncSession, mail: WatchedMail, *, settings: Settings, choice: str = TEST_LIST
) -> MailWatchTestTask:
    """Who the mail's tender goes to and the task it would make — worked out,
    not kept. Reads the standing and the mirror; writes nothing."""
    if not wanted(mail):
        raise AssignError(
            "Only a new tender that is new, or at a later stage than its task, gets a task."
        )

    team = await _team(session)
    standing = await _standing(session, team)
    earlier = mail.task or {}
    task = MailWatchTestTask(
        mail_id=mail.id,
        action=mail.verdict,
        title=task_title(mail),
        status=NOT_STARTED,
        end_user=(mail.customer or earlier.get("customer") or None),
        current_type=CURRENT_TYPE.get(mail.stage or ""),
        start_date=(mail.received_at or datetime.now(UTC)),
        # No closing date in the mail: today, for the assignee to correct —
        # the BCD reminders ask them to.
        bcd=mail.closing_at or datetime.now(UTC),
        standing=standing,
    )
    received = mail.received_at.strftime("%d %b %Y") if mail.received_at else "today"
    refs = ", ".join([*mail.reference_numbers, *([mail.ariba_doc] if mail.ariba_doc else [])])
    task.remarks = f"From the CEO's mail of {received}" + (f" ({refs})" if refs else "") + "."
    if mail.closing_at is None:
        task.remarks += " BCD not in the mail: set to today — please correct it."
    if choice != REAL_LIST:
        task.remarks = "TESTING — " + task.remarks

    if mail.verdict == STAGE_CHANGE:
        if not (earlier.get("assignee") or earlier.get("assignee_email")):
            raise AssignError("The earlier task has nobody assigned to hand this to.")
        task.assigned_to_name = earlier.get("assignee")
        task.assigned_to_email = (earlier.get("assignee_email") or "").lower() or None
        if mail.matched_item_id:
            row = await session.get(ProposalIndexItem, mail.matched_item_id)
            task.assigned_to_lookup_id = row.assigned_lookup_id if row else None
        task.previous_item_id = mail.matched_item_id
        task.previous_title = earlier.get("title")
        task.previous_stage = earlier.get("stage")
        before = (task.previous_stage or "earlier stage").upper()
        task.assign_reason = (
            f"Worked on the {before} of this tender ({task.previous_title or 'its task'}), "
            f"now at {(mail.stage or '').upper() or 'a later stage'}."
        )
        task.remarks += f" Follows {task.previous_title or 'the earlier task'} ({before})."
    else:
        if not standing:
            raise AssignError(f"Nobody in {team.name} is eligible for a new task right now.")
        first = standing[0]
        task.assigned_to_name = first["name"]
        task.assigned_to_email = first["email"]
        task.assigned_to_lookup_id = first["lookup_id"]
        task.assign_reason = (
            f"Priority 1 in {team.name} by workload ({first['active']} active task"
            f"{'' if first['active'] == 1 else 's'})."
        )
    task.fields = _fields(task, settings)
    return task


async def create_pending(
    session: AsyncSession,
    *,
    settings: Settings,
    sharepoint: SharePointProposals | None = None,
) -> int:
    """A test task for each new tender received since assigning was switched
    on that has none yet. Rows locked one by one, so two instances never make
    two."""
    row = await session.get(MailWatchTeams, 1)
    if row is None or not row.assign_new_tenders or row.assign_from is None:
        return 0
    done = select(MailWatchTestTask.mail_id)
    mails = (
        await session.scalars(
            select(WatchedMail)
            .where(
                WatchedMail.category == NEW_TENDER,
                WatchedMail.verdict.in_((NEW, STAGE_CHANGE)),
                WatchedMail.received_at >= row.assign_from,
                WatchedMail.id.not_in(done),
            )
            .order_by(WatchedMail.received_at.asc())
            .limit(10)
            .with_for_update(skip_locked=True)
        )
    ).all()
    made = 0
    for mail in mails:
        try:
            await create(session, mail, settings=settings, sharepoint=sharepoint)
            made += 1
        except AssignError as exc:
            logger.warning("mail watch could not assign %s: %s", mail.id, exc)
    return made


async def test_task_of(session: AsyncSession, mail: WatchedMail) -> MailWatchTestTask | None:
    return await session.scalar(
        select(MailWatchTestTask).where(MailWatchTestTask.mail_id == mail.id)
    )
