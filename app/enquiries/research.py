"""New items, looked up on the web: who makes them, who sells them, roughly what for.

Only for lines the history has never met. They are sent in small batches, so
one answer stays short enough to be complete and a failed batch costs a few
lines rather than the run. What comes back is marked as from the web wherever
it is shown: a price found on a distributor's page is a guide, not a quote.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger("hamdaz.enquiries")

BATCH: Final = 6

_SUPPLIER: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "role": {"type": "string", "enum": ["manufacturer", "distributor", "reseller", "unknown"]},
        "website": {"type": "string", "description": "Homepage, or empty."},
        "email": {"type": "string", "description": "A sales address published on their site, or empty. Never guessed."},
        "phone": {"type": "string", "description": "With country code, or empty."},
        "country": {"type": "string", "description": "Where this office is, or empty."},
        "evidence": {"type": "string", "description": "The page or fact that says they supply this, in one line."},
    },
    "required": ["name", "role", "website", "email", "phone", "country", "evidence"],
    "additionalProperties": False,
}

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "The number the item was given."},
                    "manufacturer": {"type": "string", "description": "Who makes it, or empty."},
                    "manufacturer_website": {"type": "string"},
                    "product_url": {"type": "string", "description": "The product or datasheet page, or empty."},
                    "price_low": {"type": "number", "description": "Lowest unit price seen, 0 if none."},
                    "price_high": {"type": "number", "description": "Highest unit price seen, 0 if none."},
                    "currency": {"type": "string", "description": "ISO code of those prices, or empty."},
                    "price_source": {"type": "string", "description": "Where the price was seen, or empty."},
                    "suppliers": {"type": "array", "items": _SUPPLIER},
                    "notes": {"type": "string", "description": "Lead times, export limits, end of life, alternatives."},
                    "confidence": {"type": "number", "description": "0 to 1: how sure the identification is."},
                },
                "required": [
                    "index", "manufacturer", "manufacturer_website", "product_url", "price_low",
                    "price_high", "currency", "price_source", "suppliers", "notes", "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

INSTRUCTIONS: Final = """\
You research products for Hamdaz, a trading and supply company in Dubai, UAE, \
that is preparing a bid. For each numbered item, search the web and find:
who manufactures it; up to four companies that can supply it, preferring \
authorised distributors in the UAE, then the GCC, then the manufacturer \
itself; and any published unit price.

Report only what you found on a page. Leave a field empty, or 0 for a price, \
rather than guess. Never make up an email address or a phone number. Name \
the page that shows each supplier carries the product."""


@dataclass(slots=True)
class Lookup:
    index: int
    description: str
    part_number: str | None
    brand: str | None
    specification: str | None


def prompt_for(batch: list[Lookup], customer: str | None) -> str:
    lines = [f"The end customer is {customer}." if customer else "", "Items:"]
    for item in batch:
        parts = [f"{item.index}. {item.description}"]
        if item.part_number:
            parts.append(f"part number {item.part_number}")
        if item.brand:
            parts.append(f"brand {item.brand}")
        if item.specification:
            parts.append(f"spec: {item.specification[:300]}")
        lines.append("; ".join(parts))
    return "\n".join(line for line in lines if line)


def parse(raw: str) -> dict[int, dict[str, Any]]:
    """The answer per item number. A malformed answer is no answer."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for item in (payload or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        suppliers = []
        for s in item.get("suppliers") or []:
            if isinstance(s, dict) and str(s.get("name") or "").strip():
                suppliers.append(
                    {
                        "name": str(s["name"]).strip()[:200],
                        "source": "web",
                        "role": s.get("role") or "unknown",
                        "website": s.get("website") or None,
                        "email": s.get("email") or None,
                        "phone": s.get("phone") or None,
                        "country": s.get("country") or None,
                        "evidence": s.get("evidence") or None,
                        "last_rate": None,
                        "currency": None,
                        "last_date": None,
                        "partner": None,
                    }
                )
        low, high = _price(item.get("price_low")), _price(item.get("price_high"))
        currency = (str(item.get("currency") or "").strip().upper()[:3] or None) if (low or high) else None
        out[index] = {
            "manufacturer": item.get("manufacturer") or None,
            "manufacturer_website": item.get("manufacturer_website") or None,
            "product_url": item.get("product_url") or None,
            "price_low": low,
            "price_high": high or low,
            "currency": currency,
            "price_source": item.get("price_source") or None,
            "notes": item.get("notes") or None,
            "confidence": _confidence(item.get("confidence")),
            "suppliers": suppliers,
        }
    return out


def _price(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:.2f}" if number > 0 else None


def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, round(number, 2)))
