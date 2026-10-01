"""Correcting what the automatic reading got wrong: a supplier's offer, and a
filed document's notes and kind. Through the API, on the test database.
"""

from __future__ import annotations

import io

from tests.test_quoting_documents_routes import RFQ_CSV, RecordingDrive, _with_task
from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    _as,
    _priced,
    quoting,
    requester,
    team,
)


async def test_a_supplier_offer_is_corrected_in_place_and_compared_again(
    quoting, db, requester, team
) -> None:
    quote_id = await _priced(quoting, requester, team)
    body = (await _as(quoting, requester).get(f"{API}/{quote_id}")).json()
    rows = {q["supplier_name"]: q for q in body["supplier_quotes"]}
    assert set(rows) == {"Alpha Trading", "Beta Supplies"}
    beta = rows["Beta Supplies"]
    assert beta["items"] and body["selected_supplier_quote_id"] == beta["id"]

    edited = await quoting.put(
        f"{API}/{quote_id}/supplier-quotes/{beta['id']}",
        json={
            "supplier_name": "Beta Supplies LLC",
            "currency": beta["currency"],
            "fx_rate": "1",
            "payment_terms": "30 days",
            "warranty": "3 years",
            "charges": [{"kind": "handling", "label": "Handling", "amount": "150"}],
            "items": [
                {"description": "Firewall appliance", "part_number": "FW-1", "quantity": "2",
                 "unit_price": "9000"},
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    out = edited.json()
    fixed = next(q for q in out["supplier_quotes"] if q["id"] == beta["id"])
    # The same offer, corrected — still the chosen one.
    assert fixed["supplier_name"] == "Beta Supplies LLC" and fixed["warranty"] == "3 years"
    assert [i["description"] for i in fixed["items"]] == ["Firewall appliance"]
    assert fixed["charges"][0]["kind"] == "handling"
    assert out["selected_supplier_quote_id"] == beta["id"]
    # And compared again over the corrected figures.
    names = {s["supplier_name"] for s in out["comparison"]["suppliers"]}
    assert "Beta Supplies LLC" in names and "Beta Supplies" not in names

    missing = await quoting.put(
        f"{API}/{quote_id}/supplier-quotes/{beta['id']}",
        json={"supplier_name": "Beta", "currency": beta["currency"], "items": []},
    )
    assert missing.status_code == 400


async def test_a_filed_documents_notes_and_kind_can_be_corrected(
    quoting, db, requester, team
) -> None:
    quoting._transport.app.state.quote_drive = RecordingDrive(["Lab kit"])
    created = await _with_task(db, quoting, requester, team, "Lab kit")
    uploaded = await quoting.post(
        f"{API}/{created['id']}/documents",
        data={"kind": "other"},
        files=[("files", ("rfq.csv", io.BytesIO(RFQ_CSV), "text/csv"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    doc = uploaded.json()["documents"][0]

    fixed = await quoting.patch(
        f"{API}/{created['id']}/documents/{doc['id']}",
        json={"notes": "The customer's RFQ, sent late", "kind": "customer_rfq"},
    )
    assert fixed.status_code == 200, fixed.text
    after = fixed.json()["documents"][0]
    assert after["notes"] == "The customer's RFQ, sent late" and after["kind"] == "customer_rfq"

    refused = await quoting.patch(
        f"{API}/{created['id']}/documents/{doc['id']}", json={"kind": "costing_report"}
    )
    assert refused.status_code == 400
