"""Where the assistant can send somebody, and how a name becomes a route.

The assistant answers questions. Sometimes the honest answer is "that is a
screen, and here it is" — a person who asks *show me the quotes* wants the
quotes page, not a paragraph describing it. This is the resolution behind that:
a word or two from the model, turned into one of the app's own routes.

**There is no catalogue here.** Every destination comes from
``app.access.catalogue``, which already names each module's pages and their
frontend paths precisely so one list drives both the permission model and the
navigation. A second list of routes in this file would be a second thing to
forget to update, and the way it would fail — the assistant confidently sending
people to a page that moved — is the way nobody notices for a month.

**Permission comes for free, and that is the point.** What gets resolved is the
caller's *effective access*, so a page somebody may not reach is not a page the
assistant can send them to. It is not hidden and then checked; it was never in
the list. The screen behind it would refuse them anyway — but being bounced by
a page the assistant just opened is a worse experience than being told plainly
that it is not theirs to see.

Pure functions over an already-built access payload, so every case is testable
without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

#: A route parameter as the access catalogue writes it: ``/teams/[slug]/members``.
_PARAM: Final = re.compile(r"\[([a-zA-Z_]+)\]")

#: Pages whose key says they are the module's front door, best first. Asking
#: for "quotes" rather than "quotes.list" is the common case — somebody naming
#: a module has named the obvious screen in it.
#:
#: ``mine`` beats ``overview`` deliberately. Reports has both: ``/reports`` is
#: the list somebody means by "open reports", and ``/reports/overview`` is the
#: cross-team figures screen they would ask for by name.
_FRONT_DOORS: Final[tuple[str, ...]] = (
    "list", "mine", "overview", "board", "home", "all",
)


class PlaceError(Exception):
    """The destination could not be resolved. The message is shown to a person."""


@dataclass(frozen=True, slots=True)
class Place:
    """One screen the caller can reach."""

    #: ``module.page``, which is what the model is asked for.
    key: str
    module_key: str
    module_name: str
    name: str
    #: The frontend route, parameters and all: ``/teams/[slug]/members``.
    path: str
    team_scoped: bool

    @property
    def label(self) -> str:
        """How the destination reads on screen: "Quotes · All quotes"."""
        return f"{self.module_name} · {self.name}"

    @property
    def params(self) -> tuple[str, ...]:
        return tuple(match.group(1) for match in _PARAM.finditer(self.path))


def places_from(access: dict[str, Any]) -> list[Place]:
    """Every screen in an ``/access/me`` payload, flattened.

    Takes the payload rather than a session so the caller decides how access
    was worked out, and so this stays a function of its input — the property
    that makes "can the assistant send me here" answerable in a test.
    """
    out: list[Place] = []
    for module in access.get("modules") or []:
        module_key = str(module.get("key") or "")
        module_name = str(module.get("name") or module_key)
        for page in module.get("pages") or []:
            key = str(page.get("key") or "")
            path = str(page.get("path") or "")
            if not key or not path:
                continue
            out.append(
                Place(
                    key=f"{module_key}.{key}",
                    module_key=module_key,
                    module_name=module_name,
                    name=str(page.get("name") or key),
                    path=path,
                    team_scoped=bool(page.get("team_scoped")),
                )
            )
    return out


def _norm(value: str) -> str:
    """Lower case, with every run of punctuation flattened to one space.

    One form for both sides of every comparison, which is what lets
    ``quote_comparison``, ``quote comparison`` and ``Quote Comparison`` all be
    the same thing. Doing it by stripping separators rather than by swapping
    one for another is the fix for the bug this replaced: turning spaces into
    dots made ``quote comparison`` into ``quote.comparison``, which matched no
    module key, so three whole modules could not be opened by name.
    """
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def resolve(places: list[Place], wanted: str) -> Place:
    """Turn what the model asked for into one screen.

    Tried in order, widest last, because a near-miss that opens the wrong page
    is worse than a refusal the model can correct on its next round:

    1. the exact ``module.page`` key — what the tool asks for;
    2. a bare module key, answered with that module's front door;
    3. the page's own name, as a person would say it ("my overview");
    4. a name that merely contains what was asked, but only if exactly one
       page does. Two candidates is ambiguity, and ambiguity is a refusal.

    Raises ``PlaceError`` listing what the caller *can* reach, so a wrong guess
    comes back as a usable correction rather than a dead end.
    """
    asked = _norm(wanted)
    if not asked:
        raise PlaceError("Name the page to open.")

    by_key = {_norm(p.key): p for p in places}
    if asked in by_key:
        return by_key[asked]

    # A bare module: "quotes" means the quotes page somebody pictures.
    module_pages = [p for p in places if _norm(p.module_key) == asked]
    if module_pages:
        for door in _FRONT_DOORS:
            for page in module_pages:
                if page.key.endswith(f".{door}"):
                    return page
        # Nothing named like a front door — HR, finance and proposals all look
        # like this. The catalogue lists a module's pages in the order somebody
        # thought about them, so the first one that can be opened on its own is
        # a better answer than a refusal. Pages needing a record are skipped:
        # "open HR" cannot mean one particular application.
        for page in module_pages:
            if not page.params:
                return page
        raise PlaceError(
            f"Every page in {module_pages[0].module_name} is about one record, "
            "so there is nothing to open on its own."
        )

    # "project.detail" for "projects.detail" — a singular where the catalogue
    # is plural, which a model does constantly and a person would never notice.
    # Only when exactly one module could be meant: a near miss that picks one
    # of two is the guessing this function exists to avoid.
    head, _, tail = asked.partition(" ")
    if tail:
        near = {
            p.module_key
            for p in places
            if _norm(p.module_key).startswith(head) or head.startswith(_norm(p.module_key))
        }
        if len(near) == 1:
            wanted_key = f"{_norm(near.pop())} {tail}"
            if wanted_key in by_key:
                return by_key[wanted_key]

    spoken = asked
    exact = [p for p in places if _norm(p.name) == spoken]
    if len(exact) == 1:
        return exact[0]

    partial = [p for p in places if spoken and spoken in _norm(p.name)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise PlaceError(
            f"{wanted!r} matches several pages. Ask for one of: "
            + ", ".join(sorted(p.key for p in partial))
        )

    raise PlaceError(
        f"There is no page called {wanted!r} that you can reach. You can open: "
        + ", ".join(sorted({p.module_key for p in places}))
    )


#: What a path segment may contain once a value is substituted into it. Ids
#: here are uuids, keys are short words, and a team handle is a slug — none of
#: which contain a slash. Anything that does is refused rather than escaped:
#: a value that could add a segment could send somebody to a different screen
#: from the one that was resolved and checked.
_SEGMENT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def fill(place: Place, *, team: str | None = None, record: str | None = None) -> str:
    """The route with its parameters filled in.

    Two values can be supplied and they answer different questions. ``team`` is
    the handle of a team-scoped page — "the team's reports" is a different
    screen for each team somebody belongs to. ``record`` is the one thing a
    detail page is about: a project's id, a quote's, a template's key.

    Neither is guessed at. The assistant finds a record's id with the module's
    own tools — which is what makes "open the ADNOC project" a search followed
    by a navigation, both of them visible in the run log, rather than a URL
    somebody hoped was right.

    Anything still unfilled is refused: a route sent with ``[id]`` in it is a
    404 in the browser, and the assistant would have no idea it had done
    anything wrong.
    """
    path = place.path
    if team:
        path = path.replace("[slug]", _segment(team, "team handle"))
    if record:
        value = _segment(record, "record")
        # Every remaining parameter but the team's. A route has at most one:
        # nothing in the catalogue is about two records at once.
        path = re.sub(r"\[(?!slug\])[a-zA-Z_]+\]", value, path)

    missing = _PARAM.findall(path)
    if missing:
        if missing == ["slug"]:
            raise PlaceError(
                f"{place.label} is a team's page — say which team to open it for."
            )
        raise PlaceError(
            f"{place.label} is about one particular record. Find its id first — "
            f"the {place.module_key} tools list them — and pass it as `record`."
        )
    return path


def _segment(value: str, what: str) -> str:
    cleaned = (value or "").strip().strip("/")
    if not _SEGMENT.match(cleaned):
        raise PlaceError(f"That is not a usable {what}.")
    return cleaned


def describe(places: list[Place], path: str) -> Place | None:
    """Which screen a route belongs to, for saying where somebody is.

    The reverse of ``fill``: ``/projects/8973-…/plan`` is the project plan page.
    Matched segment by segment so a parameter swallows exactly one, and the
    candidate matching the most *literal* segments wins. Both halves are
    load-bearing: without the first, ``/projects/portfolio`` matches nothing;
    without the second it matches ``/projects/[id]`` and the assistant tells
    somebody they are looking at a project called "portfolio".
    """
    wanted = [segment for segment in (path or "").split("/") if segment]
    best: Place | None = None
    best_score = -1
    for place in places:
        pattern = [segment for segment in place.path.split("/") if segment]
        if len(pattern) != len(wanted):
            continue
        score = 0
        for expected, actual in zip(pattern, wanted):
            if _PARAM.fullmatch(expected):
                continue
            if expected.lower() != actual.lower():
                break
            score += 1
        else:
            if score > best_score:
                best, best_score = place, score
    return best


def detail_page(places: list[Place], module_key: str) -> Place | None:
    """The page in a module that is about one record.

    Wanted when somebody names a record but the page resolves to a list — "open
    the Hamdaz ERP project" arriving as page='projects' with a record on it,
    which is exactly how a model phrases it. Answering with the list would drop
    the half of the request that mattered.

    ``.detail`` first, then anything else carrying a record parameter, so a
    module that names its page differently still works.
    """
    candidates = [
        place
        for place in places
        if place.module_key == module_key
        and any(param != "slug" for param in place.params)
    ]
    for place in candidates:
        if place.key.endswith(".detail"):
            return place
    return candidates[0] if candidates else None
