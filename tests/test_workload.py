"""Per-person proposal workload.

``summarise`` is pure and synchronous on purpose, so the counting rules can be
tested exactly without going near SharePoint. The cache is tested separately for
the one behaviour that matters: it must not re-sweep until it expires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.proposals.analytics import SOON_DAYS, WorkloadCache, summarise
from app.proposals.sharepoint import ProposalTask

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _task(
    *,
    who: str | None = "15",
    status: str | None = "In Progress",
    bcd: datetime | None = None,
    due: datetime | None = None,
    name: str | None = "Sebin",
) -> ProposalTask:
    return ProposalTask(
        id="1", title="A proposal", status=status, priority="High",
        assigned_to_lookup_id=who, assigned_to_name=name,
        start_date=None,
        due_date=due.isoformat().replace("+00:00", "Z") if due else None,
        bid_closing_date=bcd.isoformat().replace("+00:00", "Z") if bcd else None,
        end_user=None, submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None, web_url=None,
    )


PEOPLE = {
    "15": {"name": "Sebin", "email": "sebin@hamdaz.com"},
    "27": {"name": "Goutham", "email": "goutham@hamdaz.com"},
}


def _one(summary, name):
    return next(p for p in summary["people"] if p["name"] == name)


# ── the deadline is BCD ─────────────────────────────────────────────────


def test_bcd_is_the_deadline_not_due_date() -> None:
    """BCD is set on every row; DueDate on a third, and they disagree."""
    task = _task(bcd=NOW + timedelta(days=2), due=NOW + timedelta(days=90))
    assert task.deadline == task.bid_closing_date

    summary = summarise([task], PEOPLE, now=NOW)
    assert _one(summary, "Sebin")["due_soon"] == 1


def test_due_date_is_used_only_when_bcd_is_missing() -> None:
    task = _task(bcd=None, due=NOW - timedelta(days=1))
    assert task.deadline == task.due_date
    assert _one(summarise([task], PEOPLE, now=NOW), "Sebin")["overdue"] == 1


# ── bucketing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "offset_days,bucket",
    [(-30, "overdue"), (-1, "overdue"), (1, "due_soon"),
     (SOON_DAYS, "due_soon"), (SOON_DAYS + 1, "later"), (365, "later")],
)
def test_open_tasks_land_in_the_right_bucket(offset_days: int, bucket: str) -> None:
    task = _task(bcd=NOW + timedelta(days=offset_days))
    person = _one(summarise([task], PEOPLE, now=NOW), "Sebin")
    assert person[bucket] == 1
    assert person["open"] == 1


def test_an_open_task_with_no_deadline_is_its_own_bucket() -> None:
    person = _one(summarise([_task(bcd=None)], PEOPLE, now=NOW), "Sebin")
    assert person["no_deadline"] == 1
    assert person["overdue"] == 0


def test_completed_tasks_are_never_overdue() -> None:
    """Something finished late is done, not outstanding."""
    task = _task(status="Completed", bcd=NOW - timedelta(days=100))
    person = _one(summarise([task], PEOPLE, now=NOW), "Sebin")
    assert person["completed"] == 1
    assert person["open"] == 0
    assert person["overdue"] == 0


def test_a_row_with_no_status_counts_as_open_and_is_flagged() -> None:
    """A fifth of the real list has no status; hiding that would overstate nothing
    and understating it would hide a real backlog. So: both."""
    person = _one(summarise([_task(status=None)], PEOPLE, now=NOW), "Sebin")
    assert person["open"] == 1
    assert person["no_status"] == 1
    assert person["by_status"]["(no status)"] == 1


# ── per person and organisation ─────────────────────────────────────────


def test_counts_are_split_by_person() -> None:
    tasks = [
        _task(who="15", name="Sebin"),
        _task(who="27", name="Goutham"),
        _task(who="27", name="Goutham", status="Completed"),
    ]
    summary = summarise(tasks, PEOPLE, now=NOW)

    assert summary["person_count"] == 2
    assert _one(summary, "Sebin")["total"] == 1
    assert _one(summary, "Goutham")["total"] == 2
    assert _one(summary, "Goutham")["completed"] == 1


def test_the_organisation_total_is_the_sum() -> None:
    tasks = [_task(who="15"), _task(who="27", name="Goutham"), _task(who="27", name="Goutham")]
    org = summarise(tasks, PEOPLE, now=NOW)["organisation"]
    assert org["total"] == 3
    assert org["open"] == 3


def test_names_come_from_the_site_user_list() -> None:
    """The list's display name may be stale; the user list is authoritative."""
    summary = summarise([_task(who="27", name="stale name")], PEOPLE, now=NOW)
    assert summary["people"][0]["name"] == "Goutham"
    assert summary["people"][0]["email"] == "goutham@hamdaz.com"


def test_an_unknown_assignee_falls_back_to_the_display_name() -> None:
    summary = summarise([_task(who="999", name="Someone Else")], PEOPLE, now=NOW)
    assert summary["people"][0]["name"] == "Someone Else"
    assert summary["people"][0]["email"] is None


def test_rows_assigned_to_nobody_are_shown_not_hidden() -> None:
    summary = summarise([_task(who=None, name=None)], PEOPLE, now=NOW)
    assert summary["people"][0]["name"] == "Unassigned"
    assert summary["people"][0]["lookup_id"] is None


def test_people_are_sorted_by_overdue_then_open() -> None:
    """An admin opening this wants the overloaded people first."""
    tasks = (
        [_task(who="15", name="Sebin", bcd=NOW + timedelta(days=1))] * 5
        + [_task(who="27", name="Goutham", bcd=NOW - timedelta(days=1))] * 2
    )
    names = [p["name"] for p in summarise(tasks, PEOPLE, now=NOW)["people"]]
    assert names == ["Goutham", "Sebin"]  # 2 overdue beats 5 merely open


def test_next_deadline_is_the_soonest_still_ahead() -> None:
    tasks = [
        _task(bcd=NOW + timedelta(days=20)),
        _task(bcd=NOW + timedelta(days=3)),
        _task(bcd=NOW - timedelta(days=5)),  # past; must not win
    ]
    person = _one(summarise(tasks, PEOPLE, now=NOW), "Sebin")
    assert person["next_deadline"].startswith("2026-06-04")


def test_an_empty_list_summarises_cleanly() -> None:
    summary = summarise([], PEOPLE, now=NOW)
    assert summary["people"] == []
    assert summary["organisation"]["total"] == 0


def test_a_malformed_date_is_treated_as_no_deadline() -> None:
    task = _task(bcd=None)
    object.__setattr__(task, "bid_closing_date", "not-a-date")
    assert _one(summarise([task], PEOPLE, now=NOW), "Sebin")["no_deadline"] == 1


# ── the cache ───────────────────────────────────────────────────────────


class _Stub:
    def __init__(self) -> None:
        self.sweeps = 0

    async def all_tasks(self, **kwargs):
        self.sweeps += 1
        return [_task()]

    async def site_people(self):
        return PEOPLE


async def test_the_first_call_sweeps() -> None:
    stub = _Stub()
    out = await WorkloadCache().get(stub)
    assert stub.sweeps == 1
    assert out["cached"] is False
    assert out["row_count"] == 1


async def test_a_second_call_does_not_sweep_again() -> None:
    """The whole point: one sweep serves every admin looking at once."""
    stub, cache = _Stub(), WorkloadCache()
    await cache.get(stub)
    out = await cache.get(stub)
    assert stub.sweeps == 1
    assert out["cached"] is True


async def test_refresh_forces_a_sweep() -> None:
    stub, cache = _Stub(), WorkloadCache()
    await cache.get(stub)
    out = await cache.get(stub, refresh=True)
    assert stub.sweeps == 2
    assert out["cached"] is False


async def test_the_cache_expires() -> None:
    stub, cache = _Stub(), WorkloadCache(ttl_seconds=0)
    await cache.get(stub)
    await cache.get(stub)
    assert stub.sweeps == 2


async def test_invalidate_drops_it() -> None:
    stub, cache = _Stub(), WorkloadCache()
    await cache.get(stub)
    cache.invalidate()
    await cache.get(stub)
    assert stub.sweeps == 2


async def test_a_failure_is_not_cached() -> None:
    """A transient SharePoint error must not poison the next 60 seconds."""
    from app.proposals.sharepoint import SharePointError

    class _Broken(_Stub):
        async def all_tasks(self, **kwargs):
            raise SharePointError("503")

    cache = WorkloadCache()
    with pytest.raises(SharePointError):
        await cache.get(_Broken())

    good = _Stub()
    out = await cache.get(good)
    assert out["cached"] is False
    assert good.sweeps == 1


# ── scoping to a team's members ─────────────────────────────────────────


def test_scoping_keeps_only_the_named_people() -> None:
    tasks = [
        _task(who="15", name="Sebin"),
        _task(who="27", name="Goutham"),
        _task(who="99", name="Outsider"),
    ]
    summary = summarise(tasks, PEOPLE, now=NOW, only={"15", "27"})
    assert {p["name"] for p in summary["people"]} == {"Sebin", "Goutham"}
    assert summary["organisation"]["total"] == 2


def test_scoping_reports_what_it_excluded() -> None:
    """A view that quietly hides four hundred overdue tasks is worse than an
    empty one. The count and the names both come back."""
    tasks = [_task(who="15")] + [_task(who="99", name="Outsider")] * 7
    summary = summarise(tasks, PEOPLE, now=NOW, only={"15"})

    assert summary["excluded"]["rows"] == 7
    assert summary["excluded"]["people"] == 1
    assert summary["excluded"]["names"] == ["Outsider"]


def test_unassigned_rows_are_excluded_by_a_scope() -> None:
    """Nobody is a member, so rows assigned to nobody fall outside any team."""
    summary = summarise([_task(who=None, name=None)], PEOPLE, now=NOW, only={"15"})
    assert summary["people"] == []
    assert summary["excluded"]["rows"] == 1


def test_no_scope_means_everyone() -> None:
    tasks = [_task(who="15"), _task(who="99", name="Outsider")]
    summary = summarise(tasks, PEOPLE, now=NOW)
    assert summary["person_count"] == 2
    assert summary["excluded"]["rows"] == 0


def test_a_scope_matching_nobody_gives_empty_but_explained() -> None:
    summary = summarise([_task(who="15")], PEOPLE, now=NOW, only={"404"})
    assert summary["people"] == []
    assert summary["organisation"]["total"] == 0
    assert summary["excluded"]["rows"] == 1  # and says so


def test_a_member_with_no_rows_simply_does_not_appear() -> None:
    """Scoping does not invent zero rows for people with nothing assigned."""
    summary = summarise([_task(who="15")], PEOPLE, now=NOW, only={"15", "27"})
    assert [p["name"] for p in summary["people"]] == ["Sebin"]


async def test_one_sweep_serves_several_scopes() -> None:
    """The raw rows are cached, not the summary, so a second team is free."""
    stub, cache = _Stub(), WorkloadCache()
    await cache.get(stub, only={"15"})
    await cache.get(stub, only={"27"})
    await cache.get(stub)
    assert stub.sweeps == 1


async def test_a_scoped_call_reports_the_cache_state() -> None:
    stub, cache = _Stub(), WorkloadCache()
    first = await cache.get(stub, only={"15"})
    second = await cache.get(stub, only={"15"})
    assert first["cached"] is False and first["fetch_ms"] >= 0
    assert second["cached"] is True and second["fetch_ms"] == 0
