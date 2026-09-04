"""The visibility HTTP surface.

The central rule under test: setting visibility is super-admin-only, which is
deliberately narrower than the admin role that governs everything else. A manager
who can create a team must still be refused here.
"""

from __future__ import annotations

import uuid

import pytest

from app.access import service
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1"


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
    await service.seed_modules(db)
    await db.commit()


@pytest.fixture
async def team(db, seeded):
    t = await teams.create_team(db, name="Site Operations")
    await db.commit()
    return t


@pytest.fixture
async def super_admin(db, seeded):
    return await _make(db, "boss@hamdaz.com", "super_admin")


@pytest.fixture
async def manager(db, seeded):
    return await _make(db, "manager@hamdaz.com", "manager")


@pytest.fixture
async def member(db, team, seeded):
    user = await _make(db, "member@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


# ── the catalogue ──────────────────────────────────────────────────────


async def test_modules_require_a_session(client, seeded) -> None:
    assert (await client.get(f"{API}/modules")).status_code == 401


async def test_any_signed_in_user_can_read_the_catalogue(client, member) -> None:
    res = await _as(client, member).get(f"{API}/modules")
    assert res.status_code == 200
    keys = {m["key"] for m in res.json()}
    assert {"dashboard", "directory", "teams"} <= keys


async def test_the_catalogue_marks_admin_only_modules(client, member) -> None:
    body = {m["key"]: m for m in (await _as(client, member).get(f"{API}/modules")).json()}
    assert body["roles"]["admin_only"] is True
    assert body["directory"]["admin_only"] is False


# ── who may change visibility ──────────────────────────────────────────


async def test_a_super_admin_grants_a_module(client, super_admin, team) -> None:
    res = await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    assert res.status_code == 201
    assert res.json()["modules"][0]["module_key"] == "directory"


async def test_a_manager_cannot_grant_a_module(client, manager, team) -> None:
    """The distinction the brief asks for: only a super admin sets the rules."""
    res = await _as(client, manager).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    assert res.status_code == 403
    assert "super admin" in res.json()["detail"].lower()


async def test_a_manager_can_still_create_a_team(client, manager) -> None:
    """Proves the refusal above is about visibility, not about being an admin."""
    res = await _as(client, manager).post(f"{API}/teams", json={"name": "Mgr Team"})
    assert res.status_code == 201


@pytest.mark.parametrize("method,path,body", [
    ("post", "access", {"module_key": "directory"}),
    ("put", "access", {"modules": {}}),
])
async def test_a_plain_member_cannot_change_visibility(
    client, member, team, method: str, path: str, body: dict
) -> None:
    res = await getattr(_as(client, member), method)(
        f"{API}/teams/{team.slug}/{path}", json=body
    )
    assert res.status_code == 403


async def test_a_manager_cannot_revoke(client, manager, super_admin, team) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    res = await _as(client, manager).delete(f"{API}/teams/{team.slug}/access/directory")
    assert res.status_code == 403


async def test_reading_a_team_s_access_is_open(client, member, team) -> None:
    assert (await _as(client, member).get(f"{API}/teams/{team.slug}/access")).status_code == 200


# ── granting mechanics ─────────────────────────────────────────────────


async def test_granting_specific_pages(client, super_admin, team) -> None:
    res = await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access",
        json={"module_key": "teams", "page_keys": ["list"]},
    )
    grant = res.json()["modules"][0]
    assert grant["all_pages"] is False
    assert [p["key"] for p in grant["pages"]] == ["list"]


async def test_granting_an_admin_only_module_is_a_conflict(client, super_admin, team) -> None:
    res = await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "roles"}
    )
    assert res.status_code == 409


async def test_granting_an_unknown_module_is_404(client, super_admin, team) -> None:
    res = await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "payroll"}
    )
    assert res.status_code == 404


async def test_granting_an_unknown_page_is_404(client, super_admin, team) -> None:
    res = await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access",
        json={"module_key": "teams", "page_keys": ["nope"]},
    )
    assert res.status_code == 404


async def test_put_replaces_the_whole_set(client, super_admin, team) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    res = await _as(client, super_admin).put(
        f"{API}/teams/{team.slug}/access", json={"modules": {"dashboard": None}}
    )
    assert {m["module_key"] for m in res.json()["modules"]} == {"dashboard"}


async def test_revoking(client, super_admin, team) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    assert (
        await _as(client, super_admin).delete(f"{API}/teams/{team.slug}/access/directory")
    ).status_code == 204
    assert (
        await _as(client, super_admin).delete(f"{API}/teams/{team.slug}/access/directory")
    ).status_code == 404


async def test_access_on_an_unknown_team_is_404(client, super_admin) -> None:
    res = await _as(client, super_admin).get(f"{API}/teams/no-such-team/access")
    assert res.status_code == 404


# ── effective access ───────────────────────────────────────────────────


async def test_a_member_of_a_team_with_nothing_sees_nothing(client, member) -> None:
    body = (await _as(client, member).get(f"{API}/access/me")).json()
    assert body["modules"] == []
    assert body["source"] == "teams"


async def test_a_member_sees_what_their_team_was_given(
    client, member, super_admin, team
) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access",
        json={"module_key": "teams", "page_keys": ["list"]},
    )
    body = (await _as(client, member).get(f"{API}/access/me")).json()

    assert [m["key"] for m in body["modules"]] == ["teams"]
    assert [p["key"] for p in body["modules"][0]["pages"]] == ["list"]
    assert body["via_teams"] == [team.slug]


async def test_a_super_admin_sees_everything(client, super_admin) -> None:
    body = (await _as(client, super_admin).get(f"{API}/access/me")).json()
    assert body["source"] == "super_admin"
    assert {"roles", "user_admin", "dashboard"} <= {m["key"] for m in body["modules"]}


async def test_revoking_takes_effect_on_the_next_request(
    client, member, super_admin, team
) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    assert (await _as(client, member).get(f"{API}/access/me")).json()["modules"]

    await _as(client, super_admin).delete(f"{API}/teams/{team.slug}/access/directory")
    assert (await _as(client, member).get(f"{API}/access/me")).json()["modules"] == []


async def test_someone_else_s_access_can_be_inspected(
    client, member, super_admin, team
) -> None:
    await _as(client, super_admin).post(
        f"{API}/teams/{team.slug}/access", json={"module_key": "directory"}
    )
    body = (await _as(client, super_admin).get(f"{API}/access/users/{member.id}")).json()
    assert [m["key"] for m in body["modules"]] == ["directory"]


async def test_access_for_an_unknown_user_is_404(client, super_admin) -> None:
    res = await _as(client, super_admin).get(f"{API}/access/users/{uuid.uuid4()}")
    assert res.status_code == 404


async def test_access_me_requires_a_session(client, seeded) -> None:
    assert (await client.get(f"{API}/access/me")).status_code == 401
