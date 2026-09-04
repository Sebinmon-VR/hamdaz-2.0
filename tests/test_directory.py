"""The Graph directory client, against a mock transport.

Nothing here reaches Microsoft. The transport returns canned Graph payloads, so
these tests cover the parts that are easy to get quietly wrong — paging, token
caching, and which accounts are filtered out — without a network round trip or a
dependency on who happens to work at the company today.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.directory.graph import GRAPH_BASE, GraphDirectory, GraphError, OrgUser


def _settings() -> Settings:
    return Settings(
        azure_tenant_id="test-tenant",
        azure_client_id="test-client",
        azure_client_secret="test-secret",
    )


def _raw(
    oid: str = "oid-1",
    name: str = "A Person",
    mail: str | None = "person@hamdaz.com",
    upn: str | None = None,
    enabled: bool = True,
    user_type: str = "Member",
    **extra,
) -> dict:
    return {
        "id": oid,
        "displayName": name,
        "mail": mail,
        "userPrincipalName": upn or (mail or f"{oid}@hamdaz.com"),
        "accountEnabled": enabled,
        "userType": user_type,
        **extra,
    }


class GraphStub:
    """A mock transport that records what was asked of it."""

    def __init__(self, pages: list[dict] | None = None, token_status: int = 200) -> None:
        self.pages = pages if pages is not None else [{"value": []}]
        self.token_status = token_status
        self.token_requests = 0
        self.data_requests: list[httpx.URL] = []
        self.user_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            self.token_requests += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": f"token-{self.token_requests}", "expires_in": 3600}
            )

        self.data_requests.append(request.url)
        if self.user_status != 200:
            return httpx.Response(self.user_status, json={"error": {"message": "nope"}})

        index = len(self.data_requests) - 1
        return httpx.Response(200, json=self.pages[min(index, len(self.pages) - 1)])

    def directory(self) -> GraphDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return GraphDirectory(_settings(), client)


# ── field mapping ──────────────────────────────────────────────────────


def test_maps_graph_fields() -> None:
    user = OrgUser.from_graph(
        _raw(jobTitle="Engineer", department="Delivery", officeLocation="Dubai",
             mobilePhone="+971", givenName="A", surname="Person")
    )
    assert user.object_id == "oid-1"
    assert user.job_title == "Engineer"
    assert user.department == "Delivery"
    assert user.account_enabled is True


def test_lowercases_email_and_upn() -> None:
    user = OrgUser.from_graph(_raw(mail="Person@Hamdaz.com", upn="Person@Hamdaz.com"))
    assert user.email == "person@hamdaz.com"
    assert user.user_principal_name == "person@hamdaz.com"


def test_account_without_a_mailbox_has_no_email() -> None:
    """Real accounts in this tenant have no mail value; the UPN always exists."""
    user = OrgUser.from_graph(_raw(mail=None, upn="service@hamdaz.com"))
    assert user.email is None
    assert user.user_principal_name == "service@hamdaz.com"


def test_display_name_falls_back_to_upn() -> None:
    user = OrgUser.from_graph({"id": "x", "userPrincipalName": "nameless@hamdaz.com"})
    assert user.display_name == "nameless@hamdaz.com"


def test_guest_detection_is_case_insensitive() -> None:
    assert OrgUser.from_graph(_raw(user_type="GUEST")).is_guest is True
    assert OrgUser.from_graph(_raw(user_type="Member")).is_guest is False


# ── filtering ──────────────────────────────────────────────────────────


async def test_excludes_disabled_accounts_by_default() -> None:
    stub = GraphStub([{"value": [_raw("a", "Active"), _raw("b", "Gone", enabled=False)]}])
    users = await stub.directory().list_users()
    assert [u.display_name for u in users] == ["Active"]


async def test_includes_disabled_when_asked() -> None:
    stub = GraphStub([{"value": [_raw("a", "Active"), _raw("b", "Gone", enabled=False)]}])
    users = await stub.directory().list_users(include_disabled=True)
    assert len(users) == 2


async def test_asks_graph_to_exclude_guests_by_default() -> None:
    """Guest filtering is pushed to Graph, so assert the query, not the result.

    Read $filter off the decoded params: the raw URL is percent-encoded, and
    "userType" also appears in $select, so a substring check on it proves nothing.
    """
    stub = GraphStub()
    await stub.directory().list_users()
    assert stub.data_requests[0].params.get("$filter") == "userType eq 'Member'"


async def test_drops_the_member_filter_when_guests_are_wanted() -> None:
    stub = GraphStub()
    await stub.directory().list_users(include_guests=True)
    assert "$filter" not in stub.data_requests[0].params


async def test_sorts_by_display_name_case_insensitively() -> None:
    stub = GraphStub([{"value": [_raw("c", "zoe"), _raw("a", "Adam"), _raw("b", "bob")]}])
    users = await stub.directory().list_users()
    assert [u.display_name for u in users] == ["Adam", "bob", "zoe"]


# ── paging ─────────────────────────────────────────────────────────────


async def test_follows_next_link_to_the_end() -> None:
    stub = GraphStub(
        [
            {"value": [_raw("a", "A")], "@odata.nextLink": f"{GRAPH_BASE}/users?$skiptoken=1"},
            {"value": [_raw("b", "B")], "@odata.nextLink": f"{GRAPH_BASE}/users?$skiptoken=2"},
            {"value": [_raw("c", "C")]},
        ]
    )
    users = await stub.directory().list_users()
    assert [u.display_name for u in users] == ["A", "B", "C"]
    assert len(stub.data_requests) == 3


async def test_does_not_resend_params_on_a_next_link() -> None:
    """nextLink already carries the query; re-adding it makes Graph 400."""
    stub = GraphStub(
        [
            {"value": [], "@odata.nextLink": f"{GRAPH_BASE}/users?$skiptoken=abc"},
            {"value": []},
        ]
    )
    await stub.directory().list_users()
    second = str(stub.data_requests[1])
    assert "$skiptoken=abc" in second
    assert "%24select" not in second and "$select" not in second


async def test_stops_rather_than_looping_forever() -> None:
    """A nextLink that never terminates must not hang the request."""
    endless = [{"value": [_raw()], "@odata.nextLink": f"{GRAPH_BASE}/users?$skiptoken=x"}]
    with pytest.raises(GraphError, match="exceeded"):
        await GraphStub(endless).directory().list_users()


# ── the application token ──────────────────────────────────────────────


async def test_token_is_fetched_once_and_reused() -> None:
    stub = GraphStub()
    directory = stub.directory()
    await directory.list_users()
    await directory.list_users()
    assert stub.token_requests == 1


async def test_concurrent_calls_share_one_token_request() -> None:
    """A cold cache under load must not stampede the token endpoint."""
    import asyncio

    stub = GraphStub()
    directory = stub.directory()
    await asyncio.gather(*(directory.list_users() for _ in range(5)))
    assert stub.token_requests == 1


async def test_token_failure_is_reported() -> None:
    stub = GraphStub(token_status=401)
    with pytest.raises(GraphError, match="token request failed"):
        await stub.directory().list_users()


async def test_a_rejected_token_is_discarded_so_the_next_call_reauths() -> None:
    stub = GraphStub()
    stub.user_status = 401
    directory = stub.directory()

    with pytest.raises(GraphError, match="rejected"):
        await directory.list_users()
    stub.user_status = 200
    await directory.list_users()

    assert stub.token_requests == 2  # not reusing the token Graph refused


async def test_graph_failure_is_reported() -> None:
    stub = GraphStub()
    stub.user_status = 500
    with pytest.raises(GraphError, match="500"):
        await stub.directory().list_users()


async def test_get_user_reports_not_found() -> None:
    stub = GraphStub()
    stub.user_status = 404
    with pytest.raises(GraphError, match="not found"):
        await stub.directory().get_user("missing")
