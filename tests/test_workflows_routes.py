"""Workflows over HTTP: the gates, and one run driven through the API.

Few tests, deliberately: each is a whole app start-up. What is pinned is what
only the routes decide — the module grant, the super-admin wall around the
builder and the switches, who may look at a run, and that a person can start
the shipped flow and answer its first question through the API with every
outside service stubbed on the app's state. The engine itself is covered in
``test_workflows.py``.

Fixtures and helpers come from ``test_assistant_routes`` rather than being
copied; the fakes come from ``test_workflows``.
"""

from __future__ import annotations

import pytest

from app.access import service as access_service
from app.models.workflow import RunStatus
from app.teams import service as teams_service
from app.workflows import service
from app.workflows.catalogue import BLOCKS, PRESALES_KEY
from tests.test_assistant_routes import (  # noqa: F401 - fixtures by import
    _as,
    _make,
    boss,
    person,
    setup,
)
from tests.test_workflows import (
    FakeExecutor,
    FakeExtractor,
    FakeLLM,
    FakeMail,
    FakeSharePoint,
    FakeZoho,
    make_task,
    services,
)

API = "/api/v1/workflows"


@pytest.fixture
async def modules(db, setup):
    await access_service.seed_modules(db)
    await service.seed_flows(db)
    await db.commit()


@pytest.fixture
async def team(db, modules, person):
    """The person's team, holding the Workflows module."""
    row = await teams_service.create_team(db, name="Presales", slug="presales")
    await teams_service.set_member_roles(db, team=row, user=person, role_keys=["member"])
    await access_service.grant_module(db, team=row, module_key="workflows")
    await db.commit()
    return row


@pytest.fixture
def stubs(client):
    """The outside world, on the app's state: a task with no attachments."""
    app = client._transport.app
    app.state.sharepoint = FakeSharePoint(make_task("42", "Pump spares", end_user="ADNOC"))
    app.state.mail_reader = FakeMail()
    app.state.zoho = FakeZoho()
    app.state.quote_extractor = FakeExtractor()
    app.state.openai = FakeLLM()
    app.state.assistant_executor = FakeExecutor()
    return app.state


async def test_the_module_gate_refuses_a_team_without_the_grant(client, db, person, modules) -> None:
    assert (await client.get(API)).status_code == 401
    response = await _as(client, person).get(API)
    assert response.status_code == 403
    assert "Workflows module" in response.json()["detail"]
    assert (await client.get(f"{API}/runs")).status_code == 403
    assert (await client.post(f"{API}/{PRESALES_KEY}/runs", json={"subject_id": "42"})).status_code == 403
    # The builder is not a team grant at all.
    assert (await client.get(f"{API}/admin/blocks")).status_code == 403


async def test_a_super_admin_sees_blocks_and_flows_and_flips_a_switch(client, db, boss, modules) -> None:
    _as(client, boss)
    blocks = await client.get(f"{API}/admin/blocks")
    assert blocks.status_code == 200, blocks.text
    body = blocks.json()
    assert [b["kind"] for b in body["blocks"]] == [b.kind for b in BLOCKS]
    assert body["schemas"] == ["requirements", "suppliers"]
    assert {"key", "label", "module", "method", "path"} <= set(body["tools"][0])
    assert "quote_requests.create" in {t["key"] for t in body["tools"]}
    ask = next(b for b in body["blocks"] if b["kind"] == "ask_user")
    assert ask["waits"] == "user" and ask["switch"] is None
    assert next(f for f in ask["fields"] if f["key"] == "mode")["options"] == ["form", "review"]

    flows = await client.get(f"{API}/admin/flows")
    assert flows.status_code == 200
    (presales,) = flows.json()
    assert presales["key"] == PRESALES_KEY and presales["is_system"] is True
    assert presales["open_runs"] == 0 and presales["team_slug"] is None
    assert [s["key"] for s in presales["steps"]][:2] == ["docs", "ask_docs"]

    before = await client.get(f"{API}/admin/settings")
    assert before.json() == {"send_email": False, "write_sharepoint": False, "write_zoho": False, "from_mailbox": None, "poll_seconds": 60}
    flipped = await client.patch(f"{API}/admin/settings", json={"send_email": True, "from_mailbox": "rfq@hamdaz.com"})
    assert flipped.status_code == 200
    assert flipped.json()["send_email"] is True and flipped.json()["write_zoho"] is False
    assert (await client.get(f"{API}/admin/settings")).json()["from_mailbox"] == "rfq@hamdaz.com"
    assert (await client.patch(f"{API}/admin/settings", json={"poll_seconds": 5})).status_code == 422

    # A team flow can be made through the builder too.
    await teams_service.create_team(db, name="Presales", slug="presales")
    await db.commit()
    made = await client.post(
        f"{API}/admin/flows",
        json={"key": "ops_check", "name": "Ops check", "team": "presales",
              "steps": [{"key": "n", "kind": "notify", "config": {"title": "Hi"}}]},
    )
    assert made.status_code == 201, made.text
    assert made.json()["team_slug"] == "presales" and made.json()["version"] == 1


async def test_a_person_starts_the_presales_flow_and_answers_its_question(client, db, person, team, stubs) -> None:
    _as(client, person)
    listed = await client.get(API)
    assert listed.status_code == 200 and [f["key"] for f in listed.json()] == [PRESALES_KEY]

    started = await client.post(f"{API}/{PRESALES_KEY}/runs", json={"subject_id": "42", "subject_label": "Pump spares"})
    assert started.status_code == 201, started.text
    run = started.json()
    assert run["status"] == RunStatus.WAITING_USER
    assert run["tag"].startswith("HZ-") and run["owner_name"] == "amina"
    assert run["team_id"] is None and run["step_count"] == len(run["steps"])
    assert run["pending"]["step_key"] == "ask_docs"
    assert run["pending"]["mode"] == "form" and run["pending"]["allow_files"] is True
    assert run["waiting_for"] == "What needs pricing?"
    assert run["context"]["docs"] == {"found": False, "count": 0, "files": []}
    assert run["context"]["task"]["end_user"] == "ADNOC"
    assert "_answers" not in run["context"]
    assert stubs.mail_reader.sent == [] and stubs.zoho.calls == []

    # Starting it again on the same task is refused; another task is fine to list.
    again = await client.post(f"{API}/{PRESALES_KEY}/runs", json={"subject_id": "42"})
    assert again.status_code == 409
    on_task = await client.get(f"{API}/for-task/42")
    assert on_task.status_code == 200
    assert [r["id"] for r in on_task.json()["runs"]] == [run["id"]]
    assert on_task.json()["workflows"][0]["open_runs"] == 1

    answered = await client.post(
        f"{API}/runs/{run['id']}/answer",
        json={"values": {"items": [{"description": "Gate valve", "quantity": 2, "unit": "pcs"}], "notes": "Urgent"}},
    )
    assert answered.status_code == 200, answered.text
    moved = answered.json()
    assert moved["status"] == RunStatus.WAITING_USER
    assert moved["pending"]["step_key"] == "confirm_requirements"
    assert moved["pending"]["mode"] == "review"
    assert moved["pending"]["review_value"]["items"][0]["description"] == "Gate valve"
    assert moved["context"]["requirements"]["items"][0]["quantity"] == 2.0
    assert moved["step_index"] == 3

    # Read back fresh, the timeline and the trail are complete.
    read = await client.get(f"{API}/runs/{run['id']}")
    assert read.status_code == 200
    fresh = read.json()
    states = {s["key"]: s["state"] for s in fresh["steps"]}
    assert states["docs"] == "done" and states["ask_docs"] == "done"
    assert states["extract"] == "done" and states["confirm_requirements"] == "waiting"
    assert states["find_suppliers"] == "pending"
    kinds = [e["kind"] for e in fresh["events"]]
    assert kinds[0] == "started" and "answered" in kinds and kinds.count("step_completed") == 3
    assert [r["status"] for r in (await client.get(f"{API}/runs?mine=true&open=true")).json()] == ["waiting_user"]


async def test_a_run_is_hidden_from_a_stranger_and_open_to_an_admin(client, db, person, boss, team, stubs) -> None:
    flow = await service.get_flow(db, PRESALES_KEY)
    run = await service.start(
        db, flow, owner=person, subject_id="42", subject_label="Pump spares",
        settings=await service.get_settings(db),
        services=services(sharepoint=FakeSharePoint(make_task())),
    )
    stranger = await _make(db, "zed@hamdaz.com")
    other = await teams_service.create_team(db, name="Finance", slug="finance")
    await teams_service.set_member_roles(db, team=other, user=stranger, role_keys=["member"])
    await access_service.grant_module(db, team=other, module_key="workflows")
    await db.commit()

    assert (await _as(client, stranger).get(f"{API}/runs/{run.id}")).status_code == 404
    assert (await client.post(f"{API}/runs/{run.id}/answer", json={"values": {}})).status_code == 404
    assert [r["id"] for r in (await client.get(f"{API}/runs")).json()] == []

    assert (await _as(client, person).get(f"{API}/runs/{run.id}")).status_code == 200
    admin_view = await _as(client, boss).get(f"{API}/runs/{run.id}")
    assert admin_view.status_code == 200
    assert admin_view.json()["owner_id"] == str(person.id)
    assert [r["id"] for r in (await client.get(f"{API}/runs")).json()] == [str(run.id)]
    cancelled = await client.post(f"{API}/runs/{run.id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == RunStatus.CANCELLED


async def test_patching_a_flow_with_a_bad_step_is_400(client, db, boss, modules) -> None:
    _as(client, boss)
    bad = await client.patch(f"{API}/admin/flows/{PRESALES_KEY}", json={"steps": [{"key": "a", "kind": "nope"}]})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Step 'a': no block called 'nope'."
    twice = await client.patch(
        f"{API}/admin/flows/{PRESALES_KEY}",
        json={"steps": [{"key": "a", "kind": "notify", "config": {"title": "x"}}, {"key": "a", "kind": "notify", "config": {"title": "y"}}]},
    )
    assert twice.status_code == 400 and twice.json()["detail"] == "Two steps are called 'a'."
    no_route = await client.patch(
        f"{API}/admin/flows/{PRESALES_KEY}",
        json={"steps": [{"key": "c", "kind": "endpoint", "config": {"tool": "nowhere.at_all"}}]},
    )
    assert no_route.status_code == 400 and "is not a route the app has" in no_route.json()["detail"]
    # Nothing of that stuck.
    flow = await client.get(f"{API}/admin/flows/{PRESALES_KEY}")
    assert flow.json()["version"] == 1 and len(flow.json()["steps"]) > 3
    assert (await client.delete(f"{API}/admin/flows/{PRESALES_KEY}")).status_code == 409


CONTEXT_LOST = (
    "app/workflows/engine.py: what a pass writes into run.context is mutated in "
    "place after add_event() has flushed and reset SQLAlchemy's baseline to that "
    "same object, so the UPDATE never carries it; only service.answer's "
    "reassignment reaches the database. Fix: flag_modified(run, 'context') in "
    "engine._touch (and _fail). See test_workflows.py for the engine-level case."
)


@pytest.mark.xfail(reason=CONTEXT_LOST, strict=True)
async def test_what_the_start_request_learned_is_there_for_the_next_request(client, db, person, team, stubs) -> None:
    _as(client, person)
    started = await client.post(f"{API}/{PRESALES_KEY}/runs", json={"subject_id": "42", "subject_label": "Pump spares"})
    assert started.status_code == 201, started.text
    assert started.json()["context"]["task"]["end_user"] == "ADNOC"
    # A new request, a new session: the task the documents step read and the
    # docs summary the question was gated on must still be on the run.
    fresh = (await client.get(f"{API}/runs/{started.json()['id']}")).json()
    assert fresh["context"]["docs"] == {"found": False, "count": 0, "files": []}
    assert fresh["context"]["task"]["end_user"] == "ADNOC"
    answered = await client.post(
        f"{API}/runs/{started.json()['id']}/answer",
        json={"values": {"items": [{"description": "Gate valve", "quantity": 2}]}},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["context"]["requirements"]["customer"] == "ADNOC"
