"""Leave: the concurrency rule, decisions, and who may make them.

The rule is "at most N people off on the same day", and the subtlety worth
testing hardest is that a request covers a *range*: it must be refused if any
single day inside it is full, not if the range is busy on average.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.leave import service
from app.leave.service import LeaveConflictError, LeaveError, LeaveNotFoundError
from app.models.leave import DecisionBy, LeaveStatus, LeaveType
from app.roles import service as roles
from app.teams import service as teams

D0 = date.today() + timedelta(days=30)


def d(offset: int) -> date:
    return D0 + timedelta(days=offset)


async def _user(db, email: str):
    return await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )


async def _book(db, user, start: date, end: date, **kw):
    return await service.submit(
        db, user=user, leave_type=LeaveType.ANNUAL, start=start, end=end, **kw
    )


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await db.commit()


@pytest.fixture
async def people(db, seeded):
    users = [await _user(db, f"p{i}@hamdaz.com") for i in range(5)]
    await db.commit()
    return users


# ── settings ───────────────────────────────────────────────────────────


async def test_settings_appear_with_defaults(db, seeded) -> None:
    s = await service.get_settings(db)
    assert s.max_concurrent == 2
    assert s.auto_decide is True
    assert s.limit_scope == "organisation"
    # Off by default: a development build must not mail colleagues.
    assert s.notify_hr_by_email is False


async def test_settings_are_a_single_row(db, seeded) -> None:
    first = await service.get_settings(db)
    await db.commit()
    assert (await service.get_settings(db)).id == first.id


async def test_hr_can_change_the_limit(db, seeded) -> None:
    s = await service.update_settings(db, max_concurrent=5)
    assert s.max_concurrent == 5


@pytest.mark.parametrize("bad", [0, -1])
async def test_a_limit_below_one_is_refused(db, seeded, bad: int) -> None:
    with pytest.raises(LeaveError):
        await service.update_settings(db, max_concurrent=bad)


async def test_an_unknown_scope_is_refused(db, seeded) -> None:
    with pytest.raises(LeaveError, match="limit_scope"):
        await service.update_settings(db, limit_scope="galaxy")


# ── the concurrency rule ───────────────────────────────────────────────


async def test_the_first_requests_are_approved_up_to_the_limit(db, people) -> None:
    a = await _book(db, people[0], d(0), d(2))
    b = await _book(db, people[1], d(0), d(2))
    assert a.status == LeaveStatus.APPROVED
    assert b.status == LeaveStatus.APPROVED
    assert b.conflicting_count == 1


async def test_the_one_over_the_limit_is_rejected(db, people) -> None:
    await _book(db, people[0], d(0), d(2))
    await _book(db, people[1], d(0), d(2))
    third = await _book(db, people[2], d(0), d(2))

    assert third.status == LeaveStatus.REJECTED
    assert third.decided_by == DecisionBy.SYSTEM
    assert third.conflicting_count == 2


async def test_the_rejection_names_the_day_and_the_people(db, people) -> None:
    """A refusal that does not say why is an obstacle, not a decision."""
    await _book(db, people[0], d(0), d(2))
    await _book(db, people[1], d(0), d(2))
    third = await _book(db, people[2], d(0), d(2))

    note = third.decision_note
    assert d(0).isoformat() in note
    assert "p0" in note and "p1" in note
    assert "limit is 2" in note


async def test_one_busy_day_blocks_the_whole_range(db, people) -> None:
    """The range is checked day by day, not on average."""
    await _book(db, people[0], d(5), d(5))
    await _book(db, people[1], d(5), d(5))

    # Ten days, only one of which is full.
    spanning = await _book(db, people[2], d(0), d(9))
    assert spanning.status == LeaveStatus.REJECTED
    assert d(5).isoformat() in spanning.decision_note


async def test_ranges_that_do_not_overlap_do_not_count(db, people) -> None:
    await _book(db, people[0], d(0), d(1))
    await _book(db, people[1], d(0), d(1))
    later = await _book(db, people[2], d(2), d(3))
    assert later.status == LeaveStatus.APPROVED


async def test_a_rejected_request_does_not_block_others(db, people) -> None:
    """Only approved leave occupies a slot."""
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    await _book(db, people[2], d(0), d(0))  # rejected

    fourth = await _book(db, people[3], d(1), d(1))
    assert fourth.status == LeaveStatus.APPROVED


async def test_raising_the_limit_lets_the_next_one_through(db, people) -> None:
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    await service.update_settings(db, max_concurrent=3)

    third = await _book(db, people[2], d(0), d(0))
    assert third.status == LeaveStatus.APPROVED


async def test_auto_decide_off_leaves_everything_pending(db, people) -> None:
    await service.update_settings(db, auto_decide=False)
    request = await _book(db, people[0], d(0), d(0))
    assert request.status == LeaveStatus.PENDING
    assert request.decided_by is None


# ── scope ──────────────────────────────────────────────────────────────


async def test_team_scope_only_counts_teammates(db, people, seeded) -> None:
    """A busy day in one team should not block a different team."""
    alpha = await teams.create_team(db, name="Alpha")
    beta = await teams.create_team(db, name="Beta")
    for u in people[:2]:
        await teams.set_member_roles(db, team=alpha, user=u, role_keys=["member"])
    await teams.set_member_roles(db, team=beta, user=people[2], role_keys=["member"])
    await service.update_settings(db, limit_scope="team")
    await db.commit()

    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    other_team = await _book(db, people[2], d(0), d(0))

    assert other_team.status == LeaveStatus.APPROVED


async def test_organisation_scope_counts_everyone(db, people, seeded) -> None:
    alpha = await teams.create_team(db, name="Alpha")
    for u in people[:2]:
        await teams.set_member_roles(db, team=alpha, user=u, role_keys=["member"])
    await db.commit()

    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    outsider = await _book(db, people[2], d(0), d(0))

    assert outsider.status == LeaveStatus.REJECTED


# ── validation ─────────────────────────────────────────────────────────


async def test_end_before_start_is_refused(db, people) -> None:
    with pytest.raises(LeaveError, match="end date"):
        await _book(db, people[0], d(5), d(1))


async def test_a_request_longer_than_the_maximum_is_refused(db, people) -> None:
    await service.update_settings(db, max_days_per_request=5)
    with pytest.raises(LeaveError, match="more than 5 days"):
        await _book(db, people[0], d(0), d(10))


async def test_overlapping_your_own_leave_is_refused(db, people) -> None:
    await _book(db, people[0], d(0), d(4))
    with pytest.raises(LeaveConflictError, match="already have leave"):
        await _book(db, people[0], d(3), d(6))


async def test_your_own_leave_does_not_count_against_you(db, people) -> None:
    """The limit is about colleagues, not about the requester."""
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    later = await _book(db, people[0], d(1), d(1))
    assert later.status == LeaveStatus.APPROVED


# ── HR decisions ───────────────────────────────────────────────────────


async def test_hr_can_approve_within_the_limit(db, people) -> None:
    await service.update_settings(db, auto_decide=False)
    request = await _book(db, people[0], d(0), d(0))
    await service.approve(db, request=request, actor=people[4])

    assert request.status == LeaveStatus.APPROVED
    assert request.decided_by == DecisionBy.HR
    assert request.emergency_override is False


async def test_approving_over_the_limit_needs_the_emergency_flag(db, people) -> None:
    """The rule must not be steppable over by accident."""
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    rejected = await _book(db, people[2], d(0), d(0))

    with pytest.raises(LeaveConflictError, match="emergency"):
        await service.approve(db, request=rejected, actor=people[4])


async def test_an_emergency_approval_overrides_the_limit(db, people) -> None:
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    rejected = await _book(db, people[2], d(0), d(0))

    await service.approve(db, request=rejected, actor=people[4], emergency=True)
    assert rejected.status == LeaveStatus.APPROVED
    assert rejected.emergency_override is True
    assert rejected.decided_by == DecisionBy.HR


async def test_an_emergency_approval_is_recorded_as_such(db, people) -> None:
    """So "how often do we override the rule" is answerable."""
    await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    rejected = await _book(db, people[2], d(0), d(0))
    await service.approve(db, request=rejected, actor=people[4], emergency=True)

    assert "emergency" in rejected.decision_note.lower()


async def test_rejecting_requires_a_reason(db, people) -> None:
    request = await _book(db, people[0], d(0), d(0))
    for empty in ("", "   "):
        with pytest.raises(LeaveError, match="reason"):
            await service.reject(db, request=request, actor=people[4], note=empty)


async def test_hr_rejection_keeps_the_reason(db, people) -> None:
    request = await _book(db, people[0], d(0), d(0))
    await service.reject(db, request=request, actor=people[4], note="Project deadline")

    assert request.status == LeaveStatus.REJECTED
    assert request.decision_note == "Project deadline"
    assert request.decided_by == DecisionBy.HR


# ── withdrawing ────────────────────────────────────────────────────────


async def test_the_requester_can_withdraw(db, people) -> None:
    request = await _book(db, people[0], d(0), d(0))
    await service.cancel(db, request=request, actor=people[0])
    assert request.status == LeaveStatus.CANCELLED
    assert request.decided_by == DecisionBy.REQUESTER


async def test_somebody_else_cannot_withdraw_it(db, people) -> None:
    request = await _book(db, people[0], d(0), d(0))
    with pytest.raises(LeaveError, match="who requested"):
        await service.cancel(db, request=request, actor=people[1])


async def test_withdrawing_frees_the_slot(db, people) -> None:
    first = await _book(db, people[0], d(0), d(0))
    await _book(db, people[1], d(0), d(0))
    await service.cancel(db, request=first, actor=people[0])
    await db.commit()

    third = await _book(db, people[2], d(0), d(0))
    assert third.status == LeaveStatus.APPROVED


async def test_deciding_a_withdrawn_request_is_refused(db, people) -> None:
    request = await _book(db, people[0], d(0), d(0))
    await service.cancel(db, request=request, actor=people[0])
    with pytest.raises(LeaveConflictError, match="withdrawn"):
        await service.approve(db, request=request, actor=people[4])


# ── who is HR ──────────────────────────────────────────────────────────


async def test_hr_is_the_configured_team(db, people, seeded) -> None:
    hr = await teams.create_team(db, name="HR", slug="hr")
    await teams.set_member_roles(db, team=hr, user=people[4], role_keys=["member"])
    await db.commit()

    assert await service.is_hr(db, people[4].id) is True
    assert await service.is_hr(db, people[0].id) is False


async def test_a_missing_hr_team_is_not_a_crash(db, people, seeded) -> None:
    """Better an empty list than every submission failing."""
    await service.update_settings(db, hr_team_slug="does-not-exist")
    assert await service.hr_members(db) == []
    assert await service.is_hr(db, people[0].id) is False


async def test_renaming_the_hr_team_moves_the_authority(db, people, seeded) -> None:
    old = await teams.create_team(db, name="HR", slug="hr")
    new = await teams.create_team(db, name="People", slug="people")
    await teams.set_member_roles(db, team=old, user=people[0], role_keys=["member"])
    await teams.set_member_roles(db, team=new, user=people[1], role_keys=["member"])
    await db.commit()

    assert await service.is_hr(db, people[0].id) is True
    await service.update_settings(db, hr_team_slug="people")
    assert await service.is_hr(db, people[0].id) is False
    assert await service.is_hr(db, people[1].id) is True


# ── views ──────────────────────────────────────────────────────────────


async def test_the_calendar_shows_only_approved_leave(db, people) -> None:
    await _book(db, people[0], d(0), d(1))
    await service.update_settings(db, auto_decide=False)
    await _book(db, people[1], d(0), d(1))  # pending
    await db.commit()

    days = await service.calendar(db, start=d(0), end=d(1))
    assert [p["name"] for p in days[d(0).isoformat()]] == ["p0"]


async def test_the_calendar_omits_empty_days(db, people) -> None:
    await _book(db, people[0], d(0), d(0))
    await db.commit()
    days = await service.calendar(db, start=d(0), end=d(5))
    assert list(days) == [d(0).isoformat()]


async def test_a_persons_summary_counts_their_own_only(db, people) -> None:
    await _book(db, people[0], d(0), d(2))
    await _book(db, people[1], d(0), d(2))
    await db.commit()

    summary = await service.summary(db, people[0].id)
    assert summary["total"] == 1
    assert summary["approved"] == 1
    assert summary["days_approved"] == 3


async def test_an_unknown_request_raises(db, seeded) -> None:
    with pytest.raises(LeaveNotFoundError):
        await service.get_request(db, uuid.uuid4())
