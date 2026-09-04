"""The SharePoint Proposals client, against a mock transport.

Nothing here reaches SharePoint. The important behaviours are the ones that
would be invisible until they leaked: that the request really is filtered by
lookup id, that the lookup id comes from the site's user list rather than a
display name, and that the query asks for system lists so the user list is
actually found.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.proposals.sharepoint import (
    ProposalTask,
    SharePointError,
    SharePointProposals,
)

SITE = "contoso.sharepoint.com,site-guid,web-guid"
LIST = "proposals-list-id"
UIL = "user-information-list-id"


def _settings() -> Settings:
    return Settings(
        azure_tenant_id="test-tenant",
        azure_client_id="test-client",
        azure_client_secret="test-secret",
        sharepoint_site_id=SITE,
        sharepoint_proposals_list_id=LIST,
    )


def _item(item_id: str, **fields) -> dict:
    base = {
        "Title": "A proposal",
        "Status": "In Progress",
        "Priority": "High",
        "AssignedToLookupId": "27",
        "AssignedTo": "Goutham",
    }
    return {"id": item_id, "webUrl": f"https://sp/item/{item_id}", "fields": {**base, **fields}}


class Stub:
    """Records every request so the tests can assert on the query, not the result."""

    def __init__(self) -> None:
        self.token_requests = 0
        self.requests: list[httpx.Request] = []
        self.users = [
            {"id": "11", "fields": {"Title": "Althaf", "EMail": "althaf@hamdaz.com"}},
            {"id": "15", "fields": {"Title": "Sebin", "EMail": "Sebin@Hamdaz.com"}},
            {"id": "27", "fields": {"Title": "Goutham", "EMail": "goutham@hamdaz.com"}},
            {"id": "3", "fields": {"Title": "Proposal Team Owners"}},  # no email
        ]
        self.items = [_item("1"), _item("2", Status="Completed")]
        self.include_system_list = True
        self.item_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

        self.requests.append(request)

        if "/lists?" in url or url.endswith("/lists"):
            lists = [{"id": LIST, "displayName": "Proposals"}]
            # Graph only returns system lists when the system facet is selected;
            # the stub mirrors that so the real bug can be caught.
            if self.include_system_list and "system" in url:
                lists.append({"id": UIL, "displayName": "User Information List"})
            return httpx.Response(200, json={"value": lists})

        if f"/lists/{UIL}/items" in url:
            return httpx.Response(200, json={"value": self.users})

        if f"/lists/{LIST}/items" in url:
            if self.item_status != 200:
                return httpx.Response(self.item_status, json={"error": "nope"})
            wanted = request.url.params.get("$filter", "")
            rows = [
                i for i in self.items
                if not wanted or i["fields"]["AssignedToLookupId"] in wanted
            ]
            return httpx.Response(200, json={"value": rows})

        if f"/lists/{LIST}/columns" in url:
            return httpx.Response(200, json={"value": [
                {"name": "Status", "displayName": "Status",
                 "choice": {"choices": ["Not Started", "Completed"]}},
                {"name": "Hidden", "displayName": "Hidden", "hidden": True},
            ]})

        return httpx.Response(404)

    def client(self) -> SharePointProposals:
        return SharePointProposals(
            _settings(), httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        )


# ── field mapping ──────────────────────────────────────────────────────


def test_maps_a_list_row() -> None:
    task = ProposalTask.from_item(
        _item("9", EndUser="Adnoc", zohpquoteno="QT-1", DueDate="2025-05-01T00:00:00Z")
    )
    assert task.id == "9"
    assert task.end_user == "Adnoc"
    assert task.quote_no == "QT-1"
    assert task.due_date == "2025-05-01T00:00:00Z"
    assert task.web_url == "https://sp/item/9"


def test_assigned_to_name_is_display_only() -> None:
    """The name is for humans; the lookup id is what authorises."""
    task = ProposalTask.from_item(_item("1"))
    assert task.assigned_to_name == "Goutham"
    assert task.assigned_to_lookup_id == "27"


def test_a_row_with_no_title_still_renders() -> None:
    assert ProposalTask.from_item({"id": "1", "fields": {}}).title == "(untitled)"


@pytest.mark.parametrize(
    "status,expected",
    [("In Progress", True), ("Not Started", True), ("On Hold", True),
     ("Completed", False), ("completed", False), (" COMPLETED ", False), (None, True)],
)
def test_open_versus_done(status: str | None, expected: bool) -> None:
    assert ProposalTask.from_item(_item("1", Status=status)).is_open is expected


# ── the site user list ─────────────────────────────────────────────────


async def test_maps_emails_to_lookup_ids() -> None:
    users = await Stub().client().site_users()
    assert users["althaf@hamdaz.com"] == "11"
    assert users["goutham@hamdaz.com"] == "27"


async def test_email_matching_is_case_insensitive() -> None:
    """SharePoint stores whatever case was typed; sign-in gives another."""
    client = Stub().client()
    assert await client.lookup_id_for("SEBIN@HAMDAZ.COM") == "15"
    assert await client.lookup_id_for("  sebin@hamdaz.com  ") == "15"


async def test_entries_without_an_email_are_skipped() -> None:
    """SharePoint groups appear in that list and are not people."""
    users = await Stub().client().site_users()
    assert "3" not in users.values()


async def test_the_user_list_is_cached() -> None:
    stub = Stub()
    client = stub.client()
    await client.site_users()
    before = len(stub.requests)
    await client.site_users()
    assert len(stub.requests) == before


async def test_an_unknown_email_forces_one_refresh() -> None:
    """Somebody added to the site since the cache was filled."""
    stub = Stub()
    client = stub.client()
    await client.site_users()
    calls = len(stub.requests)

    assert await client.lookup_id_for("stranger@hamdaz.com") is None
    assert len(stub.requests) > calls  # it re-read before giving up


async def test_the_user_list_query_asks_for_system_lists() -> None:
    """Regression: Graph omits system lists unless the system facet is selected.

    Without it the User Information List is simply absent from the response and
    every lookup fails with "this site has no User Information List".
    """
    stub = Stub()
    await stub.client().site_users()
    list_query = next(
        r for r in stub.requests
        if "/lists" in str(r.url) and "items" not in str(r.url)
    )
    assert "system" in list_query.url.params.get("$select", "")


async def test_a_site_without_a_user_list_is_an_error() -> None:
    stub = Stub()
    stub.include_system_list = False
    with pytest.raises(SharePointError, match="User Information List"):
        await stub.client().site_users()


# ── fetching tasks ─────────────────────────────────────────────────────


async def test_the_request_is_filtered_by_lookup_id() -> None:
    """The filter is the whole security boundary, so assert on the query."""
    stub = Stub()
    await stub.client().tasks_assigned_to("27")
    items = next(r for r in stub.requests if f"/lists/{LIST}/items" in str(r.url))
    assert items.url.params["$filter"] == "fields/AssignedToLookupId eq 27"


async def test_a_non_numeric_lookup_id_is_rejected() -> None:
    """int() guards the filter string against anything injectable."""
    with pytest.raises(ValueError):
        await Stub().client().tasks_assigned_to("27 or 1 eq 1")


async def test_the_non_indexed_header_is_sent() -> None:
    """AssignedToLookupId is not indexed; SharePoint refuses without this."""
    stub = Stub()
    await stub.client().tasks_assigned_to("27")
    items = next(r for r in stub.requests if f"/lists/{LIST}/items" in str(r.url))
    assert "HonorNonIndexedQueries" in items.headers.get("Prefer", "")


async def test_returns_only_the_matching_rows() -> None:
    stub = Stub()
    stub.items = [_item("1", AssignedToLookupId="27"), _item("2", AssignedToLookupId="99")]
    tasks = await stub.client().tasks_assigned_to("27")
    assert [t.id for t in tasks] == ["1"]


async def test_the_limit_is_honoured() -> None:
    stub = Stub()
    stub.items = [_item(str(i)) for i in range(10)]
    assert len(await stub.client().tasks_assigned_to("27", limit=3)) == 3


async def test_a_sharepoint_failure_is_raised() -> None:
    stub = Stub()
    stub.item_status = 503
    with pytest.raises(SharePointError, match="503"):
        await stub.client().tasks_assigned_to("27")


async def test_a_rejected_token_is_discarded() -> None:
    stub = Stub()
    stub.item_status = 401
    client = stub.client()
    with pytest.raises(SharePointError, match="rejected"):
        await client.tasks_assigned_to("27")
    stub.item_status = 200
    await client.tasks_assigned_to("27")
    assert stub.token_requests == 2


async def test_the_token_is_reused_between_calls() -> None:
    stub = Stub()
    client = stub.client()
    await client.tasks_assigned_to("27")
    await client.tasks_assigned_to("11")
    assert stub.token_requests == 1


# ── the list schema ────────────────────────────────────────────────────


async def test_columns_exclude_hidden_ones() -> None:
    columns = await Stub().client().list_columns()
    assert [c["name"] for c in columns] == ["Status"]
    assert columns[0]["choices"] == ["Not Started", "Completed"]
