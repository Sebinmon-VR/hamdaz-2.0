"""The counting and caching behind the team task view.

Split from the route tests for the reason ``test_workload.py`` gives: the
interesting rules here are arithmetic, and arithmetic is worth testing exactly
rather than through HTTP and a database. ``_summarise_member`` and
``TeamTasksCache`` both run without touching Postgres, SharePoint or FastAPI, so
these are the tests that say whether the numbers are right.

The authorisation rule needs a session and lives in ``test_proposals_team_tasks``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.proposals.analytics import SOON_DAYS
from app.proposals.oversight import MemberRef, TeamTasksCache, _summarise_member
from app.proposals.sharepoint import ProposalTask

#: The real clock, deliberately, where ``test_workload`` can use a frozen one.
#: ``open_count`` and ``active_count`` come from ``ProposalTask.is_open`` and
#: ``.is_active``, which read ``datetime.now`` themselves and take no argument —
#: so a row dated relative to a frozen 2026-06-01 would be judged live by the
#: arithmetic here and long closed by the properties, and the test would be
#: asserting against a state the application can never actually be in. Every
#: date below is therefore an offset from the moment the test runs.
NOW = datetime.now(UTC)
SOON = NOW + timedelta(days=SOON_DAYS)


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _task(
    task_id: str = "1",
    *,
    status: str | None = "In Progress",
    bcd: datetime | None = None,
    due: datetime | None = None,
) -> ProposalTask:
    return ProposalTask(
        id=task_id, title=f"Proposal {task_id}", status=status, priority=None,
        assigned_to_lookup_id="15", assigned_to_name="Sebin",
        start_date=None,
        due_date=_iso(due) if due else None,
        bid_closing_date=_iso(bcd) if bcd else None,
        end_user=None, submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None,
    )


MEMBER = MemberRef(
    user_id=uuid.uuid4(),
    name="Sebin",
    email="sebin@hamdaz.com",
    role_keys=["member"],
    lookup_id="15",
)


def summarise(tasks: list[ProposalTask], *, open_only: bool = False) -> dict:
    return _summarise_member(MEMBER, tasks, open_only=open_only, now=NOW, soon=SOON)


# ── what the three counts mean ─────────────────────────────────────────


def test_a_closed_bid_is_open_but_not_active() -> None:
    """The distinction the whole screen rests on.

    Unfinished work whose bid closed months ago is an archive, not a workload.
    Counting it as live is what made every person on this list look underwater.
    """
    out = summarise([_task(bcd=NOW - timedelta(days=200))])
    assert out["total"] == 1
    assert out["open_count"] == 1
    assert out["active_count"] == 0
    assert out["due_soon_count"] == 0


def test_a_live_bid_is_both_open_and_active() -> None:
    out = summarise([_task(bcd=NOW + timedelta(days=30))])
    assert out["open_count"] == 1
    assert out["active_count"] == 1
    # Thirty days out is live but not imminent.
    assert out["due_soon_count"] == 0


def test_a_bid_inside_the_horizon_is_due_soon() -> None:
    out = summarise([_task(bcd=NOW + timedelta(days=SOON_DAYS - 1))])
    assert out["active_count"] == 1
    assert out["due_soon_count"] == 1


def test_a_completed_row_is_neither_open_nor_active() -> None:
    out = summarise([_task(status="Completed", bcd=NOW + timedelta(days=5))])
    assert out["total"] == 1
    assert out["open_count"] == 0
    assert out["active_count"] == 0


def test_a_statusless_row_whose_bid_closed_reads_as_expired() -> None:
    """Derived, never written — see ProposalTask.effective_status. A fifth of
    this list has no status, and treating those as live work is what made the
    open count meaningless."""
    out = summarise([_task(status=None, bcd=NOW - timedelta(days=90))])
    assert out["open_count"] == 0
    assert out["active_count"] == 0


def test_a_statusless_row_whose_bid_is_still_open_is_real_work() -> None:
    out = summarise([_task(status=None, bcd=NOW + timedelta(days=3))])
    assert out["open_count"] == 1
    assert out["active_count"] == 1
    assert out["due_soon_count"] == 1


# ── the next deadline ──────────────────────────────────────────────────


def test_the_next_deadline_ignores_bids_that_already_closed() -> None:
    """A date in the past is not "next", however near the top of the sort it is."""
    out = summarise(
        [
            _task("old", bcd=NOW - timedelta(days=300)),
            _task("soon", bcd=NOW + timedelta(days=4)),
            _task("later", bcd=NOW + timedelta(days=90)),
        ]
    )
    assert out["next_deadline"] == _iso(NOW + timedelta(days=4))


def test_no_live_work_means_no_next_deadline() -> None:
    out = summarise([_task(bcd=NOW - timedelta(days=10))])
    assert out["next_deadline"] is None


def test_a_row_with_no_dates_at_all_is_counted_but_never_next() -> None:
    out = summarise([_task(status="In Progress")])
    assert out["open_count"] == 1
    # No closing date means it cannot be shown to be still open, so it is not
    # counted as live — the same reading is_active takes.
    assert out["active_count"] == 0
    assert out["next_deadline"] is None


# ── ordering and the open_only filter ──────────────────────────────────


def test_rows_come_back_soonest_deadline_first() -> None:
    out = summarise(
        [
            _task("c", bcd=NOW + timedelta(days=40)),
            _task("a", bcd=NOW + timedelta(days=1)),
            _task("b", bcd=NOW + timedelta(days=10)),
        ]
    )
    assert [t.id for t in out["tasks"]] == ["a", "b", "c"]


def test_an_undated_row_sorts_last() -> None:
    out = summarise([_task("undated"), _task("dated", bcd=NOW + timedelta(days=99))])
    assert [t.id for t in out["tasks"]] == ["dated", "undated"]


def test_open_only_hides_finished_rows_but_not_from_the_total() -> None:
    """The count describes everything assigned; the list describes what is shown."""
    tasks = [
        _task("live", bcd=NOW + timedelta(days=5)),
        _task("done", status="Completed", bcd=NOW + timedelta(days=5)),
    ]
    out = summarise(tasks, open_only=True)
    assert [t.id for t in out["tasks"]] == ["live"]
    assert out["total"] == 2
    assert out["open_count"] == 1


def test_the_member_is_carried_through_unchanged() -> None:
    out = summarise([])
    assert out["user_id"] == MEMBER.user_id
    assert out["email"] == "sebin@hamdaz.com"
    assert out["role_keys"] == ["member"]
    assert out["in_sharepoint"] is True


def test_a_member_absent_from_sharepoint_is_reported_not_hidden() -> None:
    absent = MemberRef(
        user_id=uuid.uuid4(), name="New", email="new@hamdaz.com", role_keys=[], lookup_id=None
    )
    out = _summarise_member(absent, [], open_only=False, now=NOW, soon=SOON)
    assert out["in_sharepoint"] is False
    assert out["total"] == 0


# ── the cache ──────────────────────────────────────────────────────────


class StubSharePoint:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def tasks_assigned_to(self, lookup_id: str, *, limit: int = 200):
        self.calls.append(lookup_id)
        return [_task(f"{lookup_id}-1", bcd=NOW + timedelta(days=5))]


def _member(lookup_id: str | None) -> MemberRef:
    return MemberRef(
        user_id=uuid.uuid4(),
        name=lookup_id or "nobody",
        email=f"{lookup_id or 'nobody'}@hamdaz.com",
        role_keys=[],
        lookup_id=lookup_id,
    )


TEAM = uuid.uuid4()


async def test_a_warm_cache_does_not_re_query() -> None:
    cache, sp, members = TeamTasksCache(), StubSharePoint(), [_member("1"), _member("2")]

    _, cached, _ = await cache.rows(sp, team_id=TEAM, members=members, limit=10, refresh=False)
    assert cached is False
    assert sorted(sp.calls) == ["1", "2"]

    _, cached, _ = await cache.rows(sp, team_id=TEAM, members=members, limit=10, refresh=False)
    assert cached is True
    assert sorted(sp.calls) == ["1", "2"], "a cache hit must not touch SharePoint"


async def test_refresh_forces_a_re_read() -> None:
    cache, sp, members = TeamTasksCache(), StubSharePoint(), [_member("1")]
    await cache.rows(sp, team_id=TEAM, members=members, limit=10, refresh=False)
    _, cached, _ = await cache.rows(sp, team_id=TEAM, members=members, limit=10, refresh=True)
    assert cached is False
    assert sp.calls == ["1", "1"]


async def test_a_new_member_invalidates_the_sweep() -> None:
    """Serving a stale sweep would show a new joiner as carrying nothing, which
    is a lie rather than staleness — so completeness beats the TTL."""
    cache, sp = TeamTasksCache(), StubSharePoint()
    await cache.rows(sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False)

    rows, cached, _ = await cache.rows(
        sp, team_id=TEAM, members=[_member("1"), _member("2")], limit=10, refresh=False
    )
    assert cached is False
    assert set(rows) == {"1", "2"}


async def test_a_member_without_a_lookup_id_is_never_fetched() -> None:
    cache, sp = TeamTasksCache(), StubSharePoint()
    rows, _, _ = await cache.rows(
        sp, team_id=TEAM, members=[_member("1"), _member(None)], limit=10, refresh=False
    )
    assert sp.calls == ["1"]
    assert set(rows) == {"1"}


async def test_teams_are_cached_apart() -> None:
    """One lead's refresh must not serve another team's rows to the next caller."""
    cache, sp = TeamTasksCache(), StubSharePoint()
    other = uuid.uuid4()

    await cache.rows(sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False)
    _, cached, _ = await cache.rows(
        sp, team_id=other, members=[_member("9")], limit=10, refresh=False
    )
    assert cached is False
    assert sp.calls == ["1", "9"]


async def test_an_expired_entry_is_re_read() -> None:
    cache, sp = TeamTasksCache(ttl_seconds=0), StubSharePoint()
    await cache.rows(sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False)
    _, cached, _ = await cache.rows(
        sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False
    )
    assert cached is False


@pytest.mark.parametrize("scope", ["one", "all"])
async def test_invalidate_drops_what_it_says(scope: str) -> None:
    cache, sp = TeamTasksCache(), StubSharePoint()
    await cache.rows(sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False)

    cache.invalidate(TEAM if scope == "one" else None)
    _, cached, _ = await cache.rows(
        sp, team_id=TEAM, members=[_member("1")], limit=10, refresh=False
    )
    assert cached is False
