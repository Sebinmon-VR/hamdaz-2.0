"""Publishing the priority score to the useranalytics list.

No database and no SharePoint: the client runs against a recording transport,
so every test can say exactly which rows were written and — more importantly —
which were not. The rules under test are the ones that protect a shared list:
a row that has not changed is not touched, a row we did not make is matched by
name and rewritten rather than duplicated, only an excluded candidate's row is
ever removed, and the switch being off means no request at all.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.analytics.publisher import (
    COLUMNS,
    Publisher,
    Standing,
    _same,
    fields_for,
    from_live,
)
from app.core.config import Settings
from app.models.analytics import LiveScore
from app.proposals.sharepoint import SharePointProposals

SITE = "hamdaz1.sharepoint.com,site,web"
LIST = "list-guid"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "azure_tenant_id": "t",
        "azure_client_id": "c",
        "azure_client_secret": "s",
        "analytics_publish_enabled": True,
        "analytics_site_id": SITE,
        "analytics_list_id": LIST,
        "analytics_list_url": "https://sp/sites/Test/Lists/useranalytics",
    }
    values.update(overrides)
    return Settings(**values)


class ListStub:
    """A useranalytics list that records what is done to it."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.reads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.method == "GET" and f"/lists/{LIST}/items" in url:
            self.reads += 1
            return httpx.Response(200, json={"value": self.rows})
        if request.method == "POST" and url.endswith(f"/lists/{LIST}/items"):
            fields = json.loads(request.content)["fields"]
            self.created.append(fields)
            return httpx.Response(201, json={"id": str(900 + len(self.created)), "fields": fields})
        if request.method == "PATCH" and "/fields" in url:
            item_id = url.split("/items/")[1].split("/")[0]
            self.updated.append((item_id, json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404)

    def publisher(self, settings: Settings | None = None) -> Publisher:
        settings = settings or _settings()
        client = SharePointProposals(
            settings, httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        )
        return Publisher(settings, client)


def who(name: str, rank: int = 1, **overrides: Any) -> Standing:
    values: dict[str, Any] = {
        "display_name": name,
        "rank": rank,
        "active_tasks": 3,
        "open_tasks": 5,
        "last_assigned": date(2026, 9, 11),
        "labels": [],
        "team": "presales",
    }
    values.update(overrides)
    return Standing(**values)


def row(item_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": item_id, "fields": fields}


# ── the row ────────────────────────────────────────────────────────────


def test_a_row_says_who_is_next_in_the_lists_own_columns() -> None:
    fields = fields_for(who("Jasna", rank=1))
    assert set(fields) == set(COLUMNS)
    assert fields["Title"] == fields["Username"] == "Jasna"
    assert fields["Priority"] == 1
    assert fields["ActiveTasks"] == 3
    assert fields["RecentDate"] == "2026-09-11T00:00:00Z"
    assert fields["Jobs"] == "presales"


def test_leave_is_not_a_column() -> None:
    assert "Leave" not in fields_for(who("Anyone"))
    assert not who("Feba", rank=0, labels=["on-leave"]).eligible


def test_never_assigned_has_no_recent_date() -> None:
    assert fields_for(who("New", last_assigned=None))["RecentDate"] is None


def test_swapcounter_and_jobcount_are_never_ours() -> None:
    assert "swapcounter" not in fields_for(who("Anyone"))
    assert "jobcount" not in fields_for(who("Anyone"))


def test_a_live_row_gives_the_same_date_a_kept_run_would() -> None:
    """Assigned 18:30 on the 11th, scored 07:37 on the 14th: 2.55 days. Whole
    days would say the 12th; the timestamp says the 11th, and so must this."""
    from datetime import UTC, datetime

    live = LiveScore(
        display_name="Haleema",
        rank=1,
        eligible=True,
        active_tasks=5,
        open_tasks=9,
        days_since_assigned=2,
        factors={"days_since_last_assign": {"raw": 2.5465}},
        labels=[],
        computed_at=datetime(2026, 9, 14, 7, 37, tzinfo=UTC),
    )
    [standing] = from_live([live], team="presales")
    assert standing.last_assigned == date(2026, 9, 11)
    assert standing.eligible and standing.rank == 1


# ── change detection ───────────────────────────────────────────────────


def test_graph_floats_and_sharepoint_times_still_count_as_the_same() -> None:
    wanted = fields_for(who("Jasna"))
    have = {
        **wanted,
        "Priority": 1.0,
        "ActiveTasks": 3.0,
        "RecentDate": "2026-09-11T07:00:00Z",
    }
    assert _same(have, wanted)


def test_a_moved_rank_is_a_change() -> None:
    wanted = fields_for(who("Jasna", rank=2))
    assert not _same({**wanted, "Priority": 1.0}, wanted)


def test_a_missing_column_is_a_change() -> None:
    wanted = fields_for(who("Jasna"))
    have = {k: v for k, v in wanted.items() if k != "Jobs"}
    assert not _same(have, wanted)


# ── the publisher ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_means_no_request_at_all() -> None:
    stub = ListStub()
    report = await stub.publisher(_settings(analytics_publish_enabled=False)).publish(
        [who("Jasna")]
    )
    assert report.error and "off" in report.error
    assert stub.reads == 0 and not stub.created


@pytest.mark.asyncio
async def test_a_new_person_gets_a_row_and_a_known_one_is_rewritten() -> None:
    stub = ListStub([row("332", Username="Jasna", Priority=3.0, ActiveTasks=9.0)])
    report = await stub.publisher().publish(
        [who("Jasna", rank=1), who("Haleema", rank=2)]
    )

    assert report.updated == 1 and report.created == 1 and report.unchanged == 0
    assert stub.updated[0][0] == "332"
    assert stub.updated[0][1]["Priority"] == 1
    assert stub.created[0]["Username"] == "Haleema"
    assert sorted(report.names) == ["Haleema", "Jasna"]


@pytest.mark.asyncio
async def test_a_row_that_already_says_this_is_left_alone() -> None:
    fields = fields_for(who("Jasna"))
    as_graph_returns_it = {**fields, "Priority": 1.0, "RecentDate": "2026-09-11T07:00:00Z"}
    stub = ListStub([row("332", **as_graph_returns_it)])
    report = await stub.publisher().publish([who("Jasna")])

    assert report.unchanged == 1
    assert not stub.updated and not stub.created


@pytest.mark.asyncio
async def test_matching_is_by_name_and_forgiving_about_case_and_spaces() -> None:
    stub = ListStub([row("1", Title="fasna  sherin")])  # no Username at all
    await stub.publisher().publish([who("Fasna Sherin")])
    assert stub.updated and stub.updated[0][0] == "1"
    assert not stub.created


@pytest.mark.asyncio
async def test_a_duplicate_row_is_left_exactly_as_it_was() -> None:
    stub = ListStub(
        [row("1", Username="Jasna", Priority=9.0), row("2", Username="Jasna", Priority=9.0)]
    )
    await stub.publisher().publish([who("Jasna")])
    assert [item_id for item_id, _ in stub.updated] == ["1"]


@pytest.mark.asyncio
async def test_somebody_on_leave_has_no_row() -> None:
    stub = ListStub([row("352", Username="Feba", Priority=2.0)])
    report = await stub.publisher().publish(
        [who("Jasna", rank=1), who("Feba", rank=0, labels=["on-leave"])]
    )
    assert stub.deleted == ["352"]
    assert report.removed == 1 and "Feba" in report.names
    assert not any(f["Username"] == "Feba" for f in stub.created)


@pytest.mark.asyncio
async def test_an_excluded_person_with_no_row_gets_none() -> None:
    stub = ListStub()
    report = await stub.publisher().publish([who("Sebin", rank=0)])
    assert not stub.created and not stub.deleted
    assert report.written == 0 and report.unchanged == 0


@pytest.mark.asyncio
async def test_somebody_who_left_the_team_keeps_their_row() -> None:
    stub = ListStub([row("7", Username="Gone", Priority=1.0)])
    await stub.publisher().publish([who("Jasna")])
    assert not stub.deleted
    assert all(item_id != "7" for item_id, _ in stub.updated)


@pytest.mark.asyncio
async def test_one_refused_row_does_not_stop_the_others() -> None:
    stub = ListStub()
    refused = {"Haleema"}

    original = stub.handler

    def handler(request: httpx.Request) -> httpx.Response:
        creating = request.method == "POST" and request.url.path.endswith("/items")
        if creating and json.loads(request.content)["fields"]["Username"] in refused:
            return httpx.Response(400, json={"error": "no"})
        return original(request)

    settings = _settings()
    transport = httpx.MockTransport(handler)
    client = SharePointProposals(settings, httpx.AsyncClient(transport=transport))
    report = await Publisher(settings, client).publish([who("Haleema"), who("Jasna")])

    assert report.created == 1 and report.names == ["Jasna"]
    assert report.error and "Haleema" in report.error


@pytest.mark.asyncio
async def test_an_unreachable_list_is_a_report_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(503, text="down")

    settings = _settings()
    transport = httpx.MockTransport(handler)
    client = SharePointProposals(settings, httpx.AsyncClient(transport=transport))
    report = await Publisher(settings, client).publish([who("Jasna")])
    assert report.error and "503" in report.error
    assert report.written == 0
