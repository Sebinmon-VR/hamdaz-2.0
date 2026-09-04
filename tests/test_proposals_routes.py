"""The proposals HTTP surface.

Two gates, and both are tested from the outside: the caller's team must hold the
proposals module, and the rows returned must be theirs. The second is the one
worth attacking — nothing in a request may widen it.
"""

from __future__ import annotations

import pytest

from app.access import service as access
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.proposals.sharepoint import ProposalTask, SharePointError
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/proposals"


def _task(task_id: str, title: str, status: str = "In Progress", due: str | None = None):
    return ProposalTask(
        id=task_id, title=title, status=status, priority="High",
        assigned_to_lookup_id="15", assigned_to_name="Sebin",
        start_date=None, due_date=due, bid_closing_date=None, end_user="Adnoc",
        submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None, web_url=f"https://sp/{task_id}",
    )


class StubSharePoint:
    """Answers per email, so a test can prove one user cannot see another's rows."""

    def __init__(self) -> None:
        self.by_email: dict[str, tuple[str, list[ProposalTask]]] = {}
        self.error: Exception | None = None
        self.asked_for: list[str] = []

    async def lookup_id_for(self, email: str):
        if self.error:
            raise self.error
        entry = self.by_email.get(email.casefold())
        return entry[0] if entry else None

    async def tasks_assigned_to(self, lookup_id: str, *, limit: int = 200):
        if self.error:
            raise self.error
        self.asked_for.append(lookup_id)
        for lid, tasks in self.by_email.values():
            if lid == lookup_id:
                return tasks[:limit]
        return []

    async def list_columns(self):
        if self.error:
            raise self.error
        return [{"name": "Status", "display_name": "Status", "choices": ["Open"]}]


@pytest.fixture
def sharepoint(client):
    stub = StubSharePoint()
    client._transport.app.state.sharepoint = stub
    return stub


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


def _as(client, user):
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await access.seed_modules(db)
    await db.commit()


@pytest.fixture
async def team(db, seeded):
    t = await teams.create_team(db, name="Presales")
    await access.grant_module(db, team=t, module_key="proposals")
    await db.commit()
    return t


@pytest.fixture
async def member(db, team):
    """In the team that has the module, and present in SharePoint."""
    user = await _make(db, "sebin@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


# ── gate 1: the module ─────────────────────────────────────────────────


async def test_a_session_is_required(client, seeded, sharepoint) -> None:
    assert (await client.get(f"{API}/my-tasks")).status_code == 401


async def test_a_team_without_the_module_is_refused(client, db, seeded, sharepoint) -> None:
    other = await teams.create_team(db, name="Finance")
    user = await _make(db, "nobody@hamdaz.com")
    await teams.set_member_roles(db, team=other, user=user, role_keys=["member"])
    await db.commit()

    res = await _as(client, user).get(f"{API}/my-tasks")
    assert res.status_code == 403
    assert "Proposals module" in res.json()["detail"]


async def test_someone_in_no_team_is_refused(client, db, seeded, sharepoint) -> None:
    user = await _make(db, "loner@hamdaz.com")
    assert (await _as(client, user).get(f"{API}/my-tasks")).status_code == 403


async def test_sharepoint_is_not_touched_when_refused(client, db, seeded, sharepoint) -> None:
    """The gate must short-circuit before spending a SharePoint call."""
    user = await _make(db, "loner@hamdaz.com")
    await _as(client, user).get(f"{API}/my-tasks")
    assert sharepoint.asked_for == []


# ── gate 2: ownership ──────────────────────────────────────────────────


async def test_a_member_sees_their_own_tasks(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Mine")])
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()

    assert body["in_sharepoint"] is True
    assert body["sharepoint_user_id"] == "15"
    assert [t["title"] for t in body["tasks"]] == ["Mine"]


async def test_one_user_cannot_see_another_s_tasks(client, db, team, member, sharepoint) -> None:
    """The rule the whole module exists for."""
    other = await _make(db, "goutham@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=other, role_keys=["member"])
    await db.commit()

    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Sebin's")])
    sharepoint.by_email["goutham@hamdaz.com"] = ("27", [_task("2", "Goutham's")])

    mine = (await _as(client, member).get(f"{API}/my-tasks")).json()
    theirs = (await _as(client, other).get(f"{API}/my-tasks")).json()

    assert [t["title"] for t in mine["tasks"]] == ["Sebin's"]
    assert [t["title"] for t in theirs["tasks"]] == ["Goutham's"]


@pytest.mark.parametrize(
    "params",
    [
        {"user": "goutham@hamdaz.com"},
        {"lookup_id": "27"},
        {"assigned_to": "27"},
        {"sharepoint_user_id": "27"},
        {"email": "goutham@hamdaz.com"},
    ],
)
async def test_the_filter_cannot_be_widened_from_the_request(
    client, member, sharepoint, params: dict
) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Mine")])
    sharepoint.by_email["goutham@hamdaz.com"] = ("27", [_task("2", "Not mine")])

    body = (await _as(client, member).get(f"{API}/my-tasks", params=params)).json()
    assert [t["title"] for t in body["tasks"]] == ["Mine"]
    assert sharepoint.asked_for == ["15"]


async def test_someone_absent_from_sharepoint_gets_an_empty_list(
    client, member, sharepoint
) -> None:
    """Not an error: they simply have no presence on that site."""
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert body["in_sharepoint"] is False
    assert body["sharepoint_user_id"] is None
    assert body["tasks"] == []


# ── filtering and ordering ─────────────────────────────────────────────


async def test_open_only_is_the_default(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [_task("1", "Open"), _task("2", "Done", status="Completed")],
    )
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()

    assert [t["title"] for t in body["tasks"]] == ["Open"]
    assert body["total"] == 2  # the count is of everything assigned
    assert body["open_count"] == 1


async def test_completed_tasks_can_be_included(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [_task("1", "Open"), _task("2", "Done", status="Completed")],
    )
    body = (
        await _as(client, member).get(f"{API}/my-tasks", params={"open_only": "false"})
    ).json()
    assert len(body["tasks"]) == 2


async def test_soonest_due_first_and_undated_last(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [
            _task("1", "No date"),
            _task("2", "Later", due="2025-12-01T00:00:00Z"),
            _task("3", "Sooner", due="2025-06-01T00:00:00Z"),
        ],
    )
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert [t["title"] for t in body["tasks"]] == ["Sooner", "Later", "No date"]


@pytest.mark.parametrize("limit", [0, 501])
async def test_a_nonsense_limit_is_rejected(client, member, sharepoint, limit: int) -> None:
    res = await _as(client, member).get(f"{API}/my-tasks", params={"limit": limit})
    assert res.status_code == 422


# ── upstream failure ───────────────────────────────────────────────────


async def test_a_sharepoint_failure_is_a_502(client, member, sharepoint) -> None:
    sharepoint.error = SharePointError("SharePoint returned 503")
    assert (await _as(client, member).get(f"{API}/my-tasks")).status_code == 502


async def test_the_failure_reason_is_not_leaked(client, member, sharepoint) -> None:
    sharepoint.error = SharePointError("token request failed: secret expired")
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert "secret" not in body["detail"]


# ── the list schema ────────────────────────────────────────────────────


async def test_columns_need_the_module_too(client, db, seeded, sharepoint) -> None:
    user = await _make(db, "loner@hamdaz.com")
    assert (await _as(client, user).get(f"{API}/columns")).status_code == 403


async def test_columns_are_returned_to_a_member(client, member, sharepoint) -> None:
    body = (await _as(client, member).get(f"{API}/columns")).json()
    assert body[0]["name"] == "Status"
