"""Response shapes for quotes.

Zoho's vocabulary stops here. It calls a quote an *estimate*, spells the bid
closing date ``cf_bcd``, and returns money as bare floats with the currency in a
sibling field. What leaves this module is named the way the rest of the ERP names
things, so a frontend never has to learn Zoho's schema.

Dates stay strings, as they do in ``app.proposals.schemas``. Zoho sends them as
``YYYY-MM-DD`` already, and parsing them here only to format them again there
would be work in service of nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int | None:
    """Zoho sends some numbers as strings ("635180"), so int() is not enough."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class QuoteOut(BaseModel):
    """One row of the quotes list."""

    id: str
    number: str
    status: str | None
    #: Zoho's finer-grained state within a status, when the org uses them.
    sub_status: str | None

    customer_id: str | None
    customer_name: str | None
    company_name: str | None

    date: str | None
    expiry_date: str | None
    #: The customer's own reference — usually their PO number.
    reference_number: str | None

    total: float
    currency_code: str | None

    #: Bid closing date, from the ``cf_bcd`` custom field. The same concept as
    #: BCD on the SharePoint Proposals list, which is what makes the two joinable.
    bcd: str | None
    #: The ``cf_portal`` custom field — which client portal the bid came through.
    portal: str | None

    salesperson_name: str | None
    has_attachment: bool
    created_time: str | None
    last_modified_time: str | None

    #: Deep link into Zoho Books, where the quote is actually worked on — the
    #: same role ``web_url`` plays for a SharePoint proposal task. Distinct from
    #: ``QuoteDetailOut.estimate_url``, which is the customer's view.
    web_url: str | None = None

    @classmethod
    def from_zoho(cls, row: dict, *, app_base: str | None = None) -> QuoteOut:
        quote_id = str(row.get("estimate_id") or "")
        return cls(
            web_url=f"{app_base}#/quotes/{quote_id}" if app_base and quote_id else None,
            id=quote_id,
            number=row.get("estimate_number") or "(unnumbered)",
            status=row.get("status"),
            sub_status=row.get("current_sub_status") or None,
            customer_id=str(row["customer_id"]) if row.get("customer_id") else None,
            customer_name=row.get("customer_name") or None,
            company_name=row.get("company_name") or None,
            date=row.get("date") or None,
            expiry_date=row.get("expiry_date") or None,
            reference_number=row.get("reference_number") or None,
            total=_money(row.get("total")),
            currency_code=row.get("currency_code") or None,
            bcd=row.get("cf_bcd") or None,
            portal=row.get("cf_portal") or None,
            salesperson_name=row.get("salesperson_name") or None,
            has_attachment=bool(row.get("has_attachment")),
            created_time=row.get("created_time") or None,
            last_modified_time=row.get("last_modified_time") or None,
        )


class LineItemOut(BaseModel):
    item_id: str | None
    name: str | None
    description: str | None
    code: str | None
    quantity: float
    unit: str | None
    rate: float
    discount: float
    tax_name: str | None
    total: float

    @classmethod
    def from_zoho(cls, row: dict) -> LineItemOut:
        return cls(
            item_id=str(row["item_id"]) if row.get("item_id") else None,
            name=row.get("name") or row.get("internal_name") or None,
            description=row.get("description") or None,
            code=row.get("item_code") or None,
            quantity=_money(row.get("quantity")),
            unit=row.get("unit") or None,
            rate=_money(row.get("rate")),
            discount=_money(row.get("discount_amount") or row.get("discount")),
            tax_name=row.get("tax_name") or None,
            total=_money(row.get("item_total")),
        )


class SalesOrderRefOut(BaseModel):
    """A sales order this quote turned into.

    Zoho embeds these in the estimate rather than making them a separate lookup,
    so this needs no extra call.
    """

    id: str
    number: str | None
    date: str | None
    status: str | None
    total: float

    @classmethod
    def from_zoho(cls, row: dict) -> SalesOrderRefOut:
        return cls(
            id=str(row.get("salesorder_id") or ""),
            number=row.get("salesorder_number") or None,
            date=row.get("date") or None,
            status=row.get("salesorder_order_status") or row.get("status") or None,
            total=_money(row.get("total")),
        )


class DocumentOut(BaseModel):
    """An attachment on a quote — the drawing, datasheet or signed PDF."""

    id: str | None
    file_name: str | None
    file_type: str | None
    file_size: int | None
    file_size_formatted: str | None
    uploaded_by: str | None
    uploaded_on: str | None

    #: Where to fetch the bytes. Points at *this* API, not at Zoho: Zoho wants
    #: the OAuth token in a header, which a browser following a link cannot
    #: supply, so the download is relayed under the caller's own session.
    download_url: str | None = None

    @classmethod
    def from_zoho(cls, row: dict, *, quote_id: str | None = None) -> DocumentOut:
        document_id = str(row["document_id"]) if row.get("document_id") else None
        return cls(
            id=document_id,
            file_name=row.get("file_name") or None,
            file_type=row.get("file_type") or None,
            # Zoho sends this as a string of digits, not a number.
            file_size=_int(row.get("file_size")),
            file_size_formatted=row.get("file_size_formatted") or None,
            uploaded_by=row.get("uploaded_by") or None,
            uploaded_on=row.get("uploaded_on_date_formatted") or row.get("uploaded_on") or None,
            download_url=(
                f"/api/v1/quotes/{quote_id}/documents/{document_id}"
                if quote_id and document_id
                else None
            ),
        )


class QuoteDetailOut(QuoteOut):
    """Everything on one quote, plus the records Zoho embeds in it."""

    sub_total: float
    tax_total: float
    discount_total: float
    shipping_charge: float
    adjustment: float

    notes: str | None
    terms: str | None
    billing_address: dict[str, Any] | None
    shipping_address: dict[str, Any] | None

    line_items: list[LineItemOut]
    #: Embedded by Zoho — no second call needed.
    salesorders: list[SalesOrderRefOut]
    documents: list[DocumentOut]

    #: Invoices raised against this quote. Only the ids are available: the
    #: current Zoho scope cannot read invoices. See ``RelatedOut.invoices``.
    invoice_ids: list[str]
    invoiced_amount: float
    uninvoiced_amount: float

    #: The *customer's* link — a zohosecurepay.com page showing the quote as the
    #: client receives it. Not the same thing as ``web_url``, which opens the
    #: record in Zoho Books for staff.
    estimate_url: str | None
    #: Custom fields beyond bcd/portal, as label -> value.
    custom_fields: dict[str, Any]

    @classmethod
    def from_zoho(cls, row: dict, *, app_base: str | None = None) -> QuoteDetailOut:
        base = QuoteOut.from_zoho(row, app_base=app_base).model_dump()
        # The detail payload keeps custom fields in a list rather than as cf_*
        # columns, so bcd and portal have to be recovered from it.
        custom = {
            (f.get("label") or f.get("api_name") or "").strip(): f.get("value")
            for f in (row.get("custom_fields") or [])
        }
        by_api = {
            (f.get("api_name") or "").strip(): f.get("value")
            for f in (row.get("custom_fields") or [])
        }
        base["bcd"] = base["bcd"] or by_api.get("cf_bcd") or custom.get("BCD")
        base["portal"] = base["portal"] or by_api.get("cf_portal") or custom.get("Portal")

        return cls(
            **base,
            sub_total=_money(row.get("sub_total")),
            tax_total=_money(row.get("tax_total")),
            discount_total=_money(row.get("discount_total")),
            shipping_charge=_money(row.get("shipping_charge")),
            adjustment=_money(row.get("adjustment")),
            notes=row.get("notes") or None,
            terms=row.get("terms") or None,
            billing_address=row.get("billing_address") or None,
            shipping_address=row.get("shipping_address") or None,
            line_items=[LineItemOut.from_zoho(i) for i in row.get("line_items") or []],
            salesorders=[SalesOrderRefOut.from_zoho(s) for s in row.get("salesorders") or []],
            documents=[
                DocumentOut.from_zoho(d, quote_id=base["id"])
                for d in row.get("documents") or []
            ],
            invoice_ids=[str(i) for i in row.get("invoice_ids") or []],
            invoiced_amount=_money(row.get("invoiced_amount")),
            uninvoiced_amount=_money(row.get("uninvoiced_amount")),
            estimate_url=row.get("estimate_url") or None,
            custom_fields={k: v for k, v in custom.items() if k},
        )


class QuoteListOut(BaseModel):
    total: int
    #: True when the page ceiling stopped the sweep before Zoho ran out of rows.
    truncated: bool
    quotes: list[QuoteOut]


class BranchOut(BaseModel):
    """One related-record lookup, and whether it worked.

    Each branch reports its own outcome rather than the whole response failing:
    a quote whose customer has been deleted, or whose invoices this token may not
    read, should still show its line items.
    """

    ok: bool
    #: Present only when ``ok`` is false. Written for a person to act on.
    error: str | None = None
    data: Any = None


class RelatedOut(BaseModel):
    quote_id: str
    quote_number: str
    #: Which branches were asked for, in the order requested.
    included: list[str]
    customer: BranchOut | None = None
    items: BranchOut | None = None
    salesorders: BranchOut | None = None
    invoices: BranchOut | None = None
    comments: BranchOut | None = None
