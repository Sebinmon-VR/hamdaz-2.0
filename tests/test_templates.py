"""Form templates, and who may use one.

Two things carry the weight.

**Only a super admin writes.** If that leaks, each team quietly edits the form
it fills in and the records stop being comparable — which is a slow failure
nobody notices until somebody tries to total them.

**Access is two independent questions**: which team, and what standing inside
it. The interesting cases are the absences, because "no team named" and "no
roles named" have to mean *unrestricted on that axis* while "no grants at all"
has to mean nobody — and it is easy to write those three the same way.
"""

from __future__ import annotations

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.forms import service
from app.forms.catalogue import QUOTE_REQUEST
from app.forms.service import TemplateError, TemplatePermissionError
from app.models.templates import FieldType, TemplateStatus
from app.roles import service as roles_service
from app.teams import service as teams

ADMIN = {"super_admin"}


async def person(db, email: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    await db.commit()
    return user


@pytest.fixture
async def seeded(db):
    await roles_service.seed_system_roles(db)
    await service.seed_templates(db)
    await db.commit()


@pytest.fixture
async def admin(db, seeded):
    return await person(db, "root@hamdaz.com")


@pytest.fixture
async def presales(db, seeded):
    team = await teams.create_team(db, name="Presales")
    await db.commit()
    return team


FIELDS = [
    {"key": "customer", "label": "Customer", "type": "text", "required": True},
    {"key": "amount", "label": "Amount", "type": "currency"},
]


# ── the shipped quote template ─────────────────────────────────────────


async def test_the_quote_template_ships_with_the_real_zoho_fields(db, seeded) -> None:
    """Every mapping was read off a live estimate, not invented."""
    template = await service.get(db, QUOTE_REQUEST)
    keys = {f["key"] for f in template.fields}

    assert {"customer_name", "cf_bcd", "cf_portal", "cf_quote_creater"} <= keys
    mapped = {f["key"]: f.get("maps_to") for f in template.fields}
    assert mapped["customer_name"] == "customer_name"
    assert mapped["currency"] == "currency_code"
    assert mapped["subject"] == "subject_content"
    assert mapped["items"] == "line_items"


async def test_the_line_items_field_carries_its_columns(db, seeded) -> None:
    """A table field with no columns is a form that cannot be rendered."""
    template = await service.get(db, QUOTE_REQUEST)
    items = next(f for f in template.fields if f["key"] == "items")

    assert items["type"] == FieldType.TABLE.value
    columns = {c["key"] for c in items["columns"]}
    assert {"name", "quantity", "rate", "cost_rate"} <= columns


async def test_our_own_fields_are_not_marked_as_zoho_fields(db, seeded) -> None:
    """`maps_to` is a promise Zoho will accept it. Cost is ours alone."""
    template = await service.get(db, QUOTE_REQUEST)
    items = next(f for f in template.fields if f["key"] == "items")
    cost = next(c for c in items["columns"] if c["key"] == "cost_rate")

    assert "maps_to" not in cost
    title = next(f for f in template.fields if f["key"] == "title")
    assert "maps_to" not in title


async def test_seeding_twice_changes_nothing(db, seeded) -> None:
    before = len(await service.all_templates(db))
    await service.seed_templates(db)
    await db.commit()
    assert len(await service.all_templates(db)) == before


async def test_an_admin_edit_survives_reseeding(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.update(db, template, roles=ADMIN, actor=admin, name="Bid form")
    await db.commit()

    await service.seed_templates(db)
    await db.commit()
    assert (await service.get(db, QUOTE_REQUEST)).name == "Bid form"


# ── only a super admin writes ──────────────────────────────────────────


async def test_a_non_admin_cannot_create_one(db, seeded, admin) -> None:
    with pytest.raises(TemplatePermissionError, match="Only a super admin"):
        await service.create(
            db, roles={"manager"}, actor=admin, key="site_visit",
            name="Site visit", kind="site_visit", fields=FIELDS,
        )


async def test_a_non_admin_cannot_edit_one(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    with pytest.raises(TemplatePermissionError):
        await service.update(db, template, roles={"ceo"}, actor=admin, name="nope")


async def test_a_non_admin_cannot_change_who_may_use_one(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    with pytest.raises(TemplatePermissionError):
        await service.set_grants(db, template, roles={"manager"}, grants=[])


# ── creating, publishing, archiving ────────────────────────────────────


async def test_a_new_template_starts_as_a_draft_nobody_can_use(db, seeded, admin) -> None:
    """A half-written form should not be fillable, and a form appears for a team
    because somebody decided it should — not because it was saved."""
    template = await service.create(
        db, roles=ADMIN, actor=admin, key="site_visit", name="Site visit",
        kind="site_visit", fields=FIELDS,
    )
    await db.commit()

    assert template.status == TemplateStatus.DRAFT
    assert template.grants == []
    somebody = await person(db, "x@y.com")
    assert not (await service.may_use(db, template, user=somebody, roles=set())).allowed


async def test_publishing_makes_it_usable(db, seeded, admin, presales) -> None:
    template = await service.create(
        db, roles=ADMIN, actor=admin, key="site_visit", name="Site visit",
        kind="site_visit", fields=FIELDS,
    )
    await service.publish(db, template, roles=ADMIN, actor=admin)
    await service.set_grants(db, template, roles=ADMIN, grants=[{"team_id": None}])
    await db.commit()

    member = await person(db, "anyone@hamdaz.com")
    assert (await service.may_use(db, template, user=member, roles=set())).allowed


async def test_an_empty_template_cannot_be_published(db, seeded, admin) -> None:
    template = await service.create(
        db, roles=ADMIN, actor=admin, key="empty", name="Empty", kind="empty", fields=[]
    )
    with pytest.raises(TemplateError, match="no fields"):
        await service.publish(db, template, roles=ADMIN, actor=admin)


async def test_two_fields_cannot_share_a_key(db, seeded, admin) -> None:
    """One would silently overwrite the other on every submission."""
    with pytest.raises(TemplateError, match="share the key"):
        await service.create(
            db, roles=ADMIN, actor=admin, key="dup", name="Dup", kind="dup",
            fields=[
                {"key": "a", "label": "One", "type": "text"},
                {"key": "a", "label": "Two", "type": "text"},
            ],
        )


async def test_archiving_keeps_it_readable(db, seeded, admin) -> None:
    """A form somebody submitted last month is unreadable if its definition has gone."""
    template = await service.get(db, QUOTE_REQUEST)
    await service.archive(db, template, roles=ADMIN, actor=admin)
    await db.commit()

    assert template.status == TemplateStatus.ARCHIVED
    assert (await service.get(db, QUOTE_REQUEST)).fields  # still there


async def test_an_archived_template_cannot_be_used_or_edited(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.archive(db, template, roles=ADMIN, actor=admin)
    await db.commit()

    member = await person(db, "member@hamdaz.com")
    assert not (await service.may_use(db, template, user=member, roles=set())).allowed
    with pytest.raises(TemplateError, match="archived"):
        await service.update(db, template, roles=ADMIN, actor=admin, name="x")


async def test_archiving_can_be_undone(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.archive(db, template, roles=ADMIN, actor=admin)
    await service.restore(db, template, roles=ADMIN, actor=admin)
    await db.commit()
    assert template.status == TemplateStatus.ACTIVE


# ── who may use it: the two axes, and their absences ───────────────────


async def test_a_grant_with_no_team_covers_everyone(db, seeded, admin) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[{"team_id": None}])
    await db.commit()

    outsider = await person(db, "nobody@hamdaz.com")
    assert (await service.may_use(db, template, user=outsider, roles=set())).allowed


async def test_a_team_grant_excludes_other_teams(db, seeded, admin, presales) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[{"team_id": presales.id}])
    await db.commit()

    inside = await person(db, "inside@hamdaz.com")
    await teams.set_member_roles(db, team=presales, user=inside, role_keys=["member"])
    outside = await person(db, "outside@hamdaz.com")
    await db.commit()

    assert (await service.may_use(db, template, user=inside, roles=set())).allowed
    refused = await service.may_use(db, template, user=outside, roles=set())
    assert not refused.allowed
    assert "does not have this template" in refused.reason


async def test_a_role_restricted_template_needs_that_role(db, seeded, admin, presales) -> None:
    """Some forms are for anyone on the team; some only for a manager."""
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(
        db, template, roles=ADMIN,
        grants=[{"team_id": presales.id, "allowed_roles": ["team_manager"]}],
    )
    await db.commit()

    ordinary = await person(db, "ordinary@hamdaz.com")
    await teams.set_member_roles(db, team=presales, user=ordinary, role_keys=["member"])
    lead = await person(db, "lead@hamdaz.com")
    await teams.set_member_roles(db, team=presales, user=lead, role_keys=["team_manager"])
    await db.commit()

    assert not (await service.may_use(db, template, user=ordinary, roles=set())).allowed
    assert (await service.may_use(db, template, user=lead, roles=set())).allowed


async def test_a_global_role_satisfies_a_role_restriction(db, seeded, admin, presales) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(
        db, template, roles=ADMIN,
        grants=[{"team_id": None, "allowed_roles": ["manager"]}],
    )
    await db.commit()

    manager = await person(db, "mgr@hamdaz.com")
    assert (await service.may_use(db, template, user=manager, roles={"manager"})).allowed


async def test_no_grants_at_all_means_nobody(db, seeded, admin) -> None:
    """The safe default, and distinct from 'no team named' which means everyone."""
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[])
    await db.commit()

    member = await person(db, "member@hamdaz.com")
    refused = await service.may_use(db, template, user=member, roles=set())
    assert not refused.allowed
    assert "Nobody has been given" in refused.reason


async def test_a_super_admin_may_always_use_one(db, seeded, admin) -> None:
    """Otherwise an admin could lock themselves out of the form they just wrote."""
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[])
    await db.commit()

    assert (await service.may_use(db, template, user=admin, roles=ADMIN)).allowed


async def test_usable_by_lists_only_what_somebody_can_fill_in(db, seeded, admin, presales) -> None:
    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[{"team_id": presales.id}])
    await db.commit()

    outsider = await person(db, "outsider@hamdaz.com")
    assert await service.usable_by(db, user=outsider, roles=set()) == []


async def test_a_module_can_find_the_active_template_for_its_kind(db, seeded) -> None:
    """How the quoting module asks for 'the current quote form'."""
    found = await service.active_for(db, QUOTE_REQUEST)
    assert found is not None and found.key == QUOTE_REQUEST


# ── the response shape ─────────────────────────────────────────────────


async def test_a_template_with_grants_serialises(db, seeded, admin, presales) -> None:
    """TemplateOut validates straight off the ORM row, so its nested grants have
    to be readable from attributes.

    This 500'd in the browser: validation of the parent failed on the ORM
    TemplateGrant objects before the router got a chance to convert them.
    """
    from app.forms.router import _out

    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(
        db, template, roles=ADMIN,
        grants=[{"team_id": presales.id, "allowed_roles": ["team_manager"]}],
    )
    await db.commit()

    body = await _out(db, template, user=admin, roles=ADMIN)

    assert body.key == QUOTE_REQUEST
    assert len(body.fields) == 25
    assert body.grants[0].team_id == presales.id
    assert body.grants[0].team_name == presales.name
    assert body.grants[0].allowed_roles == ["team_manager"]


async def test_a_template_with_no_grants_still_serialises(db, seeded, admin) -> None:
    from app.forms.router import _out

    template = await service.get(db, QUOTE_REQUEST)
    await service.set_grants(db, template, roles=ADMIN, grants=[])
    await db.commit()

    body = await _out(db, template, user=admin, roles=ADMIN)
    assert body.grants == []
