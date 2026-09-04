"""Team dashboards: layout resolution and rendering.

The rule under most scrutiny: a widget is shown only while its team still holds
the module it belongs to. Module visibility is the single source of truth, and a
saved layout is a preference on top of it — never a way around it.
"""

from __future__ import annotations

import pytest

from app.access import service as access
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.dashboards import registry, service
from app.dashboards.service import DashboardError, DashboardNotFoundError
from app.roles import service as roles
from app.teams import service as teams


async def _user(db, email: str = "person@hamdaz.com"):
    return await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )


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


# ── the registry ───────────────────────────────────────────────────────


def test_every_widget_declares_a_module() -> None:
    """Without it there is no way to know whether a team should see the card."""
    assert all(w.module for w in registry.all_widgets())


def test_widget_keys_are_unique() -> None:
    keys = [w.key for w in registry.all_widgets()]
    assert len(keys) == len(set(keys))


def test_widgets_are_filtered_by_module() -> None:
    only_teams = {w.key for w in registry.for_modules({"teams"})}
    assert "team_summary" in only_teams
    assert "directory_snapshot" not in only_teams


def test_defaults_are_a_subset_of_available() -> None:
    modules = {"teams", "directory"}
    available = {w.key for w in registry.for_modules(modules)}
    assert {w.key for w in registry.defaults_for(modules)} <= available


# ── layout resolution ──────────────────────────────────────────────────


async def test_a_new_team_gets_defaults(db, team) -> None:
    placements = await service.resolve_layout(db, team)
    assert placements
    assert all(p.configured is False for p in placements)
    assert {p.widget.key for p in placements} == {
        w.key for w in registry.defaults_for({"teams"})
    }


async def test_a_team_with_no_modules_gets_no_widgets(db, seeded) -> None:
    bare = await teams.create_team(db, name="Bare")
    await db.commit()
    assert await service.resolve_layout(db, bare) == []


async def test_setting_a_layout_replaces_the_defaults(db, team) -> None:
    await service.set_layout(
        db,
        team=team,
        entries=[{"widget_key": "role_breakdown"}, {"widget_key": "team_summary"}],
    )
    await db.commit()

    placements = await service.resolve_layout(db, team)
    assert [p.widget.key for p in placements] == ["role_breakdown", "team_summary"]
    assert all(p.configured for p in placements)


async def test_order_follows_the_list(db, team) -> None:
    await service.set_layout(
        db,
        team=team,
        entries=[
            {"widget_key": "team_members"},
            {"widget_key": "team_summary"},
            {"widget_key": "role_breakdown"},
        ],
    )
    await db.commit()
    placements = await service.resolve_layout(db, team)
    assert [p.position for p in placements] == [0, 1, 2]
    assert placements[0].widget.key == "team_members"


async def test_a_disabled_widget_is_kept_but_not_rendered(db, team) -> None:
    """Turning a card back on should restore its settings, so the row survives."""
    await service.set_layout(
        db,
        team=team,
        entries=[
            {"widget_key": "team_summary"},
            {"widget_key": "role_breakdown", "enabled": False, "options": {"x": 1}},
        ],
    )
    await db.commit()

    placements = await service.resolve_layout(db, team)
    disabled = next(p for p in placements if p.widget.key == "role_breakdown")
    assert disabled.enabled is False
    assert disabled.options == {"x": 1}


async def test_options_round_trip(db, team) -> None:
    await service.set_layout(
        db, team=team, entries=[{"widget_key": "team_members", "options": {"limit": 3}}]
    )
    await db.commit()
    placements = await service.resolve_layout(db, team)
    assert placements[0].options == {"limit": 3}


async def test_reset_returns_to_defaults(db, team) -> None:
    await service.set_layout(db, team=team, entries=[{"widget_key": "role_breakdown"}])
    await db.commit()
    await service.reset_layout(db, team=team)
    await db.commit()

    placements = await service.resolve_layout(db, team)
    assert all(p.configured is False for p in placements)


# ── the rule that ties this to module visibility ───────────────────────


async def test_revoking_a_module_hides_its_widget(db, team) -> None:
    await access.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await service.set_layout(
        db,
        team=team,
        entries=[{"widget_key": "team_summary"}, {"widget_key": "directory_snapshot"}],
    )
    await db.commit()
    assert len(await service.resolve_layout(db, team)) == 2

    await access.revoke_module(db, team=team, module_key="directory")
    await db.commit()

    keys = [p.widget.key for p in await service.resolve_layout(db, team)]
    assert keys == ["team_summary"]


async def test_the_saved_row_survives_a_revoke(db, team) -> None:
    """Restoring the module must bring the card back where it was."""
    await access.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await service.set_layout(
        db,
        team=team,
        entries=[{"widget_key": "team_summary"}, {"widget_key": "directory_snapshot"}],
    )
    await db.commit()

    await access.revoke_module(db, team=team, module_key="directory")
    await db.commit()
    await access.grant_module(db, team=team, module_key="directory")
    await db.commit()

    keys = [p.widget.key for p in await service.resolve_layout(db, team)]
    assert keys == ["team_summary", "directory_snapshot"]


async def test_a_widget_for_an_ungranted_module_is_refused(db, team) -> None:
    """Refused loudly rather than silently dropped from the save."""
    with pytest.raises(DashboardError, match="has not been granted"):
        await service.set_layout(
            db, team=team, entries=[{"widget_key": "directory_snapshot"}]
        )


async def test_an_unknown_widget_is_refused(db, team) -> None:
    with pytest.raises(DashboardNotFoundError):
        await service.set_layout(db, team=team, entries=[{"widget_key": "nope"}])


async def test_a_duplicate_widget_is_refused(db, team) -> None:
    with pytest.raises(DashboardError, match="more than once"):
        await service.set_layout(
            db,
            team=team,
            entries=[{"widget_key": "team_summary"}, {"widget_key": "team_summary"}],
        )


async def test_an_entry_without_a_key_is_refused(db, team) -> None:
    with pytest.raises(DashboardError, match="widget_key"):
        await service.set_layout(db, team=team, entries=[{"enabled": True}])


async def test_a_widget_removed_from_the_code_is_ignored(db, team) -> None:
    """A layout must survive a widget being retired, without a migration."""
    from app.models.dashboard import TeamDashboardWidget

    await service.set_layout(db, team=team, entries=[{"widget_key": "team_summary"}])
    await db.commit()
    db.add(TeamDashboardWidget(team_id=team.id, widget_key="retired_widget", position=9))
    await db.commit()

    keys = [p.widget.key for p in await service.resolve_layout(db, team)]
    assert keys == ["team_summary"]


# ── rendering ──────────────────────────────────────────────────────────


class _Directory:
    async def list_users(self, **kwargs):
        return []

    async def get_user(self, object_id):
        raise NotImplementedError


async def _render(db, factory, team, viewer, **kw):
    return await service.render(
        team=team, viewer=viewer, session=db, factory=factory, directory=_Directory(), **kw
    )


async def test_render_produces_a_card_per_enabled_widget(db, team, session_factory) -> None:
    viewer = await _user(db)
    await teams.set_member_roles(db, team=team, user=viewer, role_keys=["team_lead"])
    await db.commit()

    out = await _render(db, session_factory, team, viewer)
    assert {w["key"] for w in out["widgets"]} == {
        w.key for w in registry.defaults_for({"teams"})
    }
    assert out["errors"] == {}


async def test_render_includes_the_viewer_s_own_standing(db, team, session_factory) -> None:
    viewer = await _user(db)
    await teams.set_member_roles(db, team=team, user=viewer, role_keys=["team_lead"])
    await db.commit()

    out = await _render(db, session_factory, team, viewer)
    standing = next(w for w in out["widgets"] if w["key"] == "my_standing")
    assert standing["data"]["is_lead"] is True
    assert standing["data"]["role_keys"] == ["team_lead"]


async def test_render_skips_disabled_widgets(db, team, session_factory) -> None:
    viewer = await _user(db)
    await db.commit()
    await service.set_layout(
        db,
        team=team,
        entries=[
            {"widget_key": "team_summary"},
            {"widget_key": "role_breakdown", "enabled": False},
        ],
    )
    await db.commit()

    out = await _render(db, session_factory, team, viewer)
    assert [w["key"] for w in out["widgets"]] == ["team_summary"]


async def test_render_reports_per_widget_timings(db, team, session_factory) -> None:
    viewer = await _user(db)
    await db.commit()
    out = await _render(db, session_factory, team, viewer)
    assert set(out["meta"]["widget_ms"]) == {w["key"] for w in out["widgets"]}
    # Fanned out, so the total cannot exceed the sum of the parts.
    assert out["meta"]["elapsed_ms"] <= sum(out["meta"]["widget_ms"].values()) + 50


async def test_local_only_skips_remote_widgets(db, team, session_factory) -> None:
    viewer = await _user(db)
    await access.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await service.set_layout(
        db,
        team=team,
        entries=[{"widget_key": "team_summary"}, {"widget_key": "directory_snapshot"}],
    )
    await db.commit()

    out = await _render(db, session_factory, team, viewer, include_remote=False)
    assert [w["key"] for w in out["widgets"]] == ["team_summary"]


async def test_a_failing_widget_does_not_blank_the_page(db, team, session_factory) -> None:
    """One bad card must leave the rest of the dashboard usable."""
    from app.dashboards.registry import Widget, register

    async def _explode(ctx):
        raise RuntimeError("widget is broken")

    # The registry refuses duplicates, so only register on first use.
    if registry.get("broken_for_test") is None:
        register(Widget(
            key="broken_for_test",
            title="Broken",
            description="Always raises.",
            module="teams",
            load=_explode,
        ))

    viewer = await _user(db)
    await db.commit()
    await service.set_layout(
        db,
        team=team,
        entries=[{"widget_key": "team_summary"}, {"widget_key": "broken_for_test"}],
    )
    await db.commit()

    out = await _render(db, session_factory, team, viewer)
    good = next(w for w in out["widgets"] if w["key"] == "team_summary")
    bad = next(w for w in out["widgets"] if w["key"] == "broken_for_test")

    assert good["data"] is not None and good["error"] is None
    assert bad["data"] is None and "widget is broken" in bad["error"]
    assert "broken_for_test" in out["errors"]
