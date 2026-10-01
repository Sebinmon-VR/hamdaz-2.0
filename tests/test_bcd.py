"""The BCD check: spotting the placeholder, counting working time in India,
asking with the team lead copied, escalating after two working hours, and
the hold the other modules wait on. No database, list or mailbox — fakes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bcd import mailer as bm
from app.bcd import service
from app.models.bcd_check import BcdCheck, BcdCheckSettings, BcdCheckStatus
from app.models.followup import FollowupSettings
from tests.test_followups import task

IST = ZoneInfo("Asia/Kolkata")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def ist(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(UTC)


# ── the placeholder ────────────────────────────────────────────────────


CREATED = datetime(2026, 9, 29, 12, 14, 33, tzinfo=UTC)


def test_the_placeholder_is_found_as_the_list_writes_it() -> None:
    # As on the list: created 12:14:33, BCD 16:14:32 — the UAE clock as UTC.
    uae_as_utc = task(created_at=iso(CREATED), bid_closing_date=iso(CREATED + timedelta(hours=4, seconds=-1)))
    assert service.placeholder_of(uae_as_utc) is not None
    same_moment = task(created_at=iso(CREATED), bid_closing_date=iso(CREATED + timedelta(seconds=30)))
    assert service.placeholder_of(same_moment) is not None
    india_as_utc = task(created_at=iso(CREATED), bid_closing_date=iso(CREATED + timedelta(hours=5, minutes=30)))
    assert service.placeholder_of(india_as_utc) is not None


def test_a_real_bcd_is_left_alone() -> None:
    real = task(created_at=iso(CREATED), bid_closing_date="2026-10-05T16:00:00Z")
    assert service.placeholder_of(real) is None
    # Four hours and ten minutes on is a date somebody chose, not the stamp.
    near = task(created_at=iso(CREATED), bid_closing_date=iso(CREATED + timedelta(hours=4, minutes=10)))
    assert service.placeholder_of(near) is None
    assert service.placeholder_of(task(created_at=None, bid_closing_date="2026-10-05T16:00:00Z")) is None


# ── working time, in India, Monday to Saturday ─────────────────────────


WT = service.working_time(BcdCheckSettings(work_start="10:00", work_end="18:00",
                                           timezone="Asia/Kolkata", work_days=[0, 1, 2, 3, 4, 5]))


def test_working_minutes_skip_evenings_nights_and_sunday() -> None:
    # Friday 17:00 → Saturday 11:00: one hour each side of the night.
    assert WT.minutes_between(ist(2026, 10, 2, 17), ist(2026, 10, 3, 11)) == 120
    # Saturday 17:30 → Monday 11:30: Sunday is not a working day.
    assert WT.minutes_between(ist(2026, 10, 3, 17, 30), ist(2026, 10, 5, 11, 30)) == 120
    # Inside one day.
    assert WT.minutes_between(ist(2026, 10, 1, 10), ist(2026, 10, 1, 12)) == 120


def test_after_hours_waits_for_the_next_working_morning() -> None:
    assert not WT.is_working(ist(2026, 10, 1, 21))
    assert WT.next_start(ist(2026, 10, 1, 21)) == ist(2026, 10, 2, 10)
    # Saturday evening waits for Monday.
    assert WT.next_start(ist(2026, 10, 3, 19)) == ist(2026, 10, 5, 10)
    # 10:00 India is 08:30 in the UAE: the zone does the matching.
    assert ist(2026, 10, 1, 10).astimezone(ZoneInfo("Asia/Dubai")).strftime("%H:%M") == "08:30"


# ── the run ────────────────────────────────────────────────────────────


class Fakes:
    """The list, the mailbox and the database's answers, for one person."""

    def __init__(self, tasks):
        self.tasks = tasks
        self.asks: list = []
        self.escalations: list = []
        self.rows: list[BcdCheck] = []

    # SharePoint
    async def lookup_id_for(self, email):
        return "12"

    async def tasks_assigned_to(self, lookup, limit):
        return self.tasks

    async def task(self, item_id):
        return next(t for t in self.tasks if t.id == item_id)

    # mail
    async def send_ask(self, rows, **kw):
        self.asks.append(([r.task_id for r in rows], kw))

    async def send_escalation(self, rows, **kw):
        self.escalations.append(([r.task_id for r in rows], kw))


class FakeSession:
    def __init__(self, fakes: Fakes):
        self.fakes = fakes

    def add(self, row):
        row.id = row.id or uuid.uuid4()
        self.fakes.rows.append(row)

    async def flush(self):
        pass

    async def scalars(self, query):
        text = str(query)
        rows = self.fakes.rows
        if "bcd_checks.task_id" in text.split("FROM")[0] and "status" not in text.split("FROM")[0]:
            return type("R", (), {"all": lambda s: [r.task_id for r in rows]})()
        return type("R", (), {"all": lambda s: [r for r in rows if r.status == BcdCheckStatus.PENDING]})()


def _run(monkeypatch, fakes: Fakes, now: datetime, *, enabled=True, watch_from=None):
    person = type("U", (), {"id": uuid.uuid4(), "email": "vishnu@hamdaz.com",
                            "display_name": "Sri Vishnu", "entra_object_id": "v-id"})()
    row = BcdCheckSettings(enabled=enabled, work_start="10:00", work_end="18:00",
                           timezone="Asia/Kolkata", work_days=[0, 1, 2, 3, 4, 5],
                           escalate_after_minutes=120, only_emails=[], only_title_contains="",
                           watch_from=watch_from or datetime(2026, 9, 1, tzinfo=UTC))
    fs = FollowupSettings(team_id=uuid.uuid4(), test_mail_to=None, digest_sender_email=None)

    async def same(*a, **k):
        return row

    async def follow(*a, **k):
        return fs

    async def people(*a, **k):
        return [person]

    async def leads(session, team_id):
        return ["jasna@hamdaz.com"]

    async def escalate_to(session, team_id):
        return ["althaf@hamdaz.com", "sujeel@hamdaz.com", "sebin@hamdaz.com"]

    async def sender(session, fs_row, user):
        return user

    monkeypatch.setattr(service, "get_settings", same)
    monkeypatch.setattr(service.fu, "get_settings", follow)
    monkeypatch.setattr(service.fu, "watched_people", people)
    monkeypatch.setattr(service.fu, "_sender_for", sender)
    monkeypatch.setattr(service, "team_leads", leads)
    monkeypatch.setattr(service, "escalation_list", escalate_to)
    settings = type("S", (), {"notify_by_email": True, "followup_link_url": "https://x",
                              "frontend_url": "https://x"})()
    return asyncio.run(service.run(FakeSession(fakes), settings=settings, sharepoint=fakes,
                                   mailer=fakes, now=now))


def placeholder_task(item_id: str, created: datetime):
    return task(id=item_id, created_at=iso(created), bid_closing_date=iso(created + timedelta(hours=4)))


def test_a_new_placeholder_is_asked_in_hours_with_the_team_lead_copied(monkeypatch) -> None:
    created = ist(2026, 10, 1, 11)
    fakes = Fakes([placeholder_task("1", created), task(id="2", created_at=iso(created),
                                                        bid_closing_date="2026-10-09T16:00:00Z")])
    report = _run(monkeypatch, fakes, ist(2026, 10, 1, 11, 5))
    assert report.found == 1 and report.asked == 1
    (ids, kw), = fakes.asks
    assert ids == ["1"] and kw["to"] == ["vishnu@hamdaz.com"] and kw["cc"] == ["jasna@hamdaz.com"]


def test_after_hours_it_waits_then_escalates_after_two_working_hours(monkeypatch) -> None:
    created = ist(2026, 10, 1, 20)  # 8 PM India: after hours
    fakes = Fakes([placeholder_task("1", created)])
    _run(monkeypatch, fakes, ist(2026, 10, 1, 20, 5))
    assert fakes.rows and not fakes.asks  # found, but waiting for the morning
    _run(monkeypatch, fakes, ist(2026, 10, 2, 10, 0))
    assert len(fakes.asks) == 1  # asked at 10:00
    _run(monkeypatch, fakes, ist(2026, 10, 2, 11, 55))
    assert not fakes.escalations  # under two working hours
    _run(monkeypatch, fakes, ist(2026, 10, 2, 12, 0))
    (ids, kw), = fakes.escalations
    assert kw["to"] == ["althaf@hamdaz.com", "sujeel@hamdaz.com", "sebin@hamdaz.com"]
    assert kw["cc"] == ["jasna@hamdaz.com"]


def test_a_bcd_corrected_on_the_list_closes_the_check(monkeypatch) -> None:
    created = ist(2026, 10, 1, 11)
    fakes = Fakes([placeholder_task("1", created)])
    _run(monkeypatch, fakes, ist(2026, 10, 1, 11, 5))
    fakes.tasks[0] = task(id="1", created_at=iso(created), bid_closing_date="2026-10-08T16:00:00Z")
    report = _run(monkeypatch, fakes, ist(2026, 10, 1, 11, 10))
    assert report.resolved == 1 and fakes.rows[0].status == BcdCheckStatus.CORRECTED


def test_the_backlog_before_switch_on_is_not_asked(monkeypatch) -> None:
    fakes = Fakes([placeholder_task("1", ist(2026, 9, 20, 11))])
    report = _run(monkeypatch, fakes, ist(2026, 10, 1, 11), watch_from=ist(2026, 10, 1, 9))
    assert report.found == 0 and not fakes.asks


# ── the mail ───────────────────────────────────────────────────────────


def _check(**kw) -> BcdCheck:
    base = dict(id=uuid.uuid4(), task_id="257", task_title="test 6",
                task_url="https://sp/Lists/Proposals/DispForm.aspx?ID=257",
                task_created_at=CREATED, assignee_id=uuid.uuid4(), assignee_email="sebin@hamdaz.com",
                status=BcdCheckStatus.PENDING)
    base.update(kw)
    return BcdCheck(**base)


def test_the_question_links_to_the_sharepoint_edit_form_and_the_confirm_page() -> None:
    row = _check()
    html = bm.ask_body([row], {row.id: "https://x/bcd/1"})
    assert "https://sp/Lists/Proposals/EditForm.aspx?ID=257" in html
    assert "https://x/bcd/1?confirm=1" in html and "UAE" in html


def test_a_test_mail_goes_to_one_address_and_copies_nobody() -> None:
    sent: dict = {}

    class Mailer(bm.BcdMailer):
        def __init__(self):
            pass

        async def send(self, **kw):
            sent.update(kw)

    row = _check()
    sender = type("U", (), {"entra_object_id": "s-id", "email": "sebin@hamdaz.com", "display_name": "Sebin"})()
    asyncio.run(Mailer().send_ask([row], sender=sender, to=["vishnu@hamdaz.com"],
                                  cc=["jasna@hamdaz.com"], links={row.id: "https://x"},
                                  redirect_to="sebin@hamdaz.com"))
    assert (sent["sender"], sent["recipients"], sent["cc"]) == ("sebin@hamdaz.com", ["sebin@hamdaz.com"], [])
    assert "cc jasna@hamdaz.com" in sent["html"] and sent["subject"].startswith("[TEST]")


def test_settings_refuse_a_day_that_ends_before_it_starts() -> None:
    async def same(*a, **k):
        return BcdCheckSettings(work_start="10:00", work_end="18:00", enabled=False)

    import app.bcd.service as s

    original = s.get_settings
    s.get_settings = same
    try:
        with pytest.raises(service.BcdError, match="end after it starts"):
            asyncio.run(service.update_settings(None, actor_id=uuid.uuid4(),
                                                changes={"work_end": "09:00"}))
    finally:
        s.get_settings = original


def test_a_trial_closes_when_its_bcd_changes_whatever_it_was() -> None:
    real = task(id="257", created_at=iso(CREATED), bid_closing_date="2025-09-10T07:00:00Z")
    trial = _check(team_id=None, placeholder_bcd=service.parse_when(real.bid_closing_date))
    assert service.still_unset(trial, real)  # a real date, but unchanged: still open
    moved = task(id="257", created_at=iso(CREATED), bid_closing_date="2026-10-08T07:00:00Z")
    assert service.resolve_against(trial, moved, datetime.now(UTC))
    assert trial.status == BcdCheckStatus.CORRECTED
