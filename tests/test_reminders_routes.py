"""The status reminder through its API, on the test database: trying it on
one's own task, opening the form, answering it with the write off and on.

SharePoint and the mailbox are fakes on the app's state; nothing real is read
or written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_assistant_routes import _as, _make, boss, person, setup  # noqa: F401
from tests.test_followups import stored, task

API = "/api/v1/reminders"


class FakeList:
    def __init__(self) -> None:
        due = stored(datetime.now(UTC) + timedelta(hours=30))
        self.row = task(id="901", title="test 6", bid_closing_date=due, status="In Progress",
                        remarks="Quotes awaited", assigned_to_lookup_id="12")
        self.written: list = []

    async def task(self, item_id):
        return self.row

    async def lookup_id_for(self, email):
        return "12"

    async def list_columns(self):
        return [{"name": "Status", "choices": ["Not Started", "In Progress", "Completed", "On Hold"]},
                {"name": "SubmissionStatus", "choices": ["Submitted", "Not Submitted"]}]

    async def update_task(self, item_id, fields):
        self.written.append((item_id, fields))


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_reminders(self, rows, **kw):
        self.sent.append(([r.task_title for r in rows], kw))


@pytest.fixture
def stubs(client):
    app = client._transport.app
    app.state.sharepoint = FakeList()
    app.state.reminder_worker = type("W", (), {"mailer": FakeMailer()})()
    return app.state


async def test_try_open_and_answer_with_the_write_off_then_on(client, db, boss, person, stubs) -> None:
    c = _as(client, boss)
    made = await c.post(f"{API}/try", json={"task_id": "901"})
    assert made.status_code == 200, made.text
    reminder = made.json()
    assert reminder["status"] == "pending" and reminder["team_id"] is None
    assert reminder["remarks_at_ask"] == "Quotes awaited"

    form = await c.get(f"{API}/{reminder['id']}")
    assert form.status_code == 200, form.text
    live = form.json()["task"]
    assert live["status"] == "In Progress" and live["remarks"] == "Quotes awaited"
    assert live["writes_to_sharepoint"] is False
    assert form.json()["reminder"]["may_answer"] is True

    seen = {"status": "In Progress", "submission_status": "", "remarks": "Quotes awaited",
            "working_notes": ""}
    answered = await c.post(f"{API}/{reminder['id']}/answer",
                            json={"values": {**seen, "status": "On Hold"}, "seen": seen})
    assert answered.status_code == 200, answered.text
    assert answered.json()["changes"] == {"Status": "On Hold"}
    assert answered.json()["written_at"] is None and stubs.sharepoint.written == []

    # Switched on, a fresh trial's answer reaches the list — only what changed.
    on = await c.patch(f"{API}/settings", json={"write_sharepoint": True})
    assert on.status_code == 200 and on.json()["write_sharepoint"] is True
    again = (await c.post(f"{API}/try", json={"task_id": "901"})).json()
    assert again["id"] == reminder["id"] and again["status"] == "pending"
    done = await c.post(f"{API}/{again['id']}/answer",
                        json={"values": {**seen, "working_notes": "Sent to buyer"}, "seen": seen})
    assert done.status_code == 200, done.text
    assert stubs.sharepoint.written == [("901", {"WorkingNotes": "Sent to buyer"})]
    assert done.json()["written_at"] is not None

    # Somebody else neither sees it nor answers it.
    assert (await _as(client, person).get(f"{API}/{reminder['id']}")).status_code == 404
    recent = await _as(client, boss).get(f"{API}/recent")
    assert recent.json()[0]["id"] == reminder["id"]


async def test_only_a_super_admin_changes_the_settings(client, db, person, stubs) -> None:
    refused = await _as(client, person).patch(f"{API}/settings", json={"write_sharepoint": True})
    assert refused.status_code == 403
