"""The assignment policy: the settings, and who may change them.

Two things are worth attacking here.

**The reach rule.** A manager may edit their own team's policy and nothing else.
Getting that wrong is not a crash — it is one manager quietly reshaping another
team's workload, invisible to the team it affects.

**Capacity arithmetic.** "New joiners get one task for every two" is a 0.5
multiplier, and the rule that a *lower* label wins is what stops a protective
label being cancelled out by a flattering one.

Nothing here assigns work, and nothing writes outside this database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.assignment import service as policy_service
from app.assignment.router import _ratio
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.labels import service as labels
from app.roles import service as roles
from app.teams import service as teams


async def person(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await labels.seed_labels(db)
    await db.commit()


@pytest.fixture
async def presales(db, seeded):
    team = await teams.create_team(db, name="Presales")
    await db.commit()
    return team


@pytest.fixture
async def estimation(db, seeded):
    team = await teams.create_team(db, name="Estimation")
    await db.commit()
    return team


async def known(db) -> set[str]:
    return {label.key for label in await labels.all_labels(db)}


# ── the default and the fallback ───────────────────────────────────────


async def test_the_default_is_created_on_first_use(db, seeded) -> None:
    policy = await policy_service.default_policy(db)
    await db.commit()

    assert policy.team_id is None
    assert policy.capacity_by_label["new-joiner"] == 0.5
    assert "on-leave" in policy.excluded_labels


async def test_a_team_without_its_own_uses_the_default(db, presales) -> None:
    resolved = await policy_service.for_team(db, presales.id)
    await db.commit()
    assert resolved.team_id is None


async def test_a_team_policy_is_seeded_from_the_default(db, presales) -> None:
    """The first edit should change something that already works."""
    actor = await person(db, "boss@hamdaz.com", "super_admin")
    own = await policy_service.create_for_team(db, team=presales, actor=actor)
    await db.commit()

    assert own.team_id == presales.id
    assert own.capacity_by_label["new-joiner"] == 0.5


async def test_a_disabled_team_policy_falls_back_to_the_default(db, presales) -> None:
    """Off means 'use the default', not 'assign to nobody'."""
    actor = await person(db, "boss@hamdaz.com", "super_admin")
    own = await policy_service.create_for_team(db, team=presales, actor=actor)
    own.enabled = False
    await db.commit()

    assert (await policy_service.for_team(db, presales.id)).team_id is None


async def test_the_default_cannot_be_deleted(db, seeded) -> None:
    with pytest.raises(policy_service.PolicyError, match="cannot be deleted"):
        await policy_service.delete_for_team(db, await policy_service.default_policy(db))


# ── who may edit ───────────────────────────────────────────────────────


async def test_a_super_admin_may_edit_anything(db, presales) -> None:
    actor = await person(db, "root@hamdaz.com", "super_admin")
    assert (
        await policy_service.reach(db, user=actor, roles={"super_admin"}, team_id=None)
    ).may_edit
    assert (
        await policy_service.reach(db, user=actor, roles={"super_admin"}, team_id=presales.id)
    ).may_edit


async def test_the_ceo_may_edit_anything(db, presales) -> None:
    actor = await person(db, "ceo@hamdaz.com", "ceo")
    assert (await policy_service.reach(db, user=actor, roles={"ceo"}, team_id=None)).may_edit


async def test_a_manager_may_edit_their_own_team(db, presales) -> None:
    """`manager` is an organisation-wide role; belonging to the team is separate.

    That separation is the whole mechanism — the role says what kind of person
    they are, the membership says which teams they may act on.
    """
    manager = await person(db, "mgr@hamdaz.com", "manager")
    await teams.set_member_roles(db, team=presales, user=manager, role_keys=["team_lead"])
    await db.commit()

    allowed = await policy_service.reach(
        db, user=manager, roles={"manager"}, team_id=presales.id
    )
    assert allowed.may_edit


async def test_a_manager_may_not_edit_another_team(db, presales, estimation) -> None:
    """The one that matters: reshaping a team you have nothing to do with."""
    manager = await person(db, "mgr@hamdaz.com", "manager")
    await teams.set_member_roles(db, team=presales, user=manager, role_keys=["team_lead"])
    await db.commit()

    allowed = await policy_service.reach(
        db, user=manager, roles={"manager"}, team_id=estimation.id
    )
    assert not allowed.may_edit
    assert "own teams" in allowed.reason


async def test_a_manager_may_not_edit_the_org_default(db, presales) -> None:
    """The default governs teams they have nothing to do with."""
    manager = await person(db, "mgr@hamdaz.com", "manager")
    await teams.set_member_roles(db, team=presales, user=manager, role_keys=["team_lead"])
    await db.commit()

    allowed = await policy_service.reach(db, user=manager, roles={"manager"}, team_id=None)
    assert not allowed.may_edit
    assert "super admin" in allowed.reason


async def test_an_ordinary_member_may_not_edit(db, presales) -> None:
    member = await person(db, "member@hamdaz.com")
    await teams.set_member_roles(db, team=presales, user=member, role_keys=["member"])
    await db.commit()

    allowed = await policy_service.reach(
        db, user=member, roles={"member"}, team_id=presales.id
    )
    assert not allowed.may_edit


# ── capacity, which is what a ratio actually is ────────────────────────


async def test_capacity_falls_back_to_the_default(db, seeded) -> None:
    policy = await policy_service.default_policy(db)
    assert policy_service.capacity_for(policy, {"nothing-relevant"}) == Decimal("1.0")


async def test_the_lowest_label_wins(db, seeded) -> None:
    """A senior in training is treated as in training.

    Taking the higher value would let a label meant to protect somebody be
    cancelled out by one that flatters them.
    """
    policy = await policy_service.default_policy(db)

    assert policy_service.capacity_for(policy, {"senior"}) == Decimal("1.4")
    assert policy_service.capacity_for(policy, {"senior", "training"}) == Decimal("0.4")


async def test_zero_capacity_is_exclusion_even_without_a_listed_label(db, seeded) -> None:
    """Saying it twice would invite the two settings to disagree."""
    policy = await policy_service.default_policy(db)
    policy.excluded_labels = []
    policy.capacity_by_label = {"sabbatical": 0.0}
    await db.commit()

    assert policy_service.is_excluded(policy, {"sabbatical"}) is not None


async def test_a_hard_ceiling_takes_the_lowest_that_applies(db, seeded) -> None:
    policy = await policy_service.default_policy(db)
    policy.default_max_open = 10
    policy.max_open_by_label = {"new-joiner": 4}
    await db.commit()

    assert policy_service.max_open_for(policy, set()) == 10
    assert policy_service.max_open_for(policy, {"new-joiner"}) == 4


async def test_no_ceiling_is_expressed_as_none_not_zero(db, seeded) -> None:
    """0 would mean 'assign nobody anything', which is not the same thing."""
    policy = await policy_service.default_policy(db)
    policy.default_max_open = None
    policy.max_open_by_label = {}
    await db.commit()

    assert policy_service.max_open_for(policy, {"senior"}) is None


def test_a_ratio_reads_the_way_people_discuss_it() -> None:
    assert _ratio(Decimal("0.5")) == "1 task for every 2"
    assert _ratio(Decimal("1.0")) == "a full share"
    assert _ratio(Decimal("1.4")) == "1.4 tasks for every 1"
    assert _ratio(Decimal("0")) == "no work"
    # 1/0.7 is 1.4285714... — a ratio quoted to 25 places reads as a bug.
    assert _ratio(Decimal("0.7")) == "1 task for every 1.4"


# ── edits that would make the policy incoherent ────────────────────────


async def test_a_capacity_for_an_unknown_label_is_refused(db, seeded) -> None:
    """Otherwise the policy silently governs nobody through a typo."""
    actor = await person(db, "root@hamdaz.com", "super_admin")
    policy = await policy_service.default_policy(db)

    with pytest.raises(policy_service.PolicyError, match="no label"):
        await policy_service.update(
            db,
            policy,
            actor=actor,
            known_labels=await known(db),
            capacity_by_label={"snior": 1.5},
        )


async def test_an_implausible_capacity_is_refused(db, seeded) -> None:
    """500 is a typo every time, and would funnel a team's work to one person."""
    actor = await person(db, "root@hamdaz.com", "super_admin")
    policy = await policy_service.default_policy(db)

    with pytest.raises(policy_service.PolicyError, match="implausible"):
        await policy_service.update(
            db, policy, actor=actor, known_labels=await known(db), default_capacity=500
        )


async def test_a_negative_capacity_is_refused(db, seeded) -> None:
    actor = await person(db, "root@hamdaz.com", "super_admin")
    policy = await policy_service.default_policy(db)

    with pytest.raises(policy_service.PolicyError, match="negative"):
        await policy_service.update(
            db, policy, actor=actor, known_labels=await known(db), default_capacity=-1
        )


async def test_all_weights_at_zero_is_refused(db, seeded) -> None:
    """A policy that cannot rank anybody cannot decide anything."""
    actor = await person(db, "root@hamdaz.com", "super_admin")
    policy = await policy_service.default_policy(db)

    with pytest.raises(policy_service.PolicyError, match="above zero"):
        await policy_service.update(
            db,
            policy,
            actor=actor,
            known_labels=await known(db),
            weight_load=0,
            weight_open_count=0,
            weight_idle_days=0,
        )


async def test_an_edit_records_who_made_it(db, seeded) -> None:
    actor = await person(db, "root@hamdaz.com", "super_admin")
    policy = await policy_service.default_policy(db)

    await policy_service.update(
        db, policy, actor=actor, known_labels=await known(db), new_joiner_days=30
    )
    await db.commit()

    assert policy.new_joiner_days == 30
    assert policy.updated_by_id == actor.id


# ── which teams distribute work through the scoring ────────────────────


async def test_a_team_without_its_own_policy_is_not_in_scope(db, presales) -> None:
    """Only teams with a policy of their own are assigned through the scoring.

    This is what keeps it to presales for now without a team name being written
    into the code: give another team a policy and it joins, take it away and it
    leaves.
    """
    from app.analytics.service import NotInScopeError, in_scope

    with pytest.raises(NotInScopeError, match="does not distribute work"):
        await in_scope(db, presales)


async def test_creating_a_policy_puts_a_team_in_scope(db, presales) -> None:
    from app.analytics.service import in_scope

    actor = await person(db, "root@hamdaz.com", "super_admin")
    await policy_service.create_for_team(db, team=presales, actor=actor)
    await db.commit()

    assert (await in_scope(db, presales)).team_id == presales.id


async def test_the_org_default_is_not_a_licence_to_assign(db, presales, estimation) -> None:
    """It is the template a team policy is seeded from, not a blanket permission.

    Treating it as one would put every team in the company into the ranking the
    moment somebody looked.
    """
    from app.analytics.service import NotInScopeError, in_scope

    await policy_service.default_policy(db)
    await db.commit()

    with pytest.raises(NotInScopeError):
        await in_scope(db, estimation)


async def test_disabling_a_team_policy_takes_it_out_of_scope(db, presales) -> None:
    from app.analytics.service import NotInScopeError, in_scope

    actor = await person(db, "root@hamdaz.com", "super_admin")
    own = await policy_service.create_for_team(db, team=presales, actor=actor)
    own.enabled = False
    await db.commit()

    with pytest.raises(NotInScopeError):
        await in_scope(db, presales)
