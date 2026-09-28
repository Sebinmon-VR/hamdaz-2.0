"""When a task is asked about, and what the two emails say.

No database, no SharePoint, no mail: the rule is a function of one task, a
clock and the settings, and the emails are functions of one row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.followups import mailer
from app.followups.service import decide, due_of
from app.models.followup import FollowupStatus, TaskFollowup
from app.models.user import User
from app.proposals.sharepoint import ProposalTask

DUE = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
WATCH = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def task(**overrides) -> ProposalTask:
    fields = dict(
        id="901", title="test — overdue follow-up", status="In Progress", priority=None,
        assigned_to_lookup_id="12", assigned_to_name="Sebin", start_date=None,
        due_date=None, bid_closing_date=DUE.isoformat().replace("+00:00", "Z"),
        end_user="ADNOC", submission_status=None, current_type=None, order_status=None,
        negotiation=None, quote_no=None, remarks=None, working_notes=None,
        created_at=None, modified_at=None,
    )
    fields.update(overrides)
    return ProposalTask(**fields)


def at(minutes_after_due: int) -> datetime:
    return DUE + timedelta(minutes=minutes_after_due)


def test_asked_once_the_grace_after_the_due_time_has_passed() -> None:
    assert not decide(task(), now=at(19), grace_minutes=20, watch_from=WATCH).ask
    ruled = decide(task(), now=at(20), grace_minutes=20, watch_from=WATCH)
    assert ruled.ask and ruled.due_at == DUE


def test_a_submitted_bid_is_never_asked_about() -> None:
    ruled = decide(task(submission_status="Submitted"), now=at(60), grace_minutes=20, watch_from=WATCH)
    assert not ruled.ask and ruled.why == "finished"


def test_completed_but_not_submitted_is_still_asked_about() -> None:
    """The case from the list: Status says Completed, Submission Status says
    Not Submitted. The submission decides."""
    both = task(status="Completed", submission_status="Not Submitted")
    assert decide(both, now=at(60), grace_minutes=20, watch_from=WATCH).ask
    assert decide(task(status="Completed"), now=at(60), grace_minutes=20, watch_from=WATCH).ask


def test_marked_not_submitted_is_asked_at_once_not_at_the_deadline() -> None:
    marked = task(status="Completed", submission_status="Not Submitted")
    ruled = decide(marked, now=at(-300), grace_minutes=20, watch_from=WATCH)
    assert ruled.ask and ruled.why == "marked not submitted"
    # A blank submission status is still work in hand: it waits for the due time.
    assert not decide(task(), now=at(-300), grace_minutes=20, watch_from=WATCH).ask


def test_a_task_with_no_status_is_asked_about_because_that_is_the_point() -> None:
    assert decide(task(status=None), now=at(60), grace_minutes=20, watch_from=WATCH).ask


def test_nothing_due_before_the_watch_began_is_asked_about() -> None:
    old = task(bid_closing_date="2025-11-27T00:00:00Z")
    ruled = decide(old, now=at(60), grace_minutes=20, watch_from=WATCH)
    assert not ruled.ask and ruled.why == "due before the watch began"
    assert not decide(task(), now=at(60), grace_minutes=20, watch_from=None).ask


def test_the_title_filter_keeps_a_trial_to_the_test_tasks() -> None:
    real = task(title="RFQ 6000129780 Customization")
    assert not decide(real, now=at(60), grace_minutes=20, watch_from=WATCH, title_contains="test").ask
    assert decide(task(), now=at(60), grace_minutes=20, watch_from=WATCH, title_contains="TEST").ask


def test_the_bid_closing_time_leads_and_a_due_date_means_the_end_of_that_day() -> None:
    # BCD UAE Time 5:51 PM arrives as 13:51Z, and that is the moment.
    both = task(due_date="2026-09-27T20:00:00Z", bid_closing_date="2026-09-28T13:51:46Z")
    assert due_of(both) == datetime(2026, 9, 28, 13, 51, 46, tzinfo=UTC)
    # A date-only Due Date (midnight in the UAE, stored as 20:00Z the day
    # before) is due at 23:59:59 UAE time that day, not at its midnight.
    assert due_of(task(due_date="2026-09-27T20:00:00Z", bid_closing_date=None)) == datetime(
        2026, 9, 28, 19, 59, 59, tzinfo=UTC
    )
    assert due_of(task(due_date=None, bid_closing_date=None)) is None
    assert not decide(
        task(due_date=None, bid_closing_date=None), now=at(60), grace_minutes=20, watch_from=WATCH
    ).ask


def row(**overrides) -> TaskFollowup:
    fields = dict(
        task_id="901", task_title="test — overdue follow-up", task_url="https://example/DispForm.aspx?ID=901",
        end_user="ADNOC", status_at_ask="Not Started", due_at=DUE,
        assignee_email="sebin@hamdaz.com", status=FollowupStatus.PENDING,
    )
    fields.update(overrides)
    followup = TaskFollowup(**fields)
    followup.assignee = User(email="sebin@hamdaz.com", display_name="Sebin", entra_object_id="x")
    return followup


def test_the_ask_offers_the_way_out_for_a_task_already_updated() -> None:
    link = "https://calm-sky-08cea4100.6.azurestaticapps.net/followups/abc"
    body = mailer.ask_body(row(), link)

    assert "ignore this mail" in body
    assert f"{link}?false-positive=1" in body
    assert "Give the reason" in body
    # In the Gulf's own time: 10:00 UTC is 14:00 in Dubai.
    assert "14:00 UAE" in body
    assert "due time has passed" in body
    assert "Not submitted by the due time" in mailer.ask_subject(row())


def test_an_early_ask_says_it_was_marked_not_submitted() -> None:
    body = mailer.ask_body(row(), "https://x/followups/abc", early=True)
    assert "marked <b>Not Submitted</b>" in body
    assert "Marked Not Submitted" in mailer.ask_subject(row(), early=True)


def test_mail_links_point_at_production_not_localhost() -> None:
    import uuid as _uuid

    from app.core.config import get_settings
    from app.followups.service import form_link

    link = form_link(get_settings(), _uuid.UUID(int=1))
    assert link.startswith("https://") and "localhost" not in link


def test_the_reason_mail_carries_what_was_said_and_escapes_it() -> None:
    answered = row(reason="Waiting on <supplier> pricing\nsince Friday", answered_at=at(90))
    body = mailer.reason_body(answered, "Sebin", "http://localhost:3000/followups/abc")

    assert "Waiting on &lt;supplier&gt; pricing<br>since Friday" in body
    assert "Sebin" in mailer.reason_subject(answered, "Sebin")


def test_today_is_the_gulfs_day_not_utcs() -> None:
    from app.followups.service import today_bounds

    # 22:00 UTC on the 27th is already 02:00 on the 28th in Dubai.
    start, end = today_bounds(datetime(2026, 9, 27, 22, 0, tzinfo=UTC))
    assert start == datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


def test_the_screen_says_when_a_task_is_outside_the_trial() -> None:
    from app.followups.service import watches
    from app.models.followup import FollowupSettings

    trial = FollowupSettings(
        enabled=True, only_emails=["sebin@hamdaz.com"], only_title_contains="test",
        watch_from=WATCH,
    )
    sebin = User(email="sebin@hamdaz.com", display_name="Sebin", entra_object_id="x")
    fasna = User(email="fasna@hamdaz.com", display_name="Fasna", entra_object_id="y")

    assert watches(trial, sebin, task()) is None
    assert "outside the trial" in watches(trial, fasna, task())
    assert "in the title" in watches(trial, sebin, task(title="RFQ 6000151129"))
    trial.enabled = False
    assert watches(trial, sebin, task()) == "The follow-up is switched off."


def test_due_today_lists_todays_tasks_soonest_first_with_the_ask_time() -> None:
    from app.followups.service import due_today_rows

    person = User(email="sebin@hamdaz.com", display_name="Sebin", entra_object_id="x")
    later = task(id="902", bid_closing_date="2026-09-28T15:00:00Z")
    done = task(id="903", submission_status="Submitted", bid_closing_date="2026-09-28T06:00:00Z")
    tomorrow = task(id="904", bid_closing_date="2026-09-29T21:00:00Z")
    rows = due_today_rows(
        [(person, [later, task(), done, tomorrow])],
        now=datetime(2026, 9, 28, 8, 0, tzinfo=UTC), grace_minutes=20, asked={},
    )

    assert [r["task_id"] for r in rows] == ["901", "902", "903"]
    assert rows[0]["ask_at"] == DUE + timedelta(minutes=20)
    assert rows[-1]["finished"] is True
