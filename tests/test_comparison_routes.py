"""The quote comparison HTTP surface.

Two things carry the weight here.

**The gate.** These are live bid prices from competing suppliers. The module is
granted to presales, not held by everyone, and the tests attack that from the
outside rather than trusting the dependency is wired up.

**The manual path without an API key.** An engineer with no Anthropic credit
must still be able to type quotes into a form and get a real comparison. If that
breaks, the module is unusable exactly when somebody is evaluating it.
"""

from __future__ import annotations

import io

import pytest

from app.access import service as access
from app.auth.api_key import HEADER
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.comparison.extraction import ExtractionError, QuoteExtractor
from app.core.config import get_settings
from app.core.security import sign
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/comparisons"

#: As a supplier actually writes it: a time, and what it is conditional on.
LONG_DELIVERY = (
    "2 Weeks - In Stock (Subject to stock availability at time of order and to "
    "export licence restrictions from Huawei side, confirmed at PO)"
)

ALPHA = {
    "supplier_name": "Alpha Trading",
    "currency": "AED",
    "delivery_time": "4 weeks",
    "validity": "30 days",
    "items": [
        {"description": "Filter element FX-200", "part_number": "FX-200",
         "quantity": 10, "unit_price": 120},
        {"description": "Seal kit", "part_number": "SK-9", "quantity": 4, "unit_price": 85},
    ],
}
BETA = {
    "supplier_name": "Beta Supplies",
    "currency": "AED",
    "delivery_time": "8 weeks",
    "items": [
        {"description": "FX200 Filter Elm.", "part_number": "FX-200",
         "quantity": 10, "unit_price": 105},
    ],
}


class StubExtractor(QuoteExtractor):
    """A QuoteExtractor with no credential, so matching falls back to part numbers.

    Subclassed rather than mocked: the fallback path is the one an engineer
    without an API key actually runs, and it should be exercised as written.
    """

    def __init__(self) -> None:
        settings = get_settings().model_copy()
        settings.anthropic_api_key = ""
        super().__init__(settings)
        self.read_calls: list[str] = []
        self.result = None

    async def read_all(self, readables):
        self.read_calls = [r.file_name for r in readables]
        if self.result is None:
            return [ExtractionError(f"no key for {r.file_name}") for r in readables]
        return [self.result for _ in readables]


@pytest.fixture
def extractor(client):
    stub = StubExtractor()
    client._transport.app.state.quote_extractor = stub
    return stub


async def _user(db, email="sebin@hamdaz.com"):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
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


@pytest.fixture
async def presales(db):
    """A user on a team that holds the module."""
    await roles.seed_system_roles(db)
    await access.seed_modules(db)
    team = await teams.create_team(db, name="Presales")
    await access.grant_module(db, team=team, module_key="quote_comparison")
    user = await _user(db)
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


@pytest.fixture
async def outsider(db, presales):
    """Signed in, but on no team that holds the module."""
    return await _user(db, "nobody@hamdaz.com")


# ── the gate ───────────────────────────────────────────────────────────


async def test_a_stranger_is_turned_away(client, extractor) -> None:
    assert (await client.post(f"{API}/analyse", json={"quotes": [ALPHA]})).status_code == 401


async def test_a_user_without_the_module_is_refused(client, db, outsider, extractor) -> None:
    """Bid prices are not company-wide reading."""
    response = await _as(client, outsider).post(f"{API}/analyse", json={"quotes": [ALPHA]})

    assert response.status_code == 403
    assert "Quote Comparison" in response.json()["detail"]


# ── the manual path, with no API key ───────────────────────────────────


async def test_typed_quotes_compare_without_any_api_key(client, db, presales, extractor) -> None:
    """The form path. No upload, no Anthropic credential, still a real answer."""
    response = await _as(client, presales).post(
        f"{API}/analyse", json={"currency": "AED", "quotes": [ALPHA, BETA]}
    )

    assert response.status_code == 200
    analysis = response.json()["analysis"]
    assert analysis["supplier_count"] == 2
    # Matched on part number, so both suppliers' FX-200 is one line.
    assert analysis["item_count"] == 2
    assert analysis["groups"][0]["best"]["supplier_name"] == "Beta Supplies"


async def test_the_cheaper_supplier_who_missed_a_line_does_not_win(
    client, db, presales, extractor
) -> None:
    """Beta's total is lower only because they left the seal kit out."""
    response = await _as(client, presales).post(
        f"{API}/analyse", json={"quotes": [ALPHA, BETA]}
    )
    analysis = response.json()["analysis"]

    assert analysis["cheapest_supplier"]["supplier_name"] == "Alpha Trading"
    assert analysis["all_suppliers_complete"] is False
    assert any(i["kind"] == "incomplete" for i in analysis["insights"])


async def test_an_empty_comparison_is_refused(client, db, presales, extractor) -> None:
    response = await _as(client, presales).post(f"{API}/analyse", json={"quotes": []})
    assert response.status_code == 422


# ── uploading ──────────────────────────────────────────────────────────


async def test_an_unreadable_file_is_reported_not_raised(client, db, presales, extractor) -> None:
    response = await _as(client, presales).post(
        f"{API}/extract",
        files={"files": ("quote.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["quotes"] == []
    assert body["failed"][0]["file_name"] == "quote.zip"
    assert "PDF" in body["failed"][0]["error"]
    # Never reached the model, so nothing was billed for a file we cannot read.
    assert extractor.read_calls == []


async def test_a_missing_api_key_fails_the_document_not_the_module(
    client, db, presales, extractor
) -> None:
    """Extraction needs credit; the rest of the module must not."""
    response = await _as(client, presales).post(
        f"{API}/extract",
        files={"files": ("quote.csv", io.BytesIO(b"Item,Qty,Price\nFilter,10,120\n"), "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["failed"][0]["file_name"] == "quote.csv"


async def test_a_long_delivery_time_survives_the_document_it_came_from(
    client, db, presales, extractor
) -> None:
    """Terms are free text, and the condition on them is the point.

    A real quote says how long delivery takes *and what that depends on*. Held
    to a length, that sentence either loses its condition or fails the upload —
    which it did, on a document that had been read perfectly well.
    """
    from app.comparison.extraction import ExtractedItem, ExtractedQuote

    extractor.result = ExtractedQuote(
        supplier_name="Alpha Trading",
        quote_number="Q-1",
        quote_date="2026-09-01",
        currency="AED",
        validity="30 days",
        delivery_time=LONG_DELIVERY,
        payment_terms="30 days",
        warranty="1 year",
        # A code, not prose. Trimmed to fit rather than failing the upload.
        incoterms="DDP " + "x" * 200,
        contact="sales@example.com",
        discount=0,
        freight=0,
        tax=0,
        quoted_total=1200,
        items=[
            ExtractedItem(
                description="Filter element FX-200",
                part_number="FX-200",
                brand="Fortinet",
                unit="each",
                quantity=10,
                unit_price=120,
                line_total=1200,
                lead_time=LONG_DELIVERY,
            )
        ],
        note="",
    )

    response = await _as(client, presales).post(
        f"{API}/extract",
        files={"files": ("quote.csv", io.BytesIO(b"Item,Qty,Price"), "text/csv")},
    )

    assert response.status_code == 200, response.text
    quote = response.json()["quotes"][0]
    assert quote["delivery_time"] == LONG_DELIVERY
    assert quote["items"][0]["lead_time"] == LONG_DELIVERY
    assert len(quote["incoterms"]) == 60


async def test_uploading_nothing_says_so(client, db, presales, extractor) -> None:
    response = await _as(client, presales).post(f"{API}/extract", files={})
    assert response.status_code in (400, 422)


# ── saving ─────────────────────────────────────────────────────────────


async def test_a_saved_comparison_carries_its_analysis(client, db, presales, extractor) -> None:
    response = await _as(client, presales).post(
        API, json={"title": "Filters — Aug", "reference": "RFQ-1042", "quotes": [ALPHA, BETA]}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "saved"
    assert body["analysis"]["cheapest_supplier"]["supplier_name"] == "Alpha Trading"
    assert len(body["quotes"]) == 2
    assert len(body["quotes"][0]["items"]) == 2


async def test_the_stored_analysis_is_computed_not_accepted(
    client, db, presales, extractor
) -> None:
    """A caller cannot post totals that do not follow from their own line items."""
    lying = {**ALPHA, "quoted_total": 1}
    response = await _as(client, presales).post(
        API, json={"title": "Attempt", "quotes": [lying]}
    )

    analysis = response.json()["analysis"]
    assert analysis["suppliers"][0]["total"] == 10 * 120 + 4 * 85
    # The bogus figure is kept and reported as a discrepancy, not adopted.
    assert analysis["suppliers"][0]["total_mismatch"] is not None


async def test_a_saved_comparison_appears_in_the_list(client, db, presales, extractor) -> None:
    signed = _as(client, presales)
    await signed.post(API, json={"title": "Filters — Aug", "quotes": [ALPHA, BETA]})

    rows = (await signed.get(API)).json()
    assert rows[0]["title"] == "Filters — Aug"
    assert rows[0]["supplier_count"] == 2
    assert rows[0]["best_supplier"] == "Alpha Trading"


async def test_one_comparison_can_be_read_back(client, db, presales, extractor) -> None:
    signed = _as(client, presales)
    created = (await signed.post(API, json={"title": "Filters", "quotes": [ALPHA]})).json()

    body = (await signed.get(f"{API}/{created['id']}")).json()
    assert body["title"] == "Filters"
    assert body["analysis"]["item_count"] == 2


async def test_an_unknown_comparison_is_a_404(client, db, presales, extractor) -> None:
    response = await _as(client, presales).get(
        f"{API}/00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404


# ── deleting ───────────────────────────────────────────────────────────


async def test_only_the_author_may_delete(client, db, presales, extractor) -> None:
    signed = _as(client, presales)
    created = (await signed.post(API, json={"title": "Mine", "quotes": [ALPHA]})).json()

    # A second presales member, on the same team.
    other = await _user(db, "other@hamdaz.com")
    team = await teams.get_team(db, "presales")
    await teams.set_member_roles(db, team=team, user=other, role_keys=["member"])
    await db.commit()

    assert (await _as(client, other).delete(f"{API}/{created['id']}")).status_code == 400
    assert (await _as(client, presales).delete(f"{API}/{created['id']}")).status_code == 204


# ── machine callers ────────────────────────────────────────────────────


async def test_an_api_key_can_read_the_list(client, db, presales, extractor) -> None:
    await _as(client, presales).post(API, json={"title": "Filters", "quotes": [ALPHA]})
    client.cookies.clear()

    response = await client.get(API, headers={HEADER: get_settings().hamdaz_api_key})

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Filters"


async def test_a_wrong_api_key_is_refused(client, extractor) -> None:
    client.cookies.clear()
    assert (await client.get(API, headers={HEADER: "hmdz_wrong"})).status_code == 401


async def test_an_api_key_cannot_write(client, db, presales, extractor) -> None:
    """Reading is machine work; creating a record that names an author is not."""
    client.cookies.clear()
    response = await client.post(
        API,
        json={"title": "Machine made", "quotes": [ALPHA]},
        headers={HEADER: get_settings().hamdaz_api_key},
    )
    assert response.status_code == 401
