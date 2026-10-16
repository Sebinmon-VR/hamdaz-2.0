"""AI agents: each person's own, and their runs.

Open to every signed-in user and gated on nothing, like notifications: an
agent belongs to the person who made it and works as them, so every query is
scoped to the caller and no route takes a user id. Another person's agent is a
404, not a 403 — whether it exists is not theirs to know.

A run reads the task's documents and our history as its owner could on a
screen; it writes its own row and the owner's notifications, files documents
its owner hands it into the task's folder, and sends mail from the owner's
mailbox only when they tick it.

Members of the presales team can add Nova (``app.agents.nova``) if they want
it — nobody is given it unasked. Its job is the app's, so it cannot be edited,
only switched off or removed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import assist, engine, nova, rfqs
from app.agents.model import Spent
from app.agents.schemas import (
    AgentIn,
    AgentOut,
    AnswerIn,
    AssistIn,
    AssistOut,
    GoIn,
    GuideIn,
    GuideOut,
    RecentRunOut,
    RfqOut,
    RunOut,
    RunSummaryOut,
    StatusOut,
    SwitchIn,
    TaskOut,
    TeamsAnswerIn,
    TeamsAnswerOut,
)
from app.auth.deps import CurrentUser
from app.core.config import get_settings
from app.core.db import get_session, get_session_factory
from app.enquiries.claude import LLMError
from app.models.agent import AgentRfq, AgentRun, UserAgent
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User
from app.quoting.storage import DriveError

router = APIRouter(prefix="/agents", tags=["agents"])

Session = Annotated[AsyncSession, Depends(get_session)]

MAX_AGENTS = 30
MAX_UPLOADS = 10
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
#: Runs one person may have going at once.
MAX_LIVE_RUNS = 3


def get_deps(request: Request) -> engine.Deps:
    return engine.deps_for(request.app.state, get_settings(), get_session_factory())


Dependencies = Annotated[engine.Deps, Depends(get_deps)]


async def _agent(session: AsyncSession, agent_id: uuid.UUID, user: User) -> UserAgent:
    agent = await session.get(UserAgent, agent_id)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such agent.")
    return agent


async def _run(session: AsyncSession, run_id: uuid.UUID, user: User) -> AgentRun:
    run = await session.get(AgentRun, run_id)
    if run is None or run.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    return run


def _not_system(agent: UserAgent) -> None:
    if agent.system_key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{agent.name} does the same job for everyone in your team, so it cannot be edited. "
            "You can switch it off, or remove it.",
        )


async def _answer(session: AsyncSession, deps: engine.Deps, run: AgentRun, answer: dict) -> None:
    """Hand an answer to the run: onto its row, to its runner if it is here, or pick a cut-off run up."""
    engine.record_answer(run, answer)
    await session.commit()
    runner = engine.RUNNING.get(run.id)
    if runner is not None:
        runner.give_answer(answer)
    elif engine.is_cut_off(run):
        await engine.answer_cut_off(deps, run.id)


def _steps(raw: list) -> list:
    try:
        return engine.validate_steps(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ── what the run panel needs first ─────────────────────────────────────


@router.get("/status", response_model=StatusOut)
async def agent_status(request: Request, session: Session, user: CurrentUser) -> StatusOut:
    model = request.app.state.agent_model
    settings = get_settings()
    return StatusOut(
        model=model.model, effort=model.effort, configured=model.configured, web_allowed=model.web_allowed,
        nova_available=await nova.in_presales(session, user, settings) and await nova.mine(session, user) is None,
        triggers_on=settings.agent_triggers_enabled and settings.mirror_sync_enabled,
        teams_on=bool(settings.agent_teams_ask_url or settings.agent_teams_webhook_url),
    )


@router.get("/tasks", response_model=list[TaskOut])
async def my_tasks(session: Session, user: CurrentUser) -> list[TaskOut]:
    """The caller's open Proposals tasks, to run an agent on. From the mirror."""
    rows = (await session.scalars(
        select(ProposalIndexItem)
        .where(func.lower(ProposalIndexItem.assigned_email) == (user.email or "").lower())
        .where(ProposalIndexItem.is_open.is_(True), ProposalIndexItem.deleted.is_(False))
        .order_by(ProposalIndexItem.deadline.asc().nulls_last())
        .limit(200)
    )).all()
    return [
        TaskOut(id=r.item_id, title=r.title, end_user=r.end_user,
                deadline=r.bid_closing_date or r.deadline or r.due_date, status=r.effective_status or r.status)
        for r in rows
    ]


@router.post("/assist", response_model=AssistOut)
async def assist_person(body: AssistIn, request: Request, session: Session, user: CurrentUser) -> AssistOut:
    """The companion's bubble: a walk-through, a sum the app can do, or an answer."""
    question = body.question.strip()
    if assist.wants_guide(question):
        return AssistOut(answer="Let me show you.", how="guide", guide_goal=question)

    around = assist.Around(
        column=body.around.column, row=body.around.row, section=body.around.section,
        currencies=tuple(c.upper() for c in body.around.currencies), page_text=body.around.page_text,
    )
    converted = await assist.convert(
        getattr(request.app.state, "zoho", None), question=question, selection=body.selection, around=around
    )
    if converted is not None:
        return AssistOut(answer=converted[0], how=converted[1])

    name = "Your agent"
    if body.agent_id is not None:
        agent = await session.get(UserAgent, body.agent_id)
        if agent is not None and agent.owner_id == user.id:
            name = agent.name
    model = request.app.state.agent_model
    spent = Spent()
    try:
        said, how, who = await assist.answer(
            model, request.app.state.text_model, agent=name, person=user.display_name, page=body.page,
            selection=body.selection, around=around, question=question,
            history=[t.model_dump() for t in body.history], user_key=str(user.id), spent=spent,
            paid_fallback=get_settings().agent_explain_paid_fallback,
        )
    except LLMError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return AssistOut(answer=said, how=how, model=who, cost_usd=spent.cost(model.model))


@router.post("/teams/answer", response_model=TeamsAnswerOut)
async def answer_from_teams(
    body: TeamsAnswerIn, session: Session, response: Response, deps: Dependencies
) -> TeamsAnswerOut:
    """An answer given in Teams, posted back by the owner's Power Automate flow.

    No session here — Power Automate has none. What it carries instead is the
    token the run put on that one card (``engine.answer_token``): it opens
    this run's question at this step and nothing else, and it is checked in
    constant time. When the flow says who answered, that must be the agent's
    owner. A question already answered in the app says so, so the flow can
    put that on the card instead. "Got it" on a card that only told the owner
    something (``info-…``) is thanked and changes nothing.

    On "send these emails?" Teams can only say don't send: the emails are
    reviewed, edited and confirmed in the app, never sent from a card.
    """
    import hmac

    expected = engine.answer_token(get_settings().session_secret, body.run_id, body.step_id)
    if not hmac.compare_digest(expected, body.token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This answer link is not valid.")
    if body.step_id.startswith("info-"):
        return TeamsAnswerOut(ok=True, message="Thanks!")
    go = body.go if isinstance(body.go, bool) else body.go.strip().lower() not in ("false", "0", "no", "stop", "")
    if body.step_id.startswith(("rfq-", "qa-")):
        return await _follow_up_from_teams(session, deps, body, go, response)
    run = await session.get(AgentRun, body.run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    owner = await session.get(User, run.owner_id)
    if body.responder and owner and owner.email and body.responder.strip().lower() != owner.email.lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the agent's owner can answer it.")
    question = run.question or {}
    if run.status != "waiting" or question.get("step_id") != body.step_id or question.get("answer"):
        response.status_code = status.HTTP_409_CONFLICT
        return TeamsAnswerOut(ok=False, message="Already answered, or the run has moved on.")
    go = body.go if isinstance(body.go, bool) else body.go.strip().lower() not in ("false", "0", "no", "stop", "")
    note = body.text.strip() or str((body.data or {}).get("note") or "").strip()
    answer: dict = {"go": go, "text": f"{note} (answered in Teams)" if note else "", "by": "teams"}
    message = "Thanks — carrying on." if go else "Stopped, as you asked."
    if question.get("mode") == "send" and go:
        # Never from Teams: the emails are reviewed, edited and confirmed in the app.
        response.status_code = status.HTTP_409_CONFLICT
        return TeamsAnswerOut(ok=False, message="Open the app to review the emails and send them — nothing was sent.")
    if question.get("mode") == "send":
        message = "Not sent, as you asked."
    elif question.get("mode") == "docs" and go:
        message = "Thanks — I'll pick the documents up and start."
    await _answer(session, deps, run, answer)
    return TeamsAnswerOut(ok=True, message=message)


async def _follow_up_from_teams(
    session: AsyncSession, deps: engine.Deps, body: TeamsAnswerIn, go: bool, response: Response
) -> TeamsAnswerOut:
    """"Take their quote" (``rfq-<id>``) and "Send for approval" (``qa-<id>``) from Teams."""
    kind, _, raw = body.step_id.partition("-")
    try:
        rfq = await session.get(AgentRfq, uuid.UUID(raw))
    except ValueError:
        rfq = None
    if rfq is None or rfq.run_id != body.run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such RFQ.")
    owner = await session.get(User, rfq.owner_id)
    if body.responder and owner and owner.email and body.responder.strip().lower() != owner.email.lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the agent's owner can answer it.")
    who = rfq.supplier_name or rfq.supplier_email
    if kind == "rfq":
        if rfq.status not in ("replied", "failed"):
            response.status_code = status.HTTP_409_CONFLICT
            return TeamsAnswerOut(ok=False, message="Already answered.")
        rfqs.schedule(rfqs.take(deps, rfq.id, go))
        return TeamsAnswerOut(ok=True, message=f"Taking {who}'s quote — I'll come back with the report." if go else "Left it, as you said.")
    if rfq.status != "taken":
        response.status_code = status.HTTP_409_CONFLICT
        return TeamsAnswerOut(ok=False, message="Already answered.")
    rfqs.schedule(rfqs.submit(deps, rfq.id, go))
    return TeamsAnswerOut(ok=True, message="On it." if go else "Not sent, as you said.")


@router.post("/guide/next", response_model=GuideOut)
async def guide_next(body: GuideIn, request: Request, user: CurrentUser) -> GuideOut:
    """The next step of a walk-through, from what is on the person's screen now."""
    step = await assist.next_step(
        request.app.state.text_model,
        person=user.display_name,
        goal=body.goal,
        page_title=body.page,
        path=body.path,
        elements=[assist.Element(e.id, e.label, e.kind) for e in body.elements],
        pages=[assist.Page(p.path, p.label) for p in body.pages],
        done=body.done,
    )
    return GuideOut(action=step.action, say=step.say, target=step.target, path=step.path, how=step.how)


# ── agents ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[AgentOut])
async def list_agents(session: Session, user: CurrentUser) -> list[UserAgent]:
    # Only brought up to date here; made only when they add it (POST /agents/nova).
    await nova.refresh_for(session, user)
    return list((await session.scalars(
        select(UserAgent).where(UserAgent.owner_id == user.id).order_by(UserAgent.created_at.desc())
    )).all())


@router.post("/nova", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def add_nova(session: Session, user: CurrentUser) -> UserAgent:
    """Add Nova, the presales team's agent — only when its owner asks for it."""
    agent = await nova.add_for(session, user, get_settings())
    if agent is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nova is for members of the presales team.")
    await session.refresh(agent)
    return agent


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentIn, session: Session, user: CurrentUser) -> UserAgent:
    count = await session.scalar(select(func.count()).select_from(UserAgent).where(UserAgent.owner_id == user.id))
    if (count or 0) >= MAX_AGENTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"You can have at most {MAX_AGENTS} agents.")
    agent = UserAgent(
        owner_id=user.id, name=body.name.strip(), role=body.role.strip(), avatar=body.avatar,
        instructions=body.instructions, steps=_steps(body.steps),
    )
    if body.id is not None and await session.get(UserAgent, body.id) is None:
        agent.id = body.id
    session.add(agent)
    await session.flush()
    await session.refresh(agent)
    return agent


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: uuid.UUID, session: Session, user: CurrentUser) -> UserAgent:
    return await _agent(session, agent_id, user)


@router.put("/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: uuid.UUID, body: AgentIn, session: Session, user: CurrentUser) -> UserAgent:
    agent = await _agent(session, agent_id, user)
    _not_system(agent)
    agent.name, agent.role = body.name.strip(), body.role.strip()
    agent.avatar, agent.instructions, agent.steps = body.avatar, body.instructions, _steps(body.steps)
    await session.flush()
    await session.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, session: Session, user: CurrentUser) -> Response:
    # Nova too: they added it, so they may take it away again.
    agent = await _agent(session, agent_id, user)
    for _run_id, runner in list(engine.RUNNING.items()):
        if runner.run.agent_id == agent.id:
            runner.stop()
    await session.delete(agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


#: A face is a small PNG; anything bigger is not one.
MAX_FACE_BYTES = 400_000


@router.post("/{agent_id}/face", status_code=status.HTTP_204_NO_CONTENT)
async def save_face(
    agent_id: uuid.UUID,
    session: Session,
    user: CurrentUser,
    face: Annotated[UploadFile, File()],
    key: Annotated[str, Form(max_length=120)],
) -> Response:
    """The agent's face as a PNG, drawn by the owner's browser, for its Teams cards."""
    agent = await _agent(session, agent_id, user)
    content = await face.read()
    if len(content) > MAX_FACE_BYTES or not content.startswith(b"\x89PNG"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A face must be a PNG under 400 KB.")
    agent.face_png, agent.face_key = content, key.strip()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/face.png", include_in_schema=False)
async def agent_face(agent_id: uuid.UUID, session: Session) -> Response:
    """The face, for Teams to fetch — no session, as Teams has none. A cartoon,
    behind an id nobody can guess; nothing about the agent's owner or work."""
    png = await session.scalar(select(UserAgent.face_png).where(UserAgent.id == agent_id))
    if not png:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No face yet.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@router.put("/{agent_id}/always-on", response_model=AgentOut)
async def switch_agent(agent_id: uuid.UUID, body: SwitchIn, session: Session, user: CurrentUser) -> UserAgent:
    """Let its trigger start it on its own, or not. Switched on, it starts with what is new from now."""
    agent = await _agent(session, agent_id, user)
    if body.always_on and not agent.always_on:
        agent.trigger_state = {}
    agent.always_on = body.always_on
    await session.flush()
    await session.refresh(agent)
    return agent


# ── runs ───────────────────────────────────────────────────────────────


@router.post("/{agent_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_run(
    agent_id: uuid.UUID,
    session: Session,
    user: CurrentUser,
    deps: Dependencies,
    task_id: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    as_assigned: Annotated[bool, Form()] = False,
) -> AgentRun:
    """Run it now. ``as_assigned`` runs it as though the task had just been
    assigned — what the always-on trigger would do — to try that out on a
    task that is already in the list."""
    agent = await _agent(session, agent_id, user)
    if not agent.steps:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This agent has no steps yet. Add some on “What it does”.")
    live = sum(1 for r in engine.RUNNING.values() if r.run.owner_id == user.id)
    if live >= MAX_LIVE_RUNS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"You have {live} runs going. Let one finish first.")

    uploads: list[tuple[str, bytes, str | None]] = []
    total = 0
    for upload in (files or [])[: MAX_UPLOADS + 1]:
        if len(uploads) >= MAX_UPLOADS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"At most {MAX_UPLOADS} files per run.")
        content = await upload.read()
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The files are too large together (40 MB at most).")
        uploads.append((upload.filename or "document", content, upload.content_type))

    task_title = None
    task_id = (task_id or "").strip() or None
    if task_id:
        row = await session.get(ProposalIndexItem, task_id)
        if row is None or row.deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That task is not in the Proposals list.")
        task_title = row.title

    run = AgentRun(
        agent_id=agent.id, owner_id=user.id, task_id=task_id, task_title=task_title, status="running",
        plan={"name": agent.name, "instructions": agent.instructions, "steps": agent.steps},
        results=[], started_by="trigger" if as_assigned and task_id else "hand",
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    # Committed before the background run opens its own session and looks for it.
    await session.commit()
    engine.start(deps, run.id, uploads)
    return run


@router.get("/{agent_id}/runs", response_model=list[RunSummaryOut])
async def list_runs(agent_id: uuid.UUID, session: Session, user: CurrentUser) -> list[AgentRun]:
    await _agent(session, agent_id, user)
    runs = list((await session.scalars(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.created_at.desc()).limit(30)
    )).all())
    for run in runs:
        engine.settle_orphan(run)
    return runs


@router.get("/runs/recent", response_model=list[RecentRunOut])
async def recent_runs(session: Session, user: CurrentUser, limit: int = 12) -> list[RecentRunOut]:
    """The caller's latest runs across every agent — the ones waiting for them first."""
    rows = (await session.execute(
        select(AgentRun, UserAgent.name, UserAgent.avatar)
        .join(UserAgent, UserAgent.id == AgentRun.agent_id)
        .where(AgentRun.owner_id == user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )).all()
    out = []
    for run, name, avatar in rows:
        engine.settle_orphan(run)
        out.append(RecentRunOut.model_validate({
            **RunSummaryOut.model_validate(run).model_dump(),
            "agent_name": name, "agent_avatar": avatar or {},
            "question_title": (run.question or {}).get("title") if run.status == "waiting" else None,
        }))
    out.sort(key=lambda r: r.status != "waiting")
    return out


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, session: Session, user: CurrentUser) -> AgentRun:
    run = await _run(session, run_id, user)
    engine.settle_orphan(run)
    return run


@router.post("/runs/{run_id}/answer", response_model=RunOut)
async def answer_run(
    run_id: uuid.UUID, body: AnswerIn, session: Session, user: CurrentUser, deps: Dependencies
) -> AgentRun:
    run = await _run(session, run_id, user)
    question = run.question or {}
    if run.status != "waiting" or not question or question.get("answer"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run is not waiting for an answer.")
    if engine.settle_orphan(run):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run was cut off by a restart. Run it again.")
    answer: dict = {"go": body.go, "text": body.text.strip()[:4000], "by": "app"}
    if question.get("mode") == "send" and body.go:
        # The failsafe: supplier mail goes only once the owner has read the
        # emails, picked them one by one and confirmed the list.
        if not body.confirmed or not body.picks:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick the emails to send and confirm them — nothing goes without that.",
            )
        answer["reviewed"] = True
    if body.picks is not None:
        answer["picks"] = body.picks
    if body.edits:
        answer["edits"] = {
            k: {f: str(v)[:20_000] for f, v in e.items() if f in ("subject", "body", "to_email")}
            for k, e in body.edits.items()
        }
    await _answer(session, deps, run, answer)
    await session.refresh(run)
    return run


@router.post("/runs/{run_id}/documents", response_model=RunOut)
async def hand_documents(
    run_id: uuid.UUID,
    session: Session,
    user: CurrentUser,
    deps: Dependencies,
    files: Annotated[list[UploadFile], File()],
) -> AgentRun:
    """The documents a run asked for, given in the app: filed into the task's folder, then it carries on.

    Into the folder rather than into the run, so the team has them too and
    the run reads them where it would have found them anyway.
    """
    run = await _run(session, run_id, user)
    question = run.question or {}
    if run.status != "waiting" or question.get("mode") != "docs" or question.get("answer"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run is not waiting for documents.")
    if not deps.drive.enabled or not run.task_title:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Documents cannot be filed here — attach them to the task instead.")
    if not files or len(files) > MAX_UPLOADS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Add between 1 and {MAX_UPLOADS} files.")
    contents: list[tuple[UploadFile, bytes]] = []
    total = 0
    for upload in files:
        content = await upload.read()
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The files are too large together (40 MB at most).")
        contents.append((upload, content))
    try:
        folder = await deps.drive.find_task_folder(run.task_title)
        for upload, content in contents:
            await deps.drive.file_document(
                folder=folder.name, filename=upload.filename or "document", content=content,
                content_type=upload.content_type,
            )
    except DriveError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The files could not be filed: {exc}") from exc
    await _answer(session, deps, run, {"go": True, "text": "", "by": "app", "filed": len(contents)})
    await session.refresh(run)
    return run


@router.post("/runs/{run_id}/stop", response_model=RunOut)
async def stop_run(run_id: uuid.UUID, session: Session, user: CurrentUser) -> AgentRun:
    run = await _run(session, run_id, user)
    runner = engine.RUNNING.get(run.id)
    if runner is not None:
        runner.stop()
    elif engine.is_cut_off(run):
        if not engine.settle_orphan(run):
            # Cut off while waiting on documents or a yes to send: nothing to carry on for.
            run.status, run.question, run.finished_at = "stopped", None, datetime.now(UTC)
            run.summary = f"{(run.plan or {}).get('name') or 'Your agent'} stopped: stopped by you."
    elif run.status == "waiting" and not (run.question or {}).get("answer"):
        # Waiting in another process: it reads this from the row.
        engine.record_answer(run, {"go": False, "text": "", "stop": True, "by": "app"})
    return run


# ── after the RFQs: replies, quotes, approval ──────────────────────────


async def _rfq(session: AsyncSession, rfq_id: uuid.UUID, user: User) -> AgentRfq:
    rfq = await session.get(AgentRfq, rfq_id)
    if rfq is None or rfq.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such RFQ.")
    return rfq


@router.get("/runs/{run_id}/rfqs", response_model=list[RfqOut])
async def run_rfqs(run_id: uuid.UUID, session: Session, user: CurrentUser) -> list[AgentRfq]:
    """The RFQs a run sent, and what became of each."""
    await _run(session, run_id, user)
    return list((await session.scalars(
        select(AgentRfq).where(AgentRfq.run_id == run_id).order_by(AgentRfq.created_at)
    )).all())


@router.post("/runs/{run_id}/rfqs/check", response_model=list[RfqOut])
async def check_replies(run_id: uuid.UUID, session: Session, user: CurrentUser, deps: Dependencies) -> list[AgentRfq]:
    """Look in your inbox for replies to this run's RFQs now, rather than at the next round."""
    await _run(session, run_id, user)
    found = await rfqs.watch(session, deps, run_id=run_id)
    await session.commit()
    if found:
        rfqs.schedule(rfqs.tell_replied(deps, found))
    return await run_rfqs(run_id, session, user)


@router.post("/rfqs/{rfq_id}/take", response_model=RfqOut)
async def take_quote(rfq_id: uuid.UUID, body: GoIn, session: Session, user: CurrentUser, deps: Dependencies) -> AgentRfq:
    """Take the supplier's quote (onto the quote request, priced, report made) — or leave it."""
    rfq = await _rfq(session, rfq_id, user)
    if rfq.status not in ("replied", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This reply has been answered already.")
    rfqs.schedule(rfqs.take(deps, rfq.id, body.go))
    return rfq


@router.post("/rfqs/{rfq_id}/approval", response_model=RfqOut)
async def send_for_approval(
    rfq_id: uuid.UUID, body: GoIn, session: Session, user: CurrentUser, deps: Dependencies
) -> AgentRfq:
    """Send the quote request for approval (in test mode: who it would go to) — or not yet."""
    rfq = await _rfq(session, rfq_id, user)
    if rfq.status != "taken":
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no quote ready to send for approval.")
    rfqs.schedule(rfqs.submit(deps, rfq.id, body.go))
    return rfq
