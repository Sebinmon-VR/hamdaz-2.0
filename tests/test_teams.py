"""The team service: teams, membership, and the invariants around both."""

from __future__ import annotations

import uuid

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.models.team import slugify
from app.roles import service as roles
from app.teams import service
from app.teams.service import TeamConflictError, TeamError, TeamNotFoundError


async def _user(db, email: str = "person@hamdaz.com"):
    return await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await db.commit()


# ── slugs ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Site Operations (UAE)", "site-operations-uae"),
        ("  Finance  ", "finance"),
        ("R&D / Labs", "r-d-labs"),
        ("MEP", "mep"),
        ("---", ""),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


async def test_slug_is_derived_from_the_name(db, seeded) -> None:
    team = await service.create_team(db, name="Site Operations (UAE)")
    assert team.slug == "site-operations-uae"


async def test_duplicate_names_get_distinct_slugs(db, seeded) -> None:
    first = await service.create_team(db, name="Finance")
    second = await service.create_team(db, name="Finance")
    third = await service.create_team(db, name="Finance")
    assert [first.slug, second.slug, third.slug] == ["finance", "finance-2", "finance-3"]


async def test_an_explicit_slug_clash_is_an_error(db, seeded) -> None:
    """Auto-derived slugs de-duplicate; a slug you chose deliberately must not."""
    await service.create_team(db, name="Finance", slug="fin")
    with pytest.raises(TeamConflictError, match="already exists"):
        await service.create_team(db, name="Something Else", slug="fin")


async def test_a_team_needs_a_name(db, seeded) -> None:
    with pytest.raises(TeamError, match="needs a name"):
        await service.create_team(db, name="   ")


# ── lookup ─────────────────────────────────────────────────────────────


async def test_a_team_can_be_fetched_by_slug_or_id(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    await db.commit()
    assert (await service.get_team(db, "finance")).id == team.id
    assert (await service.get_team(db, team.id)).id == team.id


async def test_unknown_team_raises(db, seeded) -> None:
    with pytest.raises(TeamNotFoundError):
        await service.get_team(db, "no-such-team")


async def test_search_matches_name_and_slug(db, seeded) -> None:
    await service.create_team(db, name="Site Operations")
    await service.create_team(db, name="Finance")
    await db.commit()
    assert [t.name for t in await service.list_teams(db, search="site")] == ["Site Operations"]
    assert [t.name for t in await service.list_teams(db, search="FINANCE")] == ["Finance"]


# ── archiving and deleting ─────────────────────────────────────────────


async def test_archived_teams_are_hidden_by_default(db, seeded) -> None:
    await service.create_team(db, name="Old")
    await db.commit()
    await service.archive_team(db, "old")
    await db.commit()

    assert await service.list_teams(db) == []
    assert len(await service.list_teams(db, include_archived=True)) == 1


async def test_archiving_is_reversible(db, seeded) -> None:
    await service.create_team(db, name="Old")
    await db.commit()
    await service.archive_team(db, "old")
    restored = await service.restore_team(db, "old")
    assert restored.is_archived is False


async def test_archiving_twice_keeps_the_first_timestamp(db, seeded) -> None:
    await service.create_team(db, name="Old")
    await db.commit()
    first = (await service.archive_team(db, "old")).archived_at
    assert (await service.archive_team(db, "old")).archived_at == first


async def test_a_live_team_cannot_be_deleted(db, seeded) -> None:
    """One mistaken call must not destroy a team and everyone's place in it."""
    await service.create_team(db, name="Live")
    await db.commit()
    with pytest.raises(TeamConflictError, match="archive it first"):
        await service.delete_team(db, "live")


async def test_an_archived_team_can_be_deleted(db, seeded) -> None:
    await service.create_team(db, name="Gone")
    await db.commit()
    await service.archive_team(db, "gone")
    await service.delete_team(db, "gone")
    with pytest.raises(TeamNotFoundError):
        await service.get_team(db, "gone")


async def test_deleting_a_team_removes_its_memberships(db, seeded) -> None:
    team = await service.create_team(db, name="Gone")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()

    await service.archive_team(db, "gone")
    await service.delete_team(db, "gone")
    await db.commit()

    assert await service.teams_for_user(db, user.id) == []


# ── membership ─────────────────────────────────────────────────────────


async def test_a_member_defaults_to_the_member_role(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    rows = await service.set_member_roles(db, team=team, user=user, role_keys=[])
    assert [r.role.key for r in rows] == ["member"]


async def test_a_person_can_hold_several_roles_in_one_team(db, seeded) -> None:
    """Leading a team and approving its work often land on the same person."""
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    rows = await service.set_member_roles(
        db, team=team, user=user, role_keys=["team_lead", "approver"]
    )
    assert sorted(r.role.key for r in rows) == ["approver", "team_lead"]


async def test_setting_roles_replaces_rather_than_adds(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["team_lead", "approver"])
    rows = await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    assert [r.role.key for r in rows] == ["member"]


async def test_setting_the_same_roles_twice_changes_nothing(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    first = await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    second = await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    assert {r.id for r in first} == {r.id for r in second}


async def test_duplicate_role_keys_collapse(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    rows = await service.set_member_roles(
        db, team=team, user=user, role_keys=["member", "member"]
    )
    assert len(rows) == 1


async def test_a_global_role_cannot_be_held_inside_a_team(db, seeded) -> None:
    """"manager of this team" is not a thing the model should be able to say."""
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    with pytest.raises(TeamConflictError, match="organisation-wide"):
        await service.set_member_roles(db, team=team, user=user, role_keys=["manager"])


async def test_an_unknown_role_is_refused(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    with pytest.raises(TeamNotFoundError, match="No such role"):
        await service.set_member_roles(db, team=team, user=user, role_keys=["wizard"])


async def test_removing_a_member_removes_every_role_they_held(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["team_lead", "approver"])
    await db.commit()

    await service.remove_member(db, team=team, user_id=user.id)
    assert await service.member_rows(db, team.id, user.id) == []


async def test_removing_someone_who_is_not_a_member_raises(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    with pytest.raises(TeamNotFoundError, match="not in this team"):
        await service.remove_member(db, team=team, user_id=user.id)


# ── views over membership ──────────────────────────────────────────────


async def test_member_count_counts_people_not_rows(db, seeded) -> None:
    """Someone who is both lead and approver is still one member."""
    team = await service.create_team(db, name="Finance")
    both = await _user(db, "both@hamdaz.com")
    plain = await _user(db, "plain@hamdaz.com")
    await service.set_member_roles(db, team=team, user=both, role_keys=["team_lead", "approver"])
    await service.set_member_roles(db, team=team, user=plain, role_keys=["member"])
    await db.commit()

    assert (await service.member_counts(db, [team.id]))[team.id] == 2


async def test_member_counts_reports_zero_for_an_empty_team(db, seeded) -> None:
    team = await service.create_team(db, name="Empty")
    await db.commit()
    assert (await service.member_counts(db, [team.id]))[team.id] == 0


async def test_members_are_grouped_by_person(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["team_lead", "approver"])
    await db.commit()

    members = await service.list_members(db, team.id)
    assert len(members) == 1
    assert sorted(r.role.key for r in members[0][1]) == ["approver", "team_lead"]


async def test_someone_can_lead_one_team_and_merely_join_another(db, seeded) -> None:
    """The whole point of team-scoped roles."""
    led = await service.create_team(db, name="Alpha")
    joined = await service.create_team(db, name="Beta")
    user = await _user(db)
    await service.set_member_roles(db, team=led, user=user, role_keys=["team_lead"])
    await service.set_member_roles(db, team=joined, user=user, role_keys=["member"])
    await db.commit()

    by_team = {t.slug: keys for t, keys in await service.teams_for_user(db, user.id)}
    assert by_team == {"alpha": ["team_lead"], "beta": ["member"]}


async def test_teams_for_user_hides_archived_by_default(db, seeded) -> None:
    team = await service.create_team(db, name="Old")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    await service.archive_team(db, "old")
    await db.commit()

    assert await service.teams_for_user(db, user.id) == []
    assert len(await service.teams_for_user(db, user.id, include_archived=True)) == 1


async def test_team_role_keys_are_scoped_to_that_team(db, seeded) -> None:
    alpha = await service.create_team(db, name="Alpha")
    beta = await service.create_team(db, name="Beta")
    user = await _user(db)
    await service.set_member_roles(db, team=alpha, user=user, role_keys=["team_lead"])
    await db.commit()

    assert await service.team_role_keys(db, team_id=alpha.id, user_id=user.id) == {"team_lead"}
    assert await service.team_role_keys(db, team_id=beta.id, user_id=user.id) == set()


async def test_leads_lists_only_team_leads(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    lead = await _user(db, "lead@hamdaz.com")
    plain = await _user(db, "plain@hamdaz.com")
    await service.set_member_roles(db, team=team, user=lead, role_keys=["team_lead"])
    await service.set_member_roles(db, team=team, user=plain, role_keys=["member"])
    await db.commit()

    assert [u.email for u in await service.leads(db, team.id)] == ["lead@hamdaz.com"]


async def test_deleting_a_user_removes_them_from_their_teams(db, seeded) -> None:
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()

    await db.delete(user)
    await db.commit()

    assert await service.list_members(db, team.id) == []
    assert (await service.member_counts(db, [team.id]))[team.id] == 0


async def test_a_role_held_in_a_team_cannot_be_deleted(db, seeded) -> None:
    """RESTRICT on the FK: deleting the role would silently empty the team."""
    from app.roles.service import RoleConflictError

    await roles.create_role(db, key="reviewer", name="Reviewer", scope="team")
    team = await service.create_team(db, name="Finance")
    user = await _user(db)
    await service.set_member_roles(db, team=team, user=user, role_keys=["reviewer"])
    await db.commit()

    with pytest.raises(RoleConflictError):
        await roles.delete_role(db, "reviewer")


async def test_member_counts_of_nothing_is_empty(db) -> None:
    assert await service.member_counts(db, []) == {}


async def test_teams_for_an_unknown_user_is_empty(db, seeded) -> None:
    assert await service.teams_for_user(db, uuid.uuid4()) == []
