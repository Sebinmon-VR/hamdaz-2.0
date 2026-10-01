"""The status reminder: which tasks are reminded about, when, what the mail
and the form say, and what an answer writes to the list.

No database, no SharePoint, no mail: fakes stand in for each.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.followup import FollowupSettings
from app.models.status_reminder import ReminderStatus, StatusReminder, StatusReminderSettings
from app.reminders import mailer as rm
from app.reminders import service
from app.reminders.service import LiveTask, ReminderError, changes_for, decide
from tests.test_followups import stored, task

# A Thursday morning in the UAE.
NOW = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


def due_in(hours: float):
    return stored(NOW + timedelta(hours=hours))


# ── which tasks ────────────────────────────────────────────────────────


def test_a_task_due_within_two_days_and_still_open_is_reminded_about() -> None:
    ruled = decide(task(bid_closing_date=due_in(30)), now=NOW, days_before=2)
    assert ruled.ask and ruled.why == "due soon"


def test_completed_or_submitted_is_never_reminded_about() -> None:
    for done in (dict(status="Completed"), dict(submission_status="Submitted")):
        ruled = decide(task(bid_closing_date=due_in(30), **done), now=NOW, days_before=2)
        assert not ruled.ask and ruled.why == "completed or submitted"


def test_in_progress_on_hold_not_started_and_blank_are_all_reminded_about() -> None:
    for status in ("In Progress", "On Hold", "Not Started", None):
        assert decide(task(bid_closing_date=due_in(30), status=status), now=NOW, days_before=2).ask


def test_too_far_ahead_or_already_due_is_not_this_reminders() -> None:
    assert decide(task(bid_closing_date=due_in(49)), now=NOW, days_before=2).why == (
        "not yet within the reminder window"
    )
    assert decide(task(bid_closing_date=due_in(-1)), now=NOW, days_before=2).why == "already due"
    assert decide(task(bid_closing_date=None), now=NOW, days_before=2).why == "no due date"


def test_the_trial_title_filter_applies() -> None:
    other = task(bid_closing_date=due_in(30), title="RFQ 6000150752")
    assert decide(other, now=NOW, days_before=2, title_contains="test").why == "title outside the filter"


# ── when ───────────────────────────────────────────────────────────────


def test_it_runs_once_a_day_from_the_reminder_time() -> None:
    fs = FollowupSettings(digest_timezone="Asia/Dubai")
    row = StatusReminderSettings(enabled=True, ask_time="10:00", last_run_on=None)
    assert not service.is_due(fs, row, datetime(2026, 10, 1, 5, 59, tzinfo=UTC))  # 09:59 UAE
    assert service.is_due(fs, row, datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
    row.last_run_on = date(2026, 10, 1)
    assert not service.is_due(fs, row, datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    row.enabled, row.last_run_on = False, None
    assert not service.is_due(fs, row, datetime(2026, 10, 1, 9, 0, tzinfo=UTC))


# ── closing the unanswered ─────────────────────────────────────────────


def _reminder(**kw) -> StatusReminder:
    base = dict(
        id=uuid.uuid4(), task_id="901", task_title="test 6", due_at=NOW + timedelta(hours=30),
        assignee_id=uuid.uuid4(), assignee_email="sebin@hamdaz.com",
        status=ReminderStatus.PENDING, changes={},
    )
    base.update(kw)
    return StatusReminder(**base)


def test_an_unanswered_reminder_closes_when_the_task_is_done_or_moves() -> None:
    due = service.fu.due_of(task(bid_closing_date=due_in(30)))
    done = _reminder(due_at=due)
    assert service.close_pending([done], task(bid_closing_date=due_in(30), status="Completed"), NOW) == 1
    assert "Completed or Submitted" in done.closed_note
    moved = _reminder(due_at=due)
    assert service.close_pending([moved], task(bid_closing_date=due_in(60)), NOW) == 1
    assert "due date changed" in moved.closed_note
    standing = _reminder(due_at=due)
    assert service.close_pending([standing], task(bid_closing_date=due_in(30)), NOW) == 0
    assert standing.status == ReminderStatus.PENDING


# ── what an answer changes ─────────────────────────────────────────────


LIST = LiveTask(
    values={"Status": "In Progress", "SubmissionStatus": "", "Remarks": "Quotes awaited",
            "WorkingNotes": ""},
    choices={"Status": ["Not Started", "In Progress", "Completed", "On Hold"],
             "SubmissionStatus": ["Submitted", "Not Submitted"]},
)


def test_only_what_was_changed_is_written() -> None:
    wanted = {"Status": "Completed", "SubmissionStatus": "", "Remarks": "Quotes awaited",
              "WorkingNotes": "Sent to the buyer\nawaiting reply"}
    assert changes_for(wanted, dict(LIST.values), LIST) == {
        "Status": "Completed", "WorkingNotes": "Sent to the buyer\nawaiting reply",
    }
    # Nothing changed: confirmed as it is.
    assert changes_for(dict(LIST.values), dict(LIST.values), LIST) == {}


def test_a_field_changed_on_the_list_since_the_form_opened_is_refused() -> None:
    seen = {**LIST.values, "Remarks": "Old remark"}
    with pytest.raises(ReminderError, match="Remarks was changed on the Proposals list"):
        changes_for({"Remarks": "My new remark"}, seen, LIST)
    # Untouched by the person, a change on the list is no conflict — and the
    # form's stale copy of it is not written back over it.
    assert changes_for({"Remarks": "Quotes awaited"}, seen, LIST) == {}
    assert changes_for({**seen, "Status": "Completed"}, seen, LIST) == {"Status": "Completed"}


def test_a_choice_must_be_one_of_the_lists() -> None:
    with pytest.raises(ReminderError, match="not one of the list's choices"):
        changes_for({"Status": "Nearly there"}, dict(LIST.values), LIST)
    with pytest.raises(ReminderError, match="Choose a Status"):
        changes_for({"Status": ""}, dict(LIST.values), LIST)


class _List:
    """A SharePoint stand-in: one task, and a record of what was written."""

    def __init__(self, fail: bool = False):
        self.written: list = []
        self.fail = fail

    async def task(self, item_id):
        return task(id=item_id, bid_closing_date=due_in(30), remarks="Quotes awaited")

    async def list_columns(self):
        return [{"name": "Status", "choices": ["Not Started", "In Progress", "Completed", "On Hold", "Choice 5"]},
                {"name": "SubmissionStatus", "choices": ["Submitted", "Not Submitted"]}]

    async def update_task(self, item_id, fields):
        if self.fail:
            raise RuntimeError("SharePoint said no")
        self.written.append((item_id, fields))


class _Session:
    async def flush(self):
        pass


def _answer(monkeypatch, *, write: bool, sp: _List, wanted: dict) -> StatusReminder:
    async def settings_row(session):
        return StatusReminderSettings(write_sharepoint=write)

    monkeypatch.setattr(service, "get_settings", settings_row)
    row = _reminder()
    user = type("U", (), {"id": row.assignee_id})()
    return asyncio.run(
        service.answer(_Session(), row, user=user, wanted=wanted, seen={}, sharepoint=sp)
    )


def test_with_the_write_off_the_answer_is_kept_and_the_list_untouched(monkeypatch) -> None:
    sp = _List()
    row = _answer(monkeypatch, write=False, sp=sp, wanted={"Status": "Completed"})
    assert row.status == ReminderStatus.ANSWERED and row.changes == {"Status": "Completed"}
    assert sp.written == [] and row.written_at is None and row.write_error is None


def test_with_the_write_on_only_the_changes_reach_the_list(monkeypatch) -> None:
    sp = _List()
    row = _answer(monkeypatch, write=True, sp=sp,
                  wanted={"Status": "Completed", "Remarks": "Quotes awaited"})
    assert sp.written == [("901", {"Status": "Completed"})] and row.written_at is not None


def test_a_refused_write_keeps_the_answer_and_says_why(monkeypatch) -> None:
    row = _answer(monkeypatch, write=True, sp=_List(fail=True), wanted={"Status": "On Hold"})
    assert row.status == ReminderStatus.ANSWERED and "SharePoint said no" in row.write_error


def test_only_the_person_reminded_may_answer(monkeypatch) -> None:
    async def settings_row(session):
        return StatusReminderSettings(write_sharepoint=True)

    monkeypatch.setattr(service, "get_settings", settings_row)
    stranger = type("U", (), {"id": uuid.uuid4()})()
    with pytest.raises(service.ReminderForbidden):
        asyncio.run(service.answer(_Session(), _reminder(), user=stranger, wanted={},
                                   seen={}, sharepoint=_List()))


def test_unnamed_choices_are_not_offered() -> None:
    got = asyncio.run(service.choices(_List()))
    assert got["Status"] == ["Not Started", "In Progress", "Completed", "On Hold"]


# ── the mail ───────────────────────────────────────────────────────────


def test_the_mail_lists_each_task_with_its_four_columns_and_button() -> None:
    a = _reminder(status_at_ask="In Progress", remarks_at_ask="Quotes awaited",
                  working_notes_at_ask="Line one\nline two")
    b = _reminder(task_title="RFQ 6000150752", status_at_ask=None)
    html = rm.reminder_body([a, b], {a.id: "https://x/reminders/1", b.id: "https://x/reminders/2"})
    for text in ("In Progress", "Quotes awaited", "Line one\nline two", "Not set",
                 "Not written yet", "https://x/reminders/1", "https://x/reminders/2",
                 "Update Status"):
        assert text in html
    assert rm.reminder_subject([a, b]) == "Status Update Needed: 2 tasks due soon"
    assert rm.reminder_subject([a]) == "Status Update Needed: test 6"


def test_the_mail_goes_from_and_to_the_testing_address_when_set() -> None:
    sent: dict = {}

    class Mailer(rm.ReminderMailer):
        def __init__(self):
            pass

        async def send(self, **kw):
            sent.update(kw)

    row = _reminder()
    sender = type("U", (), {"entra_object_id": "boss-id", "display_name": "Boss"})()
    asyncio.run(Mailer().send_reminders([row], sender=sender, links={row.id: "https://x"},
                                        redirect_to="sebin@hamdaz.com"))
    assert (sent["sender"], sent["recipients"]) == ("sebin@hamdaz.com", ["sebin@hamdaz.com"])
    assert sent["subject"].startswith("[TEST] Status Update Needed")


# ── the daily run ──────────────────────────────────────────────────────


def test_the_run_sends_one_mail_per_person_and_never_twice(monkeypatch) -> None:
    sebin = type("U", (), {"id": uuid.uuid4(), "email": "sebin@hamdaz.com",
                           "display_name": "Sebin", "entra_object_id": "sebin-id"})()
    row = StatusReminderSettings(enabled=True, ask_time="10:00", days_before=2,
                                 only_emails=[], only_title_contains="", last_run_on=None)
    fs = FollowupSettings(digest_timezone="Asia/Dubai", team_id=uuid.uuid4(), test_mail_to=None)
    tasks = [
        task(id="1", title="test due tomorrow", bid_closing_date=due_in(20)),
        task(id="2", title="test also soon", bid_closing_date=due_in(40), status="On Hold"),
        task(id="3", title="test done", bid_closing_date=due_in(20), status="Completed"),
        task(id="4", title="test far off", bid_closing_date=due_in(100)),
    ]
    made: list = []
    mails: list = []

    async def same(*a, **k):
        return row

    async def follow(*a, **k):
        return fs

    async def people(*a, **k):
        return [sebin]

    async def sender(session, fs_row, user):
        return user

    async def record(session, user, t, due, *, team_id):
        r = _reminder(task_id=t.id, task_title=t.title, due_at=due, team_id=team_id)
        made.append(r)
        return r

    class List:
        async def lookup_id_for(self, email):
            return "15"

        async def tasks_assigned_to(self, lookup, limit):
            return tasks

    class Mailer:
        async def send_reminders(self, rows, **kw):
            mails.append([r.task_id for r in rows])

    class Session:
        async def scalars(self, *a):
            return type("R", (), {"all": lambda self: list(made)})()

        async def flush(self):
            pass

    monkeypatch.setattr(service, "get_settings", same)
    monkeypatch.setattr(service.fu, "get_settings", follow)
    monkeypatch.setattr(service, "watched_people", people)
    monkeypatch.setattr(service.fu, "_sender_for", sender)
    monkeypatch.setattr(service, "_record", record)

    async def nothing_held(*a, **k):
        return set()

    from app.bcd import service as bcd

    monkeypatch.setattr(bcd, "held_ids", nothing_held)
    settings = type("S", (), {"notify_by_email": True, "followup_link_url": "https://x",
                              "frontend_url": "https://x"})()

    def go(force=False):
        return asyncio.run(service.run(Session(), settings=settings, sharepoint=List(),
                                       mailer=Mailer(), force=force, now=NOW))

    report = go()
    assert report.asked == 2 and mails == [["1", "2"]]
    assert row.last_run_on == date(2026, 10, 1)
    # The same day again: not due. Forced: nothing new to ask about.
    assert not go().ran
    assert go(force=True).asked == 0 and len(mails) == 1


# -- the report on each answer --------------------------------------------


def test_the_report_shows_each_column_before_and_after() -> None:
    row = _reminder(changes={"Status": "Completed", "Remarks": "Sent on time"})
    html = rm.update_body(
        row, "Sebin", {"Status": "In Progress", "SubmissionStatus": "", "Remarks": "",
                       "WorkingNotes": "Chased"},
        "https://x/reminders/1", writes=False,
    )
    assert "In Progress  →  Completed" in html and "blank  →  Sent on time" in html
    assert "Chased  (unchanged)" in html
    assert "writing to the Proposals list is switched off" in html
    assert rm.update_subject(row, "Sebin") == "Status Updated: Sebin — test 6"


def _reporting(monkeypatch, *, team_id, test_mail_to=None):
    sent: list = []
    sebin = type("U", (), {"id": uuid.uuid4(), "email": "sebin@hamdaz.com", "display_name": "Sebin"})()
    boss = type("U", (), {"email": "sujeel@hamdaz.com"})()

    async def follow(*a, **k):
        return FollowupSettings(team_id=uuid.uuid4(), test_mail_to=test_mail_to)

    async def managers(session, team):
        return [boss, sebin]

    class Mailer:
        async def send_update(self, row, **kw):
            sent.append(kw)

    monkeypatch.setattr(service.fu, "get_settings", follow)
    monkeypatch.setattr(service.fu, "managers_of", managers)
    settings = type("S", (), {"notify_by_email": True, "followup_link_url": "https://x",
                              "frontend_url": "https://x"})()
    asyncio.run(service._report(None, _reminder(team_id=team_id), sebin, before={},
                                settings=settings, mailer=Mailer(), writes=False))
    return sent[0]


def test_a_trial_answer_is_reported_from_and_to_the_person_only(monkeypatch) -> None:
    kw = _reporting(monkeypatch, team_id=None)
    # Addressed to the managers it would have reached, but redirected to the
    # person who answered: the TEST banner names them, nobody else is mailed.
    assert kw["recipients"] == ["sujeel@hamdaz.com"] and kw["redirect_to"] == "sebin@hamdaz.com"


def test_a_real_answer_goes_to_the_managers_but_not_the_person(monkeypatch) -> None:
    kw = _reporting(monkeypatch, team_id=uuid.uuid4())
    assert kw["recipients"] == ["sujeel@hamdaz.com"] and kw["redirect_to"] is None
    assert _reporting(monkeypatch, team_id=uuid.uuid4(), test_mail_to="sebin@hamdaz.com")[
        "redirect_to"
    ] == "sebin@hamdaz.com"
