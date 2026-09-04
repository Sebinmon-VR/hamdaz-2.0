"""The templates the product ships with.

Seeded like the module and label catalogues: code, not user data, because a
module refers to its own template by ``kind``. A super admin may edit any of it
afterwards — rename fields, add their own, change what is required — and the
seed will not undo that.

The quote template below is not invented. Every ``maps_to`` was read off a live
Zoho Books estimate, including the three custom fields this organisation
actually uses: ``cf_bcd`` (BCD, the bid closing date), ``cf_portal`` and
``cf_quote_creater``. So a field here is a field Zoho will accept when the
integration is built, rather than a guess that has to be reconciled later.
"""

from __future__ import annotations

from typing import Any, Final

from app.models.templates import FieldType

#: What a module asks for when it wants "the current quote form".
QUOTE_REQUEST: Final = "quote_request"


def field(
    key: str,
    label: str,
    kind: FieldType,
    *,
    section: str,
    required: bool = False,
    maps_to: str | None = None,
    help: str | None = None,
    options: list[str] | None = None,
    default: Any = None,
    columns: list[dict] | None = None,
) -> dict[str, Any]:
    """One field. ``maps_to`` is the Zoho estimate field it becomes."""
    spec: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": kind.value,
        "section": section,
        "required": required,
    }
    if maps_to:
        spec["maps_to"] = maps_to
    if help:
        spec["help"] = help
    if options:
        spec["options"] = options
    if default is not None:
        spec["default"] = default
    if columns:
        spec["columns"] = columns
    return spec


_LINE_COLUMNS: Final[list[dict]] = [
    field("name", "Item", FieldType.TEXT, section="line", required=True, maps_to="name"),
    field("item_code", "Code / SKU", FieldType.TEXT, section="line", maps_to="item_code"),
    field("description", "Description", FieldType.TEXTAREA, section="line", maps_to="description"),
    field("quantity", "Qty", FieldType.NUMBER, section="line", required=True,
          maps_to="quantity", default=1),
    field("unit", "Unit", FieldType.TEXT, section="line", maps_to="unit",
          help="each, set, metre, box"),
    field("rate", "Unit price", FieldType.CURRENCY, section="line", required=True,
          maps_to="rate", help="Zoho calls the unit price 'rate'"),
    field("discount", "Discount", FieldType.CURRENCY, section="line", maps_to="discount"),
    field("tax_name", "Tax", FieldType.TEXT, section="line", maps_to="tax_name"),
    field("tax_percentage", "Tax %", FieldType.PERCENT, section="line",
          maps_to="tax_percentage"),
    field("cost_rate", "Our cost", FieldType.CURRENCY, section="line",
          help="Not sent to Zoho. Kept so an approver can see the margin."),
]

QUOTE_SECTIONS: Final[list[dict]] = [
    {"key": "customer", "name": "Customer", "help": "Who the quote is for."},
    {"key": "quote", "name": "Quote details", "help": "Dates, references, currency."},
    {"key": "items", "name": "Items", "help": "What is being quoted."},
    {"key": "charges", "name": "Charges and discounts", "help": "Applied to the whole quote."},
    {"key": "terms", "name": "Terms and notes", "help": "What the customer sees."},
    {"key": "suppliers", "name": "Supplier quotes",
     "help": "Attach what suppliers sent, if several quoted the same requirement."},
]

QUOTE_FIELDS: Final[list[dict]] = [
    # ── customer ───────────────────────────────────────────────────────
    field("customer_name", "Customer", FieldType.TEXT, section="customer", required=True,
          maps_to="customer_name", help="Matched to a Zoho contact when it is created."),
    field("customer_id", "Zoho contact id", FieldType.TEXT, section="customer",
          maps_to="customer_id", help="Left blank until the customer is matched."),
    field("contact_person", "Contact person", FieldType.TEXT, section="customer",
          maps_to="contact_persons"),
    field("place_of_supply", "Place of supply", FieldType.TEXT, section="customer",
          maps_to="place_of_supply", help="Emirate code, e.g. AB, DU. Drives VAT treatment."),

    # ── quote details ──────────────────────────────────────────────────
    field("title", "Title", FieldType.TEXT, section="quote", required=True,
          help="For finding it here. Not sent to Zoho."),
    field("reference_number", "Customer reference", FieldType.TEXT, section="quote",
          maps_to="reference_number", help="Their PO or enquiry number."),
    field("quote_date", "Quote date", FieldType.DATE, section="quote", maps_to="date"),
    field("expiry_date", "Valid until", FieldType.DATE, section="quote",
          maps_to="expiry_date", help="How long the price holds."),
    field("currency", "Currency", FieldType.SELECT, section="quote", required=True,
          maps_to="currency_code", default="AED",
          options=["AED", "USD", "EUR", "GBP", "SAR", "QAR", "OMR", "KWD", "BHD", "INR"]),
    field("salesperson_name", "Salesperson", FieldType.TEXT, section="quote",
          maps_to="salesperson_name"),
    field("cf_bcd", "Bid closing date (BCD)", FieldType.DATE, section="quote",
          maps_to="cf_bcd",
          help="The date the bid closes. The same date the Proposals list calls BCD."),
    field("cf_portal", "Portal", FieldType.TEXT, section="quote", maps_to="cf_portal",
          help="Which client portal the enquiry came through."),
    field("cf_quote_creater", "Quote creator", FieldType.TEXT, section="quote",
          maps_to="cf_quote_creater", help="A dropdown in Zoho."),
    field("tax_treatment", "Tax treatment", FieldType.SELECT, section="quote",
          maps_to="tax_treatment", default="vat_registered",
          options=["vat_registered", "vat_not_registered", "gcc_vat_registered",
                   "gcc_vat_not_registered", "non_gcc", "dz_vat_registered"]),

    # ── items ──────────────────────────────────────────────────────────
    field("items", "Line items", FieldType.TABLE, section="items", required=True,
          maps_to="line_items", columns=_LINE_COLUMNS,
          help="One row per priced line. Totals are computed, never typed."),

    # ── charges ────────────────────────────────────────────────────────
    field("discount", "Discount on the whole quote", FieldType.CURRENCY, section="charges",
          maps_to="discount"),
    field("shipping_charge", "Shipping", FieldType.CURRENCY, section="charges",
          maps_to="shipping_charge"),
    field("adjustment", "Adjustment", FieldType.CURRENCY, section="charges",
          maps_to="adjustment", help="Rounding, or anything the other fields do not cover."),

    # ── terms ──────────────────────────────────────────────────────────
    field("subject", "Subject", FieldType.TEXTAREA, section="terms",
          maps_to="subject_content"),
    field("payment_terms", "Payment terms", FieldType.TEXT, section="terms",
          help="e.g. 30 days net, 50% advance."),
    field("delivery_terms", "Delivery terms", FieldType.TEXT, section="terms",
          help="e.g. 4 weeks ex-stock."),
    field("notes", "Notes to the customer", FieldType.TEXTAREA, section="terms",
          maps_to="notes"),
    field("terms", "Terms and conditions", FieldType.TEXTAREA, section="terms",
          maps_to="terms"),

    # ── supplier quotes ────────────────────────────────────────────────
    field("multiple_supplier_quotes", "Several suppliers quoted this", FieldType.CHECKBOX,
          section="suppliers", default=False,
          help="Turn on to attach supplier quotes and compare them. An approver "
               "then chooses which supplier wins."),
    field("supplier_files", "Supplier quote documents", FieldType.FILE, section="suppliers",
          help="PDF, image, XLSX, CSV or DOCX. Read automatically — locally where "
               "the document allows it, and by Claude where it does not."),
]


#: The templates seeded on first run.
TEMPLATES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": QUOTE_REQUEST,
        "name": "Quote request",
        "kind": QUOTE_REQUEST,
        "description": (
            "The form presales fills in to raise a customer quote. Every field "
            "that carries a `maps_to` was taken from a live Zoho Books estimate, "
            "so the integration later is a mapping rather than a rewrite."
        ),
        "sections": QUOTE_SECTIONS,
        "fields": QUOTE_FIELDS,
    },
)

BY_KEY: Final[dict[str, dict[str, Any]]] = {t["key"]: t for t in TEMPLATES}
