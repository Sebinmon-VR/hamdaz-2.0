"""The dashboard HTTP surface."""

from __future__ import annotations

import pytest

from app.access import service as access
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
    await access.seed_modules(db)
    await db.commit()


@pytest.fixture
async def team(db, seeded):
    t = await teams.create_team(db, name="Site Operations")
    await access.grant_module(db, team=t, module_key="teams")
    await db.commit()
    return t


@pytest.fixture
async def admin(db, seeded):
    return await _make(db, "boss@hamdaz.com", "manager")


@pytest.fixture
async def member(db, team, seeded):
    user = await _make(db, "member@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["team_lead"])
    await db.commit()
    return user


# ── the catalogue ──────────────────────────────────────────────────────


async def test_widgets_require_a_session(client, seeded) -> None:
    assert (await client.get(f"{API}/widgets")).status_code == 401


async def test_the_widget_catalogue_is_readable(client, member) -> None:
    res = await _as(client, member).get(f"{API}/widgets")
    assert res.status_code == 200
    assert {"team_summary", "directory_snapshot"} <= {w["key"] for w in res.json()}


# ── rendering ──────────────────────────────────────────────────────────


async def test_a_dashboard_renders_for_a_team(client, member, team) -> None:
    res = await _as(client, member).get(
        f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == team.slug
    assert body["widgets"]
    assert body["errors"] == {}


async def test_the_dashboard_shows_the_viewer_s_own_standing(client, member, team) -> None:
    body = (
        await _as(client, member).get(
            f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
        )
    ).json()
    standing = next(w for w in body["widgets"] if w["key"] == "my_standing")
    assert standing["data"]["is_lead"] is True


async def test_two_teams_get_their_own_data(client, db, admin, member, team) -> None:
    """The point of the module: same cards, each team's own numbers."""
    other = await teams.create_team(db, name="Finance")
    await access.grant_module(db, team=other, module_key="teams")
    await db.commit()

    first = (
        await _as(client, admin).get(
            f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
        )
    ).json()
    second = (
        await _as(client, admin).get(
            f"{API}/teams/{other.slug}/dashboard", params={"local_only": "true"}
        )
    ).json()

    def members(body):
        return next(w for w in body["widgets"] if w["key"] == "team_members")["data"]["total"]

    assert first["slug"] != second["slug"]
    assert members(first) == 1
    assert members(second) == 0


async def test_a_team_with_no_modules_renders_an_empty_dashboard(
    client, db, admin, seeded
) -> None:
    bare = await teams.create_team(db, name="Bare")
    await db.commit()
    body = (await _as(client, admin).get(f"{API}/teams/{bare.slug}/dashboard")).json()
    assert body["widgets"] == []


async def test_an_unknown_team_is_404(client, admin, seeded) -> None:
    assert (await _as(client, admin).get(f"{API}/teams/nope/dashboard")).status_code == 404


async def test_the_dashboard_requires_a_session(client, team) -> None:
    assert (await client.get(f"{API}/teams/{team.slug}/dashboard")).status_code == 401


async def test_meta_reports_timings(client, member, team) -> None:
    body = (
        await _as(client, member).get(
            f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
        )
    ).json()
    assert set(body["meta"]["widget_ms"]) == {w["key"] for w in body["widgets"]}


# ── layout ─────────────────────────────────────────────────────────────


async def test_a_new_team_reports_an_unconfigured_layout(client, member, team) -> None:
    body = (await _as(client, member).get(f"{API}/teams/{team.slug}/dashboard/layout")).json()
    assert body["configured"] is False
    assert body["widgets"]


async def test_available_lists_only_what_the_team_could_use(client, member, team) -> None:
    body = (await _as(client, member).get(f"{API}/teams/{team.slug}/dashboard/layout")).json()
    keys = {w["key"] for w in body["available"]}
    assert "team_summary" in keys
    assert "directory_snapshot" not in keys  # the team has no directory module


async def test_an_admin_arranges_the_layout(client, admin, team) -> None:
    res = await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={
            "widgets": [
                {"widget_key": "role_breakdown"},
                {"widget_key": "team_members", "options": {"limit": 3}},
            ]
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True
    assert [w["widget_key"] for w in body["widgets"]] == ["role_breakdown", "team_members"]
    assert body["widgets"][1]["options"] == {"limit": 3}


async def test_the_arrangement_changes_what_renders(client, admin, member, team) -> None:
    await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={"widgets": [{"widget_key": "role_breakdown"}]},
    )
    body = (
        await _as(client, member).get(
            f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
        )
    ).json()
    assert [w["key"] for w in body["widgets"]] == ["role_breakdown"]


async def test_a_plain_member_cannot_arrange(client, member, team) -> None:
    res = await _as(client, member).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={"widgets": [{"widget_key": "team_summary"}]},
    )
    assert res.status_code == 403


async def test_a_plain_member_can_still_view(client, member, team) -> None:
    res = await _as(client, member).get(
        f"{API}/teams/{team.slug}/dashboard", params={"local_only": "true"}
    )
    assert res.status_code == 200


async def test_a_widget_for_an_ungranted_module_is_a_400(client, admin, team) -> None:
    res = await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={"widgets": [{"widget_key": "directory_snapshot"}]},
    )
    assert res.status_code == 400
    assert "not been granted" in res.json()["detail"]


async def test_an_unknown_widget_is_a_404(client, admin, team) -> None:
    res = await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={"widgets": [{"widget_key": "nope"}]},
    )
    assert res.status_code == 404


async def test_reset_returns_to_defaults(client, admin, team) -> None:
    await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={"widgets": [{"widget_key": "role_breakdown"}]},
    )
    res = await _as(client, admin).delete(f"{API}/teams/{team.slug}/dashboard/layout")
    assert res.status_code == 200
    assert res.json()["configured"] is False


async def test_a_plain_member_cannot_reset(client, member, team) -> None:
    assert (
        await _as(client, member).delete(f"{API}/teams/{team.slug}/dashboard/layout")
    ).status_code == 403


# ── revoking a module ──────────────────────────────────────────────────


async def test_revoking_a_module_removes_its_card(client, db, admin, team) -> None:
    """Visibility is the source of truth; the layout cannot outrank it."""
    await access.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await _as(client, admin).put(
        f"{API}/teams/{team.slug}/dashboard/layout",
        json={
            "widgets": [
                {"widget_key": "team_summary"},
                {"widget_key": "directory_snapshot"},
            ]
        },
    )
    await access.revoke_module(db, team=team, module_key="directory")
    await db.commit()

    body = (await _as(client, admin).get(f"{API}/teams/{team.slug}/dashboard/layout")).json()
    assert [w["widget_key"] for w in body["widgets"]] == ["team_summary"]


# ── the caller's own dashboards ────────────────────────────────────────


async def test_my_dashboards_covers_each_team(client, db, member, team) -> None:
    other = await teams.create_team(db, name="Finance")
    await access.grant_module(db, team=other, module_key="teams")
    await teams.set_member_roles(db, team=other, user=member, role_keys=["member"])
    await db.commit()

    body = (await _as(client, member).get(f"{API}/dashboards/me")).json()
    assert {d["slug"] for d in body} == {team.slug, other.slug}


async def test_my_dashboards_is_empty_for_someone_in_no_team(client, admin) -> None:
    assert (await _as(client, admin).get(f"{API}/dashboards/me")).json() == []
