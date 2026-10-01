"""The follow-up's daily ask: when the batch runs, what is carried over, and
the mails it sends — no database, no list, no mailbox."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime

from app.followups import mailer as m
from app.followups import service
from app.models.followup import FollowupSettings, TaskFollowup

# 4:00 PM in Dubai, the zone these tests set.
ASK = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _settings(**kw) -> FollowupSettings:
    base = dict(
        ask_mode="daily", ask_time="16:00", digest_timezone="Asia/Dubai", ask_last_run_on=None
    )
    return FollowupSettings(**{**base, **kw})


def _row(title: str, due: datetime, *, carried: bool = False, status: str | None = None):
    return TaskFollowup(
        id=uuid.uuid4(),
        task_id=str(abs(hash(title)) % 100000),
        task_title=title,
        due_at=due,
        status_at_ask=status,
        carried_over=carried,
        assignee_email="sebin@hamdaz.com",
    )


def test_the_batch_runs_once_a_day_from_the_ask_time() -> None:
    row = _settings()
    assert not service.daily_batch_due(row, datetime(2026, 9, 30, 11, 59, tzinfo=UTC))
    assert service.daily_batch_due(row, ASK)
    # Late is still today: a server down at four asks when it is back.
    assert service.daily_batch_due(row, datetime(2026, 9, 30, 17, 0, tzinfo=UTC))
    row.ask_last_run_on = date(2026, 9, 30)
    assert not service.daily_batch_due(row, datetime(2026, 9, 30, 17, 0, tzinfo=UTC))
    # The other mode never batches.
    assert not service.daily_batch_due(_settings(ask_mode="after_due"), ASK)


def test_a_task_due_after_yesterdays_ask_is_carried_over() -> None:
    row = _settings()
    yesterday_evening = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)  # 6:30 PM Dubai
    this_morning = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)  # 10:00 AM Dubai
    assert service.is_carried_over(row, yesterday_evening, ASK)
    assert not service.is_carried_over(row, this_morning, ASK)


def test_the_ask_time_must_come_before_the_report(monkeypatch) -> None:
    row = _settings(digest_enabled=True, digest_time="18:00")
    monkeypatch.setattr(service, "get_settings", _async(row))
    try:
        asyncio.run(_update({"ask_time": "18:30"}))
    except service.FollowupError as exc:
        assert "before the end-of-day report" in str(exc)
    else:
        raise AssertionError("an ask after the report was accepted")


async def _update(changes: dict) -> None:
    class Session:
        async def flush(self):
            return None

    await service.update_settings(Session(), actor_id=uuid.uuid4(), changes=changes)


def _async(value):
    async def inner(*_a, **_k):
        return value

    return inner


def test_one_mail_lists_today_then_carried_over_apart() -> None:
    today = _row("Today's bid", datetime(2026, 9, 30, 6, 0, tzinfo=UTC), status="Not Submitted")
    old = _row("Yesterday's late bid", datetime(2026, 9, 29, 14, 30, tzinfo=UTC), carried=True)
    links = {today.id: "https://x/followups/1", old.id: "https://x/followups/2"}
    html = m.batch_body([today, old], links, ask_time="16:00")
    assert html.index("Due today (1)") < html.index("Carried over from yesterday (1)")
    assert "after yesterday's ask time (16:00)" in html
    assert "https://x/followups/2?false-positive=1" in html
    assert m.batch_subject([today, old]) == "Reason Required: 2 tasks past due, not submitted"


def test_each_persons_report_lists_answered_and_unanswered_and_goes_to_sebin() -> None:
    sent: dict = {}

    class Mailer(m.FollowupMailer):
        def __init__(self):
            pass

        async def send(self, **kw):
            sent.update(kw)

    answered = _row("A bid", datetime(2026, 9, 30, 6, 0, tzinfo=UTC), status="Not Submitted")
    answered.reason = "Supplier quote came late"
    answered.status = "answered"
    silent = _row("Another bid", datetime(2026, 9, 29, 14, 30, tzinfo=UTC), carried=True)
    silent.status = "no_response"
    person = type("U", (), {"display_name": "Anu"})()
    asyncio.run(
        Mailer().send_person_report(
            [answered, silent], person=person, sender_email="boss@hamdaz.com",
            recipients=["boss@hamdaz.com"], day=date(2026, 9, 30), link="https://x",
            redirect_to="sebin@hamdaz.com",
            notes={silent.task_id: service.TaskNotes(
                "Waiting on the trade licence renewal", "Called the free zone on Monday"
            )},
        )
    )
    # From and to the testing address only, saying who it was meant for.
    assert (sent["sender"], sent["recipients"]) == ("sebin@hamdaz.com", ["sebin@hamdaz.com"])
    assert sent["subject"] == "[TEST] Reasons: Anu — 30 Sep 2026"
    assert "would have gone to boss@hamdaz.com" in sent["html"]
    html = sent["html"]
    # The task's own Remarks and Working notes, and what they wrote on the form.
    assert "Waiting on the trade licence renewal" in html
    assert "Called the free zone on Monday" in html and "Working notes" in html
    assert "Supplier quote came late" in html and "Other note" in html
    assert "Not answered" in html and "No reason given" not in html
    assert "(carried over from the previous day)" in html


def test_remarks_are_read_as_plain_text() -> None:
    assert service._plain_remark("<div>Quotes late<br>asked for&nbsp;extension</div>") == (
        "Quotes late\nasked for\xa0extension"
    )
    assert service._plain_remark(None) == ""


def test_person_reports_go_once_after_the_closing_time() -> None:
    row = _settings(digest_time="18:00", summaries_last_sent_on=None)
    before = datetime(2026, 9, 30, 13, 59, tzinfo=UTC)  # 5:59 PM Dubai
    after = datetime(2026, 9, 30, 14, 1, tzinfo=UTC)
    assert not service.person_reports_due(row, before)
    assert service.person_reports_due(row, after)
    row.summaries_last_sent_on = date(2026, 9, 30)
    assert not service.person_reports_due(row, after)
    assert not service.person_reports_due(_settings(ask_mode="after_due"), after)


def test_the_due_today_panel_shows_the_batch_a_task_will_be_in() -> None:
    row = _settings()
    morning = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)  # 10:00 AM Dubai
    evening = datetime(2026, 9, 30, 13, 30, tzinfo=UTC)  # 5:30 PM Dubai
    assert service.ask_at_for(row, morning, morning, 20, False) == ASK
    # After today's ask time: tomorrow's batch, carried over.
    tomorrow = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert service.ask_at_for(row, evening, evening, 20, False) == tomorrow


def test_an_answer_made_with_the_notes_is_not_repeated_in_the_report() -> None:
    row = _row("A bid", datetime(2026, 9, 30, 6, 0, tzinfo=UTC))
    notes = service.TaskNotes("Quotes came late", "Chased twice")
    row.reason = notes.text + "\n\nAlso asked for an extension"
    assert m._notes_and_note(row, {row.task_id: notes}) == (
        "Quotes came late", "Chased twice", "Also asked for an extension"
    )
    row.reason = None
    assert m._notes_and_note(row, {}) == ("—", "—", "—")


class _Session:
    async def flush(self):
        pass


def _answer(monkeypatch, row, remarks: str, working: str = "", **kw):
    async def no_managers(*a, **k):
        return []

    async def notes_of(session, sharepoint, ids):
        return {i: service.TaskNotes(remarks, working) for i in ids}

    async def no_notice(*a, **k):
        pass

    from app.followups import digest

    monkeypatch.setattr(service, "managers_of", no_managers)
    monkeypatch.setattr(service, "task_notes", notes_of)
    monkeypatch.setattr(digest, "recipients", no_managers)
    monkeypatch.setattr(service.notifications, "notify", no_notice)
    user = type("U", (), {"id": row.assignee_id, "email": "sebin@hamdaz.com", "display_name": "S"})()
    return asyncio.run(
        service.answer(
            _Session(), row, user=user, settings=None, mailer=None,
            followup_settings=_settings(), **kw,
        )
    )


def _open_row():
    row = _row("A bid", datetime(2026, 9, 30, 6, 0, tzinfo=UTC))
    row.assignee_id, row.status = uuid.uuid4(), "pending"
    return row


def test_the_reason_can_be_the_tasks_remarks_with_anything_else_after(monkeypatch) -> None:
    row = _answer(monkeypatch, _open_row(), "Lots could not be selected",
                  reason="Extended by 2 days", use_remarks=True)
    assert row.reason == "Lots could not be selected\n\nExtended by 2 days"
    assert row.status == "answered"
    row = _answer(monkeypatch, _open_row(), "Lots could not be selected", reason="", use_remarks=True)
    assert row.reason == "Lots could not be selected"
    # Working notes come after the Remarks, named; either alone will do.
    row = _answer(monkeypatch, _open_row(), "Lots could not be selected", "Chased the buyer",
                  reason="", use_remarks=True)
    assert row.reason == "Lots could not be selected\n\nWorking notes: Chased the buyer"
    row = _answer(monkeypatch, _open_row(), "", "Chased the buyer", reason="", use_remarks=True)
    assert row.reason == "Working notes: Chased the buyer"


def test_answering_with_empty_remarks_says_to_write_the_reason(monkeypatch) -> None:
    import pytest

    with pytest.raises(service.FollowupError, match="no Remarks or Working notes"):
        _answer(monkeypatch, _open_row(), "", reason="", use_remarks=True)


def test_a_trial_answer_reaches_nobody_but_the_tester(monkeypatch) -> None:
    told: list = []
    boss = type("U", (), {"id": uuid.uuid4(), "email": "boss@hamdaz.com"})()

    async def managers(*a, **k):
        return [boss]

    async def notice(session, *, users, **k):
        told.extend(users)

    async def nobody(*a, **k):
        return []

    from app.followups import digest

    monkeypatch.setattr(service, "managers_of", managers)
    monkeypatch.setattr(digest, "recipients", nobody)
    monkeypatch.setattr(service.notifications, "notify", notice)
    user = type("U", (), {"id": uuid.uuid4(), "email": "sebin@hamdaz.com", "display_name": "S"})()

    def answer(team_id):
        row = _open_row()
        row.assignee_id, row.team_id = user.id, team_id
        return asyncio.run(
            service.answer(
                _Session(), row, user=user, reason="Supplier late", settings=None,
                mailer=None, followup_settings=_settings(),
            )
        )

    assert answer(None).status == "answered" and told == []
    answer(uuid.uuid4())
    assert told == [boss]


def test_the_ask_shows_the_tasks_remarks_and_working_notes() -> None:
    late = _row("Late bid", datetime(2026, 9, 30, 6, 0, tzinfo=UTC), status="Not Submitted")
    bare = _row("Bare bid", datetime(2026, 9, 30, 7, 0, tzinfo=UTC))
    notes = {late.task_id: service.TaskNotes("Lots could not be selected", "Chased\nthe buyer")}
    html = m.batch_body([late, bare], {late.id: "https://x/1", bare.id: "https://x/2"},
                        ask_time="16:00", notes=notes)
    assert "Lots could not be selected" in html and "Chased\nthe buyer" in html
    assert html.count("Not written yet") == 2  # the bare task, both columns
    single = m.ask_body(late, "https://x/1", notes=notes)
    assert "Working notes" in single and "Lots could not be selected" in single
    # A task with no Submission Status gets the other mail, with the notes too.
    assert "Not written yet" in m.ask_body(bare, "https://x/2", notes=notes)
