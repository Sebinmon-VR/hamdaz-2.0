"""Constraint C2 — live SharePoint must never be written to.

SharePoint is in production. If these tests fail, the build must not ship.
"""

from __future__ import annotations

import pytest

from app.connectors.sharepoint.guard import (
    READ_ONLY_METHODS,
    SharePointWriteForbidden,
    SharePointWriteGuard,
)

SANDBOX_ID = "hamdaz1.sharepoint.com,sandbox-guid,web-guid"
LIVE_PROPOSALS_ID = "hamdaz1.sharepoint.com,proposalteam-guid,web-guid"
#: Named "Test" but holds live config the production app reads on every boot.
LIVE_TEST_ID = "hamdaz1.sharepoint.com,test-guid,web-guid"


def _guard(*, writes: bool = True, sandbox: str | None = SANDBOX_ID) -> SharePointWriteGuard:
    return SharePointWriteGuard(sandbox_site_id=sandbox, writes_enabled=writes)


class TestReadsAlwaysPass:
    @pytest.mark.parametrize("method", sorted(READ_ONLY_METHODS))
    @pytest.mark.parametrize("site", [SANDBOX_ID, LIVE_PROPOSALS_ID, LIVE_TEST_ID])
    def test_reads_are_permitted_everywhere(self, method: str, site: str) -> None:
        _guard(writes=False).check(method, site)

    def test_method_casing_is_normalised(self) -> None:
        _guard(writes=False).check("get", LIVE_PROPOSALS_ID)
        _guard(writes=False).check(" GeT ", LIVE_PROPOSALS_ID)


class TestWritesToLiveSitesAreRefused:
    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "MERGE"])
    def test_live_proposals_list_is_never_writable(self, method: str) -> None:
        with pytest.raises(SharePointWriteForbidden) as exc:
            _guard().check(method, LIVE_PROPOSALS_ID)
        assert "only the sandbox site" in str(exc.value)

    def test_sites_test_is_refused_despite_its_name(self) -> None:
        """/sites/Test is live: it holds superusers, approvers and excludeusers."""
        with pytest.raises(SharePointWriteForbidden):
            _guard().check("POST", LIVE_TEST_ID)

    def test_lowercase_write_verbs_are_still_writes(self) -> None:
        with pytest.raises(SharePointWriteForbidden):
            _guard().check("post", LIVE_PROPOSALS_ID)

    def test_error_names_the_constraint(self) -> None:
        """A future maintainer hitting this should learn why without archaeology."""
        with pytest.raises(SharePointWriteForbidden) as exc:
            _guard().check("PATCH", LIVE_PROPOSALS_ID)
        message = str(exc.value)
        assert "C2" in message
        assert "read-only" in message


class TestSandboxWrites:
    def test_sandbox_write_passes_when_enabled(self) -> None:
        _guard(writes=True).check("POST", SANDBOX_ID)

    def test_sandbox_write_refused_when_disabled(self) -> None:
        with pytest.raises(SharePointWriteForbidden) as exc:
            _guard(writes=False).check("POST", SANDBOX_ID)
        assert "disabled" in str(exc.value)

    def test_refused_when_no_sandbox_resolved(self) -> None:
        with pytest.raises(SharePointWriteForbidden) as exc:
            _guard(writes=True, sandbox=None).check("POST", SANDBOX_ID)
        assert "no sandbox site ID" in str(exc.value)


class TestAllowlistCannotBeWidenedByAccident:
    """The failure modes that would turn the allowlist into a wildcard."""

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_sandbox_id_does_not_match_a_blank_target(self, blank: str | None) -> None:
        guard = _guard(writes=True, sandbox=blank)
        assert guard.sandbox_site_id is None
        with pytest.raises(SharePointWriteForbidden):
            guard.check("POST", "")

    def test_empty_target_site_is_refused(self) -> None:
        with pytest.raises(SharePointWriteForbidden) as exc:
            _guard().check("POST", "")
        assert "no site ID" in str(exc.value)

    def test_prefix_of_the_sandbox_id_does_not_match(self) -> None:
        """Comparison is exact equality, not a prefix or substring test."""
        with pytest.raises(SharePointWriteForbidden):
            _guard().check("POST", SANDBOX_ID[:20])

    def test_sandbox_id_as_a_substring_of_a_longer_id_does_not_match(self) -> None:
        with pytest.raises(SharePointWriteForbidden):
            _guard().check("POST", SANDBOX_ID + ",extra")

    def test_site_path_is_not_accepted_in_place_of_an_id(self) -> None:
        """Guarding on paths would be spoofable; this asserts we guard on IDs."""
        with pytest.raises(SharePointWriteForbidden):
            _guard().check("POST", "/sites/sandbox")


class TestStatusReporting:
    def test_describe_reports_read_only_when_writes_are_off(self) -> None:
        assert _guard(writes=False).describe()["mode"] == "read_only"

    def test_describe_reports_sandbox_mode_when_writes_are_on(self) -> None:
        described = _guard(writes=True).describe()
        assert described["mode"] == "read_write_sandbox"
        assert described["sandbox_site_id"] == SANDBOX_ID
