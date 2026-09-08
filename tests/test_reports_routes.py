"""Reports over HTTP: filing one, and who can then see it.

The lifecycle is worth testing because it is short and irreversible — a report
is filed once and never edited again — but the half that earns the most tests
is the boundary. A report names customers, prices and what somebody is stuck
on. The person who wrote it expected their manager to read it and not the
company, and every test below that ends in 404 is that expectation being kept.

SharePoint is stubbed. What is exercised for real is everything else: the
prefill turning a task list into report rows, the metrics that follow from
them, the submission locking the report, and the visibility rules answering the
same tool differently for a manager and for a colleague.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.forms.service import seed_templates as seed_form_templates
from app.reports import service as reports_service
from app.roles import service as roles_service
from app.teams import service as teams_service

SESSION_COOKIE = "hamdaz_session"


# ── standing in for the Proposals list ─────────────────────────────────


def _task(
    task_id: str,
    title: str,
    *,
    is_open: bool = True,
    closing: date | None = None,
    attachments: bool = False,
):
    """One Proposals row, with the properties the prefill actually reads."""
    return SimpleNamespace(
        id=task_id,
        title=title,
        status="In Progress" if is_open else "Completed",
        effective_status="In Progress" if is_open else "Completed",
        priority="High",
        end_user="Kuwait Oil",
        quote_no="QT-1001",
        closing_date=closing,
        deadline=closing.isoformat() if closing else None,
        is_open=is_open,
        has_attachments=attachments,
        web_url=f"https://sharepoint.test/DispForm.aspx?ID={task_id}",
        attachments_url=(
            f"https://sharepoint.test/Attachments/{task_id}" if attachments else None
        ),
    )


class FakeSharePoint:
    """Answers the two calls the prefill makes, and records that it was asked."""

    def __init__(self) -> None:
        self.tasks = [
            _task("101", "Kuwait pump bid", closing=date(2099, 1, 1), attachments=True),
            _task("102", "Dubai valve enquiry", closing=date(2020, 1, 1)),
            _task("103", "Finished job", is_open=False, closing=date(2020, 6, 1)),
        ]
        self.asked_for: list[str] = []
        self.fail = False

    async def lookup_id_for(self, email: str):
        self.asked_for.append(email)
        return "77"

    async def tasks_assigned_to(self, lookup_id: str, *, limit: int = 200):
        if self.fail:
            from app.proposals.sharepoint import SharePointError

            raise SharePointError("SharePoint is down")
        return list(self.tasks)


class SilentMailer:
    """Records what would have been sent instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    async def send_submitted(self, report, recipients, *, link):
        if self.fail:
            from app.core.mail import MailError

            raise MailError("Graph refused the token")
        self.sent.append(
            {"report_id": report.id, "recipients": list(recipients), "link": link}
        )
        return {}


# ── fixtures ───────────────────────────────────────────────────────────


def _app(client):
    """The FastAPI app behind the test client, for swapping a dependency out."""
    return client._transport.app


@pytest.fixture
def sharepoint(client) -> FakeSharePoint:
    fake = FakeSharePoint()
    _app(client).state.sharepoint = fake
    return fake


@pytest.fixture
def mailer(client) -> SilentMailer:
    fake = SilentMailer()
    _app(client).state.report_mailer = fake
    return fake


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles_service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
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
async def setup(db):
    """Roles, the module, the templates, and a presales team that can reach it."""
    await roles_service.seed_system_roles(db)
    await seed_form_templates(db)
    await reports_service.seed_templates(db)

    from app.access.service import grant_module, seed_modules

    await seed_modules(db)
    team = await teams_service.create_team(db, name="Presales", slug="presales")
    await db.commit()
    await grant_module(db, team=team, module_key="reports", granted_by_id=None)
    await db.commit()
    return team


@pytest.fixture
async def presales(db, setup):
    return setup


@pytest.fixture
async def author(db, presales):
    user = await _make(db, "amina@hamdaz.com")
    await teams_service.set_member_roles(
        db, team=presales, user=user, role_keys=["member"]
    )
    await db.commit()
    return user


@pytest.fixture
async def colleague(db, presales):
    """On the same team, with no oversight. The case the module exists for."""
    user = await _make(db, "bilal@hamdaz.com")
    await teams_service.set_member_roles(
        db, team=presales, user=user, role_keys=["member"]
    )
    await db.commit()
    return user


@pytest.fixture
async def lead(db, presales):
    user = await _make(db, "lead@hamdaz.com")
    await teams_service.set_member_roles(
        db, team=presales, user=user, role_keys=["team_lead"]
    )
    await db.commit()
    return user


@pytest.fixture
async def ceo(db, presales):
    return await _make(db, "ceo@hamdaz.com", "ceo")


async def _start(client, user, team, **body):
    response = await _as(client, user).post(
        "/api/v1/reports", json={"team_id": str(team.id), "cadence": "daily", **body}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _file(client, user, team, mailer, **body):
    """Start, give it an overview, and submit — the whole lifecycle."""
    draft = await _start(client, user, team, **body)
    await _as(client, user).patch(
        f"/api/v1/reports/{draft['id']}",
        json={"overview": "Chased three suppliers on the Kuwait bid."},
    )
    response = await _as(client, user).post(f"/api/v1/reports/{draft['id']}/submit")
    assert response.status_code == 200, response.text
    return response.json()


# ── what a report will ask ─────────────────────────────────────────────


async def test_the_form_describes_the_six_sections(client, author, presales) -> None:
    body = (
        await _as(client, author).get("/api/v1/reports/form?team=presales")
    ).json()
    assert [s["key"] for s in body["sections"]] == [
        "overview", "tasks", "issues", "remarks", "metrics", "summary"
    ]
    assert body["period_start"] == body["period_end"], "a daily covers one day"


async def test_the_form_falls_back_to_the_generic_template(client, author) -> None:
    """A team with no schedule can still file, rather than waiting on an admin."""
    body = (
        await _as(client, author).get("/api/v1/reports/form?team=presales")
    ).json()
    assert body["template_name"] == "Team report"
    assert body["fields"] == []


async def test_a_scheduled_template_replaces_the_fallback(
    client, db, author, presales
) -> None:
    """This is what makes each team's report different from the next team's."""
    template = next(
        t for t in await reports_service.report_templates(db)
        if t.key == "report_presales_daily"
    )
    await reports_service.set_schedule(
        db, team_id=presales.id, cadence="daily",
        template_id=template.id, actor_id=None,
    )
    await db.commit()

    body = (
        await _as(client, author).get("/api/v1/reports/form?team=presales")
    ).json()
    assert body["template_name"] == "Presales — daily"
    keys = {f["key"] for f in body["fields"]}
    assert "quotes_sent" in keys
    assert all(f["section"] in {s["key"] for s in body["sections"]} for f in body["fields"])


async def test_somebody_not_on_the_team_cannot_see_its_form(
    client, db, presales
) -> None:
    outsider = await _make(db, "outsider@hamdaz.com")
    response = await _as(client, outsider).get("/api/v1/reports/form?team=presales")
    # 403 from the module gate: they are on no team that has Reports at all.
    assert response.status_code == 403


# ── filing one ─────────────────────────────────────────────────────────


async def test_starting_a_report_pulls_in_the_persons_own_tasks(
    client, author, presales, sharepoint
) -> None:
    body = await _start(client, author, presales)
    titles = [t["title"] for t in body["tasks"]]
    assert "Kuwait pump bid" in titles
    # Whose tasks these are is derived from the session, never from the request.
    assert sharepoint.asked_for == ["amina@hamdaz.com"]


async def test_pulled_rows_carry_their_links_and_attachments(
    client, author, presales, sharepoint
) -> None:
    body = await _start(client, author, presales)
    bid = next(t for t in body["tasks"] if t["title"] == "Kuwait pump bid")
    assert bid["source"] == "proposals"
    assert bid["external_id"] == "101"
    assert bid["link"].endswith("ID=101")
    assert bid["has_attachments"] is True
    assert bid["attachments_url"].endswith("/101")


async def test_finished_tasks_are_left_out_unless_asked_for(
    client, author, presales, sharepoint
) -> None:
    open_only = await _start(client, author, presales)
    assert "Finished job" not in [t["title"] for t in open_only["tasks"]]

    everything = await _start(
        client, author, presales, cadence="weekly", include_closed=True
    )
    assert "Finished job" in [t["title"] for t in everything["tasks"]]


async def test_the_metrics_follow_from_the_tasks(
    client, author, presales, sharepoint
) -> None:
    body = await _start(client, author, presales)
    figures = {m["key"]: m for m in body["metrics"]}
    assert figures["tasks_total"]["computed"] == "2"
    # Not done and the deadline has passed, whatever the source list says.
    assert figures["tasks_overdue"]["computed"] == "1"
    assert figures["tasks_with_attachments"]["computed"] == "1"


async def test_a_report_opens_even_when_sharepoint_is_down(
    client, author, presales, sharepoint
) -> None:
    """A reporting tool that refuses to open because another system is down is
    a reporting tool people stop using."""
    sharepoint.fail = True
    body = await _start(client, author, presales)
    assert body["tasks"] == []
    assert body["status"] == "draft"


async def test_a_correction_survives_a_recompute(
    client, author, presales, sharepoint
) -> None:
    """The computed figure moves as the rows change; what a person put stays."""
    draft = await _start(client, author, presales)
    await _as(client, author).patch(
        f"/api/v1/reports/{draft['id']}", json={"metrics": {"tasks_total": 9}}
    )
    body = (
        await _as(client, author).patch(
            f"/api/v1/reports/{draft['id']}", json={"overview": "Still nine."}
        )
    ).json()
    total = next(m for m in body["metrics"] if m["key"] == "tasks_total")
    assert total["computed"] == "2"
    assert total["value"] == "9"
    assert total["effective"] == "9"
    assert total["edited"] is True


async def test_a_teams_own_questions_are_stored_and_the_rest_dropped(
    client, db, author, presales, sharepoint
) -> None:
    """Anything the template does not ask for is dropped rather than stored, so
    a stale frontend or a creative model cannot widen what a report holds."""
    template = next(
        t for t in await reports_service.report_templates(db)
        if t.key == "report_presales_daily"
    )
    await reports_service.set_schedule(
        db, team_id=presales.id, cadence="daily",
        template_id=template.id, actor_id=None,
    )
    await db.commit()

    draft = await _start(client, author, presales)
    body = (
        await _as(client, author).patch(
            f"/api/v1/reports/{draft['id']}",
            json={"answers": {"quotes_sent": 4, "not_a_field": "ignore me"}},
        )
    ).json()
    assert body["answers"] == {"quotes_sent": 4}


async def test_two_reports_for_the_same_day_are_refused(
    client, author, presales, sharepoint
) -> None:
    await _start(client, author, presales)
    response = await _as(client, author).post(
        "/api/v1/reports", json={"team_id": str(presales.id), "cadence": "daily"}
    )
    assert response.status_code == 409


async def test_a_report_needs_an_overview_before_it_is_filed(
    client, author, presales, sharepoint, mailer
) -> None:
    draft = await _start(client, author, presales)
    response = await _as(client, author).post(f"/api/v1/reports/{draft['id']}/submit")
    assert response.status_code == 400
    assert "overview" in response.json()["detail"].lower()


async def test_filing_locks_it(client, author, presales, sharepoint, mailer) -> None:
    filed = await _file(client, author, presales, mailer)
    assert filed["status"] == "submitted"
    assert filed["submitted_at"]
    assert filed["can_edit"] is False

    response = await _as(client, author).patch(
        f"/api/v1/reports/{filed['id']}", json={"overview": "Actually, something else."}
    )
    assert response.status_code == 409


async def test_it_cannot_be_filed_twice(
    client, author, presales, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    response = await _as(client, author).post(f"/api/v1/reports/{filed['id']}/submit")
    assert response.status_code == 409


# ── the email ──────────────────────────────────────────────────────────


async def test_filing_emails_the_people_it_goes_to(
    client, author, presales, lead, ceo, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    assert len(mailer.sent) == 1
    recipients = set(mailer.sent[0]["recipients"])
    assert "lead@hamdaz.com" in recipients
    assert "ceo@hamdaz.com" in recipients
    # Nobody needs their own report mailed back to them.
    assert "amina@hamdaz.com" not in recipients


async def test_a_colleague_is_not_emailed(
    client, author, presales, lead, colleague, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    assert "bilal@hamdaz.com" not in set(mailer.sent[0]["recipients"])


async def test_the_link_points_at_the_report(
    client, author, presales, lead, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    assert mailer.sent[0]["link"].endswith(f"/reports/{filed['id']}")


async def test_a_failed_send_does_not_fail_the_filing(
    client, author, presales, lead, sharepoint, mailer
) -> None:
    """The report is filed either way. But a silent failure hides the one fact
    that matters, so what went wrong is kept on the report."""
    mailer.fail = True
    filed = await _file(client, author, presales, mailer)
    assert filed["status"] == "submitted"

    body = (await _as(client, author).get(f"/api/v1/reports/{filed['id']}")).json()
    assert body["status"] == "submitted"


# ── who can see it ─────────────────────────────────────────────────────


async def test_a_colleague_cannot_read_it(
    client, author, presales, colleague, sharepoint, mailer
) -> None:
    """Being on presales does not make a colleague's report yours to read."""
    filed = await _file(client, author, presales, mailer)
    response = await _as(client, colleague).get(f"/api/v1/reports/{filed['id']}")
    # 404, not 403: a refusal would confirm a report exists for that day.
    assert response.status_code == 404


async def test_a_colleague_sees_none_of_it_in_the_listing(
    client, author, presales, colleague, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    body = (await _as(client, colleague).get("/api/v1/reports")).json()
    assert body["reports"] == []
    assert body["total"] == 0


async def test_a_team_lead_reads_their_teams_report(
    client, author, presales, lead, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    body = (await _as(client, lead).get(f"/api/v1/reports/{filed['id']}")).json()
    assert body["overview"]
    assert body["can_comment"] is True
    assert body["can_edit"] is False


async def test_a_ceo_reads_it(
    client, author, presales, ceo, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    body = (await _as(client, ceo).get(f"/api/v1/reports/{filed['id']}")).json()
    assert body["overview"]


async def test_a_draft_is_invisible_to_everybody_else(
    client, author, presales, lead, ceo, sharepoint
) -> None:
    """Half-written notes read as a finished report is how people learn to
    draft somewhere else and paste it in at the end."""
    draft = await _start(client, author, presales)
    for reader in (lead, ceo):
        response = await _as(client, reader).get(f"/api/v1/reports/{draft['id']}")
        assert response.status_code == 404


async def test_asking_for_another_teams_reports_is_empty_not_forbidden(
    client, db, author, presales, colleague, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    body = (
        await _as(client, colleague).get("/api/v1/reports?team=presales")
    ).json()
    assert body["reports"] == []


async def test_reading_somebody_elses_report_is_recorded(
    client, db, author, presales, lead, sharepoint, mailer
) -> None:
    """The complaint reports attract is always the same one — nobody reads
    them. This is how that gets answered with a fact."""
    filed = await _file(client, author, presales, mailer)
    await _as(client, lead).get(f"/api/v1/reports/{filed['id']}")
    import uuid as _uuid

    readers = await reports_service.readers(db, _uuid.UUID(filed["id"]))
    assert [r.user_id for r in readers] == [lead.id]


# ── commenting ─────────────────────────────────────────────────────────


async def test_a_lead_can_comment_and_the_author_sees_it(
    client, author, presales, lead, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    response = await _as(client, lead).post(
        f"/api/v1/reports/{filed['id']}/comments",
        json={"body": "Push the Kuwait supplier again on Thursday."},
    )
    assert response.status_code == 201

    body = (await _as(client, author).get(f"/api/v1/reports/{filed['id']}")).json()
    assert [c["body"] for c in body["comments"]] == [
        "Push the Kuwait supplier again on Thursday."
    ]


async def test_an_author_cannot_comment_on_their_own(
    client, author, presales, sharepoint, mailer
) -> None:
    filed = await _file(client, author, presales, mailer)
    response = await _as(client, author).post(
        f"/api/v1/reports/{filed['id']}/comments", json={"body": "One more thing."}
    )
    assert response.status_code == 403


# ── the view across reports ────────────────────────────────────────────


async def test_the_overview_answers_the_question_a_ceo_asks(
    client, author, presales, ceo, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    body = (await _as(client, ceo).get("/api/v1/reports/overview")).json()
    assert body["reports"] == 1
    assert body["people"] == 1
    assert body["by_team"][0]["team"] == "Presales"
    assert any(m["key"] == "tasks_total" for m in body["metrics"])


async def test_the_overview_shows_a_ceo_what_is_blocked(
    client, author, presales, ceo, sharepoint, mailer
) -> None:
    draft = await _start(client, author, presales)
    await _as(client, author).patch(
        f"/api/v1/reports/{draft['id']}",
        json={
            "overview": "Blocked on pricing.",
            "issues": [
                {
                    "title": "No price from the Kuwait supplier",
                    "severity": "blocked",
                    "waiting_on": "Al-Faris Trading",
                },
                {"title": "Portal login expired", "severity": "low"},
            ],
        },
    )
    await _as(client, author).post(f"/api/v1/reports/{draft['id']}/submit")

    body = (await _as(client, ceo).get("/api/v1/reports/overview")).json()
    titles = [i["title"] for i in body["open_issues"]]
    # Blocked leads: it is the reason to read a report today rather than Friday.
    assert titles[0] == "No price from the Kuwait supplier"
    assert body["open_issues"][0]["waiting_on"] == "Al-Faris Trading"
    assert body["open_issues"][0]["raised_by"] == "amina"


async def test_the_overview_narrows_to_what_the_asker_may_read(
    client, author, presales, colleague, sharepoint, mailer
) -> None:
    """An ordinary person asking gets their own reports summarised and nobody
    else's, rather than a refusal."""
    await _file(client, author, presales, mailer)
    body = (await _as(client, colleague).get("/api/v1/reports/overview")).json()
    assert body["reports"] == 0
    assert body["open_issues"] == []


# ── setting them up ────────────────────────────────────────────────────


async def test_scheduling_is_super_admin_only(
    client, db, author, presales, lead
) -> None:
    template = (await reports_service.report_templates(db))[0]
    for who in (author, lead):
        response = await _as(client, who).put(
            "/api/v1/reports/admin/schedules",
            json={
                "team_id": str(presales.id),
                "cadence": "daily",
                "template_id": str(template.id),
            },
        )
        assert response.status_code == 403


async def test_a_super_admin_points_a_team_at_a_template(
    client, db, boss, presales
) -> None:
    template = next(
        t for t in await reports_service.report_templates(db)
        if t.key == "report_presales_weekly"
    )
    response = await _as(client, boss).put(
        "/api/v1/reports/admin/schedules",
        json={
            "team_id": str(presales.id),
            "cadence": "weekly",
            "template_id": str(template.id),
        },
    )
    assert response.status_code == 200
    assert response.json()["template_name"] == "Presales — weekly"

    listed = (await _as(client, boss).get("/api/v1/reports/admin/schedules")).json()
    assert [s["cadence"] for s in listed] == ["weekly"]


# ── the super admin's settings, and the log ────────────────────────────


@pytest.fixture
async def boss(db, presales):
    return await _make(db, "boss@hamdaz.com", "super_admin")


async def test_the_settings_are_super_admin_only(
    client, author, lead, ceo, presales
) -> None:
    """A CEO reads every report and still cannot change who they are mailed to.
    Reading everything and deciding what everybody gets are different powers."""
    for who in (author, lead, ceo):
        assert (
            await _as(client, who).get("/api/v1/reports/admin/settings")
        ).status_code == 403
        assert (
            await _as(client, who).patch(
                "/api/v1/reports/admin/settings", json={"notify_on_submit": False}
            )
        ).status_code == 403


async def test_the_settings_ship_as_the_module_behaved(client, boss) -> None:
    body = (await _as(client, boss).get("/api/v1/reports/admin/settings")).json()
    assert body["notify_on_submit"] is True
    assert body["notify_team_oversight"] is True
    assert set(body["company_roles"]) == {"super_admin", "ceo", "manager"}
    assert set(body["notify_cadences"]) == {"daily", "weekly", "monthly", "ad_hoc"}
    assert body["copy_author"] is False


async def test_turning_the_email_off_files_the_report_and_tells_nobody(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    """What a company piloting the module wants before reports start landing
    in the CEO's inbox."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"notify_on_submit": False}
    )
    filed = await _file(client, author, presales, mailer)
    assert filed["status"] == "submitted"
    assert mailer.sent == []


async def test_dailies_can_be_silenced_while_weeklies_still_go(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    """The common ask: a manager of six people would otherwise get thirty
    messages a week and read none of them."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings",
        json={"notify_cadences": ["weekly", "monthly"]},
    )
    await _file(client, author, presales, mailer, cadence="daily")
    assert mailer.sent == []

    await _file(client, author, presales, mailer, cadence="weekly")
    assert len(mailer.sent) == 1


async def test_the_ceo_can_be_taken_off_without_losing_their_access(
    client, db, boss, author, presales, lead, ceo, sharepoint, mailer
) -> None:
    """Delivery and visibility are separate questions, and this changes one."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"notify_company_wide": False}
    )
    filed = await _file(client, author, presales, mailer)
    recipients = set(mailer.sent[0]["recipients"])
    assert "ceo@hamdaz.com" not in recipients
    assert "lead@hamdaz.com" in recipients, "the team's own lead still gets it"

    # ...and the CEO can still open it.
    assert (
        await _as(client, ceo).get(f"/api/v1/reports/{filed['id']}")
    ).status_code == 200


async def test_an_extra_address_is_copied(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    """For people who are not users here — a shared mailbox, a consultant."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings",
        json={"extra_recipients": ["Ops@Hamdaz.com", "rubbish"]},
    )
    body = (await _as(client, boss).get("/api/v1/reports/admin/settings")).json()
    assert body["extra_recipients"] == ["ops@hamdaz.com"], "the typo is dropped"

    await _file(client, author, presales, mailer)
    assert "ops@hamdaz.com" in set(mailer.sent[0]["recipients"])


async def test_the_author_can_be_copied_in(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"copy_author": True}
    )
    await _file(client, author, presales, mailer)
    assert "amina@hamdaz.com" in set(mailer.sent[0]["recipients"])


async def test_a_role_that_does_not_exist_is_refused(client, boss) -> None:
    """A key that mails nobody is the failure nobody notices until somebody
    asks why they stopped getting reports."""
    response = await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"company_roles": ["chief_wizard"]}
    )
    assert response.status_code == 400
    assert "chief_wizard" in response.json()["detail"]


async def test_one_team_can_be_silenced_on_its_own(
    client, db, boss, author, presales, lead, sharepoint, mailer
) -> None:
    template = (await reports_service.report_templates(db))[0]
    await _as(client, boss).put(
        "/api/v1/reports/admin/schedules",
        json={
            "team_id": str(presales.id),
            "cadence": "daily",
            "template_id": str(template.id),
            "notify": False,
        },
    )
    await _file(client, author, presales, mailer)
    assert mailer.sent == []


async def test_a_team_can_have_its_own_extra_recipient(
    client, db, boss, author, presales, lead, sharepoint, mailer
) -> None:
    template = (await reports_service.report_templates(db))[0]
    await _as(client, boss).put(
        "/api/v1/reports/admin/schedules",
        json={
            "team_id": str(presales.id),
            "cadence": "daily",
            "template_id": str(template.id),
            "extra_recipients": ["presales-watch@hamdaz.com"],
        },
    )
    await _file(client, author, presales, mailer)
    assert "presales-watch@hamdaz.com" in set(mailer.sent[0]["recipients"])


async def test_the_log_records_a_send(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    body = (await _as(client, boss).get("/api/v1/reports/admin/deliveries")).json()
    assert body["total"] == 1
    row = body["deliveries"][0]
    assert row["status"] == "sent"
    assert row["author_name"] == "amina"
    assert row["team_name"] == "Presales"
    assert "lead@hamdaz.com" in row["recipients"]
    assert body["counts"]["sent"] == 1
    assert body["counts"]["failed"] == 0


async def test_the_log_records_why_nothing_was_sent(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    """"Why did my manager not get it" is the question this log exists to
    answer, and a missing row answers it with a shrug."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"notify_on_submit": False}
    )
    await _file(client, author, presales, mailer)

    body = (await _as(client, boss).get("/api/v1/reports/admin/deliveries")).json()
    row = body["deliveries"][0]
    assert row["status"] == "skipped"
    assert "switched off" in row["detail"]
    assert body["counts"]["skipped"] == 1


async def test_the_log_records_a_failure_without_failing_the_filing(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    mailer.fail = True
    filed = await _file(client, author, presales, mailer)
    assert filed["status"] == "submitted"

    body = (await _as(client, boss).get("/api/v1/reports/admin/deliveries")).json()
    row = body["deliveries"][0]
    assert row["status"] == "failed"
    assert "Graph refused" in row["detail"]
    assert body["counts"]["failed"] == 1


async def test_a_team_with_nobody_over_it_is_recorded_as_skipped(
    client, boss, author, presales, sharepoint, mailer
) -> None:
    """Not a failure. A team with no lead and no CEO on the system has nowhere
    for this to go, and saying so is more use than an error nobody caused."""
    await _as(client, boss).patch(
        "/api/v1/reports/admin/settings", json={"notify_company_wide": False}
    )
    await _file(client, author, presales, mailer)
    body = (await _as(client, boss).get("/api/v1/reports/admin/deliveries")).json()
    assert body["deliveries"][0]["status"] == "skipped"
    assert "Nobody to send it to" in body["deliveries"][0]["detail"]


async def test_the_log_is_super_admin_only(
    client, author, lead, ceo, presales
) -> None:
    """Who was mailed about whom is a map of the reporting lines, and an
    ordinary person has no business reading it."""
    for who in (author, lead, ceo):
        response = await _as(client, who).get("/api/v1/reports/admin/deliveries")
        assert response.status_code == 403


async def test_the_log_can_be_filtered_to_failures(
    client, boss, author, presales, lead, sharepoint, mailer
) -> None:
    await _file(client, author, presales, mailer)
    mailer.fail = True
    await _file(client, author, presales, mailer, cadence="weekly")

    body = (
        await _as(client, boss).get("/api/v1/reports/admin/deliveries?status=failed")
    ).json()
    assert body["total"] == 1
    assert body["deliveries"][0]["status"] == "failed"


async def test_the_admin_routes_are_not_read_as_a_report_id(client, boss) -> None:
    """/reports/admin/... must be matched before /reports/{report_id}, which
    would otherwise try to parse "admin" as a uuid and 422."""
    response = await _as(client, boss).get("/api/v1/reports/admin/templates")
    assert response.status_code == 200
    assert {t["key"] for t in response.json()} >= {
        "team_report", "report_presales_daily", "report_presales_weekly"
    }
