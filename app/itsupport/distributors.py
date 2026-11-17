"""Finding distributors, asked for in the IT Support chat.

Two kinds of ask, answered the same way:

* a brand: "find Fortinet distributors in UAE";
* a list of items, as a tender or a customer gives them: "find distributors for
  the following" then numbered lines, each with its description, quantity,
  brand or reference, part number, and the buyer's own material number.

**Our own quotes first, no model.** Suppliers who have quoted us that brand
or item before, from the supplier quotes' lines, with the contacts their
quotes gave.

**Then the web, with Claude's web search** (the agents' model, Haiku): the
brand's own partner locator or where-to-buy page first, then business
directories (Yellow Pages UAE, Connect.ae, the chambers, Kompass), then the
companies' own sites and the stores that stock the item, online ones
included — a consumer product has shops, not distributors. Nothing is listed
without a page that shows it, and a contact is given only as a page shows it,
never guessed. Directories are read as any visitor reads them, through the
search: no logins, no scraping.

**The request is read by rules**, not a model: a word like "distributors",
"resellers", "dealers" or "suppliers" with an ask ("find", "who", "list",
"any"…); "in <place>" is the place (the UAE when none is named). Numbered lines
are items; labels on them ("Brand/Reference:", "Part No:", "Material No.:",
"Qty:") are read for what they say. The buyer's material number is theirs, not
the maker's, so it is never searched for.

Not in this step: keeping the list anywhere (the supplier library), and
mailing the distributors. The list lives in the chat.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.model import AgentModel, Spent
from app.models.comparison import SupplierQuote, SupplierQuoteItem

logger = logging.getLogger("hamdaz.itsupport")

#: Companies the web search is asked for.
WEB_COUNT: Final = 8
#: Suppliers from our own quotes shown, most lines first.
OURS_COUNT: Final = 6
#: Items read from one message.
MAX_ITEMS: Final = 15

_KIND = r"(?:distributors?|distributers?|resellers?|dealers?|suppliers?|stockists?|sellers?)"
_ASK = (
    r"(?:find|search|look(?:ing)?\s+(?:for|up)|who\s+(?:are|is|sells?|supplies|distributes)|"
    r"list|give\s+me|show\s+me|get\s+me|any|which|where\s+can\s+i\s+(?:buy|get))"
)
_IS_FIND = re.compile(rf"\b{_ASK}\b.*\b{_KIND}\b|^\s*{_KIND}\s+(?:of|for)\b", re.IGNORECASE)

#: Places named after "in"; the label the reply and the search use.
PLACES: Final = {
    "uae": "the UAE", "u.a.e": "the UAE", "emirates": "the UAE", "dubai": "Dubai",
    "abu dhabi": "Abu Dhabi", "sharjah": "Sharjah", "ajman": "Ajman", "gcc": "the GCC",
    "saudi": "Saudi Arabia", "saudi arabia": "Saudi Arabia", "ksa": "Saudi Arabia",
    "qatar": "Qatar", "oman": "Oman", "bahrain": "Bahrain", "kuwait": "Kuwait",
    "india": "India", "middle east": "the Middle East", "world": "anywhere",
    "worldwide": "anywhere", "global": "anywhere",
}
_UAE: Final = frozenset({"the UAE", "Dubai", "Abu Dhabi", "Sharjah", "Ajman"})
_PLACE = re.compile(
    r"\b(?:in|from|across|within)\s+(?:the\s+)?(" + "|".join(
        sorted((re.escape(p) for p in PLACES), key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)
_FILLER = re.compile(
    rf"\b(?:{_ASK}|{_KIND}|can|could|you|pls|please|me|the|a|an|some|of|for|are|is|there|"
    r"authori[sz]ed|official|good|best|top|local|hi|hey|hello|it|support|following|below|"
    r"these|this|items?|list)\b",
    re.IGNORECASE,
)
_NUMBERED = re.compile(r"^\s*(?:\d{1,3}|[a-z])\s*[.)]\s+(.*)$", re.IGNORECASE)
#: A label on an item line, and the field it fills.
_LABEL = re.compile(
    r"(?P<label>brand\s*/\s*(?:reference|model)|brand|make|manufacturer|description|"
    r"part\s*(?:no|number|#)|p/n|mpn|"
    r"model(?:\s*no)?|material\s*(?:no|number|code)|item\s*code|sap\s*(?:no|code)|qty|"
    r"quantity)\.?\s*[:\-]\s*",
    re.IGNORECASE,
)


_SPEC = re.compile(r"^[\w /()&.-]{2,40}:\s")


def _field(label: str) -> str:
    label = label.lower()
    if label.startswith(("brand", "make", "manufacturer")):
        return "brand"
    if label.startswith(("material", "item", "sap")):
        return "code"
    if label.startswith(("qty", "quantity")):
        return "qty"
    if label.startswith("description"):
        return "details"
    return "part"


@dataclass(frozen=True, slots=True)
class Item:
    description: str = ""
    brand: str = ""
    #: The maker's part or model number.
    part: str = ""
    #: The buyer's own material number: theirs, never searched for.
    code: str = ""
    qty: str = ""
    #: The longer description under the item's heading.
    details: str = ""

    @property
    def label(self) -> str:
        if self.description and self.brand and self.brand.lower() not in self.description.lower():
            return f"{self.description} ({self.brand})"
        return self.description or self.brand

    def line(self) -> str:
        bits = [self.description or self.brand]
        if self.brand and self.description:
            bits.append(f"brand/reference: {self.brand}")
        if self.part:
            bits.append(f"maker part no: {self.part}")
        if self.details:
            bits.append(f"details: {self.details}")
        return " — ".join(bits)


@dataclass(frozen=True, slots=True)
class Ask:
    items: tuple[Item, ...]
    place: str

    @property
    def label(self) -> str:
        if len(self.items) == 1:
            return self.items[0].label
        return f"these {len(self.items)} items"

    @property
    def brands(self) -> list[str]:
        return [i.brand or i.description for i in self.items if i.brand or i.description]


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" -–—,;:.")


def _split(text: str) -> tuple[str, dict[str, str]]:
    """A line's free text (before any label), and what its labels say."""
    marks = list(_LABEL.finditer(text))
    if not marks:
        return _tidy(text), {}
    fields: dict[str, str] = {}
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        value = _tidy(text[m.end():end])
        if value:
            fields.setdefault(_field(m.group("label")), value[:120])
    return _tidy(text[: marks[0].start()]), fields


def _items(lines: list[str]) -> list[Item]:
    found: list[dict[str, str]] = []
    for line in lines:
        numbered = _NUMBERED.match(line)
        if numbered or not found:
            found.append({})
        text = numbered.group(1) if numbered else line
        free, fields = _split(text)
        current = found[-1]
        if not numbered and not fields and _SPEC.match(text):
            # "Colour Temperature: 3200K", "CRI: 95": a spec, not the item's name.
            continue
        if free and not numbered and current.get("description"):
            # Points under the item's heading describe it; the heading names it.
            current["details"] = _tidy(f"{current.get('details', '')}. {free}")[:300]
        elif free:
            current["description"] = _tidy(f"{current.get('description', '')} {free}")[:160]
        for key, value in fields.items():
            if key == "details" and current.get("details"):
                current["details"] = _tidy(f"{value}. {current['details']}")[:300]
            else:
                current.setdefault(key, value)
    items = [Item(**f) for f in found if f.get("description") or f.get("brand")]
    return items[:MAX_ITEMS]


def read_ask(message: str) -> Ask | None:
    """What is asked about and where, when the message asks for distributors;
    an ask with no items when it does but names nothing to look for."""
    if not _IS_FIND.search(message):
        return None
    place_match = _PLACE.search(message)
    place = PLACES[place_match.group(1).lower()] if place_match else "the UAE"
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    items = _items(lines[1:]) if len(lines) > 1 else []
    if not items:
        head = _tidy(_FILLER.sub(" ", _PLACE.sub(" ", lines[0])).replace("?", " "))
        if _LABEL.search(head):
            items = _items([head])
        elif head:
            items = [Item(brand=head[:80])]
    return Ask(tuple(items), place)


# ── our own supplier quotes ───────────────────────────────────────────


def _first(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


#: Words that say nothing about which item it is.
_COMMON: Final = frozenset({
    "with", "and", "for", "the", "kit", "set", "sets", "pack", "pcs", "nos", "unit", "units",
    "inch", "inches", "basics", "series", "pro", "plus", "new", "original", "portable", "type",
    "size", "color", "colour", "black", "white", "qty", "quantity", "item", "items",
})


@dataclass(frozen=True, slots=True)
class Terms:
    """What marks a quote line as this item: the maker's part number (strong),
    the brand (good), and the description's telling words (each a little)."""

    part: str
    brand: str
    words: tuple[str, ...]

    @property
    def any(self) -> list[str]:
        return [t for t in (self.part, self.brand, *self.words) if t]


def _terms(item: Item) -> Terms:
    part = item.part if len(item.part) >= 3 else ""
    brand_words = re.findall(r"[A-Za-z][\w&+-]{2,}", item.brand)
    brand = brand_words[0] if brand_words and brand_words[0].lower() not in _COMMON else ""
    # A brand/model like "Neewer Basics X12B" carries the model too.
    if not part:
        part = next((w for w in brand_words[1:] if re.search(r"\d", w) and len(w) >= 3), "")
    text = f"{item.description} {item.details}" if item.brand else item.description
    words = [
        w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", text)
        if w.lower() not in _COMMON and w.lower() != brand.lower()
    ]
    longest = sorted(dict.fromkeys(w.lower() for w in words), key=len, reverse=True)[:5]
    return Terms(part, brand, tuple(longest))


def score(terms: Terms, line_brand: str | None, line_text: str) -> int:
    """How surely a quote line is this item: a part number 4, the brand 2, each
    telling word 1. Two or more counts — the brand alone, or two words."""
    seen = f"{line_brand or ''} {line_text}".lower()
    points = 0
    if terms.part and terms.part.lower() in seen:
        points += 4
    if terms.brand and terms.brand.lower() in seen:
        points += 2
    points += sum(1 for w in terms.words if w in seen)
    return points


async def from_our_quotes(session: AsyncSession, ask: Ask) -> list[dict[str, Any]]:
    """Suppliers who quoted us these brands or items: best matched and most
    lines first, then the newest."""
    by_name: dict[str, dict[str, Any]] = {}
    for number, item in enumerate(ask.items, start=1):
        terms = _terms(item)
        if not terms.any:
            continue
        maybe = or_(*[
            or_(SupplierQuoteItem.brand.ilike(f"%{t}%"),
                SupplierQuoteItem.description.ilike(f"%{t}%"),
                SupplierQuoteItem.part_number.ilike(f"%{t}%"))
            for t in terms.any
        ])
        candidates = (await session.execute(
            select(
                SupplierQuote.supplier_name, SupplierQuote.created_at, SupplierQuote.details,
                SupplierQuote.contact, SupplierQuoteItem.brand, SupplierQuoteItem.description,
                SupplierQuoteItem.part_number,
            )
            .join(SupplierQuoteItem, SupplierQuoteItem.quote_id == SupplierQuote.id)
            .where(maybe)
        )).all()
        rows = []
        for name, created, details, contact, line_brand, text, part in candidates:
            points = score(terms, line_brand, f"{text} {part or ''}")
            if points >= 2:
                rows.append((name, created, details, contact, points))
        for name, created, details, contact, points in rows:
            key = (name or "").strip().lower()
            if not key:
                continue
            found = by_name.setdefault(
                key, {"name": name.strip(), "lines": 0, "best": 0, "last": None,
                      "items": set()}
            )
            found["lines"] += 1
            found["best"] = max(found["best"], points)
            found["items"].add(number)
            if found["last"] is None or created > found["last"]:
                d = details or {}
                found.update(
                    last=created,
                    email=_first(d.get("emails")),
                    phone=_first(d.get("phone") or d.get("mobile")),
                    person=_first(d.get("contact_person") or contact),
                    city=_first(d.get("city")), website=_first(d.get("website")),
                )
    ranked = sorted(
        by_name.values(), key=lambda s: (len(s["items"]), s["best"], s["lines"], s["last"]),
        reverse=True,
    )
    return ranked[:OURS_COUNT]


# ── the web ───────────────────────────────────────────────────────────

_AS_SHOWN: Final = "Exactly as a page shows it, or empty."

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["distributor", "reseller", "dealer", "retailer", "online store",
                                 "not stated"],
                        "description": "What the evidence page shows them to be.",
                    },
                    "authorised": {
                        "type": "boolean",
                        "description": "True only when the brand's own site lists them.",
                    },
                    "items_covered": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "The numbers of the items they can supply.",
                    },
                    "city": {"type": "string", "description": "City or emirate, or empty."},
                    "website": {"type": "string", "description": "Their homepage, or empty."},
                    "phone": {"type": "string", "description": _AS_SHOWN},
                    "email": {"type": "string", "description": _AS_SHOWN},
                    "evidence_url": {
                        "type": "string",
                        "description": "The page showing they supply the brand or item.",
                    },
                    "found_on": {
                        "type": "string",
                        "description": "Where: the brand's partner page, a directory's name, a "
                                       "store, or their own site.",
                    },
                },
                "required": ["name", "role", "authorised", "items_covered", "city", "website",
                             "phone", "email", "evidence_url", "found_on"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["companies"],
    "additionalProperties": False,
}

SYSTEM: Final = (
    "You find the companies that can supply a brand or an item in a given place, for the "
    "procurement team of Hamdaz, an IT and security solutions company in the UAE: "
    "distributors and resellers, and for consumer products the shops and online stores that "
    "stock them. You only report what web pages show. A company goes on the list only when a "
    "page you found shows it deals in that brand or sells that item; that page is its "
    "evidence_url. Phone numbers and emails are copied exactly as a page shows them, or left "
    "empty; never guess one."
)


#: Directory sites searched in their own step, by place. The search is kept to
#: these sites, so they are looked at every time, not when the model thinks to.
UAE_DIRECTORIES: Final = (
    "yellowpages-uae.com", "connect.ae", "uaecontact.com", "kompass.com", "dubaichamber.com",
)
OTHER_DIRECTORIES: Final = ("kompass.com",)
#: How the progress message names them.
DIRECTORY_NAMES: Final = {
    "yellowpages-uae.com": "Yellow Pages UAE", "connect.ae": "Connect.ae",
    "uaecontact.com": "UAE Contact", "kompass.com": "Kompass", "dubaichamber.com": "Dubai Chamber",
}
#: Searches each step may run.
WEB_SEARCHES: Final = 5
DIRECTORY_SEARCHES: Final = 4


def directories_for(place: str) -> tuple[str, ...]:
    return UAE_DIRECTORIES if place in _UAE else OTHER_DIRECTORIES


def _numbered(ask: Ask) -> str:
    return "\n".join(f"{n}. {item.line()}" for n, item in enumerate(ask.items, start=1))


def _only_brand(ask: Ask) -> str | None:
    if len(ask.items) == 1 and not ask.items[0].description:
        return ask.items[0].brand
    return None


def _prompt(ask: Ask, known: list[str]) -> str:
    """The open web: the brand's own pages, the stores, the companies' sites."""
    stores = "Amazon.ae, noon.com and the like" if ask.place in _UAE else "online stores"
    skip = f" Leave out these, we know them already: {', '.join(known)}." if known else ""
    if brand := _only_brand(ask):
        return (
            f"Find up to {WEB_COUNT} companies that distribute or resell {brand} in {ask.place}. "
            f"Look first at {brand}'s own partner locator, where-to-buy or distributor pages; "
            f"then the companies' own sites. Authorised distributors first, then authorised "
            f"resellers, then others. items_covered is [1].{skip}"
        )
    return (
        f"Find up to {WEB_COUNT} companies in {ask.place} that can supply these items: "
        f"distributors or resellers of their brands, and shops or online stores that stock "
        f"them.\n{_numbered(ask)}\n\nFor each item look at the brand's own where-to-buy or "
        f"distributor pages, then stores selling in {ask.place} ({stores}) and the companies' "
        f"own sites. A company may cover only some of the items: give their numbers in "
        f"items_covered. Companies covering more items first.{skip}"
    )


def _directory_prompt(ask: Ask, known: list[str]) -> str:
    """The business directories: by brand, and by the kind of product."""
    sites = ", ".join(directories_for(ask.place))
    skip = f" Leave out these, we know them already: {', '.join(known)}." if known else ""
    what = (f"distribute or resell {brand}" if (brand := _only_brand(ask))
            else f"can supply these items:\n{_numbered(ask)}\n")
    return (
        f"Search the business directories ({sites}) for companies in {ask.place} that {what} "
        f"Search each directory by the brand name, and by the category a directory would list "
        f"it under (for example 'photographic equipment', 'audio visual equipment', 'network "
        f"security'). Directory listings rarely name a brand: a company listed under a category "
        f"that fits counts. List up to {WEB_COUNT} companies the directory pages show; found_on "
        f"is '<directory> — <category>' (e.g. 'Yellow Pages UAE — Photographic Equipment'), "
        f"evidence_url the listing or category page. Contacts only as the listing shows them. "
        f"items_covered: the item numbers their category fits, [1] for a brand.{skip}"
    )


DIRECTORY_SYSTEM: Final = (
    "You find companies in business directories for the procurement team of Hamdaz, an IT "
    "and security solutions company in the UAE. You only report what directory pages show: "
    "a company's name, its category, and its contacts exactly as listed, or empty; never "
    "guess one. A company listed under a category that fits what is asked for counts, though "
    "the listing may not name the brand."
)


def _keep(payload: dict[str, Any], ask: Ask, seen: set[str]) -> list[dict]:
    """The companies found, less any already listed; "authorised" only on a
    brand's own word — a company's own site calling itself a distributor is
    its claim, not the brand's."""
    out = []
    for c in payload.get("companies") or []:
        name = str(c.get("name") or "").strip()
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        # Directories show a number cut short, then whole: "04 56...04 5667977".
        phone = str(c.get("phone") or "")
        if "..." in phone or "…" in phone:
            c["phone"] = re.split(r"\.\.\.|…", phone)[-1].strip()
        url = str(c.get("evidence_url") or "")
        c["authorised"] = bool(c.get("authorised")) and any(
            _on_brand_site(url, b) for b in ask.brands
        )
        out.append(c)
    return out


async def from_web(model: AgentModel, ask: Ask, known: list[str], spent: Spent) -> list[dict]:
    payload = await model.search(
        system=SYSTEM, prompt=_prompt(ask, known), schema=SCHEMA,
        user_key="itsupport-chat", spent=spent, max_uses=WEB_SEARCHES,
    )
    return _keep(payload, ask, {re.sub(r"[^a-z0-9]", "", k.lower()) for k in known})[:WEB_COUNT]


async def from_directories(
    model: AgentModel, ask: Ask, known: list[str], spent: Spent
) -> list[dict]:
    payload = await model.search(
        system=DIRECTORY_SYSTEM, prompt=_directory_prompt(ask, known), schema=SCHEMA,
        user_key="itsupport-chat", spent=spent,
        allowed_domains=list(directories_for(ask.place)), max_uses=DIRECTORY_SEARCHES,
    )
    return _keep(payload, ask, {re.sub(r"[^a-z0-9]", "", k.lower()) for k in known})[:WEB_COUNT]


def only_new(directories: list[dict], web: list[dict]) -> list[dict]:
    """The directory listings the web did not already give."""
    seen = {re.sub(r"[^a-z0-9]", "", str(c.get("name") or "").lower()) for c in web}
    return [
        c for c in directories
        if re.sub(r"[^a-z0-9]", "", str(c.get("name") or "").lower()) not in seen
    ]


def _on_brand_site(url: str, brand: str) -> bool:
    """The page is on the brand's own domain (fortinet.com, partnerportal.fortinet.com)."""
    host = urlparse(url).hostname or ""
    first = re.sub(r"[^a-z0-9]", "", (brand.split() or [""])[0].lower())
    return len(first) >= 3 and first in host.replace("-", "")


# ── the reply ─────────────────────────────────────────────────────────


def _e(value: Any) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def _link(url: str, label: str) -> str:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return _e(label) if label else ""
    return f'<a href="{html.escape(url, quote=True)}">{_e(label)}</a>'


def _covers(numbers: Any, ask: Ask) -> str:
    if len(ask.items) < 2:
        return ""
    picked = sorted({n for n in numbers or [] if isinstance(n, int) and 1 <= n <= len(ask.items)})
    if not picked:
        return ""
    if len(picked) == len(ask.items):
        return "all items"
    return ("item " if len(picked) == 1 else "items ") + ", ".join(map(str, picked))


def _company_rows(companies: list[dict[str, Any]], ask: Ask) -> str:
    rows = []
    for c in companies:
        name = _link(c.get("website") or "", c.get("name") or "") or _e(c.get("name"))
        role = "" if c.get("role") == "not stated" else str(c.get("role") or "")
        if role and c.get("authorised"):
            role = f"authorised {role}"
        bits = [f"<b>{name}</b>"] + [
            _e(x) for x in (c.get("city"), role, _covers(c.get("items_covered"), ask)) if x
        ]
        bits += [_e(c.get(k)) for k in ("phone", "email") if c.get(k)]
        source = _link(c.get("evidence_url") or "", c.get("found_on") or "source")
        if source:
            bits.append(f"found on {source}")
        rows.append(f"<li>{' · '.join(bits)}</li>")
    return f"<ul>{''.join(rows)}</ul>"


def reply_html(
    ask: Ask, ours: list[dict[str, Any]], web: list[dict[str, Any]], web_note: str | None,
    listed: list[dict[str, Any]] | None = None,
) -> str:
    listed = listed or []
    parts = [f"Here's what I found for <b>{_e(ask.label)}</b> in {_e(ask.place)}:"]
    if len(ask.items) > 1:
        listed = "".join(f"<li>{_e(i.label)}</li>" for i in ask.items)
        parts.append(f"<ol>{listed}</ol>")
    if ours:
        rows = []
        for s in ours:
            bits = [f"<b>{_e(s['name'])}</b>"]
            if covers := _covers(s.get("items"), ask):
                bits.append(covers)
            if s.get("last"):
                bits.append(f"last quoted us {s['last']:%d %b %Y}")
            bits += [_e(s.get(k)) for k in ("person", "phone", "email") if s.get(k)]
            rows.append(f"<li>{' · '.join(bits)}</li>")
        parts.append(f"<b>Already quoted us</b><ul>{''.join(rows)}</ul>")
    if web:
        parts.append(f"<b>From the web</b>{_company_rows(web, ask)}")
    if listed:
        parts.append(
            "<b>Listed in directories</b> (by category, so check they carry it)"
            f"{_company_rows(listed, ask)}"
        )
    if web_note:
        parts.append(_e(web_note))
    if not ours and not web and not listed and not web_note:
        parts.append("I couldn't find anyone for that. Try the exact brand name, or another "
                     "place?")
    elif web or listed:
        parts.append("<i>Contacts are as their pages show them; worth a quick check before you "
                     "call.</i>")
    return "<br>".join(parts)
