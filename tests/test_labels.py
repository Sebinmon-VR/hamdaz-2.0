"""User labels, and the assignment policy that reads them.

Nothing here assigns work or writes anywhere outside this database — the scoring
step does not exist yet, and that is deliberate.

The tests that carry the weight are the derived labels. ``on-leave`` and
``new-joiner`` are not stored, so the thing worth proving is that they appear and
disappear on the right day *without anything having run*. If that breaks, the
failure is silent: work keeps being assigned to somebody who is in another
country.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.labels import service as labels
from app.leave import service as leave
from app.models.labels import (
    LABEL_EXCLUDED,
    LABEL_NEW_JOINER,
    LABEL_ON_LEAVE,
    LabelKind,
    LabelSource,
)
from app.models.leave import LeaveType
from app.roles import service as roles
from app.teams import service as teams


async def person(db, email: str, *, joined_on: date | None = None):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    user.joined_on = joined_on
    await db.commit()
    return user


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await labels.seed_labels(db)
    await db.commit()


async def keys_for(db, user, **kw) -> set[str]:
    held = await labels.effective_labels(db, [user], **kw)
    return {label.key for label in held[user.id]}


# ── the catalogue ──────────────────────────────────────────────────────


async def test_seeding_is_idempotent(db, seeded) -> None:
    first = await labels.all_labels(db)
    await labels.seed_labels(db)
    await db.commit()
    assert len(await labels.all_labels(db)) == len(first)


async def test_an_admin_rename_survives_reseeding(db, seeded) -> None:
    """The catalogue owns the key, not the wording."""
    senior = await labels.get_label(db, "senior")
    senior.name = "Principal Engineer"
    await db.commit()

    await labels.seed_labels(db)
    await db.commit()
    assert (await labels.get_label(db, "senior")).name == "Principal Engineer"


async def test_a_system_label_cannot_be_deleted(db, seeded) -> None:
    """The policy refers to it by key; deleting it would leave that dangling."""
    with pytest.raises(labels.LabelError, match="cannot be deleted"):
        await labels.delete_label(db, await labels.get_label(db, LABEL_ON_LEAVE))


async def test_an_added_label_can_be_deleted(db, seeded) -> None:
    made = await labels.create_label(
        db, key="security-cleared", name="Security Cleared", kind=LabelKind.SKILL
    )
    await labels.delete_label(db, made)
    await db.commit()

    with pytest.raises(labels.LabelNotFoundError):
        await labels.get_label(db, "security-cleared")


async def test_a_derived_label_cannot_be_created_by_hand(db, seeded) -> None:
    with pytest.raises(labels.LabelError, match="automatically"):
        await labels.create_label(
            db, key=LABEL_ON_LEAVE, name="On Leave", kind=LabelKind.STATUS
        )


# ── on-leave, derived from the leave module ────────────────────────────


async def test_somebody_on_approved_leave_is_labelled_on_leave(db, seeded) -> None:
    """Not stored anywhere — read from the leave they were granted."""
    user = await person(db, "away@hamdaz.com")
    request = await leave.submit(
        db,
        user=user,
        leave_type=LeaveType.ANNUAL,
        start=date.today(),
        end=date.today() + timedelta(days=3),
    )
    await leave.approve(db, request=request, actor=user)
    await db.commit()

    assert LABEL_ON_LEAVE in await keys_for(db, user)


async def test_the_label_is_gone_the_day_the_leave_ends(db, seeded) -> None:
    """The reason nothing has to run: the row stops matching, on its own."""
    user = await person(db, "back@hamdaz.com")
    request = await leave.submit(
        db,
        user=user,
        leave_type=LeaveType.ANNUAL,
        start=date.today(),
        end=date.today() + timedelta(days=2),
    )
    await leave.approve(db, request=request, actor=user)
    await db.commit()

    assert LABEL_ON_LEAVE in await keys_for(db, user)
    # Same data, three days later.
    after = date.today() + timedelta(days=3)
    assert LABEL_ON_LEAVE not in await keys_for(db, user, on=after)


async def test_pending_leave_does_not_take_anyone_out_of_the_pool(db, seeded) -> None:
    """A request that might yet be refused must not reassign anyone's work."""
    user = await person(db, "maybe@hamdaz.com")
    settings = await leave.get_settings(db)
    settings.auto_decide = False  # leave it pending
    await db.commit()

    await leave.submit(
        db,
        user=user,
        leave_type=LeaveType.ANNUAL,
        start=date.today(),
        end=date.today() + timedelta(days=2),
    )
    await db.commit()

    assert LABEL_ON_LEAVE not in await keys_for(db, user)


# ── new-joiner, derived from the joining date ──────────────────────────


async def test_a_recent_starter_is_a_new_joiner(db, seeded) -> None:
    user = await person(db, "fresh@hamdaz.com", joined_on=date.today() - timedelta(days=10))
    assert LABEL_NEW_JOINER in await keys_for(db, user, new_joiner_days=90)


async def test_the_label_lapses_when_the_window_passes(db, seeded) -> None:
    user = await person(db, "settled@hamdaz.com", joined_on=date.today() - timedelta(days=200))
    assert LABEL_NEW_JOINER not in await keys_for(db, user, new_joiner_days=90)


async def test_the_window_is_a_policy_setting_not_a_constant(db, seeded) -> None:
    user = await person(db, "sixty@hamdaz.com", joined_on=date.today() - timedelta(days=60))

    assert LABEL_NEW_JOINER in await keys_for(db, user, new_joiner_days=90)
    assert LABEL_NEW_JOINER not in await keys_for(db, user, new_joiner_days=30)


async def test_a_zero_window_turns_the_rule_off(db, seeded) -> None:
    user = await person(db, "today@hamdaz.com", joined_on=date.today())
    assert LABEL_NEW_JOINER not in await keys_for(db, user, new_joiner_days=0)


async def test_no_joining_date_does_not_make_somebody_a_new_joiner(db, seeded) -> None:
    """Absence of evidence must not halve somebody's workload.

    Measured against real data: with the fallback on and no joining dates
    recorded, everyone first appeared when the ERP was installed, so every single
    person came out a new joiner and every capacity collapsed to 0.5 — a rule
    that applies to everybody distinguishes nobody.
    """
    user = await person(db, "unknown@hamdaz.com", joined_on=None)
    assert labels.is_new_joiner(user, window_days=90) is None


async def test_the_first_seen_fallback_works_when_turned_on_and_says_so(db, seeded) -> None:
    """Reasonable once joining dates exist, for the few people missing one."""
    user = await person(db, "unknown@hamdaz.com", joined_on=None)

    reason = labels.is_new_joiner(user, window_days=90, from_first_seen=True)
    assert reason and "first seen in this system" in reason


# ── labels somebody was given ──────────────────────────────────────────


async def test_a_label_can_be_given_and_taken_away(db, seeded) -> None:
    user = await person(db, "senior@hamdaz.com")
    senior = await labels.get_label(db, "senior")

    await labels.assign(db, user=user, label=senior)
    await db.commit()
    assert "senior" in await keys_for(db, user)

    await labels.unassign(db, user=user, label=senior)
    await db.commit()
    assert "senior" not in await keys_for(db, user)


async def test_an_expired_label_simply_stops_applying(db, seeded) -> None:
    """No reaper job, and no window where a lapsed label still shapes assignment."""
    user = await person(db, "trainee@hamdaz.com")
    await labels.assign(
        db,
        user=user,
        label=await labels.get_label(db, "training"),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await db.commit()

    assert "training" not in await keys_for(db, user)


async def test_re_assigning_extends_rather_than_duplicates(db, seeded) -> None:
    """"Give them another month" must not become delete-then-create."""
    user = await person(db, "extend@hamdaz.com")
    training = await labels.get_label(db, "training")
    later = datetime.now(UTC) + timedelta(days=30)

    await labels.assign(db, user=user, label=training, expires_at=datetime.now(UTC))
    await labels.assign(db, user=user, label=training, expires_at=later)
    await db.commit()

    held = (await labels.effective_labels(db, [user]))[user.id]
    assert [label.key for label in held].count("training") == 1
    assert "training" in {label.key for label in held}


async def test_a_team_scoped_label_does_not_apply_elsewhere(db, seeded) -> None:
    """Senior in presales can be a new joiner on an estimation team."""
    user = await person(db, "scoped@hamdaz.com")
    presales = await teams.create_team(db, name="Presales")
    estimation = await teams.create_team(db, name="Estimation")
    await labels.assign(
        db, user=user, label=await labels.get_label(db, "senior"), team_id=presales.id
    )
    await db.commit()

    assert "senior" in await keys_for(db, user, team_id=presales.id)
    assert "senior" not in await keys_for(db, user, team_id=estimation.id)


async def test_a_derived_label_cannot_be_handed_out(db, seeded) -> None:
    user = await person(db, "nope@hamdaz.com")
    with pytest.raises(labels.LabelError, match="cannot be given out"):
        await labels.assign(db, user=user, label=await labels.get_label(db, LABEL_ON_LEAVE))


async def test_a_derived_label_says_where_it_came_from(db, seeded) -> None:
    """A reader must be able to tell 'somebody decided this' from 'this follows'."""
    user = await person(db, "why@hamdaz.com", joined_on=date.today())
    held = (await labels.effective_labels(db, [user], new_joiner_days=90))[user.id]

    derived = next(label for label in held if label.key == LABEL_NEW_JOINER)
    assert derived.source == LabelSource.DERIVED
    assert derived.reason and "new joiner until" in derived.reason


# ── editing a label ────────────────────────────────────────────────────


async def test_a_system_label_can_be_renamed(db, seeded) -> None:
    """The delete refusal tells you to rename instead, so renaming must work.

    Without this the error message points at nothing, which is worse than
    refusing outright.
    """
    label = await labels.get_label(db, "senior")
    await labels.update_label(db, label, name="Principal Engineer", color="#1a7f47")
    await db.commit()

    again = await labels.get_label(db, "senior")
    assert again.name == "Principal Engineer"
    assert again.key == "senior"  # identity is untouched


async def test_a_system_labels_kind_cannot_be_changed(db, seeded) -> None:
    """The policy treats a category and a status differently.

    Note which labels are 'system': the ones the policy names by key, not the
    seniority ladder. 'senior' ships with the product but nothing refers to it
    by name, so it is fully editable.
    """
    with pytest.raises(labels.LabelError, match="kind cannot be changed"):
        await labels.update_label(
            db, await labels.get_label(db, LABEL_EXCLUDED), kind=LabelKind.SKILL
        )


async def test_a_shipped_but_unreferenced_label_is_fully_editable(db, seeded) -> None:
    """'senior' is seeded but not named by the policy, so it is not a system label."""
    await labels.update_label(
        db, await labels.get_label(db, "senior"), kind=LabelKind.SKILL
    )
    await db.commit()
    assert (await labels.get_label(db, "senior")).kind == LabelKind.SKILL


async def test_an_added_labels_kind_can_be_changed(db, seeded) -> None:
    made = await labels.create_label(
        db, key="forklift", name="Forklift", kind=LabelKind.SKILL
    )
    await labels.update_label(db, made, kind=LabelKind.STATUS)
    await db.commit()

    assert (await labels.get_label(db, "forklift")).kind == LabelKind.STATUS


async def test_renaming_does_not_detach_anybody_holding_it(db, seeded) -> None:
    """The key is what assignments and the policy point at, so it never moves."""
    user = await person(db, "renamed@hamdaz.com")
    label = await labels.get_label(db, "senior")
    await labels.assign(db, user=user, label=label)
    await db.commit()

    await labels.update_label(db, label, name="Principal Engineer")
    await db.commit()

    assert "senior" in await keys_for(db, user)


async def test_a_label_cannot_be_renamed_to_nothing(db, seeded) -> None:
    with pytest.raises(labels.LabelError, match="needs a name"):
        await labels.update_label(db, await labels.get_label(db, "senior"), name="   ")
