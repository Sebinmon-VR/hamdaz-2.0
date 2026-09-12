"""Turning what somebody called a thing into the id of that thing.

"Open the Hamdaz ERP project" names a record the way a person does — by its
name — and every detail route in the app is addressed by id. Something has to
bridge that, and the honest options are two: make the model search first and
then navigate, or do the search inside the navigation.

**It is done here, and that is a decision about reliability rather than about
elegance.** Asking the model to look an id up first is one instruction among
many, and a model that skips it does not fail loudly — it opens the list
instead and says it opened the record, which is worse than an error because it
reads as success. Doing the lookup inside ``app.open`` removes the step that
could be skipped. It is also faster: one round trip instead of two, which is
the difference between an app that moves when you speak and one that thinks
about it first.

**Nothing here queries a table.** Each finder is one of the app's own list
routes, called in-process through the same executor the assistant's tools go
through, with the caller's own session. So a record somebody may not see is a
record that does not come back, by exactly the rules the screen would apply —
there is no second copy of anybody's visibility to keep in step. That is the
same argument ``app.assistant.executor`` makes, for the same reason.

Adding a module is four lines of ``Finder``. What cannot be added this way is a
module whose list route has no human-readable name on it — reports are
identified by a team and a period rather than by a name, so "open the report"
is answered by the period, not by this.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final


class RecordError(Exception):
    """The record could not be identified. The message is written for the model."""


@dataclass(frozen=True, slots=True)
class Finder:
    """How to search one module's list route for a record by name."""

    #: The route, relative to the API prefix.
    path: str
    #: Where the rows are in the response. Empty for a bare array.
    rows_key: str | None
    #: Fields to match against, best first. The first is what gets reported
    #: back, so it should be the one a person would recognise.
    names: tuple[str, ...]
    #: Fixed query parameters — usually a generous limit, since the match is
    #: made here rather than by the route.
    params: dict[str, str] = field(default_factory=dict)


#: One entry per module whose detail pages are reachable by name.
FINDERS: Final[dict[str, Finder]] = {
    "projects": Finder(
        "/projects",
        "projects",
        ("name", "code", "label"),
        {"limit": "200", "include_archived": "true"},
    ),
    "quote_requests": Finder(
        "/quote-requests",
        None,
        ("title", "reference", "customer_name"),
        {"limit": "200"},
    ),
    "quote_comparison": Finder(
        "/comparisons",
        None,
        ("title", "reference"),
        {"limit": "200"},
    ),
    "quotes": Finder(
        "/quotes",
        "quotes",
        ("number", "reference_number", "customer_name"),
        {"limit": "200"},
    ),
}

#: What an id looks like: a uuid, or the short reference a person might copy
#: out of a screen. Anything matching this is used as given rather than
#: searched for — the model usually does have the id, and a lookup it does not
#: need is a second of somebody's time.
_ID: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def looks_like_id(value: str) -> bool:
    if _ID.match(value.strip()):
        return True
    try:
        uuid.UUID(value.strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _text(row: Any, key: str) -> str:
    value = row.get(key) if isinstance(row, dict) else None
    return str(value).strip() if value not in (None, "") else ""


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def rows_of(finder: Finder, payload: Any) -> list[dict[str, Any]]:
    body = payload if finder.rows_key is None else (payload or {}).get(finder.rows_key)
    return [row for row in (body or []) if isinstance(row, dict)]


def match(finder: Finder, rows: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """The one row that is what somebody meant, or a refusal saying why not.

    Exact before partial, and a partial match only counts when exactly one row
    has it. Opening *a* project when somebody asked for a particular one is the
    failure this whole module exists to prevent, so ambiguity is handed back
    with the candidates rather than resolved by picking the first.
    """
    asked = _norm(wanted)
    if not asked:
        raise RecordError("Name the record to open.")
    spoken = set(asked.split())

    def names(row: dict[str, Any]) -> list[str]:
        return [_norm(_text(row, key)) for key in finder.names if _text(row, key)]

    # Tried in order, each one wider than the last, and every one of them has
    # to land on exactly one row. The middle two are not symmetric by accident:
    # a model passes on the words a person used, filler and all — "the h7 lms
    # one", "the vakatel address book project" — so the name being inside what
    # was asked matters as much as the other way round.
    passes = (
        lambda found: any(name == asked for name in found),
        lambda found: any(asked in name for name in found),
        lambda found: any(name and name in asked for name in found),
        # Last resort: every word of the name was said, somewhere. Catches
        # "open h7 lms for me" without matching an unrelated record.
        lambda found: any(name and set(name.split()) <= spoken for name in found),
    )
    for attempt in passes:
        hits = [row for row in rows if attempt(names(row))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RecordError(_several(finder, hits, wanted))

    raise RecordError(
        f"Nothing called {wanted!r} was found, or it is not yours to see. "
        "Do not guess an id — say you could not find it."
    )


def _several(finder: Finder, rows: list[dict[str, Any]], wanted: str) -> str:
    named = ", ".join(
        f"{_label(finder, r)} ({r.get('id')})" for r in rows[:8]
    )
    return (
        f"{wanted!r} matches more than one: {named}. "
        "Ask which one, or call again with the id."
    )


def _label(finder: Finder, row: dict[str, Any]) -> str:
    for key in finder.names:
        text = _text(row, key)
        if text:
            return text
    return str(row.get("id", "?"))


def label_of(finder: Finder, row: dict[str, Any]) -> str:
    return _label(finder, row)
