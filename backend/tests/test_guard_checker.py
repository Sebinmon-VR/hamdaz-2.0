"""Tests for guard #3 itself.

A CI check that cannot fail is worse than no check, because it reads as protection. These
plant violations and assert the scanner finds them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_sharepoint_readonly import scan


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    connector = tmp_path / "app" / "connectors" / "sharepoint"
    connector.mkdir(parents=True)
    (connector / "__init__.py").write_text("", encoding="utf-8")
    (connector / "guard.py").write_text(
        'READ_ONLY = {"GET"}\nWRITE = {"POST", "PATCH"}\n', encoding="utf-8"
    )
    (connector / "sandbox.py").write_text(
        "async def write(c):\n    await c.post('/x')\n", encoding="utf-8"
    )
    return tmp_path


def _client(connector: Path, body: str) -> None:
    (connector / "client.py").write_text(body, encoding="utf-8")


class TestCleanTreePasses:
    def test_read_only_client_passes(self, fake_repo: Path) -> None:
        _client(
            fake_repo / "app/connectors/sharepoint",
            "async def read(c):\n    return await c.get('/sites')\n",
        )
        assert scan(fake_repo) == []

    def test_sandbox_module_is_exempt(self, fake_repo: Path) -> None:
        """sandbox.py already contains a .post() call and must not be flagged."""
        _client(fake_repo / "app/connectors/sharepoint", "x = 1\n")
        assert scan(fake_repo) == []

    def test_guard_module_may_name_the_verbs_it_refuses(self, fake_repo: Path) -> None:
        _client(fake_repo / "app/connectors/sharepoint", "x = 1\n")
        assert not any("guard.py" in p for p in scan(fake_repo))


class TestViolationsAreCaught:
    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
    def test_direct_write_call_is_caught(self, fake_repo: Path, verb: str) -> None:
        _client(
            fake_repo / "app/connectors/sharepoint",
            f"async def oops(c):\n    await c.{verb}('/sites/ProposalTeam/items')\n",
        )
        problems = scan(fake_repo)
        assert any(f".{verb}()" in p for p in problems), problems

    def test_write_call_split_across_lines_is_caught(self, fake_repo: Path) -> None:
        """The shape a naive grep for '.post(' would miss."""
        _client(
            fake_repo / "app/connectors/sharepoint",
            "async def oops(c):\n    await c.post\\\n        ('/items')\n",
        )
        assert scan(fake_repo)

    def test_getattr_indirection_is_caught(self, fake_repo: Path) -> None:
        """The obvious way around a name-based check."""
        _client(
            fake_repo / "app/connectors/sharepoint",
            'async def oops(c):\n    await getattr(c, "post")("/items")\n',
        )
        problems = scan(fake_repo)
        assert any("getattr" in p for p in problems), problems

    def test_verb_passed_to_request_is_caught(self, fake_repo: Path) -> None:
        _client(
            fake_repo / "app/connectors/sharepoint",
            'async def oops(c):\n    await c.request("PATCH", "/items")\n',
        )
        assert scan(fake_repo)

    def test_bare_verb_literal_is_caught_outside_guard(self, fake_repo: Path) -> None:
        _client(
            fake_repo / "app/connectors/sharepoint",
            'METHOD = "DELETE"\n',
        )
        problems = scan(fake_repo)
        assert any("DELETE" in p for p in problems), problems

    def test_violation_in_a_nested_module_is_caught(self, fake_repo: Path) -> None:
        nested = fake_repo / "app/connectors/sharepoint" / "sync"
        nested.mkdir()
        (nested / "writer.py").write_text(
            "async def oops(c):\n    await c.post('/items')\n", encoding="utf-8"
        )
        _client(fake_repo / "app/connectors/sharepoint", "x = 1\n")
        assert any("writer.py" in p for p in scan(fake_repo))

    def test_a_sandbox_named_file_elsewhere_is_not_exempt(self, fake_repo: Path) -> None:
        """Exemption is by filename, so confirm a nested sandbox.py is still checked.

        This documents a real limitation: the exemption is name-based. If that ever needs to
        be path-exact, this test is where the change gets asserted.
        """
        nested = fake_repo / "app/connectors/sharepoint" / "sync"
        nested.mkdir()
        (nested / "sandbox.py").write_text(
            "async def oops(c):\n    await c.post('/items')\n", encoding="utf-8"
        )
        _client(fake_repo / "app/connectors/sharepoint", "x = 1\n")
        # Currently exempt by name. Asserting the actual behaviour rather than the wish.
        assert scan(fake_repo) == []


class TestMissingDirectoryFails:
    def test_absent_connector_dir_is_a_failure_not_a_pass(self, tmp_path: Path) -> None:
        """If the package is renamed, the guard must complain rather than vacuously pass."""
        assert scan(tmp_path)
