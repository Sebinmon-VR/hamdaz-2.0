"""User analytics and the assignment priority score.

Where the numbers come from and go:

* **in** — task counts read live from the SharePoint Proposals list on every
  call. Read-only, every request a GET.
* **out** — Postgres, and, when ``ANALYTICS_PUBLISH_ENABLED`` is set, the
  ``useranalytics`` list on the Test site: one row per person, rewritten by
  ``/publish``, by a kept run, and by the background loop whenever the live
  standing moves. See ``app.analytics.publisher``. The Proposals list itself
  is never written from here.

Two endpoints do the work, and the split matters. ``/preview`` computes a
ranking and keeps nothing — that is the common case, and a history full of
rankings nobody acted on would bury the ones that mattered. ``/runs`` computes
the same thing and stores it, with the policy frozen onto the row so a later
edit cannot rewrite what the decision was based on.

Nothing here assigns anything either. It says who *should* get the next task and
why; handing it to them is still a person's action.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import live, service
from app.analytics import publisher as publishing
from app.analytics.schemas import EntryOut, PublishOut, RunIn, RunOut, RunSummaryOut
from app.analytics.service import (
    AnalyticsError,
    AnalyticsNotFoundError,
    NotInScopeError,
)
from app.assignment import service as policy_service
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models.analytics import AnalyticsRun
from app.models.proposal_index import ProposalIndexItem
from app.models.team import Team
from app.proposals.analytics import WorkloadCache
from app.proposals.sharepoint import SharePointProposals
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

router = APIRouter(prefix="/analytics", tags=["user analytics"])

Session = Annotated[AsyncSession, Depends(get_session)]


def get_sharepoint(request: Request) -> SharePointProposals:
    return request.app.state.sharepoint


def get_cache(request: Request) -> WorkloadCache:
    return request.app.state.workload_cache


SharePoint = Annotated[SharePointProposals, Depends(get_sharepoint)]
Cache = Annotated[WorkloadCache, Depends(get_cache)]
Config = Annotated[Settings, Depends(get_settings)]


def _publish_out(
    report: publishing.PublishReport, publisher: publishing.Publisher
) -> PublishOut:
    return PublishOut(
        enabled=publisher.enabled,
        list_url=publisher.list_url,
        reason=report.reason,
        created=report.created,
        updated=report.updated,
        unchanged=report.unchanged,
        removed=report.removed,
        names=report.names,
        error=report.error,
    )


def _translate(exc: AnalyticsError) -> HTTPException:
    if isinstance(exc, AnalyticsNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, NotInScopeError):
        # A configuration answer, not an upstream failure — the caller asked
        # something reasonable about a team that simply is not set up for it.
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


async def _team(session: AsyncSession, slug: str | None) -> Team | None:
    if not slug:
        return None
    try:
        return await teams_service.get_team(session, slug)
    except TeamError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _out(record: AnalyticsRun) -> RunOut:
    body = RunOut.model_validate(record)
    body.created_by_name = record.created_by.display_name if record.created_by else None
    assignable = [e for e in record.entries if not e.excluded]
    body.assignable = len(assignable)
    top = min(assignable, key=lambda e: e.priority_score or 10**6, default=None)
    body.next_up = top.display_name if top else None
    return body


def _summary(record: AnalyticsRun) -> RunSummaryOut:
    assignable = [e for e in record.entries if not e.excluded]
    top = min(assignable, key=lambda e: e.priority_score or 10**6, default=None)
    return RunSummaryOut(
        id=record.id,
        team_name=record.team_name,
        rows_read=record.rows_read,
        people=len(record.entries),
        assignable=len(assignable),
        next_up=top.display_name if top else None,
        notes=record.notes,
        created_at=record.created_at,
        created_by_name=record.created_by.display_name if record.created_by else None,
    )


# ── computing ──────────────────────────────────────────────────────────


@router.get(
    "/preview",
    response_model=RunOut,
    summary="Rank people for the next assignment, keeping nothing",
)
async def preview(
    _: CurrentUser,
    session: Session,
    sharepoint: SharePoint,
    cache: Cache,
    team: Annotated[str, Query(description="Team handle. Required.")],
    refresh: Annotated[bool, Query(description="Re-read SharePoint instead of the cache")] = False,
) -> RunOut:
    """Who should get the next task, and why.

    A team is required, and it must have an assignment policy of its own — only
    those distribute work through this. There is deliberately no
    organisation-wide ranking: scoring everybody at once would put every team in
    the company into one queue they do not share.

    Task counts are read live. Nothing is stored, in this database or anywhere
    else — run it as often as you like.
    """
    resolved = await _team(session, team)
    try:
        await service.in_scope(session, resolved)
        record = await service.run(session, sharepoint, cache, team=resolved, refresh=refresh)
    except AnalyticsError as exc:
        raise _translate(exc) from exc
    return _out(record)


@router.post(
    "/runs",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Compute a ranking and keep it",
)
async def create_run(
    payload: RunIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    sharepoint: SharePoint,
    cache: Cache,
    config: Config,
    team: Annotated[str, Query(description="Team handle. Required.")],
) -> RunOut:
    """Store the ranking as the record of a decision.

    Kept in Postgres. The policy is frozen onto the row, so editing it next week
    cannot change what this run appears to have been based on.

    Restricted to the people who may edit the policy in the first place — a
    stored run is a record others will rely on, so it should not accumulate from
    anyone who happens to look.
    """
    resolved = await _team(session, team)
    try:
        await service.in_scope(session, resolved)
    except AnalyticsError as exc:
        raise _translate(exc) from exc

    try:
        await policy_service.require_edit(
            session, user=user, roles=roles, team_id=resolved.id
        )
    except policy_service.PolicyError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    try:
        record = await service.run(
            session,
            sharepoint,
            cache,
            team=resolved,
            refresh=True,  # a kept record should not be built from a cached sweep
            save=True,
            actor=user,
            notes=payload.notes,
        )
    except AnalyticsError as exc:
        raise _translate(exc) from exc

    # A kept run is a decision, and the list is where the decision is read
    # from. The report is on the response rather than raised: the run is
    # stored either way, and a list that could not be reached is not a
    # reason to lose the record of what was decided.
    publisher = publishing.Publisher(config, sharepoint)
    body = _out(record)
    if publisher.enabled:
        body.published = _publish_out(
            await publisher.publish(
                publishing.from_run(record, team=resolved.slug), reason="saved-run"
            ),
            publisher,
        )
    return body


@router.post(
    "/publish",
    response_model=PublishOut,
    summary="Write the live standing to the useranalytics list now",
)
async def publish_now(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    sharepoint: SharePoint,
    config: Config,
    team: Annotated[str, Query(description="Team handle. Required.")],
) -> PublishOut:
    """Recompute the team's live standing from the mirror and push it.

    The background loop does this on its own after every sync. This is for
    the person who wants the list right *now* and does not want to wait a
    minute, and for checking that publishing works at all. Same permission
    as keeping a run: the list is read by other tools, so it should not be
    rewritten by anyone who happens to look.
    """
    resolved = await _team(session, team)
    try:
        await service.in_scope(session, resolved)
        await policy_service.require_edit(
            session, user=user, roles=roles, team_id=resolved.id
        )
    except policy_service.PolicyError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AnalyticsError as exc:
        raise _translate(exc) from exc

    publisher = publishing.Publisher(config, sharepoint)
    if not publisher.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Publishing is off. Set ANALYTICS_PUBLISH_ENABLED=true and restart.",
        )
    mirrored = await session.scalar(
        select(func.count())
        .select_from(ProposalIndexItem)
        .where(ProposalIndexItem.deleted.is_(False))
    )
    if not mirrored:
        # An empty mirror would rank everybody at zero work and publish that.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The Proposals mirror has never synced, so there is no live "
                "standing to publish. Run POST /intake/mirror/sync first."
            ),
        )
    await live.recompute(session, team=resolved, reason="publish")
    report = await publishing.publish_team(session, publisher, resolved, reason="publish")
    return _publish_out(report, publisher)


# ── history ────────────────────────────────────────────────────────────


@router.get("/runs", response_model=list[RunSummaryOut], summary="Kept runs")
async def index(
    _: CurrentUser,
    session: Session,
    team: Annotated[str | None, Query(description="Only this team's runs")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunSummaryOut]:
    resolved = await _team(session, team)
    rows = await service.saved_runs(
        session, team_id=resolved.id if resolved else None, limit=limit
    )
    return [_summary(record) for record in rows]


@router.get("/runs/{run_id}", response_model=RunOut, summary="One kept run in full")
async def detail(run_id: uuid.UUID, _: CurrentUser, session: Session) -> RunOut:
    try:
        return _out(await service.get_run(session, run_id))
    except AnalyticsError as exc:
        raise _translate(exc) from exc


@router.get(
    "/runs/{run_id}/people/{user_id}",
    response_model=EntryOut,
    summary="Why one person scored what they did",
)
async def explain(
    run_id: uuid.UUID, user_id: uuid.UUID, _: CurrentUser, session: Session
) -> EntryOut:
    """The factor breakdown for one person.

    This is the endpoint that makes a ranking arguable: it shows the raw number,
    where it sat relative to everyone else, and how much each factor moved them.
    """
    try:
        record = await service.get_run(session, run_id)
    except AnalyticsError as exc:
        raise _translate(exc) from exc

    entry = next((e for e in record.entries if e.user_id == user_id), None)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That person is not in this run"
        )
    return EntryOut.model_validate(entry)


@router.delete(
    "/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a kept run"
)
async def remove(
    run_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session
) -> None:
    try:
        record = await service.get_run(session, run_id)
        await policy_service.require_edit(
            session, user=user, roles=roles, team_id=record.team_id
        )
        await service.delete_run(session, record)
    except policy_service.PolicyError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AnalyticsError as exc:
        raise _translate(exc) from exc
