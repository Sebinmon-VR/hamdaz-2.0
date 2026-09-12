"""Where the assistant may send somebody, decided without a database.

Two properties are worth holding hard here. The first is that a near-miss
refuses rather than guesses: an assistant that opens *a* page when asked for a
particular one is worse than one that asks again, because the person has lost
their place and has no idea why. The second is that the destinations are the
caller's own access — a page somebody cannot reach is not hidden and then
checked, it was never a candidate.

All of it is functions over an ``/access/me`` payload, so every case runs in
milliseconds against a dict.
"""

from __future__ import annotations

import pytest

from app.assistant.places import (
    Place,
    PlaceError,
    describe,
    fill,
    places_from,
    resolve,
)


def _access(*modules: dict) -> dict:
    return {"source": "grants", "modules": list(modules), "via_teams": []}


def _module(key: str, name: str, *pages: tuple[str, str, str] | tuple[str, str, str, bool]) -> dict:
    return {
        "key": key,
        "name": name,
        "admin_only": False,
        "pages": [
            {
                "key": page[0],
                "name": page[1],
                "path": page[2],
                "team_scoped": page[3] if len(page) > 3 else False,
            }
            for page in pages
        ],
    }


ACCESS = _access(
    _module(
        "dashboard",
        "Dashboard",
        ("overview", "My overview", "/dashboard"),
        ("team", "Team dashboard", "/teams/[slug]/dashboard", True),
    ),
    _module("quotes", "Quotes", ("list", "All quotes", "/quotes"), ("one", "One quote", "/quotes/[id]")),
    _module("leave", "Leave", ("mine", "My leave", "/leave")),
    _module("reports", "Reports", ("mine", "My reports", "/reports"), ("overview", "Overview", "/reports/overview")),
)

PLACES = places_from(ACCESS)


# ── reading the payload ────────────────────────────────────────────────


def test_every_page_in_the_payload_becomes_a_place() -> None:
    assert {p.key for p in PLACES} == {
        "dashboard.overview",
        "dashboard.team",
        "quotes.list",
        "quotes.one",
        "leave.mine",
        "reports.mine",
        "reports.overview",
    }


def test_a_place_reads_as_a_person_would_say_it() -> None:
    place = next(p for p in PLACES if p.key == "quotes.list")
    assert place.label == "Quotes · All quotes"


def test_a_page_without_a_path_is_not_a_destination() -> None:
    """The catalogue carries a few entries the frontend has no route for."""
    places = places_from(_access(_module("x", "X", ("a", "A", ""))))
    assert places == []


# ── resolving what the model asked for ─────────────────────────────────


def test_the_exact_key_wins() -> None:
    assert resolve(PLACES, "reports.overview").path == "/reports/overview"


def test_a_bare_module_opens_the_page_somebody_pictures() -> None:
    """"Show me the quotes" means the list, not a particular quote."""
    assert resolve(PLACES, "quotes").key == "quotes.list"
    assert resolve(PLACES, "leave").key == "leave.mine"
    assert resolve(PLACES, "dashboard").key == "dashboard.overview"


def test_a_page_can_be_asked_for_by_its_name() -> None:
    assert resolve(PLACES, "My leave").key == "leave.mine"


def test_asking_loosely_still_lands_when_only_one_page_could_be_meant() -> None:
    assert resolve(PLACES, "team dashboard").key == "dashboard.team"


def test_spacing_and_case_do_not_matter() -> None:
    assert resolve(PLACES, "  Reports.Overview ").key == "reports.overview"
    assert resolve(PLACES, "reports overview").key == "reports.overview"


def test_a_singular_module_name_still_lands() -> None:
    """Models write "project.detail" constantly. A person never would.

    Only when one module could be meant — picking one of two would be exactly
    the guessing the rest of this function exists to avoid.
    """
    assert resolve(PLACES, "quote.one").key == "quotes.one"
    assert resolve(PLACES, "report.overview").key == "reports.overview"


def test_a_near_module_name_that_could_mean_two_things_is_refused() -> None:
    places = places_from(
        _access(
            _module("quotes", "Quotes", ("list", "All", "/quotes")),
            _module("quote_requests", "Quote Requests", ("list", "All", "/quote-requests")),
        )
    )
    with pytest.raises(PlaceError):
        resolve(places, "quote.list")


def test_a_module_key_with_an_underscore_can_be_asked_for_any_way() -> None:
    """Three modules are named like this, and none of them could be opened.

    The normaliser used to swap spaces for dots, which turned
    "quote comparison" into "quote.comparison" — a module key that does not
    exist. Flattening both sides instead is what makes all four spellings the
    same question.
    """
    places = places_from(
        _access(_module("quote_comparison", "Quote Comparison", ("list", "All", "/comparisons")))
    )
    for spelling in ("quote_comparison", "quote comparison", "Quote Comparison", "quote-comparison"):
        assert resolve(places, spelling).key == "quote_comparison.list", spelling
    assert resolve(places, "quote_comparison.list").key == "quote_comparison.list"


def test_an_exact_name_beats_a_page_that_merely_contains_it() -> None:
    """"Overview" is the name of one page and part of another's.

    Both are plausible, and the tie is broken by exactness rather than by
    order — the page actually called Overview is the one somebody saying
    "overview" means, and the one called "My overview" answers to that.
    """
    assert resolve(PLACES, "overview").key == "reports.overview"
    assert resolve(PLACES, "my overview").key == "dashboard.overview"


def test_a_genuinely_ambiguous_name_is_refused_with_the_candidates() -> None:
    """Two modules, one page name. Opening either loses somebody's place.

    This is the case exactness cannot break, and the only honest answer is to
    hand the candidates back and let the model ask.
    """
    places = places_from(
        _access(
            _module("quotes", "Quotes", ("board", "Approvals", "/quotes/approvals")),
            _module("hr", "HR", ("reviews", "Approvals", "/hr/approvals")),
        )
    )
    with pytest.raises(PlaceError) as caught:
        resolve(places, "approvals")

    message = str(caught.value)
    assert "quotes.board" in message and "hr.reviews" in message


def test_a_module_with_no_obvious_front_door_opens_its_first_real_page() -> None:
    """HR, finance and proposals all look like this — no page called "list".

    The catalogue lists a module's pages in the order somebody thought about
    them, so the first openable one is a better answer than a refusal.
    """
    places = places_from(
        _access(_module("hr", "HR", ("docs", "Documents", "/hr/docs"), ("jobs", "Jobs", "/hr/jobs")))
    )
    assert resolve(places, "hr").key == "hr.docs"


def test_the_front_door_skips_pages_that_need_a_record() -> None:
    """"Open HR" cannot mean one particular application."""
    places = places_from(
        _access(_module("hr", "HR", ("one", "An application", "/hr/[id]"), ("jobs", "Jobs", "/hr/jobs")))
    )
    assert resolve(places, "hr").key == "hr.jobs"


def test_the_list_is_preferred_to_the_overview() -> None:
    """Reports has both, and "open reports" means the list."""
    places = places_from(
        _access(
            _module(
                "reports",
                "Reports",
                ("mine", "My reports", "/reports"),
                ("overview", "Overview", "/reports/overview"),
            )
        )
    )
    assert resolve(places, "reports").key == "reports.mine"


def test_a_module_of_nothing_but_records_says_so() -> None:
    places = places_from(_access(_module("hr", "HR", ("one", "An application", "/hr/[id]"))))
    with pytest.raises(PlaceError) as caught:
        resolve(places, "hr")

    assert "one record" in str(caught.value)


def test_a_module_with_exactly_one_page_needs_no_front_door() -> None:
    places = places_from(_access(_module("hr", "HR", ("jobs", "Jobs", "/hr/jobs"))))
    assert resolve(places, "hr").key == "hr.jobs"


def test_somewhere_this_person_cannot_reach_is_not_a_destination() -> None:
    """The refusal names what they *can* open, so the model corrects itself."""
    with pytest.raises(PlaceError) as caught:
        resolve(PLACES, "finance")

    message = str(caught.value)
    assert "finance" in message
    assert "quotes" in message and "leave" in message


def test_nothing_asked_for_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(PlaceError):
        resolve(PLACES, "   ")


# ── filling the route in ───────────────────────────────────────────────


def test_a_plain_route_needs_nothing() -> None:
    assert fill(resolve(PLACES, "quotes")) == "/quotes"


def test_a_team_page_takes_the_team_handle() -> None:
    assert fill(resolve(PLACES, "dashboard.team"), team="presales") == "/teams/presales/dashboard"


def test_a_team_page_without_a_team_says_so() -> None:
    """Rather than sending somebody to a URL with a bracket in it."""
    with pytest.raises(PlaceError) as caught:
        fill(resolve(PLACES, "dashboard.team"))

    assert "which team" in str(caught.value)


def test_a_page_about_one_record_needs_that_record() -> None:
    with pytest.raises(PlaceError) as caught:
        fill(resolve(PLACES, "quotes.one"))

    # The refusal says what to do about it, because the model is the reader.
    assert "record" in str(caught.value)


def test_a_record_fills_the_detail_route() -> None:
    """"Open the ADNOC project" is a search, then this."""
    assert fill(resolve(PLACES, "quotes.one"), record="9f2b-71") == "/quotes/9f2b-71"


def test_a_record_cannot_smuggle_in_another_segment() -> None:
    """A value that could add a slash could open a screen nobody resolved."""
    for bad in ("../admin", "a/b", "", "  ", "?x=1"):
        with pytest.raises(PlaceError):
            fill(resolve(PLACES, "quotes.one"), record=bad)


def test_a_team_handle_is_held_to_the_same_rule() -> None:
    with pytest.raises(PlaceError):
        fill(resolve(PLACES, "dashboard.team"), team="presales/../admin")


# ── saying where somebody is ───────────────────────────────────────────


def test_a_route_is_recognised_as_the_screen_it_belongs_to() -> None:
    assert describe(PLACES, "/reports/overview").key == "reports.overview"
    assert describe(PLACES, "/leave").key == "leave.mine"


def test_a_detail_route_is_recognised_whatever_the_id_is() -> None:
    assert describe(PLACES, "/quotes/9f2b-71").key == "quotes.one"


def test_a_literal_segment_beats_a_parameter() -> None:
    """Without this, /reports/overview reads as a report called "overview"."""
    places = places_from(
        _access(
            _module(
                "reports",
                "Reports",
                ("detail", "One report", "/reports/[id]"),
                ("overview", "Overview", "/reports/overview"),
            )
        )
    )
    assert describe(places, "/reports/overview").key == "reports.overview"
    assert describe(places, "/reports/abc").key == "reports.detail"


def test_a_route_that_is_not_ours_is_not_described() -> None:
    assert describe(PLACES, "/somewhere-else") is None
    assert describe(PLACES, "") is None


def test_a_route_with_the_wrong_number_of_segments_is_not_described() -> None:
    assert describe(PLACES, "/quotes/9f2b-71/extra") is None


def test_a_handle_with_slashes_in_it_cannot_escape_the_route() -> None:
    place = Place(
        key="dashboard.team",
        module_key="dashboard",
        module_name="Dashboard",
        name="Team dashboard",
        path="/teams/[slug]/dashboard",
        team_scoped=True,
    )
    assert fill(place, team="/presales/") == "/teams/presales/dashboard"
