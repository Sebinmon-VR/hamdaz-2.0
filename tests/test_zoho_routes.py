"""The quotes HTTP surface.

Quotes are open to every signed-in user, so there is only one gate to test — but
it is worth testing, because "open to everyone" and "open to anyone" differ by a
cookie.

The rest of the weight is on failure translation. Zoho being rate-limited and
Zoho being broken need different status codes, or a client cannot tell a wait
from an outage; and a related-record branch failing must not take the others
with it.
"""

from __future__ import annotations

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.zoho.cache import QuoteCache
from app.zoho.client import ZohoError, ZohoRateLimitError

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/quotes"


def _estimate(number: str = "QT-001645", **over) -> dict:
    row = {
        "estimate_id": f"id-{number}",
        "estimate_number": number,
        "status": "draft",
        "customer_id": "cust-1",
        "customer_name": "ADNOC",
        "date": "2026-08-30",
        "total": 27106.8,
        "currency_code": "AED",
        "cf_bcd": "31 Aug 2026",
        "line_items": [{"item_id": "i1", "name": "Filter", "item_total": 27106.8}],
        "salesorders": [
            {"salesorder_id": "so1", "salesorder_number": "SO-00288", "total": 691.95}
        ],
        "invoice_ids": [],
        "documents": [
            {
                "document_id": "doc-1",
                "file_name": "HAMDZ-19870.pdf",
                "file_size": "635180",
                "file_size_formatted": "620.3 KB",
                "uploaded_by": "Proposal Team",
            }
        ],
    }
    row.update(over)
    return row


class StubZoho:
    def __init__(self) -> None:
        self.rows = [_estimate("QT-001645"), _estimate("QT-001643", status="invoiced")]
        self.error: Exception | None = None
        #: Per-method failures, for proving one branch fails alone.
        self.branch_errors: dict[str, Exception] = {}
        self.item_calls: list[str] = []
        self.document_calls: list[tuple[str, str]] = []
        self.last_filters: dict = {}

    def _maybe_raise(self, name: str) -> None:
        if self.error:
            raise self.error
        if name in self.branch_errors:
            raise self.branch_errors[name]

    async def estimates(self, **filters):
        self._maybe_raise("estimates")
        self.last_filters = filters
        number = filters.get("estimate_number")
        rows = [r for r in self.rows if not number or r["estimate_number"] == number]
        return rows[: filters.get("limit") or len(rows)]

    async def estimate(self, estimate_id: str):
        self._maybe_raise("estimate")
        found = next((r for r in self.rows if r["estimate_id"] == estimate_id), None)
        if found is None:
            raise ZohoError("No such quote in Zoho Books")
        return found

    async def contact(self, contact_id: str):
        self._maybe_raise("contact")
        return {"contact_id": contact_id, "contact_name": "ADNOC", "email": "p@adnoc.ae"}

    async def item(self, item_id: str):
        self._maybe_raise("item")
        self.item_calls.append(item_id)
        return {"item_id": item_id, "name": "Filter", "rate": 12.5}

    async def comments(self, estimate_id: str):
        self._maybe_raise("comments")
        return [{"comment_id": "c1", "description": "Sent to client"}]

    async def invoice(self, invoice_id: str):
        self._maybe_raise("invoice")
        raise ZohoError("Zoho Books returned 403: no permission")

    async def document(self, estimate_id: str, document_id: str):
        self._maybe_raise("document")
        self.document_calls.append((estimate_id, document_id))
        return b"%PDF-1.5 pretend", "application/pdf"


@pytest.fixture
def zoho(client):
    stub = StubZoho()
    client._transport.app.state.zoho = stub
    # Lifespan does not run under the test transport, so the cache is wired here.
    client._transport.app.state.quote_cache = QuoteCache()
    return stub


async def _user(db, email: str = "sebin@hamdaz.com"):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name="Sebin")
    )
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


# ── the gate ───────────────────────────────────────────────────────────


async def test_a_stranger_gets_nothing(client, zoho) -> None:
    assert (await client.get(API)).status_code == 401


async def test_any_signed_in_user_may_read_quotes(client, db, zoho) -> None:
    """No team grant and no role — a login is the whole requirement."""
    response = await _as(client, await _user(db)).get(API)

    assert response.status_code == 200
    assert response.json()["total"] == 2


# ── listing ────────────────────────────────────────────────────────────


async def test_the_list_speaks_our_vocabulary_not_zohos(client, db, zoho) -> None:
    response = await _as(client, await _user(db)).get(API)
    first = response.json()["quotes"][0]

    assert first["number"] == "QT-001645"
    assert first["bcd"] == "31 Aug 2026"
    # Zoho's own field names must not leak through.
    assert "estimate_number" not in first and "cf_bcd" not in first


async def test_a_status_filter_reaches_zoho(client, db, zoho) -> None:
    await _as(client, await _user(db)).get(API, params={"status": "invoiced"})
    assert zoho.last_filters["status"] == "invoiced"


async def test_an_unknown_status_is_refused_before_the_call(client, db, zoho) -> None:
    """A typo should say so, not come back as an opaque 502 from Zoho."""
    response = await _as(client, await _user(db)).get(API, params={"status": "pending"})

    assert response.status_code == 400
    assert "draft" in response.json()["detail"]
    assert zoho.last_filters == {}


async def test_the_second_call_is_served_from_cache(client, db, zoho) -> None:
    signed = _as(client, await _user(db))
    await signed.get(API)
    zoho.rows = []  # if Zoho is consulted again, the count will drop

    assert (await signed.get(API)).json()["total"] == 2


async def test_refresh_bypasses_the_cache(client, db, zoho) -> None:
    signed = _as(client, await _user(db))
    await signed.get(API)
    zoho.rows = []

    assert (await signed.get(API, params={"refresh": True})).json()["total"] == 0


# ── one quote ──────────────────────────────────────────────────────────


async def test_a_quote_carries_its_line_items_and_sales_orders(client, db, zoho) -> None:
    response = await _as(client, await _user(db)).get(f"{API}/id-QT-001645")
    body = response.json()

    assert body["line_items"][0]["name"] == "Filter"
    # Embedded by Zoho, so this costs no extra call.
    assert body["salesorders"][0]["number"] == "SO-00288"


async def test_an_unknown_quote_is_a_502_not_a_crash(client, db, zoho) -> None:
    response = await _as(client, await _user(db)).get(f"{API}/nope")
    assert response.status_code == 502


# ── the bridge from SharePoint ─────────────────────────────────────────


async def test_a_quote_number_from_sharepoint_resolves(client, db, zoho) -> None:
    """The proposals list stores a bare quote_no; this turns it into the quote."""
    response = await _as(client, await _user(db)).get(f"{API}/by-number/QT-001643")

    assert response.status_code == 200
    assert response.json()["number"] == "QT-001643"


async def test_an_unknown_quote_number_is_a_404(client, db, zoho) -> None:
    response = await _as(client, await _user(db)).get(f"{API}/by-number/QT-999999")

    assert response.status_code == 404
    assert "QT-999999" in response.json()["detail"]


async def test_by_number_is_not_swallowed_as_an_id(client, db, zoho) -> None:
    """Route order matters: /by-number/x must not match /{quote_id}."""
    response = await _as(client, await _user(db)).get(f"{API}/by-number/QT-001645")
    assert response.json()["number"] == "QT-001645"


# ── related records ────────────────────────────────────────────────────


async def test_related_returns_every_branch_by_default(client, db, zoho) -> None:
    body = (await _as(client, await _user(db)).get(f"{API}/id-QT-001645/related")).json()

    assert body["included"] == ["customer", "items", "salesorders", "invoices", "comments"]
    assert body["customer"]["ok"] is True
    assert body["customer"]["data"]["name"] == "ADNOC"
    assert body["salesorders"]["data"][0]["number"] == "SO-00288"


async def test_include_limits_the_fan_out(client, db, zoho) -> None:
    """Each branch costs upstream calls, so nothing unasked-for is fetched."""
    body = (
        await _as(client, await _user(db)).get(
            f"{API}/id-QT-001645/related", params={"include": "customer"}
        )
    ).json()

    assert body["included"] == ["customer"]
    assert body["comments"] is None
    assert zoho.item_calls == []


async def test_an_unknown_include_name_is_ignored_not_rejected(client, db, zoho) -> None:
    body = (
        await _as(client, await _user(db)).get(
            f"{API}/id-QT-001645/related", params={"include": "customer,unicorns"}
        )
    ).json()

    assert body["included"] == ["customer"]


async def test_repeated_items_are_fetched_once(client, db, zoho) -> None:
    zoho.rows[0]["line_items"] = [
        {"item_id": "i1", "name": "Filter", "item_total": 1},
        {"item_id": "i1", "name": "Filter", "item_total": 1},
        {"item_id": "i2", "name": "Seal", "item_total": 1},
    ]
    await _as(client, await _user(db)).get(
        f"{API}/id-QT-001645/related", params={"include": "items"}
    )

    assert sorted(zoho.item_calls) == ["i1", "i2"]


async def test_one_failing_branch_does_not_blank_the_others(client, db, zoho) -> None:
    """The live case: this token cannot read invoices, but the quote still shows."""
    zoho.branch_errors["contact"] = ZohoError("customer has been deleted")

    body = (await _as(client, await _user(db)).get(f"{API}/id-QT-001645/related")).json()

    assert body["customer"]["ok"] is False
    assert "deleted" in body["customer"]["error"]
    # Everything else survived.
    assert body["comments"]["ok"] is True
    assert body["salesorders"]["ok"] is True


async def test_unreadable_invoices_report_what_is_known(client, db, zoho) -> None:
    """Ids and totals come off the quote itself even when the invoice is 403."""
    zoho.rows[0]["invoice_ids"] = ["inv-1"]
    zoho.rows[0]["invoiced_amount"] = 500.0

    body = (
        await _as(client, await _user(db)).get(
            f"{API}/id-QT-001645/related", params={"include": "invoices"}
        )
    ).json()
    data = body["invoices"]["data"]

    assert body["invoices"]["ok"] is True
    assert data["invoice_ids"] == ["inv-1"]
    assert data["invoiced_amount"] == 500.0
    assert data["unreadable"] == 1
    assert "ZohoBooks.invoices.READ" in data["reason"]


# ── telling failures apart ─────────────────────────────────────────────


async def test_a_rate_limit_is_a_503_with_retry_after(client, db, zoho) -> None:
    """Temporary, and worth retrying — which a 502 would not tell the caller."""
    zoho.error = ZohoRateLimitError("slow down", retry_after=30)

    response = await _as(client, await _user(db)).get(API)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


async def test_zoho_being_broken_is_a_502(client, db, zoho) -> None:
    zoho.error = ZohoError("Zoho Books returned 500")

    assert (await _as(client, await _user(db)).get(API)).status_code == 502


async def test_an_unconfigured_zoho_explains_itself(client, db, zoho) -> None:
    zoho.error = ZohoError(
        "Zoho Books is not configured: set zoho_CLIENT_ID, zoho_CLIENT_SECRET, "
        "zoho_REFRESH_TOKEN and zoho_ORGANIZATION_ID."
    )

    response = await _as(client, await _user(db)).get(API)

    assert response.status_code == 502
    assert "zoho_CLIENT_ID" in response.json()["detail"]


async def test_every_quote_carries_a_link_into_zoho_books(client, db, zoho) -> None:
    """So a person reading the list can open the record where it is edited."""
    body = (await _as(client, await _user(db)).get(API)).json()
    link = body["quotes"][0]["web_url"]

    assert link.startswith("https://books.zoho.com/app/")
    assert link.endswith("#/quotes/id-QT-001645")


# ── attachments ────────────────────────────────────────────────────────


async def test_documents_carry_a_usable_download_link(client, db, zoho) -> None:
    zoho.rows[0]["documents"] = [
        {
            "document_id": "doc-1",
            "file_name": "HAMDZ-19870.pdf",
            "file_size": "635180",
            "file_size_formatted": "620.3 KB",
            "uploaded_by": "Proposal Team",
        }
    ]
    body = (await _as(client, await _user(db)).get(f"{API}/id-QT-001645")).json()
    doc = body["documents"][0]

    assert doc["download_url"] == "/api/v1/quotes/id-QT-001645/documents/doc-1"
    # Zoho sends the size as a string of digits; it must arrive as a number.
    assert doc["file_size"] == 635180


async def test_an_attachment_is_relayed_with_its_type_and_name(client, db, zoho) -> None:
    response = await _as(client, await _user(db)).get(
        f"{API}/id-QT-001645/documents/doc-1"
    )

    assert response.status_code == 200
    assert response.content == b"%PDF-1.5 pretend"
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="HAMDZ-19870.pdf"' in response.headers["content-disposition"]


async def test_an_attachment_needs_a_session(client, zoho) -> None:
    assert (
        await client.get(f"{API}/id-QT-001645/documents/doc-1")
    ).status_code == 401


async def test_a_document_from_another_quote_is_not_served(client, db, zoho) -> None:
    """The id must belong to this quote, or anyone could walk the org's files."""
    response = await _as(client, await _user(db)).get(
        f"{API}/id-QT-001645/documents/doc-belonging-to-QT-001643"
    )

    assert response.status_code == 404
    assert zoho.document_calls == []


async def test_a_filename_cannot_forge_response_headers(client, db, zoho) -> None:
    """The name is whatever somebody uploaded to Zoho, so it is not trusted."""
    zoho.rows[0]["documents"] = [
        {"document_id": "doc-1", "file_name": 'evil";\r\nX-Injected: yes'}
    ]
    response = await _as(client, await _user(db)).get(
        f"{API}/id-QT-001645/documents/doc-1"
    )

    assert "x-injected" not in response.headers
    assert response.headers["content-disposition"] == 'inline; filename="evil;X-Injected: yes"'
