"""The selling & costing report over HTTP, and a supplier quote typed in.

The arithmetic is covered in test_quoting_report.py against the report
module. What is tested here is the surface: that the report serialises from a
quote read back inside the same async session (the trap every route in this
module has fallen into once), that the PDF comes back as a file, and that a
typed-in supplier quote reaches the comparison exactly as an uploaded one
does.

Zoho is stubbed with a currency table, so a USD quote gets its AED column.
"""

from __future__ import annotations

from decimal import Decimal

from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    FORM,
    _as,
    quoting,
    requester,
    team,
)

USD_FORM = {
    **FORM,
    "currency": "USD",
    "items": [
        {"name": "HPE 2.4TB SAS HDD", "quantity": 1, "rate": "2125.70", "cost_rate": "637"},
    ],
    "tax_name": "VAT",
    "tax_percentage": "5",
    "cost_lines": [
        {"stage": "origin", "label": "Freight", "amount_base": "60"},
        {"stage": "origin", "label": "Insurance", "percent": "1", "percent_of": "goods"},
    ],
    "customs_duty_percent": "5",
    "supplier_name": "router-switch.com",
    "supplier_basis": "online purchase",
}


async def test_the_report_reads_back_with_both_currencies(quoting, db, requester, team) -> None:
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=USD_FORM)).json()

    response = await quoting.get(f"{API}/{created['id']}/report")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currency"] == "USD"
    assert body["base_currency"] == "AED"
    # StubZoho's table: 1 USD = 3.672501 AED.
    assert Decimal(body["base_rate"]) == Decimal("3.672501")
    assert body["supplier"]["name"] == "router-switch.com"
    assert body["quoted_price"]["amount"] == "2125.70"
    assert Decimal(body["quoted_price"]["base"]) == (
        Decimal("2125.70") * Decimal("3.672501")
    ).quantize(Decimal("0.01"))
    labels = [row["label"] for row in body["cost_rows"]]
    assert "Insurance (1%)" in labels and "Import duty" in labels
    assert body["negotiation"][0]["status"] == "comfortable"
    assert body["recommendation"].startswith("Counter at")


async def test_the_report_downloads_as_a_pdf(quoting, db, requester, team) -> None:
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=USD_FORM)).json()

    response = await quoting.get(f"{API}/{created['id']}/report.pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert "Selling_and_Costing_Report.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


async def test_a_typed_supplier_quote_is_compared_like_an_uploaded_one(
    quoting, db, requester, team
) -> None:
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes/typed",
        json={
            "quotes": [
                {
                    "supplier_name": "Phone quote from Gamma",
                    "currency": "AED",
                    "items": [
                        {"description": "FortiGate 201G", "part_number": "FG-201G",
                         "quantity": "2", "unit_price": "10500"},
                    ],
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["comparison_id"]
    names = [s["supplier_name"] for s in body["comparison"]["suppliers"]]
    assert names == ["Phone quote from Gamma"]
    assert body["comparison"]["suppliers"][0]["total"] == 21000
    # And it can be priced from, exactly like an uploaded one.
    chosen = await quoting.post(
        f"{API}/{created['id']}/select-supplier",
        json={"supplier_quote_id": body["comparison"]["suppliers"][0]["quote_id"],
              "markup_percent": 20},
    )
    assert chosen.status_code == 200, chosen.text
    assert Decimal(chosen.json()["items"][0]["cost_rate"]) == Decimal(10500)


async def test_a_typed_quote_with_no_lines_is_refused(quoting, db, requester, team) -> None:
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes/typed",
        json={"quotes": [{"supplier_name": "Nobody", "currency": "AED", "items": []}]},
    )

    assert response.status_code == 400
    assert "no priced lines" in response.json()["detail"]
