"""A rate typed on the chosen supplier's offer re-costs the quote.

The case that was failing: a dirham quote priced from a sterling offer at
Zoho's 4.93; the rate typed on the offer as 4.86 changed the comparison and
nothing else, so the goods stayed costed at 4.93. No database here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.comparison import SupplierQuoteItem
from app.models.quoting import CostStage, QuoteCostLine, QuoteRequestItem
from app.quoting import service
from tests.test_quoting_costing import request_with, supplier


def priced_from_sterling():
    offer = supplier(
        id=uuid.uuid4(), supplier_name="IC INTERNATIONAL LIMITED", currency="GBP",
        fx_rate=Decimal("4.93"),
    )
    offer.items = [SupplierQuoteItem(position=0, description="Fire blanket",
                                     quantity=Decimal(2), unit_price=Decimal("370.44"))]
    request = request_with(currency="AED", target_markup_percent=Decimal(20))
    request.selected_supplier_quote_id = offer.id
    request.items = [
        QuoteRequestItem(position=0, name="Fire blanket", quantity=Decimal(2),
                         cost_rate=Decimal("1826.27"), rate=Decimal("2357.48"),
                         discount=Decimal(0), source_supplier_quote_id=offer.id),
    ]
    request.cost_lines = [
        QuoteCostLine(position=1, stage=CostStage.ORIGIN,
                      label="Packing – IC INTERNATIONAL LIMITED", basis="Supplier quotation",
                      amount_base=Decimal("9.86"), is_principal=False, is_firm=True),
    ]
    return request, offer


def test_a_new_rate_on_the_chosen_offer_recosts_its_lines() -> None:
    request, offer = priced_from_sterling()
    offer.fx_rate = Decimal("4.86")

    assert service.restate_at_offer_rate(request, offer, was=Decimal("4.93"), now=Decimal("4.86"))

    assert request.items[0].cost_rate == Decimal("1800.34")  # 370.44 x 4.86
    assert request.cost_lines[0].amount_base == Decimal("9.72")  # 2.00 x 4.86
    # Repriced at the quote's margin over the new landed cost.
    assert request.items[0].rate < Decimal("2357.48")


def test_the_rate_the_lines_were_costed_at_is_read_off_them() -> None:
    # 4.85 was saved on the offer before the fix was live, so the offer says
    # 4.85 while the lines are still at 4.93. Saving 4.85 again must move them.
    request, offer = priced_from_sterling()
    offer.fx_rate = Decimal("4.85")

    assert service.restate_at_offer_rate(request, offer, was=Decimal("4.85"), now=Decimal("4.85"))

    assert request.items[0].cost_rate == Decimal("1796.63")  # 370.44 x 4.85
    assert request.cost_lines[0].amount_base == Decimal("9.70")  # 9.86 x 4.85 / 4.93


def test_nothing_moves_without_a_new_rate_or_for_another_offer() -> None:
    request, offer = priced_from_sterling()
    assert not service.restate_at_offer_rate(
        request, offer, was=Decimal("4.93"), now=Decimal("4.93")
    )
    request.selected_supplier_quote_id = uuid.uuid4()
    assert not service.restate_at_offer_rate(
        request, offer, was=Decimal("4.93"), now=Decimal("4.86")
    )
    assert request.items[0].cost_rate == Decimal("1826.27")
