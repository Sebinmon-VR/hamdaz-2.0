"""The BCD check through its API, on the test database: trying it on one's own
task, opening the page, confirming the date. SharePoint and mail are fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_assistant_routes import _as, _make, boss, person, setup  # noqa: F401
from tests.test_followups import task

API = "/api/v1/bcd-checks"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeList:
    def __init__(self) -> None:
        created = datetime.now(UTC) - timedelta(minutes=30)
        self.row = task(id="901", title="test 6", created_at=iso(created),
                        bid_closing_date=iso(created + timedelta(hours=4)), assigned_to_lookup_id="12")

    async def task(self, item_id):
        return self.row

    async def lookup_id_for(self, email):
        return "12"


class FakeMailer:
    def __init__(self) -> None:
        self.asks: list = []

    async def send_ask(self, rows, **kw):
        self.asks.append(([r.task_title for r in rows], kw))


@pytest.fixture
def stubs(client):
    app = client._transport.app
    app.state.sharepoint = FakeList()
    app.state.bcd_worker = type("W", (), {"mailer": FakeMailer()})()
    return app.state


async def test_try_open_and_confirm(client, db, boss, person, stubs) -> None:
    c = _as(client, boss)
    made = await c.post(f"{API}/try", json={"task_id": "901"})
    assert made.status_code == 200, made.text
    check = made.json()
    assert check["status"] == "pending" and check["team_id"] is None
    assert check["edit_url"] and "EditForm.aspx" in check["edit_url"] or check["edit_url"] is None

    page = await c.get(f"{API}/{check['id']}")
    assert page.status_code == 200, page.text
    assert page.json()["still_placeholder"] is True

    done = await c.post(f"{API}/{check['id']}/confirm")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "confirmed"

    # Somebody else neither sees it nor confirms it.
    assert (await _as(client, person).get(f"{API}/{check['id']}")).status_code == 404

    settings = await _as(client, boss).get(f"{API}/settings")
    assert settings.status_code == 200 and settings.json()["enabled"] is False
