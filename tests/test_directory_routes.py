"""The directory HTTP surface: auth, search, paging, and upstream failures."""

from __future__ import annotations

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.directory.graph import GraphError, OrgUser

SESSION_COOKIE = "hamdaz_session"


def _org_user(oid: str, name: str, **over) -> OrgUser:
    return OrgUser(
        **{
            "object_id": oid,
            "display_name": name,
            "email": f"{name.lower().replace(' ', '.')}@hamdaz.com",
            "user_principal_name": f"{name.lower().replace(' ', '.')}@hamdaz.com",
            "job_title": None,
            "department": None,
            "office_location": None,
            "mobile_phone": None,
            "account_enabled": True,
            "user_type": "Member",
            **over,
        }
    )


@pytest.fixture
async def authed(client, db):
    user = await upsert_user(
        db, EntraIdentity(object_id="oid-me", email="me@hamdaz.com", display_name="Me")
    )
    await db.commit()
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


# ── authorization ──────────────────────────────────────────────────────


async def test_listing_requires_a_session(client) -> None:
    assert (await client.get("/api/v1/directory/users")).status_code == 401


async def test_single_lookup_requires_a_session(client) -> None:
    assert (await client.get("/api/v1/directory/users/oid-1")).status_code == 401


async def test_the_directory_is_not_reached_without_a_session(client, graph) -> None:
    """Auth must short-circuit before we spend a Graph call."""
    await client.get("/api/v1/directory/users")
    assert graph.calls == []


# ── listing ────────────────────────────────────────────────────────────


async def test_returns_the_directory(authed, graph) -> None:
    graph.users = [_org_user("a", "Alice"), _org_user("b", "Bob")]
    response = await authed.get("/api/v1/directory/users")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["count"] == 2
    assert [u["display_name"] for u in body["users"]] == ["Alice", "Bob"]


async def test_exposes_the_object_id_the_team_module_will_need(authed, graph) -> None:
    graph.users = [_org_user("entra-oid-123", "Alice")]
    body = (await authed.get("/api/v1/directory/users")).json()
    assert body["users"][0]["object_id"] == "entra-oid-123"


async def test_defaults_exclude_guests_and_disabled(authed, graph) -> None:
    await authed.get("/api/v1/directory/users")
    assert graph.calls[0] == {"include_guests": False, "include_disabled": False}


@pytest.mark.parametrize("flag", ["include_guests", "include_disabled"])
async def test_flags_are_passed_through(authed, graph, flag: str) -> None:
    await authed.get("/api/v1/directory/users", params={flag: "true"})
    assert graph.calls[0][flag] is True


# ── search ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "term,expected",
    [
        ("alice", ["Alice Smith"]),
        ("ALICE", ["Alice Smith"]),          # case-insensitive
        ("smith", ["Alice Smith"]),
        ("hamdaz.com", ["Alice Smith", "Bob Jones"]),  # matches on email
        ("nobody", []),
    ],
)
async def test_search_matches_name_and_email(authed, graph, term: str, expected: list) -> None:
    graph.users = [_org_user("a", "Alice Smith"), _org_user("b", "Bob Jones")]
    body = (await authed.get("/api/v1/directory/users", params={"search": term})).json()
    assert [u["display_name"] for u in body["users"]] == expected


async def test_search_matches_job_title_and_department(authed, graph) -> None:
    graph.users = [
        _org_user("a", "Alice", job_title="Site Engineer"),
        _org_user("b", "Bob", department="Finance"),
    ]
    engineers = (await authed.get("/api/v1/directory/users", params={"search": "engineer"})).json()
    finance = (await authed.get("/api/v1/directory/users", params={"search": "finance"})).json()

    assert [u["display_name"] for u in engineers["users"]] == ["Alice"]
    assert [u["display_name"] for u in finance["users"]] == ["Bob"]


async def test_search_ignores_surrounding_whitespace(authed, graph) -> None:
    graph.users = [_org_user("a", "Alice")]
    body = (await authed.get("/api/v1/directory/users", params={"search": "  alice  "})).json()
    assert body["total"] == 1


# ── paging ─────────────────────────────────────────────────────────────


async def test_total_counts_matches_not_the_window(authed, graph) -> None:
    graph.users = [_org_user(str(i), f"User {i:02d}") for i in range(10)]
    body = (await authed.get("/api/v1/directory/users", params={"limit": 3})).json()

    assert body["total"] == 10  # the whole match set
    assert body["count"] == 3  # what came back
    assert len(body["users"]) == 3


async def test_offset_windows_do_not_overlap(authed, graph) -> None:
    graph.users = [_org_user(str(i), f"User {i:02d}") for i in range(10)]

    first = (await authed.get("/api/v1/directory/users", params={"limit": 4, "offset": 0})).json()
    second = (await authed.get("/api/v1/directory/users", params={"limit": 4, "offset": 4})).json()

    names = {u["display_name"] for u in first["users"]}
    assert names.isdisjoint({u["display_name"] for u in second["users"]})


async def test_offset_past_the_end_returns_empty_not_an_error(authed, graph) -> None:
    graph.users = [_org_user("a", "Alice")]
    body = (await authed.get("/api/v1/directory/users", params={"offset": 500})).json()
    assert body["users"] == []
    assert body["total"] == 1


async def test_total_reflects_the_search_not_the_directory(authed, graph) -> None:
    graph.users = [_org_user("a", "Alice"), _org_user("b", "Bob")]
    body = (await authed.get("/api/v1/directory/users", params={"search": "alice"})).json()
    assert body["total"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1000}, {"offset": -1}])
async def test_rejects_nonsense_paging(authed, params: dict) -> None:
    assert (await authed.get("/api/v1/directory/users", params=params)).status_code == 422


# ── single user ────────────────────────────────────────────────────────


async def test_fetches_one_user(authed, graph) -> None:
    graph.users = [_org_user("oid-1", "Alice")]
    response = await authed.get("/api/v1/directory/users/oid-1")
    assert response.status_code == 200
    assert response.json()["display_name"] == "Alice"


async def test_unknown_user_is_404(authed, graph) -> None:
    graph.users = []
    assert (await authed.get("/api/v1/directory/users/nope")).status_code == 404


# ── upstream failure ───────────────────────────────────────────────────


async def test_graph_failure_is_a_502_not_a_500(authed, graph) -> None:
    """The fault is upstream; saying so is what makes the log readable."""
    graph.error = GraphError("Graph returned 503")
    response = await authed.get("/api/v1/directory/users")
    assert response.status_code == 502


async def test_graph_failure_does_not_leak_internals(authed, graph) -> None:
    graph.error = GraphError("client-credentials token request failed: secret expired")
    body = (await authed.get("/api/v1/directory/users")).json()
    assert "secret" not in body["detail"]


async def test_graph_failure_on_single_lookup_is_a_502(authed, graph) -> None:
    graph.error = GraphError("Graph returned 500")
    assert (await authed.get("/api/v1/directory/users/oid-1")).status_code == 502
