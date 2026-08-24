"""SharePoint ingest — token acquisition and the field mapping (§8.1).

These lock down the two reasons live data never arrived: nothing acquired an app-only Graph
token, and the field map named columns the live Proposals list does not have.

Every column name asserted here was read off the live list on /sites/ProposalTeam. If
SharePoint's schema changes, these fail — which is the point. A mapping that silently maps
nothing is indistinguishable from an empty list.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.connectors.sharepoint.client import SharePointReadClient
from app.connectors.sharepoint.guard import SharePointWriteGuard
from app.core.config import Environment, Settings
from app.core.graph_auth import GRAPH_DEFAULT_SCOPE, GraphAuthError, GraphTokenProvider
from app.models.proposals import ProposalStatus
from app.services.sharepoint_sync import (
    ASSIGNEE_LOOKUP_FIELD,
    FIELD_MAP,
    _map_fields,
    _map_status,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": Environment.LOCAL,
        "jwt_secret": "test-secret-not-a-real-key",
        "azure_tenant_id": "tenant-guid",
        "azure_client_id": "client-guid",
        "azure_client_secret": "client-secret",
    }
    base.update(overrides)
    return Settings(**base)


def _provider(handler: Any, **overrides: Any) -> GraphTokenProvider:
    return GraphTokenProvider(
        _settings(**overrides),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ── app-only token ─────────────────────────────────────────────────────


class TestGraphTokenProvider:
    @pytest.mark.asyncio
    async def test_acquires_an_app_only_token(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

        provider = _provider(handler)
        assert await provider.get_token() == "tok-1"
        await provider.aclose()

        # client_credentials, not authorization_code: there is no user in a scheduled sync.
        assert seen[0]["grant_type"] == "client_credentials"
        assert seen[0]["scope"] == GRAPH_DEFAULT_SCOPE

    @pytest.mark.asyncio
    async def test_token_is_cached_rather_than_refetched_per_call(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

        provider = _provider(handler)
        for _ in range(5):
            assert await provider.get_token() == "tok-1"
        await provider.aclose()
        assert calls == 1

    @pytest.mark.asyncio
    async def test_near_expiry_token_is_refreshed(self) -> None:
        """A token past the safety margin must not be handed out again."""
        tokens = iter(["tok-1", "tok-2"])

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 30})

        provider = _provider(handler)
        assert await provider.get_token() == "tok-1"
        provider._expires_at = 0.0  # simulate the clock passing the margin
        assert await provider.get_token() == "tok-2"
        await provider.aclose()

    @pytest.mark.asyncio
    async def test_missing_credentials_are_reported_not_silently_skipped(self) -> None:
        """The old behaviour — return "skipped" and look fine — is what hid this bug."""
        provider = _provider(lambda _: httpx.Response(200), azure_client_secret="")
        with pytest.raises(GraphAuthError, match="AZURE_CLIENT_SECRET"):
            await provider.get_token()
        await provider.aclose()

    @pytest.mark.asyncio
    async def test_rejected_credentials_surface_the_aadsts_code(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": "AADSTS7000215: Invalid client secret provided.",
                },
            )

        provider = _provider(handler)
        with pytest.raises(GraphAuthError, match="AADSTS7000215"):
            await provider.get_token()
        await provider.aclose()


# ── field mapping ──────────────────────────────────────────────────────


class TestFieldMap:
    def test_maps_only_columns_the_live_list_actually_has(self) -> None:
        """Guards against the regression this fixed: names that map nothing.

        ``Customer``, ``Reference`` and a bare ``AssignedTo`` are not columns on the live
        Proposals list. Mapping them produced empty rows, not an error.
        """
        assert set(FIELD_MAP) == {"Title", "EndUser", "BCD", "SubmissionStatus"}

    def test_end_user_is_the_customer_column(self) -> None:
        mapped = _map_fields({"Title": "RFQ 6000148728", "EndUser": "ADNOC"})
        assert mapped == {"title": "RFQ 6000148728", "customer_name": "ADNOC"}

    def test_submission_status_is_distinct_from_workflow_status(self) -> None:
        """SharePoint has both; the old map fed Status into submission_status."""
        mapped = _map_fields({"Status": "Completed", "SubmissionStatus": "Submitted"})
        assert mapped == {"submission_status": "Submitted"}

    def test_bcd_is_parsed_to_a_datetime(self) -> None:
        mapped = _map_fields({"BCD": "2026-08-26T17:00:00Z"})
        assert mapped["bcd"].year == 2026

    def test_unparseable_bcd_is_dropped_not_stored_as_text(self) -> None:
        assert _map_fields({"BCD": "not a date"}) == {}

    def test_assignee_is_read_from_the_lookup_field(self) -> None:
        """Person columns arrive as ``AssignedToLookupId``, never as ``AssignedTo``."""
        assert ASSIGNEE_LOOKUP_FIELD == "AssignedToLookupId"
        assert "AssignedTo" not in FIELD_MAP


class TestStatusMap:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # The majority value on the live list — it used to fall through to NEW.
            ("Completed", ProposalStatus.SUBMITTED),
            ("In Progress", ProposalStatus.IN_PROGRESS),
            ("  in progress  ", ProposalStatus.IN_PROGRESS),
            ("Won", ProposalStatus.WON),
            ("Lost", ProposalStatus.LOST),
        ],
    )
    def test_live_status_values_map(self, raw: str, expected: ProposalStatus) -> None:
        assert _map_status(raw) == expected

    @pytest.mark.parametrize("raw", ["Not Started", "On Hold", "Choice 5", "", None])
    def test_unknown_values_do_not_invent_a_status(self, raw: object) -> None:
        assert _map_status(raw) == ProposalStatus.NEW

    def test_unknown_value_preserves_an_existing_status(self) -> None:
        """A new SharePoint choice must not silently reopen closed work."""
        assert _map_status("Choice 9", current=ProposalStatus.WON) == ProposalStatus.WON


# ── person directory ───────────────────────────────────────────────────


class TestSiteUserDirectory:
    @pytest.mark.asyncio
    async def test_lookup_ids_resolve_to_addresses_and_groups_are_skipped(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "41", "fields": {"Title": "Jasna", "EMail": "Jasna@hamdaz.com"}},
                        # A security group: no address, must not become an assignee.
                        {"id": "3", "fields": {"Title": "Proposal Team Owners"}},
                    ]
                },
            )

        client = SharePointReadClient(
            access_token="tok",
            guard=SharePointWriteGuard(sandbox_site_id=None, writes_enabled=False),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        directory = await client.get_site_users("site-id")
        await client.aclose()

        assert list(directory) == ["41"]
        assert directory["41"]["email"] == "jasna@hamdaz.com"  # normalised for matching
