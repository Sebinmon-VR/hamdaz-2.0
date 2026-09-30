"""Who the supplier is: read off their quotation and emails, confirmed on the
summary tab, told to the approvers with what is missing.

The route tests use the recording mailer and stub drive; nothing reaches a
real mailbox or SharePoint.
"""

from __future__ import annotations

from app.comparison import supplier_details as sd
from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    _priced,
    quoting,
    requester,
    team,
)

LETTERHEAD = """GULF TECH TRADING LLC
Office 504, Al Saqr Tower, Sheikh Zayed Road, Dubai, UAE
P.O. Box 12345 | Tel: +971 4 123 4567 | Mob: +971 50 765 4321
Email: sales@gulftech.ae | www.gulftech.ae
TRN: 100234567800003
To: Hamdaz Technologies, Dubai, sebin@hamdaz.com
Contact Person: Ahmed Khan
Bank Name: Emirates NBD
Account Name: Gulf Tech Trading LLC
Account No: 1012345678901
IBAN: AE07 0331 2345 6789 0123 456
SWIFT: EBILAEAD
Authorized distributor for HPE
Trade Licence No: 765432
"""


def test_the_letterhead_and_footer_say_who_the_supplier_is() -> None:
    found = sd.suggest(LETTERHEAD, exclude=sd.OWN_COMPANY)
    assert found["address"] == "Office 504, Al Saqr Tower, Sheikh Zayed Road, Dubai, UAE"
    assert found["country"] == "United Arab Emirates"
    assert found["phone"] == "+971 4 123 4567"
    assert found["mobile"] == "+971 50 765 4321"
    assert found["website"] == "www.gulftech.ae"
    assert found["tax_id"] == "100234567800003"
    assert found["trade_licence_no"] == "765432"
    assert found["iban"] == "AE070331234567890123456"
    assert found["swift"] == "EBILAEAD"
    assert found["bank_name"] == "Emirates NBD"
    assert found["account_number"] == "1012345678901"
    assert found["contact_person"] == "Ahmed Khan"
    assert found["supplier_type"] == "Distributor"


def test_our_own_company_is_never_read_as_the_supplier() -> None:
    found = sd.suggest(LETTERHEAD, exclude=sd.OWN_COMPANY)
    assert found["emails"] == ["sales@gulftech.ae"]


def test_blank_fields_are_listed_in_the_forms_order() -> None:
    details = sd.SupplierDetails(address="Dubai", emails="a@x.ae; b@x.ae")
    assert details.emails == ["a@x.ae", "b@x.ae"]
    missing = sd.missing(details)
    assert "Office address" not in missing and "Email ids" not in missing
    assert missing[0] == "City" and len(missing) == len(sd.LABELS) - 2


def test_suggestions_skip_what_is_already_there_and_name_their_source() -> None:
    details = sd.SupplierDetails(phone="+971 4 123 4567", emails=["sales@gulftech.ae"])
    email = {
        "subject": "RE: Offer",
        "from": {"name": "Ahmed Khan", "address": "ahmed@gulftech.ae"},
        "body": "Regards,\nAhmed\nMob: +971 55 111 2222",
    }
    offered = sd.suggestions_for(
        details,
        document=sd.suggest(LETTERHEAD, exclude=sd.OWN_COMPANY),
        document_name="GT-1182.pdf",
        terms={"payment_terms": "100% advance", "incoterm": None, "currency": "AED"},
        emails=[email],
    )
    assert "phone" not in offered
    assert offered["emails"]["value"] == ["ahmed@gulftech.ae"]
    assert offered["emails"]["source"] == "their email “RE: Offer”"
    assert offered["tax_id"]["source"] == "the quotation (GT-1182.pdf)"
    assert offered["payment_terms"]["value"] == "100% advance"
    # The quotation's mobile came first; the email's is not offered over it.
    assert offered["mobile"]["value"] == "+971 50 765 4321"


def test_an_email_belongs_to_the_supplier_by_domain_or_name() -> None:
    known = {"emails": ["sales@gulftech.ae"], "website": "www.gulftech.ae"}
    ours = {"from": {"name": "Ahmed", "address": "ahmed@gulftech.ae"}}
    other = {"from": {"name": "Priya", "address": "priya@othertrading.com"}}
    assert sd.belongs_to(ours, "Gulf Tech Trading LLC", known, alone=False)
    assert not sd.belongs_to(other, "Gulf Tech Trading LLC", known, alone=False)
    assert sd.belongs_to(other, "Gulf Tech Trading LLC", known, alone=True)


# ── over HTTP ──────────────────────────────────────────────────────────


async def test_details_are_saved_per_offer_and_reach_the_approvers(
    quoting, requester, team
) -> None:
    quote_id = await _priced(quoting, requester, team)

    form = await quoting.get(f"{API}/{quote_id}/supplier-details")
    assert form.status_code == 200, form.text
    body = form.json()
    assert [g["title"] for g in body["groups"]][0] == "Address"
    supplier = body["suppliers"][0]
    assert supplier["may_edit"] is True
    assert "Office address" in supplier["missing"]

    saved = await quoting.put(
        f"{API}/{quote_id}/supplier-quotes/{supplier['supplier_quote_id']}/details",
        json={"address": "Office 504, Dubai", "country": "United Arab Emirates",
              "emails": "sales@gulftech.ae, ahmed@gulftech.ae", "tax_id": "100234567800003"},
    )
    assert saved.status_code == 200, saved.text
    after = saved.json()["suppliers"][0]
    assert after["details"]["emails"] == ["sales@gulftech.ae", "ahmed@gulftech.ae"]
    assert "Office address" not in after["missing"]

    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()
    sent = await quoting.post(f"{API}/{quote_id}/submit")
    assert sent.status_code == 200, sent.text
    html = mailer.sent[-1]["html"]
    assert "Office 504, Dubai" in html and "100234567800003" in html
    assert "Not given:" in html and "IBAN" in html
