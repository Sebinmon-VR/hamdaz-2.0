"""The mail watch API. Super admin only — nobody else sees what it read.

No page yet: these are tried from the API docs (``/docs``). The webhook is the
one route not behind a session, because its caller is Microsoft; it is
authenticated by echoing Graph's validation token and by checking the secret
we gave Graph when we subscribed.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.auth.router import LOGIN_STATE_AUDIENCE
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.mailwatch import assign, matching, service, teams
from app.mailwatch.classify import CATEGORIES, NEGOTIATION, NEW_TENDER, SURE, Reading
from app.mailwatch.classify import references as refs_in
from app.mailwatch.schemas import (
    CandidateOut,
    MailWatchSettingsIn,
    MailWatchSettingsOut,
    MatchOut,
    ReadingOut,
    ReadReportOut,
    SearchIn,
    TeamsIn,
    TeamsOut,
    TestTaskOut,
    TrialIn,
    TrialOut,
    TrialRowOut,
    WatchedMailDetailOut,
    WatchedMailOut,
)
from app.models.mailwatch import MailWatchTestTask, WatchedMail
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/mailwatch", tags=["mail watch"])
#: Not authenticated by session: the caller is Microsoft, not a person.
webhook_router = APIRouter(prefix="/mailwatch", tags=["mail watch"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can see the mail watch.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _bad(exc: service.MailWatchError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _reader(request: Request):
    return request.app.state.mail_reader


def _model(request: Request):
    return getattr(request.app.state, "agent_model", None)


def _sharepoint(request: Request):
    return getattr(request.app.state, "sharepoint", None)


def _match_out(found: matching.Match | None) -> MatchOut | None:
    if found is None:
        return None
    return MatchOut(
        verdict=found.verdict,
        method=found.method,
        item_id=found.item_id,
        score=found.score,
        reason=found.reason,
        candidates=[CandidateOut(**c.as_dict()) for c in found.candidates],
        task=found.task,
        duplicate_of_id=found.duplicate_of_id,
        model_cost_usd=found.model_cost_usd,
        error=found.error,
    )


def _reading_out(reading: Reading) -> ReadingOut:
    return ReadingOut(
        category=reading.category,
        kind=reading.kind,
        stage=reading.stage,
        decided_by=reading.decided_by,
        reason=reading.reason,
        confidence=reading.confidence,
        title=reading.title,
        customer=reading.customer,
        references=reading.references,
        ariba_doc=reading.ariba_doc,
        closing_at=reading.closing_at,
        original_sender=reading.original_sender,
        model_cost_usd=reading.model_cost_usd,
        error=reading.error,
    )


async def _settings_out(session: AsyncSession, config: Settings) -> MailWatchSettingsOut:
    row = await service.get_settings(session)
    body = MailWatchSettingsOut.model_validate(row)
    body.watching = await service.watched_senders(session, row)
    body.has_bookmark = bool(row.delta_link)
    try:
        body.notification_url = service.notification_url(config)
    except service.MailWatchError as exc:
        body.notification_url = str(exc)
    body.mails_kept = int(
        await session.scalar(select(func.count()).select_from(WatchedMail)) or 0
    )
    return body


@router.get("/settings", response_model=MailWatchSettingsOut, summary="Mailbox, senders, switch, subscription")
async def read_settings(admin: SuperAdmin, session: Session, config: Config) -> MailWatchSettingsOut:
    out = await _settings_out(session, config)
    await session.commit()
    return out


@router.patch("/settings", response_model=MailWatchSettingsOut, summary="Change them")
async def update_settings(
    body: MailWatchSettingsIn, admin: SuperAdmin, session: Session, request: Request, config: Config
) -> MailWatchSettingsOut:
    """Changing the mailbox moves the subscription to it and starts it from now.
    Switching on subscribes (on the public server); switching off unsubscribes."""
    try:
        await service.update_settings(
            session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True),
            reader=_reader(request), settings=config,
        )
    except service.MailWatchError as exc:
        raise _bad(exc) from exc
    await session.commit()
    return await _settings_out(session, config)


@router.post("/subscribe", response_model=MailWatchSettingsOut, summary="Subscribe to the mailbox again")
async def subscribe(
    admin: SuperAdmin, session: Session, request: Request, config: Config
) -> MailWatchSettingsOut:
    try:
        await service.subscribe(session, reader=_reader(request), settings=config)
    except service.MailWatchError as exc:
        await session.rollback()
        raise _bad(exc) from exc
    await session.commit()
    return await _settings_out(session, config)


@router.delete("/subscribe", response_model=MailWatchSettingsOut, summary="Stop Graph posting")
async def unsubscribe(
    admin: SuperAdmin, session: Session, request: Request, config: Config
) -> MailWatchSettingsOut:
    await service.unsubscribe(session, reader=_reader(request))
    await session.commit()
    return await _settings_out(session, config)


@router.post("/catch-up", response_model=ReadReportOut, summary="Read what arrived since the bookmark, now")
async def catch_up(admin: SuperAdmin, session: Session, request: Request) -> ReadReportOut:
    """Runs whether switched on or not — somebody asked. Reads only mail from
    the watched senders received since the mailbox was set."""
    report = await service.read_new(
        session, reader=_reader(request), model=_model(request), sharepoint=_sharepoint(request)
    )
    await session.commit()
    # New tenders get their test task (once assigning is on), as on a live pass.
    await assign.create_pending(
        session, settings=get_settings(), sharepoint=_sharepoint(request)
    )
    await session.commit()
    await teams.post_pending(session, poster=_poster(request), settings=get_settings())
    await session.commit()
    return ReadReportOut(**report.as_dict())


@router.post("/trial", response_model=TrialOut, summary="Classify past mail, store nothing")
async def run_trial(body: TrialIn, admin: SuperAdmin, session: Session, request: Request) -> TrialOut:
    """The watched senders' mail of the last ``days`` days, classified as if it
    had just arrived. Nothing is stored. Confidential mails show no subject —
    open them with their link to check the label."""
    settings_row = await service.get_settings(session)
    use_model = settings_row.use_model if body.use_model is None else body.use_model
    try:
        row, senders, since, rows = await service.trial(
            session, reader=_reader(request), model=_model(request),
            days=body.days, limit=body.limit, use_model=use_model,
            sharepoint=_sharepoint(request),
        )
    except service.MailWatchError as exc:
        raise _bad(exc) from exc
    await session.commit()
    readings = [r.reading for r in rows]
    return TrialOut(
        mailbox=row.mailbox,
        senders=senders,
        since=since,
        mails=len(rows),
        by_category=dict(Counter(r.category for r in readings)),
        by_decided=dict(Counter(r.decided_by for r in readings)),
        by_verdict=dict(Counter(r.match.verdict for r in rows if r.match is not None)),
        unsure=sum(1 for r in readings if r.decided_by != "model" and r.confidence < SURE),
        model_cost_usd=sum((r.model_cost_usd for r in readings), Decimal("0")),
        rows=[
            TrialRowOut(
                received_at=r.received_at, subject=r.subject, web_link=r.web_link,
                attachments=r.attachments, reading=_reading_out(r.reading),
                match=_match_out(r.match),
            )
            for r in rows
        ],
    )


@router.post("/search", response_model=MatchOut, summary="Search the Proposals list as a mail would")
async def search_proposals(
    body: SearchIn, admin: SuperAdmin, session: Session, request: Request
) -> MatchOut:
    """The same search a new tender goes through — number, then title words,
    then Haiku for a close call — with the matched task read live from
    SharePoint. For trying titles with typos and renamed stages. Stores nothing."""
    settings_row = await service.get_settings(session)
    found = await matching.match(
        session,
        category=NEGOTIATION if body.negotiation else NEW_TENDER,
        stage=body.stage,
        title=body.text,
        subject=body.text,
        customer=body.customer,
        references=list(dict.fromkeys([*body.references, *refs_in(body.text)])),
        ariba_doc=None,
        conversation_id=None,
        received_at=None,
        mail_id=None,
        sharepoint=_sharepoint(request),
        model=_model(request),
        use_model=settings_row.use_model,
    )
    await session.commit()
    out = _match_out(found)
    assert out is not None
    return out


# ── Teams: negotiation updates to the locked channel ─────────────────


async def _teams_out(session: AsyncSession) -> TeamsOut:
    row = await teams.get_connection(session)
    body = TeamsOut.model_validate(row)
    body.connected = teams.is_connected(row)
    body.test_team, body.test_channel = teams.TEST_CHANNEL
    body.is_test_channel = teams.is_test_channel(row)
    body.posted = int(
        await session.scalar(
            select(func.count()).select_from(WatchedMail).where(WatchedMail.teams_posted_at.is_not(None))
        )
        or 0
    )
    body.test_tasks = int(
        await session.scalar(select(func.count()).select_from(MailWatchTestTask)) or 0
    )
    config = get_settings()
    body.task_lists = [
        {"key": key, "name": target.name, "url": target.list_url}
        for key in (assign.TEST_LIST, assign.REAL_LIST)
        if (target := assign.list_target(config, key)) is not None
    ]
    return body


def _poster(request: Request) -> teams.TeamsPoster:
    return request.app.state.teams_poster


@router.get("/teams", response_model=TeamsOut, summary="The connected Teams account and channel")
async def teams_status(admin: SuperAdmin, session: Session) -> TeamsOut:
    out = await _teams_out(session)
    await session.commit()
    return out


@router.get("/teams/connect", summary="Connect your Teams account (opens Microsoft sign-in)")
async def teams_connect(admin: SuperAdmin, config: Config):
    """Open this in the browser, not from the API page: it redirects to
    Microsoft, which asks the signed-in account to allow posting channel
    messages, and comes back to the Mail watch page."""
    return teams.start(
        settings=config, user_id=admin.id,
        login_state_cookie=config.login_state_cookie_name, audience=LOGIN_STATE_AUDIENCE,
    )


@router.patch("/teams", response_model=TeamsOut, summary="Switch negotiation posts on or off")
async def teams_switch(
    body: TeamsIn, admin: SuperAdmin, session: Session, request: Request
) -> TeamsOut:
    row = await teams.get_connection(session)
    if body.post_negotiations is not None:
        if body.post_negotiations and not teams.is_connected(row):
            raise HTTPException(status_code=400, detail="Connect a Teams account first.")
        if body.post_negotiations and not row.post_negotiations:
            # From now on: switching on never posts the backlog.
            row.posting_from = datetime.now(UTC)
        row.post_negotiations = body.post_negotiations
    if body.task_list is not None:
        row.task_list = body.task_list
    if body.team_id or body.channel_id:
        if not (body.team_id and body.channel_id):
            raise HTTPException(status_code=400, detail="Choose both a team and a channel.")
        try:
            await teams.choose_channel(
                row, _reader(request), team_id=body.team_id, channel_id=body.channel_id
            )
        except teams.TeamsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.assign_new_tenders is not None:
        # Test tasks need no Teams account; the cards wait for one.
        if body.assign_new_tenders and not row.assign_new_tenders:
            row.assign_from = datetime.now(UTC)
        row.assign_new_tenders = body.assign_new_tenders
    await session.commit()
    return await _teams_out(session)


@router.get("/teams/teams", summary="The organisation's teams, to choose where cards go")
async def list_teams(admin: SuperAdmin, request: Request) -> list[dict[str, str]]:
    try:
        return await teams.list_teams(_reader(request))
    except teams.TeamsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/teams/channels", summary="One team's channels")
async def list_channels(
    admin: SuperAdmin, request: Request, team_id: Annotated[str, Query(min_length=1)]
) -> list[dict[str, str]]:
    try:
        return await teams.list_channels(_reader(request), team_id)
    except teams.TeamsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/teams", response_model=TeamsOut, summary="Disconnect the Teams account")
async def teams_disconnect(admin: SuperAdmin, session: Session) -> TeamsOut:
    teams.disconnect(await teams.get_connection(session))
    await session.commit()
    return await _teams_out(session)


@router.post("/teams/test", response_model=TeamsOut, summary="Post a test message to the locked channel")
async def teams_test(admin: SuperAdmin, session: Session, request: Request) -> TeamsOut:
    row = await teams.get_connection(session)
    if not teams.is_connected(row):
        raise HTTPException(status_code=400, detail="Connect a Teams account first.")
    try:
        await _poster(request).post(row, teams.test_html(row))
    except teams.TeamsError as exc:
        row.last_error = str(exc)[:500]
        await session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await session.commit()
    return await _teams_out(session)


@router.post(
    "/mails/{mail_id}/teams",
    response_model=WatchedMailDetailOut,
    summary="Post this negotiation mail to the locked channel now",
)
async def post_mail_to_teams(
    mail_id: uuid.UUID, admin: SuperAdmin, session: Session, request: Request, config: Config
) -> WatchedMailDetailOut:
    mail = await session.get(WatchedMail, mail_id)
    if mail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such mail.")
    try:
        await teams.post_mail(session, mail, poster=_poster(request), settings=config)
    except teams.TeamsError as exc:
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(mail)
    return WatchedMailDetailOut.model_validate(mail)


def _test_task_out(task: MailWatchTestTask, mail: WatchedMail | None) -> TestTaskOut:
    out = TestTaskOut.model_validate(task)
    if mail is not None:
        out.subject, out.received_at = mail.subject, mail.received_at
        out.teams_posted_at, out.teams_error = mail.teams_posted_at, mail.teams_error
    return out


@router.get(
    "/test-tasks",
    response_model=list[TestTaskOut],
    summary="Tasks new tenders would have created on the Proposals list (the test list)",
)
async def list_test_tasks(
    admin: SuperAdmin, session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[TestTaskOut]:
    rows = (
        await session.execute(
            select(MailWatchTestTask, WatchedMail)
            .join(WatchedMail, WatchedMail.id == MailWatchTestTask.mail_id)
            .order_by(MailWatchTestTask.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [_test_task_out(task, mail) for task, mail in rows]


@router.post(
    "/mails/{mail_id}/assign",
    response_model=TestTaskOut,
    summary="Assign this new tender now and keep its task in the test list",
)
async def assign_mail(
    mail_id: uuid.UUID, admin: SuperAdmin, session: Session, config: Config, request: Request
) -> TestTaskOut:
    """For trying it on a mail by hand, switch or no switch. Writes the test
    list only — never the Proposals list."""
    mail = await session.get(WatchedMail, mail_id)
    if mail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such mail.")
    try:
        task = await assign.create(
            session, mail, settings=config, sharepoint=_sharepoint(request)
        )
    except assign.AssignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _test_task_out(task, mail)


@router.delete(
    "/test-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a task from the test list (so its mail can be assigned again)",
)
async def delete_test_task(
    task_id: uuid.UUID, admin: SuperAdmin, session: Session, config: Config, request: Request
) -> Response:
    """Its item in the Proposals Test list goes too."""
    task = await session.get(MailWatchTestTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such test task.")
    sharepoint = _sharepoint(request)
    if sharepoint is not None:
        try:
            # From the test list only; an item in the real list stays.
            await assign.remove_from_list(task, sharepoint=sharepoint, settings=config)
        except Exception as exc:  # noqa: BLE001 - say so, keep the row
            raise HTTPException(
                status_code=502, detail=f"Could not remove it from the test list: {exc}"
            ) from exc
    await session.delete(task)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/mails", response_model=list[WatchedMailOut], summary="Mails kept, newest first")
async def list_mails(
    admin: SuperAdmin,
    session: Session,
    category: Annotated[
        str | None, Query(description=f"One of: {', '.join(CATEGORIES)}.")
    ] = None,
    stage: Annotated[str | None, Query(description="eoi, rfi, rfq, rfp, rft, negotiation…")] = None,
    verdict: Annotated[
        str | None,
        Query(description="new, existing, stage_change, duplicate_mail, not_found or unsure."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WatchedMailOut]:
    rows = await service.recent(
        session, limit=limit, offset=offset, category=category, stage=stage, verdict=verdict
    )
    return [WatchedMailOut.model_validate(r) for r in rows]


@router.get("/mails/{mail_id}", response_model=WatchedMailDetailOut, summary="One mail, with its body")
async def read_mail(mail_id: uuid.UUID, admin: SuperAdmin, session: Session) -> WatchedMailDetailOut:
    row = await session.get(WatchedMail, mail_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such mail.")
    return WatchedMailDetailOut.model_validate(row)


@router.post(
    "/mails/{mail_id}/classify",
    response_model=WatchedMailDetailOut,
    summary="Read it from the mailbox again and classify it afresh",
)
async def classify_again(
    mail_id: uuid.UUID,
    admin: SuperAdmin,
    session: Session,
    request: Request,
    use_model: Annotated[bool | None, Query(description="Unset follows the settings.")] = None,
) -> WatchedMailDetailOut:
    row = await session.get(WatchedMail, mail_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such mail.")
    settings_row = await service.get_settings(session)
    try:
        await service.reclassify(
            session, row, reader=_reader(request), model=_model(request),
            use_model=settings_row.use_model if use_model is None else use_model,
            sharepoint=_sharepoint(request),
        )
    except service.MailWatchError as exc:
        raise _bad(exc) from exc
    await session.commit()
    await session.refresh(row)
    return WatchedMailDetailOut.model_validate(row)


# ── Graph's webhook ────────────────────────────────────────────────────


@webhook_router.post("/notifications", include_in_schema=False)
async def graph_notification(
    request: Request,
    session: Session,
    validationToken: Annotated[str | None, Query()] = None,
) -> Response:
    """Graph says mail arrived. Echo the validation token when subscribing;
    otherwise check the secret and wake the worker. Always 202, so a bug of
    ours does not become a storm of Graph's retries."""
    if validationToken:
        return Response(content=validationToken, media_type="text/plain")

    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed is simply ignored
        return Response(status_code=status.HTTP_202_ACCEPTED)

    row = await service.get_settings(session)
    if any(service.accepts(row, item.get("clientState")) for item in payload.get("value", []) or []):
        row.last_notified_at = datetime.now(UTC)
        await session.commit()
        worker = getattr(request.app.state, "mailwatch_worker", None)
        if worker is not None:
            worker.kick()
    return Response(status_code=status.HTTP_202_ACCEPTED)
