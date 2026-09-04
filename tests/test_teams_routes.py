"""The teams HTTP surface: every operation, and who is refused each one."""

from __future__ import annotations

import uuid

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.directory.graph import OrgUser
from app.roles import service as roles

SESSION_COOKIE = "hamdaz_session"
BASE = "/api/v1/teams"


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


def _org(oid: str, name: str) -> OrgUser:
    return OrgUser(
        object_id=oid, display_name=name, email=f"{name.lower()}@hamdaz.com",
        user_principal_name=f"{name.lower()}@hamdaz.com", job_title=None, department=None,
        office_location=None, mobile_phone=None, account_enabled=True, user_type="Member",
    )


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await db.commit()


@pytest.fixture
async def admin(db, seeded):
    return await _make(db, "boss@hamdaz.com", "manager")


@pytest.fixture
async def nobody(db, seeded):
    return await _make(db, "nobody@hamdaz.com")


@pytest.fixture
async def team(client, admin):
    res = await _as(client, admin).post(BASE, json={"name": "Site Operations"})
    return res.json()


# ── creating ───────────────────────────────────────────────────────────


async def test_creating_requires_a_session(client, seeded) -> None:
    assert (await client.post(BASE, json={"name": "X"})).status_code == 401


async def test_a_plain_user_cannot_create_a_team(client, nobody) -> None:
    assert (await _as(client, nobody).post(BASE, json={"name": "X"})).status_code == 403


async def test_an_admin_creates_a_team(client, admin) -> None:
    res = await _as(client, admin).post(
        BASE, json={"name": "Site Operations (UAE)", "description": "Field delivery"}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["slug"] == "site-operations-uae"
    assert body["member_count"] == 0
    assert body["created_by_id"] == str(admin.id)


async def test_a_blank_name_is_rejected(client, admin) -> None:
    assert (await _as(client, admin).post(BASE, json={"name": ""})).status_code == 422


async def test_an_explicit_slug_clash_is_a_conflict(client, admin) -> None:
    await _as(client, admin).post(BASE, json={"name": "A", "slug": "ops"})
    res = await _as(client, admin).post(BASE, json={"name": "B", "slug": "ops"})
    assert res.status_code == 409


# ── reading ────────────────────────────────────────────────────────────


async def test_any_signed_in_user_can_list_teams(client, nobody, admin) -> None:
    await _as(client, admin).post(BASE, json={"name": "Finance"})
    res = await _as(client, nobody).get(BASE)
    assert res.status_code == 200
    assert [t["slug"] for t in res.json()] == ["finance"]


async def test_a_team_can_be_fetched_by_slug(client, nobody, team) -> None:
    res = await _as(client, nobody).get(f"{BASE}/{team['slug']}")
    assert res.status_code == 200
    assert res.json()["members"] == []


async def test_an_unknown_team_is_404(client, nobody, seeded) -> None:
    assert (await _as(client, nobody).get(f"{BASE}/nope")).status_code == 404


async def test_my_teams_is_empty_for_someone_in_none(client, nobody) -> None:
    res = await _as(client, nobody).get(f"{BASE}/me")
    assert res.status_code == 200
    assert res.json() == []


# ── members ────────────────────────────────────────────────────────────


async def test_an_admin_adds_a_member(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members",
        json={"user_id": str(target.id), "role_keys": ["team_lead", "approver"]},
    )
    assert res.status_code == 201
    assert res.json()["role_keys"] == ["approver", "team_lead"]


async def test_adding_without_a_role_defaults_to_member(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": str(target.id)}
    )
    assert res.json()["role_keys"] == ["member"]


async def test_a_member_can_be_added_by_entra_object_id(client, admin, team, graph) -> None:
    """Someone straight out of the directory who has never signed in."""
    oid = str(uuid.uuid4())
    graph.users = [_org(oid, "Newcomer")]

    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": oid, "role_keys": ["member"]}
    )
    assert res.status_code == 201
    assert res.json()["email"] == "newcomer@hamdaz.com"
    assert res.json()["entra_object_id"] == oid


async def test_a_plain_user_cannot_add_members(client, db, nobody, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    res = await _as(client, nobody).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": str(target.id)}
    )
    assert res.status_code == 403


async def test_a_global_role_inside_a_team_is_a_conflict(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members",
        json={"user_id": str(target.id), "role_keys": ["manager"]},
    )
    assert res.status_code == 409
    assert "organisation-wide" in res.json()["detail"]


async def test_changing_someone_s_roles_replaces_them(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members",
        json={"user_id": str(target.id), "role_keys": ["team_lead", "approver"]},
    )
    res = await _as(client, admin).patch(
        f"{BASE}/{team['slug']}/members/{target.id}", json={"role_keys": ["member"]}
    )
    assert res.status_code == 200
    assert res.json()["role_keys"] == ["member"]


async def test_changing_roles_of_a_non_member_is_404(client, db, admin, team) -> None:
    outsider = await _make(db, "outsider@hamdaz.com")
    res = await _as(client, admin).patch(
        f"{BASE}/{team['slug']}/members/{outsider.id}", json={"role_keys": ["member"]}
    )
    assert res.status_code == 404


async def test_removing_a_member(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": str(target.id)}
    )
    assert (
        await _as(client, admin).delete(f"{BASE}/{team['slug']}/members/{target.id}")
    ).status_code == 204
    assert (await _as(client, admin).get(f"{BASE}/{team['slug']}/members")).json() == []


async def test_removing_a_non_member_is_404(client, db, admin, team) -> None:
    outsider = await _make(db, "outsider@hamdaz.com")
    res = await _as(client, admin).delete(f"{BASE}/{team['slug']}/members/{outsider.id}")
    assert res.status_code == 404


async def test_a_plain_user_cannot_remove_members(client, db, nobody, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": str(target.id)}
    )
    res = await _as(client, nobody).delete(f"{BASE}/{team['slug']}/members/{target.id}")
    assert res.status_code == 403


# ── bulk ───────────────────────────────────────────────────────────────


async def test_bulk_add(client, db, admin, team) -> None:
    a = await _make(db, "a@hamdaz.com")
    b = await _make(db, "b@hamdaz.com")
    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members/bulk",
        json={"user_ids": [str(a.id), str(b.id)], "role_keys": ["member"]},
    )
    assert res.status_code == 200
    assert len(res.json()["added"]) == 2
    assert res.json()["failed"] == []


async def test_bulk_reports_failures_without_discarding_the_rest(
    client, db, admin, team, graph
) -> None:
    """A partial failure must not read as success."""
    good = await _make(db, "good@hamdaz.com")
    graph.users = []

    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members/bulk",
        json={"user_ids": [str(good.id), str(uuid.uuid4())], "role_keys": ["member"]},
    )
    body = res.json()
    assert [m["email"] for m in body["added"]] == ["good@hamdaz.com"]
    assert len(body["failed"]) == 1
    assert "reason" in body["failed"][0]


async def test_bulk_needs_at_least_one_id(client, admin, team) -> None:
    res = await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members/bulk", json={"user_ids": []}
    )
    assert res.status_code == 422


# ── editing, archiving, deleting ───────────────────────────────────────


async def test_an_admin_renames_a_team(client, admin, team) -> None:
    res = await _as(client, admin).patch(
        f"{BASE}/{team['slug']}", json={"name": "Site Ops", "description": "Renamed"}
    )
    assert res.status_code == 200
    assert res.json()["name"] == "Site Ops"
    # The handle is stable unless changed on purpose, so existing links survive.
    assert res.json()["slug"] == team["slug"]


async def test_a_plain_user_cannot_rename(client, nobody, team) -> None:
    res = await _as(client, nobody).patch(f"{BASE}/{team['slug']}", json={"name": "X"})
    assert res.status_code == 403


async def test_archiving_hides_a_team_from_the_default_listing(client, admin, team) -> None:
    assert (await _as(client, admin).post(f"{BASE}/{team['slug']}/archive")).status_code == 200
    assert (await _as(client, admin).get(BASE)).json() == []
    archived = await _as(client, admin).get(BASE, params={"include_archived": "true"})
    assert len(archived.json()) == 1


async def test_restoring_brings_it_back(client, admin, team) -> None:
    await _as(client, admin).post(f"{BASE}/{team['slug']}/archive")
    assert (await _as(client, admin).post(f"{BASE}/{team['slug']}/restore")).status_code == 200
    assert len((await _as(client, admin).get(BASE)).json()) == 1


async def test_a_live_team_cannot_be_deleted(client, admin, team) -> None:
    res = await _as(client, admin).delete(f"{BASE}/{team['slug']}")
    assert res.status_code == 409
    assert "archive it first" in res.json()["detail"]


async def test_an_archived_team_can_be_deleted(client, admin, team) -> None:
    await _as(client, admin).post(f"{BASE}/{team['slug']}/archive")
    assert (await _as(client, admin).delete(f"{BASE}/{team['slug']}")).status_code == 204
    assert (await _as(client, admin).get(f"{BASE}/{team['slug']}")).status_code == 404


async def test_a_plain_user_cannot_archive_or_delete(client, nobody, team) -> None:
    assert (await _as(client, nobody).post(f"{BASE}/{team['slug']}/archive")).status_code == 403
    assert (await _as(client, nobody).delete(f"{BASE}/{team['slug']}")).status_code == 403


# ── membership views ───────────────────────────────────────────────────


async def test_member_count_counts_people_not_rows(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members",
        json={"user_id": str(target.id), "role_keys": ["team_lead", "approver"]},
    )
    assert (await _as(client, admin).get(f"{BASE}/{team['slug']}")).json()["member_count"] == 1


async def test_my_teams_lists_the_roles_held_in_each(client, db, admin) -> None:
    alpha = (await _as(client, admin).post(BASE, json={"name": "Alpha"})).json()
    beta = (await _as(client, admin).post(BASE, json={"name": "Beta"})).json()
    member = await _make(db, "member@hamdaz.com")

    await _as(client, admin).post(
        f"{BASE}/{alpha['slug']}/members",
        json={"user_id": str(member.id), "role_keys": ["team_lead"]},
    )
    await _as(client, admin).post(
        f"{BASE}/{beta['slug']}/members",
        json={"user_id": str(member.id), "role_keys": ["member"]},
    )

    body = (await _as(client, member).get(f"{BASE}/me")).json()
    assert {t["team"]["slug"]: t["role_keys"] for t in body} == {
        "alpha": ["team_lead"], "beta": ["member"],
    }


async def test_teams_for_another_user(client, db, admin, team) -> None:
    target = await _make(db, "target@hamdaz.com")
    await _as(client, admin).post(
        f"{BASE}/{team['slug']}/members", json={"user_id": str(target.id)}
    )
    res = await _as(client, admin).get(f"{BASE}/by-user/{target.id}")
    assert res.status_code == 200
    assert [t["team"]["slug"] for t in res.json()] == [team["slug"]]


async def test_teams_for_an_unknown_user_is_404(client, admin, seeded) -> None:
    assert (await _as(client, admin).get(f"{BASE}/by-user/{uuid.uuid4()}")).status_code == 404
