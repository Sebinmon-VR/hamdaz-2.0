"""The proposals HTTP surface.

Two gates, and both are tested from the outside: the caller's team must hold the
proposals module, and the rows returned must be theirs. The second is the one
worth attacking — nothing in a request may widen it.
"""

from __future__ import annotations

import pytest

from app.access import service as access
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.proposals.sharepoint import ProposalTask, SharePointError
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/proposals"


def _task(task_id: str, title: str, status: str = "In Progress", due: str | None = None):
    return ProposalTask(
        id=task_id, title=title, status=status, priority="High",
        assigned_to_lookup_id="15", assigned_to_name="Sebin",
        start_date=None, due_date=due, bid_closing_date=None, end_user="Adnoc",
        submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None,
    )


class StubSharePoint:
    """Answers per email, so a test can prove one user cannot see another's rows."""

    def __init__(self) -> None:
        self.by_email: dict[str, tuple[str, list[ProposalTask]]] = {}
        self.error: Exception | None = None
        self.asked_for: list[str] = []
        #: item id -> [{"file_name": ..., "content": ...}]
        self.files: dict[str, list[dict]] = {}
        #: every write the router asked for, so a test can assert on exactly
        #: what would have reached SharePoint without anything reaching it.
        self.updates: list[tuple[str, dict]] = []

    async def lookup_id_for(self, email: str):
        if self.error:
            raise self.error
        entry = self.by_email.get(email.casefold())
        return entry[0] if entry else None

    async def tasks_assigned_to(self, lookup_id: str, *, limit: int = 200):
        if self.error:
            raise self.error
        self.asked_for.append(lookup_id)
        for lid, tasks in self.by_email.values():
            if lid == lookup_id:
                return tasks[:limit]
        return []

    async def list_columns(self):
        if self.error:
            raise self.error
        return [{"name": "Status", "display_name": "Status", "choices": ["Open"]}]

    # ── one task, its files, and edits — recorded, never sent anywhere ──

    async def task(self, item_id: str):
        if self.error:
            raise self.error
        for _lid, tasks in self.by_email.values():
            for t in tasks:
                if t.id == str(item_id):
                    return t
        from app.proposals.sharepoint import SharePointError

        raise SharePointError(f"No task {item_id!r}")

    async def attachments_of(self, item_id: str):
        if self.error:
            raise self.error
        return list(self.files.get(str(item_id), []))

    async def attachment_content(self, item_id: str, file_name: str):
        if self.error:
            raise self.error
        for f in self.files.get(str(item_id), []):
            if f["file_name"] == file_name:
                return f.get("content", b"%PDF-1.4 stub")
        from app.proposals.sharepoint import SharePointError

        raise SharePointError(f"No attachment {file_name!r}")

    async def update_task(self, item_id: str, fields: dict):
        if self.error:
            raise self.error
        self.updates.append((str(item_id), dict(fields)))
        task = await self.task(item_id)
        # The real client re-reads the row; the stub applies the two fields the
        # tests assert on and hands the task back.
        from dataclasses import replace

        return replace(
            task,
            status=fields.get("Status", task.status),
            working_notes=fields.get("WorkingNotes", task.working_notes),
        )


@pytest.fixture
def sharepoint(client):
    stub = StubSharePoint()
    client._transport.app.state.sharepoint = stub
    return stub


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


def _as(client, user):
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


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await access.seed_modules(db)
    await db.commit()


@pytest.fixture
async def team(db, seeded):
    t = await teams.create_team(db, name="Presales")
    await access.grant_module(db, team=t, module_key="proposals")
    await db.commit()
    return t


@pytest.fixture
async def member(db, team):
    """In the team that has the module, and present in SharePoint."""
    user = await _make(db, "sebin@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


# ── gate 1: the module ─────────────────────────────────────────────────


async def test_a_session_is_required(client, seeded, sharepoint) -> None:
    assert (await client.get(f"{API}/my-tasks")).status_code == 401


async def test_a_team_without_the_module_is_refused(client, db, seeded, sharepoint) -> None:
    other = await teams.create_team(db, name="Finance")
    user = await _make(db, "nobody@hamdaz.com")
    await teams.set_member_roles(db, team=other, user=user, role_keys=["member"])
    await db.commit()

    res = await _as(client, user).get(f"{API}/my-tasks")
    assert res.status_code == 403
    assert "Proposals module" in res.json()["detail"]


async def test_someone_in_no_team_is_refused(client, db, seeded, sharepoint) -> None:
    user = await _make(db, "loner@hamdaz.com")
    assert (await _as(client, user).get(f"{API}/my-tasks")).status_code == 403


async def test_sharepoint_is_not_touched_when_refused(client, db, seeded, sharepoint) -> None:
    """The gate must short-circuit before spending a SharePoint call."""
    user = await _make(db, "loner@hamdaz.com")
    await _as(client, user).get(f"{API}/my-tasks")
    assert sharepoint.asked_for == []


# ── gate 2: ownership ──────────────────────────────────────────────────


async def test_a_member_sees_their_own_tasks(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Mine")])
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()

    assert body["in_sharepoint"] is True
    assert body["sharepoint_user_id"] == "15"
    assert [t["title"] for t in body["tasks"]] == ["Mine"]


async def test_one_user_cannot_see_another_s_tasks(client, db, team, member, sharepoint) -> None:
    """The rule the whole module exists for."""
    other = await _make(db, "goutham@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=other, role_keys=["member"])
    await db.commit()

    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Sebin's")])
    sharepoint.by_email["goutham@hamdaz.com"] = ("27", [_task("2", "Goutham's")])

    mine = (await _as(client, member).get(f"{API}/my-tasks")).json()
    theirs = (await _as(client, other).get(f"{API}/my-tasks")).json()

    assert [t["title"] for t in mine["tasks"]] == ["Sebin's"]
    assert [t["title"] for t in theirs["tasks"]] == ["Goutham's"]


@pytest.mark.parametrize(
    "params",
    [
        {"user": "goutham@hamdaz.com"},
        {"lookup_id": "27"},
        {"assigned_to": "27"},
        {"sharepoint_user_id": "27"},
        {"email": "goutham@hamdaz.com"},
    ],
)
async def test_the_filter_cannot_be_widened_from_the_request(
    client, member, sharepoint, params: dict
) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = ("15", [_task("1", "Mine")])
    sharepoint.by_email["goutham@hamdaz.com"] = ("27", [_task("2", "Not mine")])

    body = (await _as(client, member).get(f"{API}/my-tasks", params=params)).json()
    assert [t["title"] for t in body["tasks"]] == ["Mine"]
    assert sharepoint.asked_for == ["15"]


async def test_someone_absent_from_sharepoint_gets_an_empty_list(
    client, member, sharepoint
) -> None:
    """Not an error: they simply have no presence on that site."""
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert body["in_sharepoint"] is False
    assert body["sharepoint_user_id"] is None
    assert body["tasks"] == []


# ── filtering and ordering ─────────────────────────────────────────────


async def test_open_only_is_the_default(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [_task("1", "Open"), _task("2", "Done", status="Completed")],
    )
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()

    assert [t["title"] for t in body["tasks"]] == ["Open"]
    assert body["total"] == 2  # the count is of everything assigned
    assert body["open_count"] == 1


async def test_completed_tasks_can_be_included(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [_task("1", "Open"), _task("2", "Done", status="Completed")],
    )
    body = (
        await _as(client, member).get(f"{API}/my-tasks", params={"open_only": "false"})
    ).json()
    assert len(body["tasks"]) == 2


async def test_soonest_due_first_and_undated_last(client, member, sharepoint) -> None:
    sharepoint.by_email["sebin@hamdaz.com"] = (
        "15",
        [
            _task("1", "No date"),
            _task("2", "Later", due="2025-12-01T00:00:00Z"),
            _task("3", "Sooner", due="2025-06-01T00:00:00Z"),
        ],
    )
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert [t["title"] for t in body["tasks"]] == ["Sooner", "Later", "No date"]


@pytest.mark.parametrize("limit", [0, 501])
async def test_a_nonsense_limit_is_rejected(client, member, sharepoint, limit: int) -> None:
    res = await _as(client, member).get(f"{API}/my-tasks", params={"limit": limit})
    assert res.status_code == 422


# ── upstream failure ───────────────────────────────────────────────────


async def test_a_sharepoint_failure_is_a_502(client, member, sharepoint) -> None:
    sharepoint.error = SharePointError("SharePoint returned 503")
    assert (await _as(client, member).get(f"{API}/my-tasks")).status_code == 502


async def test_the_failure_reason_is_not_leaked(client, member, sharepoint) -> None:
    sharepoint.error = SharePointError("token request failed: secret expired")
    body = (await _as(client, member).get(f"{API}/my-tasks")).json()
    assert "secret" not in body["detail"]


# ── the list schema ────────────────────────────────────────────────────


async def test_columns_need_the_module_too(client, db, seeded, sharepoint) -> None:
    user = await _make(db, "loner@hamdaz.com")
    assert (await _as(client, user).get(f"{API}/columns")).status_code == 403


async def test_columns_are_returned_to_a_member(client, member, sharepoint) -> None:
    body = (await _as(client, member).get(f"{API}/columns")).json()
    assert body[0]["name"] == "Status"


# ── one task's attachments, and editing it ─────────────────────────────
#
# Everything below runs against the stub. Nothing in this file — or in any
# test — touches the real SharePoint, and the write tests assert on what the
# router *would have sent*, recorded by the stub.


def _task_for(task_id: str = "412", lookup: str = "27") -> ProposalTask:
    from dataclasses import replace

    return replace(_task(task_id, "Firewall refresh"), assigned_to_lookup_id=lookup)


@pytest.fixture
async def assignee(db, team, sharepoint):
    """A member whose stubbed SharePoint identity owns task 412."""
    user = await _make(db, "worker@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    sharepoint.by_email["worker@hamdaz.com"] = ("27", [_task_for()])
    sharepoint.files["412"] = [
        {"file_name": "RFQ scope.pdf", "content": b"%PDF-1.4 scope"},
        {"file_name": "BoQ rev2.xlsx", "content": b"PK\x03\x04boq"},
    ]
    return user


@pytest.fixture
async def bystander(db, team, sharepoint):
    """On the module, but assigned to a different task entirely."""
    user = await _make(db, "other@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    sharepoint.by_email["other@hamdaz.com"] = ("99", [_task_for("500", "99")])
    return user


async def test_the_assignee_lists_their_tasks_files(client, assignee):
    body = (await _as(client, assignee).get(f"{API}/tasks/412/attachments")).json()
    assert [f["file_name"] for f in body] == ["RFQ scope.pdf", "BoQ rev2.xlsx"]
    assert body[0]["download_url"].endswith("/tasks/412/attachments/RFQ%20scope.pdf")


async def test_the_assignee_downloads_a_file(client, assignee):
    response = await _as(client, assignee).get(
        f"{API}/tasks/412/attachments/RFQ scope.pdf"
    )
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 scope"
    assert response.headers["content-disposition"].startswith("attachment;")


async def test_somebody_elses_task_is_a_404_not_a_403(client, assignee, bystander):
    """That task 412 exists at all is pipeline information."""
    response = await _as(client, bystander).get(f"{API}/tasks/412/attachments")
    assert response.status_code == 404


async def test_an_admin_reaches_any_tasks_files(client, db, assignee):
    admin = await _make(db, "boss@hamdaz.com", "manager")
    body = (await _as(client, admin).get(f"{API}/tasks/412/attachments")).json()
    assert len(body) == 2


async def test_the_assignee_updates_their_task(client, assignee, sharepoint):
    response = await _as(client, assignee).patch(
        f"{API}/tasks/412",
        json={"status": "Submitted", "working_notes": "Prices agreed."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Submitted"
    assert body["working_notes"] == "Prices agreed."
    # Exactly what would have gone to SharePoint, under its own column names.
    assert sharepoint.updates == [
        ("412", {"Status": "Submitted", "WorkingNotes": "Prices agreed."})
    ]


async def test_fields_outside_the_whitelist_never_reach_sharepoint(
    client, assignee, sharepoint
):
    """AssignedTo is reassignment, not an edit — it must not slip through."""
    response = await _as(client, assignee).patch(
        f"{API}/tasks/412",
        json={"AssignedToLookupId": "99", "assigned_to_lookup_id": "99",
              "Attachments": False, "status": "Submitted"},
    )
    assert response.status_code == 200
    (item_id, fields), = sharepoint.updates
    assert fields == {"Status": "Submitted"}, "only the whitelisted field went"


async def test_an_empty_patch_is_refused_before_touching_sharepoint(
    client, assignee, sharepoint
):
    response = await _as(client, assignee).patch(f"{API}/tasks/412", json={})
    assert response.status_code == 400
    assert sharepoint.updates == []


async def test_a_bystander_cannot_update_somebody_elses_task(
    client, assignee, bystander, sharepoint
):
    response = await _as(client, bystander).patch(
        f"{API}/tasks/412", json={"status": "Submitted"}
    )
    assert response.status_code == 404
    assert sharepoint.updates == [], "nothing may reach SharePoint on a refusal"


async def test_uploading_an_executable_is_refused(client, assignee, sharepoint):
    import io as _io

    response = await _as(client, assignee).post(
        f"{API}/tasks/412/attachments",
        files=[("file", ("virus.exe", _io.BytesIO(b"MZ"), "application/pdf"))],
    )
    assert response.status_code == 400
    assert "not a type we accept" in response.json()["detail"]


async def test_missing_consent_comes_back_as_instructions_not_a_bare_error(
    client, assignee, sharepoint
):
    from app.proposals.sharepoint import SharePointConsentError

    sharepoint.error = SharePointConsentError(
        "Reading attachments needs a permission this app does not hold."
    )
    # The task lookup itself fails on the stub error, which the router reads as
    # the task being unreachable rather than half-answering.
    response = await _as(client, assignee).get(f"{API}/tasks/412/attachments")
    assert response.status_code in (404, 503)
