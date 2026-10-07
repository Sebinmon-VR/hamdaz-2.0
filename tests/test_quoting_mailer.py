"""The approval email's body. No database and no mail: ``_body`` is a function
of the quote, and the bugs worth catching are in how it is put together."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.comparison import supplier_details
from app.quoting import mailer


def quote_with_a_chosen_supplier() -> SimpleNamespace:
    chosen = uuid.uuid4()
    return SimpleNamespace(
        created_by=SimpleNamespace(display_name="Fasna Sherin"),
        customer_name="ADNOC",
        title="RFQ 6000152157",
        items=[object()],
        total=Decimal("15124"),
        currency="AED",
        selected_supplier_quote_id=chosen,
        comparison=SimpleNamespace(
            analysis={
                "suppliers": [{"quote_id": str(chosen), "supplier_name": "PAT-Kruger systems ME"}]
            }
        ),
        win_probability=None,
        win_basis=None,
        cf_bcd=None,
        revision=1,
    )


def test_a_quote_with_a_chosen_supplier_and_its_details_still_notifies() -> None:
    """The name for "Priced from" once overwrote the (name, details) pair the
    supplier block unpacks, and every such approval mail failed with "too many
    values to unpack" — the quote waited and nobody was told."""
    details = supplier_details.stored({"address": "South Zone 2, Jebel Ali"})
    body = mailer._body(
        quote_with_a_chosen_supplier(),
        "https://x/quote-requests/1",
        report=None,
        emails=[],
        supplier=("PAT-Kruger systems ME", details),
    )
    assert "Priced from" in body
    assert "Supplier — PAT-Kruger systems ME" in body
