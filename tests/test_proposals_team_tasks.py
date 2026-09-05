"""The team-wide task view, and who may open it.

``/my-tasks`` is safe because the rows are chosen by the session. ``/team-tasks``
gives that up — it returns other people's rows — so the gate replacing it is the
thing worth attacking, and most of what follows attacks it: a lead of one team
asking about another, a plain member asking about their own, somebody with no
team at all.

The second half checks that the *set of people* is the team's membership and
nothing else. A view that quietly included a non-member, or quietly dropped a
member with no SharePoint account, would be wrong in a way no status code shows.
"""

from __future__ import annotations

import pytest

from app.access import service as access
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.proposals.oversight import TeamTasksCache
from app.proposals.sharepoint import ProposalTask, SharePointError
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/proposals"

#: Far enough ahead that these rows stay "active" whenever the suite is run.
OPEN_BID = "2099-01-01T00:00:00Z"
#: Long past, so the row reads as a closed bid rather than live work.
CLOSED_BID = "2020-01-01T00:00:00Z"


def _task(task_id: str, title: str, *, lookup_id: str, bcd: str, status: str = "In Progress"):
    return ProposalTask(
        id=task_id, title=title, status=status, priority="High",
        assigned_to_lookup_id=lookup_id, assigned_to_name=title,
        start_date=None, due_date=None, bid_closing_date=bcd, end_user="Adnoc",
        submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None,
    )


class StubSharePoint:
    """Keyed by lookup id, so a test can prove which people were asked about."""

    def __init__(self) -> None:
        #: email (casefolded) -> lookup id, as the site user list would give it.
        self.users: dict[str, str] = {}
        self.tasks: dict[str, list[ProposalTask]] = {}
        self.error: Exception | None = None
        self.asked_for: list[str] = []

    async def site_users(self, *, force: bool = False) -> dict[str, str]:
        if self.error:
            raise self.error
        return dict(self.users)

    async def lookup_id_for(self, email: str):
        return self.users.get(email.casefold())

    async def tasks_assigned_to(self, lookup_id: str, *, limit: int = 200):
        if self.error:
            raise self.error
        self.asked_for.append(lookup_id)
        return self.tasks.get(lookup_id, [])[:limit]


@pytest.fixture
def sharepoint(client):
    stub = StubSharePoint()
    client._transport.app.state.sharepoint = stub
    # A fresh cache per test: a process-wide one would carry one test's rows
    # into the next and the fan-out assertions would pass without fetching.
    client._transport.app.state.team_tasks_cache = TeamTasksCache()
    return stub


async def _make(db, email: str, *global_roles: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in global_roles:
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
async def other_team(db, seeded):
    t = await teams.create_team(db, name="Finance")
    await db.commit()
    return t


async def _join(db, team, user, *role_keys: str):
    await teams.set_member_roles(db, team=team, user=user, role_keys=list(role_keys))
    await db.commit()
    return user


@pytest.fixture
async def worker(db, team, sharepoint):
    """A plain member with two rows: one live bid, one long closed."""
    user = await _make(db, "goutham@hamdaz.com")
    await _join(db, team, user, "member")
    sharepoint.users["goutham@hamdaz.com"] = "15"
    sharepoint.tasks["15"] = [
        _task("1", "Live bid", lookup_id="15", bcd=OPEN_BID),
        _task("2", "Old bid", lookup_id="15", bcd=CLOSED_BID),
    ]
    return user


@pytest.fixture
async def lead(db, team, sharepoint):
    user = await _make(db, "lead@hamdaz.com")
    await _join(db, team, user, "team_lead")
    sharepoint.users["lead@hamdaz.com"] = "16"
    return user


# ── the gate ───────────────────────────────────────────────────────────


async def test_a_session_is_required(client, team, sharepoint) -> None:
    res = await client.get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 401


async def test_a_team_lead_sees_their_own_team(client, team, sharepoint, lead, worker) -> None:
    res = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 200
    assert res.json()["scope"]["team_slug"] == team.slug


async def test_a_team_manager_sees_their_own_team(
    client, db, team, sharepoint, worker
) -> None:
    manager = await _make(db, "tm@hamdaz.com")
    await _join(db, team, manager, "team_manager")

    res = await _as(client, manager).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 200


async def test_a_lead_of_another_team_is_refused(
    client, db, team, other_team, sharepoint, worker
) -> None:
    """The whole point of a team-scoped role: authority that does not travel."""
    outsider = await _make(db, "otherlead@hamdaz.com")
    await _join(db, other_team, outsider, "team_lead")

    res = await _as(client, outsider).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 403
    assert "lead or manager" in res.json()["detail"]


async def test_a_plain_member_cannot_see_their_teammates(
    client, team, sharepoint, worker
) -> None:
    """Being in the team is not authority over it — they have /my-tasks."""
    res = await _as(client, worker).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 403


async def test_an_approver_is_not_a_lead(client, db, team, sharepoint, worker) -> None:
    """Approving work inside a team is a different right from overseeing it."""
    approver = await _make(db, "approver@hamdaz.com")
    await _join(db, team, approver, "approver")

    res = await _as(client, approver).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 403


async def test_someone_in_no_team_is_refused(client, db, team, sharepoint) -> None:
    loner = await _make(db, "loner@hamdaz.com")
    res = await _as(client, loner).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 403


@pytest.mark.parametrize("role", ["super_admin", "ceo", "manager"])
async def test_administrators_see_any_team(
    client, db, team, sharepoint, worker, role: str
) -> None:
    admin = await _make(db, f"{role}@hamdaz.com", role)
    res = await _as(client, admin).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 200


async def test_an_unknown_team_is_a_404(client, db, seeded, sharepoint) -> None:
    admin = await _make(db, "boss@hamdaz.com", "ceo")
    res = await _as(client, admin).get(f"{API}/team-tasks", params={"team": "no-such-team"})
    assert res.status_code == 404


# ── whose rows come back ───────────────────────────────────────────────


async def test_only_this_teams_members_are_asked_about(
    client, db, team, other_team, sharepoint, lead, worker
) -> None:
    """Somebody else's rows must not arrive by being on the same SharePoint site."""
    stranger = await _make(db, "stranger@hamdaz.com")
    await _join(db, other_team, stranger, "member")
    sharepoint.users["stranger@hamdaz.com"] = "99"
    sharepoint.tasks["99"] = [_task("9", "Not yours", lookup_id="99", bcd=OPEN_BID)]

    res = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 200

    assert "99" not in sharepoint.asked_for
    emails = {m["email"] for m in res.json()["members"]}
    assert emails == {"goutham@hamdaz.com", "lead@hamdaz.com"}


async def test_a_member_missing_from_sharepoint_is_named_not_dropped(
    client, db, team, sharepoint, lead, worker
) -> None:
    """"Why is this empty" needs an answer on the screen, not a silent gap."""
    newcomer = await _make(db, "newcomer@hamdaz.com")
    await _join(db, team, newcomer, "member")

    res = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    body = res.json()

    assert "newcomer@hamdaz.com" in body["scope"]["members_without_sharepoint"]
    assert body["scope"]["member_count"] == 3
    assert body["scope"]["matched_in_sharepoint"] == 2

    row = next(m for m in body["members"] if m["email"] == "newcomer@hamdaz.com")
    assert row["in_sharepoint"] is False
    assert row["tasks"] == []


async def test_the_rows_and_counts_are_the_members_own(
    client, team, sharepoint, lead, worker
) -> None:
    res = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    body = res.json()

    them = next(m for m in body["members"] if m["email"] == "goutham@hamdaz.com")
    assert {t["title"] for t in them["tasks"]} == {"Live bid", "Old bid"}
    assert them["total"] == 2
    # Both are unfinished, but only the one whose bid is still open is live —
    # counting the closed one as workload is the mistake this split exists for.
    assert them["open_count"] == 2
    assert them["active_count"] == 1


async def test_open_only_hides_completed_rows(client, team, sharepoint, lead, worker) -> None:
    sharepoint.tasks["15"].append(
        _task("3", "Finished", lookup_id="15", bcd=OPEN_BID, status="Completed")
    )

    both = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    them = next(m for m in both.json()["members"] if m["email"] == "goutham@hamdaz.com")
    assert len(them["tasks"]) == 3
    assert them["total"] == 3

    filtered = await _as(client, lead).get(
        f"{API}/team-tasks", params={"team": team.slug, "open_only": "true", "refresh": "true"}
    )
    them = next(m for m in filtered.json()["members"] if m["email"] == "goutham@hamdaz.com")
    assert {t["title"] for t in them["tasks"]} == {"Live bid", "Old bid"}
    # The count still describes everything assigned, not just what is shown.
    assert them["total"] == 3


async def test_team_totals_are_the_sum_of_its_members(
    client, team, sharepoint, lead, worker
) -> None:
    """The header and the list are computed once, so they cannot disagree."""
    body = (
        await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    ).json()

    assert body["total"] == sum(m["total"] for m in body["members"])
    assert body["active_count"] == sum(m["active_count"] for m in body["members"])
    assert body["member_count"] == len(body["members"])


async def test_the_busiest_person_leads(client, db, team, sharepoint, lead, worker) -> None:
    """A lead opens this to find who needs help, so it opens on that person."""
    busy = await _make(db, "busy@hamdaz.com")
    await _join(db, team, busy, "member")
    sharepoint.users["busy@hamdaz.com"] = "20"
    sharepoint.tasks["20"] = [
        _task(str(i), f"Bid {i}", lookup_id="20", bcd=OPEN_BID) for i in range(5)
    ]

    body = (
        await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    ).json()
    assert body["members"][0]["email"] == "busy@hamdaz.com"


# ── the cache and the failure path ─────────────────────────────────────


async def test_a_second_call_is_served_from_the_cache(
    client, team, sharepoint, lead, worker
) -> None:
    signed = _as(client, lead)
    first = await signed.get(f"{API}/team-tasks", params={"team": team.slug})
    asked = list(sharepoint.asked_for)

    second = await signed.get(f"{API}/team-tasks", params={"team": team.slug})
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert sharepoint.asked_for == asked, "the cached call should not re-query Graph"


async def test_refresh_re_reads_sharepoint(client, team, sharepoint, lead, worker) -> None:
    signed = _as(client, lead)
    await signed.get(f"{API}/team-tasks", params={"team": team.slug})
    asked = len(sharepoint.asked_for)

    res = await signed.get(
        f"{API}/team-tasks", params={"team": team.slug, "refresh": "true"}
    )
    assert res.json()["cached"] is False
    assert len(sharepoint.asked_for) > asked


async def test_a_member_added_since_the_sweep_invalidates_it(
    client, db, team, sharepoint, lead, worker
) -> None:
    """A stale sweep would show a new joiner as having nothing, which is a lie."""
    signed = _as(client, lead)
    await signed.get(f"{API}/team-tasks", params={"team": team.slug})

    joiner = await _make(db, "joiner@hamdaz.com")
    await _join(db, team, joiner, "member")
    sharepoint.users["joiner@hamdaz.com"] = "21"
    sharepoint.tasks["21"] = [_task("7", "Theirs", lookup_id="21", bcd=OPEN_BID)]

    body = (await signed.get(f"{API}/team-tasks", params={"team": team.slug})).json()
    assert body["cached"] is False
    them = next(m for m in body["members"] if m["email"] == "joiner@hamdaz.com")
    assert [t["title"] for t in them["tasks"]] == ["Theirs"]


async def test_sharepoint_being_down_is_a_502(client, team, sharepoint, lead, worker) -> None:
    sharepoint.error = SharePointError("Graph is unwell")
    res = await _as(client, lead).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 502
    assert "Proposals list" in res.json()["detail"]


async def test_the_gate_is_checked_before_sharepoint_is_touched(
    client, team, sharepoint, worker
) -> None:
    """A refused caller must not be able to make the server call Graph at all."""
    sharepoint.error = SharePointError("should never be reached")
    res = await _as(client, worker).get(f"{API}/team-tasks", params={"team": team.slug})
    assert res.status_code == 403
    assert sharepoint.asked_for == []
