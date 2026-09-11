"""The administration console's catalogue, checked without a database.

The console is what a frontend builds the admin screens from, so the things
worth pinning are the ones a screen would silently get wrong: a path that does
not exist, a role key that is not a role, a section with no way in.
"""

from __future__ import annotations

import pytest

from app.admin.catalogue import PERMISSION_RULES, SECTIONS, SECTIONS_BY_KEY
from app.roles.catalogue import SYSTEM_ROLES


def test_every_section_has_a_way_in() -> None:
    for section in SECTIONS:
        assert section.endpoints, f"{section.key} has no endpoints"
        assert section.description


def test_every_path_is_under_the_api_prefix() -> None:
    """A frontend concatenates these. A path missing the prefix 404s at run
    time and looks like a backend fault."""
    for section in SECTIONS:
        for endpoint in section.endpoints:
            assert endpoint.path.startswith("/api/v1/"), endpoint.path


def test_every_endpoint_names_a_real_method() -> None:
    allowed = {"GET", "POST", "PATCH", "PUT", "DELETE"}
    for section in SECTIONS:
        for endpoint in section.endpoints:
            assert endpoint.method in allowed, f"{endpoint.method} {endpoint.path}"


def test_writes_are_marked_and_reads_are_not() -> None:
    """A frontend styles these differently, and a reader should be able to see
    at a glance how much of a section changes anything."""
    for section in SECTIONS:
        for endpoint in section.endpoints:
            changes = endpoint.method != "GET"
            assert endpoint.writes is changes, f"{endpoint.method} {endpoint.path}"


def test_section_keys_are_unique() -> None:
    keys = [s.key for s in SECTIONS]
    assert len(set(keys)) == len(keys)
    assert set(SECTIONS_BY_KEY) == set(keys)


def test_the_irreversible_switch_is_called_out() -> None:
    """Writing to the live Proposals list is the one thing here that cannot be
    undone, so the section carrying it must say so before it is opened."""
    caution = SECTIONS_BY_KEY["intake"].caution or ""
    assert "create_in_sharepoint" in caution
    assert "live" in caution.lower()


def test_the_mirror_says_it_only_reads() -> None:
    assert "writes nothing" in (SECTIONS_BY_KEY["mirror"].caution or "").lower()


@pytest.mark.parametrize("key", ["intake", "mirror", "standing", "reports"])
def test_the_configuring_sections_are_super_admin_only(key: str) -> None:
    assert SECTIONS_BY_KEY[key].audience == "super_admin"


def test_notifications_are_everybodys_own() -> None:
    assert SECTIONS_BY_KEY["notifications"].audience == "everyone"


# ── the permission rules ───────────────────────────────────────────────


def test_every_named_role_is_a_real_role() -> None:
    """A rule naming a role that does not exist would list no holders and read
    as "nobody can do this", which is the confusing way to be wrong."""
    known = {r.key for r in SYSTEM_ROLES}
    for rule in PERMISSION_RULES:
        for key in rule.who:
            assert key in known, f"{rule.area}: {key!r} is not a role"


def test_a_rule_with_no_roles_explains_itself() -> None:
    """Where the answer is not a role — "its author", "anyone on the team" —
    the note is the only thing a screen can print."""
    for rule in PERMISSION_RULES:
        if not rule.who:
            assert rule.note, f"{rule.area}: {rule.what} has neither roles nor a note"


def test_every_rule_belongs_to_a_section() -> None:
    for rule in PERMISSION_RULES:
        assert rule.area in SECTIONS_BY_KEY, rule.area


def test_the_sharpest_powers_are_super_admin_alone() -> None:
    """Turning on live writes, and deleting somebody's filed report."""
    for rule in PERMISSION_RULES:
        if "live Proposals list" in rule.what or "Delete a submitted" in rule.what:
            assert rule.who == ("super_admin",), rule.what


def test_reading_a_draft_is_nobody_elses_business() -> None:
    """Not a CEO, not a super admin — so the rule must name no role at all."""
    rule = next(r for r in PERMISSION_RULES if r.what == "Read a draft")
    assert rule.who == ()
    assert "author" in (rule.note or "").lower()


def test_reports_are_readable_by_the_oversight_roles() -> None:
    rule = next(
        r for r in PERMISSION_RULES if r.what.startswith("Read somebody else's")
    )
    assert {"team_manager", "team_lead", "manager", "ceo", "super_admin"} == set(rule.who)
