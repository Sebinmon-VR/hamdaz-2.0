"""Who the supplier is: address, contacts, registration, bank and terms.

A supplier quotation is read for its prices. Its letterhead and footer also
say who the supplier is (the office address, the TRN, the bank details for
the transfer), and until now all of that was dropped. The approvers then
decided on a price from a company the quote could not describe.

The details live on the supplier quote they came with, and are typed or
confirmed on the quote's summary tab. They are kept structured, one field per
fact, so a supplier library can be built from them later without re-reading a
single document.

**Every field is expected, none blocks.** A missing TRN is shown as missing,
on the screen and in the approval mail, and the quote still goes. Whether to
accept an offer from a supplier nobody could describe is the approver's call,
and they are told.

What is read from the documents arrives as **suggestions**, the same way an
RFQ's closing date does: the value, where it came from, and nothing written
until somebody accepts it.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator

#: The fields, grouped as the form shows them, with their labels.
GROUPS: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    (
        "Address",
        (
            ("address", "Office address"),
            ("city", "City"),
            ("country", "Country"),
        ),
    ),
    (
        "Contacts",
        (
            ("contact_person", "Contact person"),
            ("designation", "Designation"),
            ("phone", "Phone"),
            ("mobile", "Mobile"),
            ("emails", "Email ids"),
            ("website", "Website"),
        ),
    ),
    (
        "Registration and tax",
        (
            ("trade_licence_no", "Trade licence no."),
            ("tax_id", "VAT / TRN / tax id"),
            ("supplier_type", "Supplier type"),
        ),
    ),
    (
        "Bank and terms",
        (
            ("bank_name", "Bank"),
            ("account_name", "Account name"),
            ("account_number", "Account no."),
            ("iban", "IBAN"),
            ("swift", "SWIFT / BIC"),
            ("payment_terms", "Payment terms"),
            ("currency", "Currency"),
            ("incoterm", "Incoterm"),
        ),
    ),
)

LABELS: Final[dict[str, str]] = {key: label for _, fields in GROUPS for key, label in fields}

#: Words of our own company. A quotation names the customer too, and our own
#: address, email or TRN is never the supplier's.
OWN_COMPANY: Final = ("hamdaz",)

#: What a supplier is to us. Free text is allowed beside these.
SUPPLIER_TYPES: Final = ("OEM / manufacturer", "Distributor", "Reseller", "Service provider")


class SupplierDetails(BaseModel):
    """Everything known about the supplier behind one offer. All optional to
    store; see the module note for why none of it blocks."""

    address: str | None = Field(default=None, max_length=1000)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=80)
    contact_person: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=60)
    mobile: str | None = Field(default=None, max_length=60)
    emails: list[str] = Field(default_factory=list, max_length=10)
    website: str | None = Field(default=None, max_length=300)
    trade_licence_no: str | None = Field(default=None, max_length=80)
    tax_id: str | None = Field(default=None, max_length=80)
    supplier_type: str | None = Field(default=None, max_length=80)
    bank_name: str | None = Field(default=None, max_length=200)
    account_name: str | None = Field(default=None, max_length=200)
    account_number: str | None = Field(default=None, max_length=60)
    iban: str | None = Field(default=None, max_length=60)
    swift: str | None = Field(default=None, max_length=20)
    payment_terms: str | None = Field(default=None, max_length=500)
    currency: str | None = Field(default=None, max_length=3)
    incoterm: str | None = Field(default=None, max_length=60)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("emails", mode="before")
    @classmethod
    def _clean_emails(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        seen: list[str] = []
        for entry in value:
            entry = str(entry).strip().lower()
            if entry and entry not in seen:
                seen.append(entry)
        return seen

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


def missing(details: SupplierDetails) -> list[str]:
    """The labels of what is still blank, in the form's order."""
    data = details.model_dump()
    return [LABELS[key] for key in LABELS if not data.get(key)]


# ── reading them off a document ────────────────────────────────────────

_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WEBSITE: Final = re.compile(r"\b(?:https?://)?www\.[\w-]+(?:\.[\w-]+)+(?:/\S*)?", re.I)
_NUMBER: Final = r"(\+?\(?\d[\d\s().\-/]{6,}\d)"
_PHONE: Final = re.compile(r"\b(?:tel(?:ephone)?|phone|ph|office|t)\s*[.:#]?\s*" + _NUMBER, re.I)
_MOBILE: Final = re.compile(r"\b(?:mob(?:ile)?|cell|m)\s*[.:#]?\s*" + _NUMBER, re.I)
_TAX: Final = re.compile(
    r"\b(?:TRN|VAT\s*(?:reg(?:istration)?\.?\s*)?(?:no\.?|number|#|id)|tax\s*(?:registration|reg\.?|id)"
    r"\s*(?:no\.?|number|#)?|GSTIN|GST\s*no\.?|EIN|CR\s*no\.?)"
    r"\s*[:#.]?\s*([A-Z0-9][A-Z0-9 -]{5,24}[A-Z0-9])",
    re.I,
)
_LICENCE: Final = re.compile(
    r"\b(?:trade\s+)?licen[cs]e\s*(?:no\.?|number|#)"
    r"\s*[:#.]?\s*([A-Z0-9][A-Z0-9/ -]{3,30}[A-Z0-9])",
    re.I,
)
_IBAN: Final = re.compile(
    r"\bIBAN[ \t]*(?:no\.?|number)?[ \t]*[:#.]?[ \t]*([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{2,4}){3,8})\b"
)
_SWIFT: Final = re.compile(
    r"\b(?:SWIFT|BIC)(?:\s*/\s*BIC)?(?:\s*code)?\s*[:#.]?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b"
)
_BANK: Final = re.compile(r"\bbank(?:\s*name)?\s*[:]\s*([^\n|]{3,120})", re.I)
_ACCOUNT_NAME: Final = re.compile(
    r"\b(?:account|a/c|beneficiary)\s*(?:holder\s*)?name\s*[:]\s*([^\n|]{3,120})", re.I
)
_ACCOUNT_NO: Final = re.compile(
    r"\b(?:account|a/c)\s*(?:no\.?|number|#)\s*[:#.]?\s*(\d[\d -]{5,30}\d)", re.I
)
_ADDRESS: Final = re.compile(
    r"\b(?:address|office|head\s+office|regd?\.?\s+office)\s*[:]\s*([^\n|]{6,300})", re.I
)
_PO_BOX: Final = re.compile(r"(P\.?\s?O\.?\s*Box\s*[:.]?\s*\d+[^\n|]{0,120})", re.I)
_CONTACT: Final = re.compile(
    # The label in any case; the name itself must start with a capital.
    r"\b(?i:contact(?:\s+person)?|attn|sales\s+(?:person|executive|manager)|prepared\s+by|"
    r"quoted\s+by)\s*[:.]\s*([A-Z][A-Za-z.'\- ]{2,60})",
)
_TYPE: Final = (
    (
        re.compile(r"\bauthori[sz]ed\s+(?:distributor|partner)\b|\bdistributor\b", re.I),
        "Distributor",
    ),
    (re.compile(r"\bmanufacturer\b|\bOEM\b", re.I), "OEM / manufacturer"),
    (re.compile(r"\breseller\b", re.I), "Reseller"),
)

#: Countries a supplier letterhead names, and how to spell them back.
_COUNTRIES: Final[dict[str, str]] = {
    "united arab emirates": "United Arab Emirates", "u.a.e": "United Arab Emirates",
    "uae": "United Arab Emirates", "dubai": "United Arab Emirates",
    "abu dhabi": "United Arab Emirates", "sharjah": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia", "ksa": "Saudi Arabia", "qatar": "Qatar", "oman": "Oman",
    "kuwait": "Kuwait", "bahrain": "Bahrain", "india": "India", "china": "China",
    "hong kong": "Hong Kong", "taiwan": "Taiwan", "singapore": "Singapore",
    "malaysia": "Malaysia", "japan": "Japan", "south korea": "South Korea", "korea": "South Korea",
    "germany": "Germany", "united kingdom": "United Kingdom", "england": "United Kingdom",
    "uk": "United Kingdom", "france": "France", "italy": "Italy", "spain": "Spain",
    "netherlands": "Netherlands", "belgium": "Belgium", "switzerland": "Switzerland",
    "sweden": "Sweden", "denmark": "Denmark", "norway": "Norway", "finland": "Finland",
    "poland": "Poland", "austria": "Austria", "ireland": "Ireland", "turkey": "Turkey",
    "türkiye": "Turkey", "egypt": "Egypt", "jordan": "Jordan", "lebanon": "Lebanon",
    "pakistan": "Pakistan", "sri lanka": "Sri Lanka", "bangladesh": "Bangladesh",
    "united states": "United States", "usa": "United States", "u.s.a": "United States",
    "canada": "Canada", "mexico": "Mexico", "brazil": "Brazil", "australia": "Australia",
    "new zealand": "New Zealand", "south africa": "South Africa", "israel": "Israel",
    "czech republic": "Czech Republic", "portugal": "Portugal", "greece": "Greece",
}


def _country_in(text: str) -> str | None:
    lowered = f" {text.lower()} "
    for name, country in sorted(_COUNTRIES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered):
            return country
    return None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return re.sub(r"\s+", " ", match.group(1)).strip(" ,;:-") if match else None


def suggest(text: str | None, *, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """What a document says about who sent it, field by field.

    ``exclude`` are words of our own company ("hamdaz"): a quotation names the
    customer too, and our own address or email is not the supplier's. A line
    carrying one of them is not read.
    """
    if not text:
        return {}
    words = tuple(w.lower() for w in exclude if w)
    lines = [line for line in text.splitlines() if not any(w in line.lower() for w in words)]
    body = "\n".join(lines)
    found: dict[str, Any] = {}

    emails = []
    for email in _EMAIL.findall(body):
        email = email.lower().rstrip(".")
        if email not in emails:
            emails.append(email)
    if emails:
        found["emails"] = emails[:5]
    for key, pattern in (
        ("website", _WEBSITE),
    ):
        match = pattern.search(body)
        if match:
            found[key] = match.group(0).rstrip(".,;")
    for key, pattern in (
        ("phone", _PHONE),
        ("mobile", _MOBILE),
        ("tax_id", _TAX),
        ("trade_licence_no", _LICENCE),
        ("iban", _IBAN),
        ("swift", _SWIFT),
        ("bank_name", _BANK),
        ("account_name", _ACCOUNT_NAME),
        ("account_number", _ACCOUNT_NO),
        ("address", _ADDRESS),
        ("contact_person", _CONTACT),
    ):
        if value := _first(pattern, body):
            found[key] = value
    if "address" not in found:
        # A letterhead rarely labels its address: the line near the top that
        # names a place, with a comma in it, is the address.
        for line in lines[:8]:
            clean = re.sub(r"\s+", " ", line.split("|")[0]).strip(" ,")
            if "," in clean and _country_in(clean) and not _EMAIL.search(clean):
                found["address"] = clean[:300]
                break
    if "address" not in found and (box := _first(_PO_BOX, body)):
        found["address"] = box
    if "iban" in found:
        found["iban"] = found["iban"].replace(" ", "").upper()
    # The country from the address when there is one; otherwise from the
    # letterhead, which is the top of the document, not the customer block.
    where = "\n".join(filter(None, [found.get("address"), *lines[:8]]))
    if country := _country_in(where):
        found["country"] = country
    for pattern, kind in _TYPE:
        if pattern.search(body):
            found["supplier_type"] = kind
            break
    return found


# ── putting it together for one offer ──────────────────────────────────


def stored(raw: dict | None) -> SupplierDetails:
    """The confirmed details on a supplier quote. A bad row reads as empty."""
    try:
        return SupplierDetails.model_validate(raw or {})
    except ValueError:
        return SupplierDetails()


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def _site_domain(url: str | None) -> str:
    if not url:
        return ""
    host = re.sub(r"^https?://", "", url.lower()).split("/")[0]
    return host.removeprefix("www.")


def belongs_to(
    email: dict[str, Any], supplier_name: str, known: dict[str, Any], alone: bool
) -> bool:
    """Whether a supplier email is from this supplier.

    By the sender's domain against what their quotation gave (its emails and
    website), or by their name in the sender's. With one supplier on the
    quote, every supplier email is theirs.
    """
    if alone:
        return True
    sender = ((email.get("from") or {}).get("address") or "").lower()
    sender_name = ((email.get("from") or {}).get("name") or "").lower()
    domains = {_domain(e) for e in known.get("emails") or []} | {_site_domain(known.get("website"))}
    domains.discard("")
    if sender and _domain(sender) in domains:
        return True
    word = next((w for w in re.split(r"\W+", supplier_name.lower()) if len(w) >= 4), "")
    return bool(word) and (word in _domain(sender) or word in sender_name)


def suggestions_for(
    details: SupplierDetails,
    *,
    document: dict[str, Any] | None,
    document_name: str | None,
    terms: dict[str, str | None],
    emails: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Every value offered for a blank or different field, with its source.

    The quotation first, then the supplier's emails: a sender address, the
    name they signed with, and whatever their signature says.
    """
    offered: dict[str, dict[str, Any]] = {}
    current = details.model_dump()

    def offer(field: str, value: Any, source: str) -> None:
        if field not in LABELS or value in (None, "", []):
            return
        if field == "emails":
            new = [e for e in value if e not in current["emails"]]
            if not new:
                return
            had = offered.get("emails")
            if had:
                had["value"] = had["value"] + [e for e in new if e not in had["value"]]
                return
            offered["emails"] = {"value": new, "source": source}
            return
        same = str(current.get(field) or "").strip().lower() == str(value).strip().lower()
        if field in offered or same:
            return
        offered[field] = {"value": value, "source": source}

    where = f"the quotation{f' ({document_name})' if document_name else ''}"
    for field, value in (document or {}).items():
        offer(field, value, where)
    for field, value in terms.items():
        offer(field, value, where)
    for email in emails:
        subject = email.get("subject") or "(no subject)"
        source = f"their email \u201c{subject}\u201d"
        sender = email.get("from") or {}
        if sender.get("address"):
            offer("emails", [sender["address"].lower()], source)
        if sender.get("name") and "@" not in sender["name"]:
            offer("contact_person", sender["name"], source)
        for field, value in suggest(email.get("body"), exclude=OWN_COMPANY).items():
            offer(field, value, source)
    return offered
