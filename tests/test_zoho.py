"""The Zoho client, and the token sharing that keeps it inside Zoho's limits.

The token tests carry the most weight here. Zoho allows only ten active access
tokens per refresh token and ten token requests per ten minutes, and exceeding
either fails *silently* — the oldest token is invalidated and some other process
starts getting 401s. So "did we refresh more than once" is a correctness
question, not an efficiency one, and it is asserted directly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.zoho import ZohoToken
from app.zoho.client import ZohoBooks, ZohoError, ZohoRateLimitError
from app.zoho.schemas import QuoteDetailOut, QuoteOut
from app.zoho.tokens import TokenStore, ZohoAuthError

API = "https://www.zohoapis.com"


@pytest.fixture
def zoho_settings() -> Settings:
    s = get_settings().model_copy()
    s.zoho_client_id = "cid"
    s.zoho_client_secret = "secret"
    s.zoho_refresh_token = "refresh"
    s.zoho_organization_id = "855589474"
    s.zoho_accounts_url = "https://accounts.zoho.com"
    return s


class Upstream:
    """A mock Zoho. Counts token requests, because that is the thing under test."""

    def __init__(self) -> None:
        self.token_calls = 0
        self.api_calls: list[httpx.Request] = []
        self.token_response: dict | None = None
        self.token_status = 200
        self.handlers: dict[str, tuple[int, dict]] = {}
        self.delay = 0.0

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth/v2/token"):
                self.token_calls += 1
                if self.delay:
                    await asyncio.sleep(self.delay)
                body = self.token_response or {
                    "access_token": f"tok-{self.token_calls}",
                    "api_domain": API,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
                return httpx.Response(self.token_status, json=body)

            self.api_calls.append(request)
            status, body = self.handlers.get(request.url.path, (200, {"code": 0}))
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handle)


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
async def http(upstream: Upstream):
    async with httpx.AsyncClient(transport=upstream.transport()) as c:
        yield c


@pytest.fixture
async def clean_token(session_factory):
    async with session_factory() as s:
        for row in (await s.scalars(select(ZohoToken))).all():
            await s.delete(row)
        await s.commit()


def store(zoho_settings, http, session_factory) -> TokenStore:
    return TokenStore(zoho_settings, http, session_factory)


# ── the token, which is the whole point ────────────────────────────────


async def test_a_token_is_fetched_when_there_is_none(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    token = await store(zoho_settings, http, session_factory).get()
    assert token.value == "tok-1"
    assert token.api_domain == API
    assert upstream.token_calls == 1


async def test_the_same_process_reuses_its_token(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    s = store(zoho_settings, http, session_factory)
    first, second = await s.get(), await s.get()
    assert first.value == second.value
    assert upstream.token_calls == 1


async def test_a_cold_process_reuses_the_stored_token(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """The restart case: new process, empty memory, token already in Postgres."""
    first = await store(zoho_settings, http, session_factory).get()
    second = await store(zoho_settings, http, session_factory).get()

    assert second.value == first.value
    # The whole reason the table exists.
    assert upstream.token_calls == 1


async def test_two_instances_starting_together_refresh_once(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """Two cold processes racing an empty table must not both mint a token.

    Without the advisory lock both see "no row", both call Zoho, and two of the
    ten slots go on one startup.
    """
    upstream.delay = 0.05  # widen the window they could race in

    a = store(zoho_settings, http, session_factory)
    b = store(zoho_settings, http, session_factory)
    first, second = await asyncio.gather(a.get(), b.get())

    assert upstream.token_calls == 1
    assert first.value == second.value


async def test_racing_coroutines_in_one_process_refresh_once(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    s = store(zoho_settings, http, session_factory)
    tokens = await asyncio.gather(*(s.get() for _ in range(10)))

    assert upstream.token_calls == 1
    assert len({t.value for t in tokens}) == 1


async def test_an_expired_stored_token_is_replaced(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    await store(zoho_settings, http, session_factory).get()

    async with session_factory() as s:
        row = await s.get(ZohoToken, 1)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()

    token = await store(zoho_settings, http, session_factory).get()
    assert token.value == "tok-2"
    assert upstream.token_calls == 2


async def test_the_expiry_carries_a_safety_margin(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """Stored expiry must be short of Zoho's, so a token cannot lapse in flight."""
    token = await store(zoho_settings, http, session_factory).get()
    remaining = token.expires_at - datetime.now(UTC)
    assert timedelta(minutes=55) < remaining < timedelta(minutes=59)


async def test_api_domain_is_persisted_and_reused(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    upstream.token_response = {
        "access_token": "eu-token",
        "api_domain": "https://www.zohoapis.eu",
        "expires_in": 3600,
    }
    await store(zoho_settings, http, session_factory).get()

    # A cold process must call the same data centre without re-deriving it.
    assert (await store(zoho_settings, http, session_factory).get()).api_domain == (
        "https://www.zohoapis.eu"
    )


async def test_invalidate_expires_the_shared_row(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """A token Zoho rejected is dead for every instance, not just this one."""
    s = store(zoho_settings, http, session_factory)
    await s.get()
    await s.invalidate()

    assert (await store(zoho_settings, http, session_factory).get()).value == "tok-2"


async def test_unconfigured_zoho_says_so(http, session_factory) -> None:
    bare = get_settings().model_copy()
    bare.zoho_client_id = ""
    bare.zoho_refresh_token = ""
    with pytest.raises(ZohoAuthError, match="not configured"):
        await TokenStore(bare, http, session_factory).get()


async def test_a_wrong_data_centre_names_the_alternatives(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """invalid_client almost always means the wrong accounts host, so say that."""
    upstream.token_response = {"error": "invalid_client"}
    with pytest.raises(ZohoAuthError) as caught:
        await store(zoho_settings, http, session_factory).get()

    message = str(caught.value)
    assert "accounts.zoho.eu" in message and "accounts.zoho.sa" in message


async def test_a_revoked_refresh_token_says_what_to_do(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    upstream.token_response = {"error": "invalid_grant"}
    with pytest.raises(ZohoAuthError, match="zoho_REFRESH_TOKEN"):
        await store(zoho_settings, http, session_factory).get()


# ── the client ─────────────────────────────────────────────────────────


def books(zoho_settings, http, session_factory) -> ZohoBooks:
    return ZohoBooks(zoho_settings, http, session_factory)


async def test_the_organisation_id_rides_on_every_call(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    upstream.handlers["/books/v3/estimates"] = (
        200,
        {"code": 0, "estimates": [], "page_context": {"has_more_page": False}},
    )
    await books(zoho_settings, http, session_factory).estimates()

    assert upstream.api_calls[0].url.params["organization_id"] == "855589474"


async def test_the_auth_header_is_zohos_own_scheme(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """Zoho-oauthtoken, not Bearer. Bearer is silently a 401."""
    upstream.handlers["/books/v3/estimates"] = (
        200,
        {"code": 0, "estimates": [], "page_context": {"has_more_page": False}},
    )
    await books(zoho_settings, http, session_factory).estimates()

    assert upstream.api_calls[0].headers["Authorization"] == "Zoho-oauthtoken tok-1"


async def test_a_lowercase_status_is_capitalised_for_zoho(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """filter_by=Status.invoiced is a 400; Status.Invoiced is not."""
    upstream.handlers["/books/v3/estimates"] = (
        200,
        {"code": 0, "estimates": [], "page_context": {"has_more_page": False}},
    )
    await books(zoho_settings, http, session_factory).estimates(status="invoiced")

    assert upstream.api_calls[0].url.params["filter_by"] == "Status.Invoiced"


async def test_a_non_zero_code_in_a_200_is_an_error(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    """Zoho reports failures inside a 200 body. Trusting the status hides them."""
    upstream.handlers["/books/v3/estimates/9"] = (
        200,
        {"code": 1002, "message": "Invalid value passed for estimate_id"},
    )
    with pytest.raises(ZohoError, match="1002"):
        await books(zoho_settings, http, session_factory).estimate("9")


async def test_a_rate_limit_is_told_apart_from_a_failure(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    upstream.handlers["/books/v3/estimates/9"] = (429, {"code": 0})
    with pytest.raises(ZohoRateLimitError):
        await books(zoho_settings, http, session_factory).estimate("9")


async def test_a_401_clears_the_shared_token(
    zoho_settings, http, session_factory, upstream, clean_token
) -> None:
    upstream.handlers["/books/v3/estimates/9"] = (401, {"code": 0})
    z = books(zoho_settings, http, session_factory)

    with pytest.raises(ZohoError, match="rejected"):
        await z.estimate("9")

    async with session_factory() as s:
        row = await s.get(ZohoToken, 1)
        assert row.expires_at < datetime.now(UTC)


async def test_paging_follows_has_more_page(
    zoho_settings, http, session_factory, upstream, clean_token, monkeypatch
) -> None:
    pages = {
        "1": {
            "code": 0,
            "estimates": [{"estimate_id": "a"}],
            "page_context": {"has_more_page": True},
        },
        "2": {
            "code": 0,
            "estimates": [{"estimate_id": "b"}],
            "page_context": {"has_more_page": False},
        },
    }

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/v2/token"):
            return httpx.Response(
                200, json={"access_token": "t", "api_domain": API, "expires_in": 3600}
            )
        return httpx.Response(200, json=pages[request.url.params["page"]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        rows = await ZohoBooks(zoho_settings, c, session_factory).estimates()

    assert [r["estimate_id"] for r in rows] == ["a", "b"]


async def test_paging_stops_at_the_limit(
    zoho_settings, session_factory, clean_token
) -> None:
    """A caller asking for 3 must not pull every page Zoho has."""
    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/v2/token"):
            return httpx.Response(
                200, json={"access_token": "t", "api_domain": API, "expires_in": 3600}
            )
        seen.append(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "estimates": [{"estimate_id": str(i)} for i in range(200)],
                "page_context": {"has_more_page": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        rows = await ZohoBooks(zoho_settings, c, session_factory).estimates(limit=3)

    assert len(rows) == 3
    assert seen == ["1"]


# ── mapping Zoho's vocabulary to ours ──────────────────────────────────


def test_a_list_row_becomes_a_quote() -> None:
    row = {
        "estimate_id": "531969400000123",
        "estimate_number": "QT-001645",
        "status": "draft",
        "customer_id": "5319694000000117043",
        "customer_name": "ADNOC",
        "date": "2026-08-30",
        "total": 27106.8,
        "currency_code": "AED",
        "cf_bcd": "31 Aug 2026",
        "cf_portal": "ADNOC",
        "reference_number": "6000148984",
    }
    quote = QuoteOut.from_zoho(row)

    assert quote.number == "QT-001645"
    assert quote.total == 27106.8
    # The join key back to the SharePoint Proposals list.
    assert quote.bcd == "31 Aug 2026"
    assert quote.portal == "ADNOC"


def test_detail_recovers_bcd_from_the_custom_field_list() -> None:
    """The detail payload drops the cf_* columns and sends a list instead."""
    detail = QuoteDetailOut.from_zoho(
        {
            "estimate_id": "1",
            "estimate_number": "QT-000001",
            "total": 100,
            "custom_fields": [
                {"api_name": "cf_bcd", "label": "BCD", "value": "31 Aug 2026"},
                {"api_name": "cf_portal", "label": "Portal", "value": "ADNOC"},
            ],
            "line_items": [{"item_id": "i1", "name": "Filter", "item_total": 100}],
            "salesorders": [
                {"salesorder_id": "so1", "salesorder_number": "SO-00288", "total": 691.95}
            ],
            "invoice_ids": ["inv1"],
        }
    )

    assert detail.bcd == "31 Aug 2026"
    assert detail.portal == "ADNOC"
    assert detail.salesorders[0].number == "SO-00288"
    assert detail.invoice_ids == ["inv1"]
    assert detail.line_items[0].name == "Filter"


def test_missing_money_does_not_crash_the_mapping() -> None:
    """Zoho omits fields on draft records rather than sending zero."""
    quote = QuoteOut.from_zoho({"estimate_id": "1", "estimate_number": "QT-1"})
    assert quote.total == 0.0
    assert quote.customer_id is None


# ── deep links into Zoho Books ─────────────────────────────────────────


def test_a_quote_links_to_its_page_in_zoho_books(zoho_settings) -> None:
    """The staff link, matching the URL Zoho's own UI produces.

    Note Zoho spells this ``#/quotes/`` even though its API says ``estimates``.
    """
    quote = QuoteOut.from_zoho(
        {"estimate_id": "5319694000004665018", "estimate_number": "QT-001645"},
        app_base=zoho_settings.zoho_app_base,
    )
    assert quote.web_url == (
        "https://books.zoho.com/app/855589474#/quotes/5319694000004665018"
    )


def test_the_link_host_follows_the_data_centre(zoho_settings) -> None:
    """books.zoho.eu for a European org — not the API host, which serves no UI."""
    eu = zoho_settings.model_copy()
    eu.zoho_accounts_url = "https://accounts.zoho.eu"

    assert eu.zoho_app_base == "https://books.zoho.eu/app/855589474"
    assert "zohoapis" not in eu.zoho_app_base


def test_no_link_is_better_than_a_broken_one() -> None:
    quote = QuoteOut.from_zoho({"estimate_id": "1", "estimate_number": "QT-1"})
    assert quote.web_url is None
