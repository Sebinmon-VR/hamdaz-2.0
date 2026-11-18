"""Customer POs, from the project tracker list (features projects, delivery_watch).

The company's customer POs live in the "projects track" SharePoint list on the
ProposalTeam site (memory: project-tracker-list); the app has no copy of it
yet. Read here, cached a few minutes, only the columns about the order and
its delivery: no GP and no loans (the user: no finance through the agent).

* :func:`find` answers "where's the ADNOC Gas PO?" (super admins only);
* :func:`due_deliveries` finds POs whose delivery date is near or just past
  and not marked delivered, for their sales person.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from typing import Any, Final

from app.core.config import Settings
from app.proposals.analytics import parse_when

LIST_ID: Final = "c068753b-27d1-495a-a547-d5c0d90ebcc3"
#: The columns read (no GP, no loans).
COLUMNS: Final = ("Title,Ref,Customer,SalesPerson,Status,CustomerPO,PODate,POValue,"
                  "PODeliveryDate,DeliveryExpected,DeliveredOn,SalesOrder,PaymentTerms,"
                  "InvoiceStatus,InvoiceNumber,PaymentStatus,Stage,Progress,Concerns,Remarks,"
                  "ProjectType")
CACHE_SECONDS: Final = 300.0
#: A delivery this close (days ahead) or this far past, not delivered, is raised.
AHEAD: Final = 3
BEHIND: Final = 2

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


@dataclass(frozen=True, slots=True)
class Po:
    item_id: str
    fields: dict[str, Any]

    def get(self, name: str) -> Any:
        return self.fields.get(name)

    @property
    def ref(self) -> str:
        return str(self.get("Ref") or self.item_id)

    @property
    def delivery(self) -> date | None:
        for name in ("DeliveryExpected", "PODeliveryDate"):
            if (when := parse_when(self.get(name))) is not None:
                # A date-only column: its UTC date is the day (stored at midnight).
                return when.astimezone(UTC).date() if when.hour == 0 else \
                    (when + timedelta(hours=12)).astimezone(UTC).date()
        return None

    @property
    def delivered(self) -> bool:
        status = str(self.get("Status") or "").casefold()
        stage = str(self.get("Stage") or "").casefold()
        return (bool(self.get("DeliveredOn")) or ("deliver" in status and "not" not in status)
                or "cancel" in status or "cancel" in stage)


async def rows(sharepoint: Any, settings: Settings) -> list[Po]:
    hit = _cache.get("rows")
    if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
        items = hit[1]
    else:
        items = await sharepoint.items_of(settings.sharepoint_site_id, LIST_ID, select=COLUMNS)
        _cache["rows"] = (time.monotonic(), items)
    return [Po(str(i.get("id")), i.get("fields") or {}) for i in items]


def find(found: list[Po], query: str, limit: int = 8) -> list[dict[str, Any]]:
    """POs matching words or numbers in any of ref, PO no., customer, title, SO."""
    words = [w.casefold() for w in query.replace(",", " ").split() if len(w) >= 3]
    if not words:
        return []
    scored = []
    for po in found:
        hay = " ".join(str(po.get(k) or "") for k in
                       ("Ref", "CustomerPO", "Customer", "Title", "SalesOrder",
                        "SalesPerson")).casefold()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, po))
    scored.sort(key=lambda x: (-x[0], x[1].ref), reverse=False)
    return [said(po) for _, po in scored[:limit]]


def said(po: Po) -> dict[str, Any]:
    out = {k: po.get(k) for k in COLUMNS.split(",") if po.get(k) not in (None, "")}
    out["Ref"] = po.ref
    return out


def due_deliveries(found: list[Po], today: date) -> list[tuple[Po, date]]:
    out = []
    for po in found:
        day = po.delivery
        if day is None or po.delivered:
            continue
        if -BEHIND <= (day - today).days <= AHEAD:
            out.append((po, day))
    return sorted(out, key=lambda x: x[1])

