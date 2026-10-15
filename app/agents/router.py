"""AI agents: each person's own, and their runs.

Open to every signed-in user and gated on nothing, like notifications: an
agent belongs to the person who made it and works as them, so every query is
scoped to the caller and no route takes a user id. Another person's agent is a
404, not a 403 — whether it exists is not theirs to know.

A run reads the task's documents and our history as its owner could on a
screen; it writes only its own row, and the owner's notifications.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import assist, engine
from app.agents.model import Spent
from app.enquiries.claude import LLMError
from app.agents.schemas import (
    AgentIn,
    AgentOut,
    AnswerIn,
    AssistIn,
    AssistOut,
    GuideIn,
    GuideOut,
    RecentRunOut,
    RunOut,
    RunSummaryOut,
    StatusOut,
    TaskOut,
    TeamsAnswerIn,
    TeamsAnswerOut,
)
from app.auth.deps import CurrentUser
from app.core.config import get_settings
from app.core.db import get_session, get_session_factory
from app.models.agent import AgentRun, UserAgent
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User

router = APIRouter(prefix="/agents", tags=["agents"])

Session = Annotated[AsyncSession, Depends(get_session)]

MAX_AGENTS = 30
MAX_UPLOADS = 10
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
#: Runs one person may have going at once.
MAX_LIVE_RUNS = 3


def get_deps(request: Request) -> engine.Deps:
    state = request.app.state
    return engine.Deps(
        settings=get_settings(),
        factory=get_session_factory(),
        sharepoint=state.sharepoint,
        drive=state.quote_drive,
        model=state.agent_model,
        free=state.text_model,
        mailer=getattr(state, "agent_mailer", None),
        http=state.http,
    )


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


def _steps(raw: list) -> list:
    try:
        return engine.validate_steps(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ── what the run panel needs first ─────────────────────────────────────


@router.get("/status", response_model=StatusOut)
async def agent_status(request: Request, user: CurrentUser) -> StatusOut:
    model = request.app.state.agent_model
    return StatusOut(model=model.model, effort=model.effort, configured=model.configured, web_allowed=model.web_allowed)


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
async def answer_from_teams(body: TeamsAnswerIn, session: Session, response: Response) -> TeamsAnswerOut:
    """An answer given in Teams, posted back by the owner's Power Automate flow.

    No session here — Power Automate has none. What it carries instead is the
    token the run put on that one question (``engine.answer_token``): it opens
    this run's question at this step and nothing else, and it is checked in
    constant time. When the flow says who answered, that must be the agent's
    owner. A question already answered in the app says so, so the flow can
    put that on the card instead.
    """
    import hmac

    expected = engine.answer_token(get_settings().session_secret, body.run_id, body.step_id)
    if not hmac.compare_digest(expected, body.token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This answer link is not valid.")
    run = await session.get(AgentRun, body.run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    owner = await session.get(User, run.owner_id)
    if body.responder and owner and owner.email and body.responder.strip().lower() != owner.email.lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the agent's owner can answer it.")
    runner = engine.RUNNING.get(run.id)
    asked = (run.question or {}).get("step_id")
    if run.status != "waiting" or runner is None or asked != body.step_id:
        response.status_code = status.HTTP_409_CONFLICT
        return TeamsAnswerOut(ok=False, message="Already answered, or the run has moved on.")
    go = body.go if isinstance(body.go, bool) else body.go.strip().lower() not in ("false", "0", "no", "stop", "")
    note = body.text.strip()
    runner.give_answer(go, f"{note} (answered in Teams)" if note else "")
    return TeamsAnswerOut(ok=True, message="Thanks — carrying on." if go else "Stopped, as you asked.")


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
    return list((await session.scalars(
        select(UserAgent).where(UserAgent.owner_id == user.id).order_by(UserAgent.created_at.desc())
    )).all())


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
    agent.name, agent.role = body.name.strip(), body.role.strip()
    agent.avatar, agent.instructions, agent.steps = body.avatar, body.instructions, _steps(body.steps)
    await session.flush()
    await session.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, session: Session, user: CurrentUser) -> Response:
    agent = await _agent(session, agent_id, user)
    for run_id, runner in list(engine.RUNNING.items()):
        if runner.run.agent_id == agent.id:
            runner.stop()
    await session.delete(agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── runs ───────────────────────────────────────────────────────────────


@router.post("/{agent_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_run(
    agent_id: uuid.UUID,
    session: Session,
    user: CurrentUser,
    deps: Dependencies,
    task_id: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> AgentRun:
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
        results=[],
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
async def answer_run(run_id: uuid.UUID, body: AnswerIn, session: Session, user: CurrentUser) -> AgentRun:
    run = await _run(session, run_id, user)
    runner = engine.RUNNING.get(run.id)
    if run.status != "waiting" or runner is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This run is not waiting for an answer.")
    runner.give_answer(body.go, body.text)
    return run


@router.post("/runs/{run_id}/stop", response_model=RunOut)
async def stop_run(run_id: uuid.UUID, session: Session, user: CurrentUser) -> AgentRun:
    run = await _run(session, run_id, user)
    runner = engine.RUNNING.get(run.id)
    if runner is None:
        engine.settle_orphan(run)
        return run
    runner.stop()
    return run
