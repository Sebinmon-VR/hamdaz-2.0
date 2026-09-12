"""Turning what somebody called a thing into the id of that thing.

The property worth holding hardest: opening *a* project when somebody asked for
a particular one is worse than refusing, because it looks like success. So
every widening pass has to land on exactly one row, and ambiguity comes back
with the candidates rather than picking the first.

Pure over a list of dicts — the rows come from the module's own list route, and
what happens to them afterwards is all here.
"""

from __future__ import annotations

import pytest

from app.assistant.records import (
    FINDERS,
    RecordError,
    label_of,
    looks_like_id,
    match,
    rows_of,
)

PROJECTS = FINDERS["projects"]

ROWS = [
    {"id": "d7cf", "name": "Vakatel Address Book", "code": "VAKATEL"},
    {"id": "8973", "name": "H7 LMS", "code": "H7LMS"},
    {"id": "9d70", "name": "Hamdaz ERP", "code": "ERP"},
]


def _find(wanted: str) -> str:
    return label_of(PROJECTS, match(PROJECTS, ROWS, wanted))


# ── telling an id from a name ──────────────────────────────────────────


def test_a_uuid_is_used_as_given() -> None:
    assert looks_like_id("9d705129-57fe-419a-a81b-68b999f84cc9")


def test_a_name_is_not_an_id() -> None:
    for value in ("Hamdaz ERP", "h7", "", "   ", "9d705129"):
        assert not looks_like_id(value)


# ── finding the row ────────────────────────────────────────────────────


def test_the_exact_name_wins() -> None:
    assert _find("Hamdaz ERP") == "Hamdaz ERP"


def test_case_and_punctuation_do_not_matter() -> None:
    assert _find("hamdaz  erp") == "Hamdaz ERP"


def test_a_code_finds_it_too() -> None:
    assert _find("ERP") == "Hamdaz ERP"


def test_part_of_the_name_is_enough_when_only_one_could_be_meant() -> None:
    assert _find("address book") == "Vakatel Address Book"


def test_the_words_a_person_actually_says_still_land() -> None:
    """A model passes the phrasing through, filler and all.

    "the h7 lms one" contains the name rather than being contained by it, which
    is why the match is tried in both directions.
    """
    assert _find("the h7 lms one") == "H7 LMS"
    assert _find("the vakatel address book project") == "Vakatel Address Book"
    assert _find("open h7 lms for me") == "H7 LMS"


def test_something_that_matches_nothing_is_refused_without_a_guess() -> None:
    with pytest.raises(RecordError) as caught:
        _find("nonsense")
    assert "Do not guess" in str(caught.value)


def test_a_word_that_describes_every_row_is_refused() -> None:
    """"Open the project" names no project."""
    with pytest.raises(RecordError):
        _find("the project")


def test_nothing_asked_for_is_refused() -> None:
    with pytest.raises(RecordError):
        _find("   ")


def test_two_candidates_come_back_with_their_ids() -> None:
    """So the assistant can ask which, rather than opening one of them."""
    rows = [
        {"id": "a1", "name": "Site survey north"},
        {"id": "b2", "name": "Site survey south"},
    ]
    with pytest.raises(RecordError) as caught:
        match(PROJECTS, rows, "site survey")

    message = str(caught.value)
    assert "a1" in message and "b2" in message
    assert "Ask which one" in message


# ── reading the list route's answer ────────────────────────────────────


def test_rows_are_read_out_of_a_wrapped_page() -> None:
    assert rows_of(PROJECTS, {"projects": ROWS, "total": 3}) == ROWS


def test_rows_are_read_out_of_a_bare_array() -> None:
    finder = FINDERS["quote_requests"]
    assert rows_of(finder, [{"id": "1", "title": "A"}]) == [{"id": "1", "title": "A"}]


def test_an_empty_or_odd_answer_is_no_rows_rather_than_an_error() -> None:
    assert rows_of(PROJECTS, None) == []
    assert rows_of(PROJECTS, {}) == []
    assert rows_of(PROJECTS, {"projects": None}) == []
    assert rows_of(PROJECTS, {"projects": ["not a row", {"id": "1"}]}) == [{"id": "1"}]


def test_every_finder_names_a_field_to_match_and_a_route() -> None:
    """A finder without these cannot do anything, and would fail at use."""
    for key, finder in FINDERS.items():
        assert finder.path.startswith("/"), key
        assert finder.names, key
