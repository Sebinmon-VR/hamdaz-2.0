"""Nova: the agent every member of the presales team is given.

The user's wish (2026-10-15): one agent, the same for everyone in presales —
not one each person builds — called Nova. When a task is assigned to you it
asks you in Teams for the documents, then does the rest on its own: reads
them, pulls out the items, looks at what we paid before, finds suppliers in
our history and on the web, and drafts the RFQ emails. Then it asks before
sending, and sends the ones you tick from your own mailbox. The results come
back to you in Teams.

Since 2026-10-16 nobody is given it unasked: a presales member adds Nova from
their agents page if they want it (``add_for``). Agents are there to build
when you want one, not something set up for you when you sign in.

Its job is defined here, in code, and written onto each member's copy every
time it is refreshed, so a change here reaches everyone at once and nobody's
Nova drifts. Its owner can switch it off (``always_on``) but not edit it. The
step ids are fixed: the token on a Teams card is signed over the run and the
step id, and they keep meaning the same step.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent import UserAgent
from app.models.team import Team, TeamMembership
from app.models.user import User

KEY: Final = "nova"
NAME: Final = "Nova"
ROLE: Final = "Starts on every task assigned to you: gets the documents, finds suppliers, drafts the RFQs and sends them once you say yes"
AVATAR: Final[dict[str, str]] = {
    "body": "mochi", "colour": "purple", "eyes": "sparkle", "mouth": "smile", "accessory": "sprout",
}
INSTRUCTIONS: Final = (
    "When a presales task is assigned to me, ask me for its documents if they are not on the task yet. "
    "Read what the customer wants, check what we paid and quoted before, find suppliers — ours first, then the "
    "web — and draft an RFQ to each. Ask me before sending anything, and send only the ones I tick, from my "
    "mailbox. Then watch for the suppliers' replies and tell me in Teams; when I say take a quote, put it on the "
    "task's quote request with the costing report, and send it for approval when I say so."
)
STEPS: Final[list[dict[str, Any]]] = [
    {"id": "nova-start", "kind": "start_new_task", "config": {}},
    {"id": "nova-docs", "kind": "ask_documents", "config": {"days": 3}},
    {"id": "nova-read", "kind": "read_documents", "config": {"which": "all"}},
    {"id": "nova-items", "kind": "extract_requirements", "config": {"ai": True}},
    {"id": "nova-history", "kind": "lookup_history", "config": {"since": "3y"}},
    {"id": "nova-suppliers", "kind": "find_suppliers", "config": {"count": 8, "where": "uae", "web": True}},
    {"id": "nova-prices", "kind": "compare_quotes", "config": {"pick": "cheapest", "margin": 0}},
    {"id": "nova-drafts", "kind": "draft_email", "config": {
        "to": "suppliers", "subject": "Request for quotation — {task title}", "ai": True,
    }},
    {"id": "nova-send", "kind": "send_emails", "config": {}},
]


def _apply(agent: UserAgent) -> bool:
    """Write the definition onto one copy. True if anything changed."""
    want = {"name": NAME, "role": ROLE, "avatar": AVATAR, "instructions": INSTRUCTIONS, "steps": STEPS}
    changed = False
    for key, value in want.items():
        if getattr(agent, key) != value:
            setattr(agent, key, value)
            changed = True
    return changed


async def members(session: AsyncSession, settings: Settings) -> list[User]:
    """The active people in the presales team (NOVA_TEAM_SLUG)."""
    return list((await session.scalars(
        select(User)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(Team.slug == settings.nova_team_slug, Team.archived_at.is_(None), User.is_active.is_(True))
        .distinct()
    )).all())


async def in_presales(session: AsyncSession, user: User, settings: Settings) -> bool:
    """Whether this person may add Nova: a member of the presales team (NOVA_TEAM_SLUG)."""
    found = await session.scalar(
        select(TeamMembership.id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(TeamMembership.user_id == user.id, Team.slug == settings.nova_team_slug, Team.archived_at.is_(None))
        .limit(1)
    )
    return found is not None


async def mine(session: AsyncSession, user: User) -> UserAgent | None:
    return await session.scalar(select(UserAgent).where(UserAgent.owner_id == user.id, UserAgent.system_key == KEY))


async def refresh_for(session: AsyncSession, user: User) -> UserAgent | None:
    """This person's Nova brought up to date, if they added one. Never makes one."""
    agent = await mine(session, user)
    if agent is not None and _apply(agent):
        await session.flush()
    return agent


async def add_for(session: AsyncSession, user: User, settings: Settings) -> UserAgent | None:
    """Add Nova for this person because they asked — None when they are not in presales."""
    if not await in_presales(session, user, settings):
        return None
    agent = await mine(session, user)
    if agent is None:
        agent = UserAgent(owner_id=user.id, system_key=KEY, always_on=True, trigger_state={})
        session.add(agent)
    _apply(agent)
    await session.flush()
    return agent


async def refresh_all(session: AsyncSession, settings: Settings) -> None:
    """Every Nova someone added, brought up to date. Makes none.

    Somebody who has left presales keeps their Nova, switched off: its runs
    are theirs to look back on, but it no longer starts on their tasks.
    """
    in_team = {u.id for u in await members(session, settings)}
    for agent in (await session.scalars(select(UserAgent).where(UserAgent.system_key == KEY))).all():
        _apply(agent)
        if agent.owner_id not in in_team and agent.always_on:
            agent.always_on = False
    await session.flush()
