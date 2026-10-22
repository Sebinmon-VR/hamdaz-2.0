"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.access.router import router as access_router
from app.admin.router import router as admin_router
from app.analytics.router import router as analytics_router
from app.ariba.mailer import AribaMailer
from app.ariba.router import router as ariba_router
from app.ariba.worker import AribaWorker
from app.assignment.router import router as assignment_router
from app.auth.oidc import EntraOIDC
from app.auth.router import router as auth_router
from app.comparison.extraction import QuoteExtractor
from app.comparison.router import router as comparison_router
from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine
from app.core.llm import TextModel
from app.dashboards.router import router as dashboards_router
from app.directory.graph import GraphDirectory
from app.directory.router import router as directory_router
from app.enquiries.claude import ClaudeEnquiry
from app.agents.model import AgentModel
from app.core.mail import GraphMailer
from app.agents import ENABLED as AGENTS_ENABLED
from app.agents.router import router as agents_router
from app.agents import engine as agent_engine
from app.agents.worker import AgentWorker
from app.enquiries.router import router as enquiries_router
from app.finance.cache import PnlCache
from app.finance.router import router as finance_router
from app.followups.mailer import FollowupMailer
from app.followups.router import router as followups_router
from app.followups.worker import FollowupWorker
from app.reminders.mailer import ReminderMailer
from app.reminders.router import router as reminders_router
from app.reminders.worker import ReminderWorker
from app.bcd.mailer import BcdMailer
from app.bcd.router import router as bcd_router
from app.bcd.worker import BcdWorker
from app.taskcalendar.graph import TaskCalendar
from app.taskcalendar.router import router as task_calendar_router
from app.taskcalendar.worker import TaskCalendarWorker
from app.forms.router import router as templates_router
from app.hr.public import router as careers_router
from app.hr.router import router as hr_router
from app.intake.graph_mail import MailReader
from app.intake.router import router as intake_router
from app.intake.router import webhook_router as intake_webhook_router
from app.intake.worker import Worker
from app.mailwatch.router import router as mailwatch_router
from app.mailwatch.router import webhook_router as mailwatch_webhook_router
from app.mailwatch.worker import MailWatchWorker
from app.labels.router import router as labels_router
from app.leave.mailer import LeaveMailer
from app.leave.router import router as leave_router
from app.meetings.calendar import GraphCalendar
from app.meetings.router import router as meetings_router
from app.notifications.router import router as notifications_router
from app.profiles.router import router as profiles_router
from app.projects.router import router as projects_router
from app.proposals.analytics import WorkloadCache
from app.proposals.oversight import TeamTasksCache
from app.proposals.router import router as proposals_router
from app.proposals.sharepoint import SharePointProposals
from app.quoting.mailer import QuoteMailer
from app.quoting.probability import WinRates
from app.quoting.router import router as quoting_router
from app.quoting.storage import QuoteDrive
from app.reports.mailer import ReportMailer
from app.reports.router import admin_router as reports_admin_router
from app.reports.router import router as reports_router
from app.roles.router import router as roles_router
from app.teams.router import router as teams_router
from app.zoho.cache import QuoteCache
from app.zoho.client import ZohoBooks
from app.zoho.router import router as zoho_router

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s [%(name)s] %(message)s")
logger = logging.getLogger("hamdaz")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_runtime()  # fail at boot, not at someone's first login

    init_engine(settings)
    # One connection pool for the whole process — Entra's token and JWKS
    # endpoints are called on every sign-in, so per-request clients would
    # re-handshake TLS each time.
    http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    app.state.http = http
    app.state.oidc = EntraOIDC(settings, http)
    # Shares the HTTP client and caches its own app-only token.
    app.state.graph = GraphDirectory(settings, http)
    # Read-only client for the Proposals list; caches its own token.
    app.state.sharepoint = SharePointProposals(settings, http)
    # Shared by every admin: the aggregate is identical for all of them.
    app.state.workload_cache = WorkloadCache()
    # Per team, and separate from the aggregate above because it holds whole
    # rows rather than counts — see app/proposals/oversight.py.
    app.state.team_tasks_cache = TeamTasksCache()
    # Sends as the requester. Disabled unless HR turns it on in leave settings.
    app.state.leave_mailer = LeaveMailer(settings, http)
    # Reads the signed-in person's own calendar, and only theirs — the
    # mailbox is never named by a request. See app/meetings/router.py.
    app.state.calendar = GraphCalendar(settings, http)
    # The optional second pass for documents the parsers cannot settle: a
    # text model behind whichever free endpoint has a key, with fallback.
    # No key configured means no model, and everything still reads.
    app.state.text_model = TextModel(settings, http)
    # Reads supplier quote documents: a parser over the document's own table,
    # OCR when an engine is installed, and the model above when the table
    # reader declines.
    app.state.quote_extractor = QuoteExtractor(settings, model=app.state.text_model)
    # Read-only over Zoho Books. Its access token is shared through Postgres
    # rather than held per process — see app/zoho/tokens.py for why.
    app.state.zoho = ZohoBooks(settings, http, get_session_factory())
    # The quotes list is the same for everyone, so one cache serves all callers.
    app.state.quote_cache = QuoteCache()
    # The ledger sweep behind a profit and loss is the most expensive thing this
    # app asks of Zoho, and the figures are identical for every viewer — so one
    # cache serves everyone who is allowed to see them. See app/finance/cache.py.
    app.state.pnl_cache = PnlCache()
    # Win rates over the estimate history. One sweep serves every draft, and
    # it is a read of Zoho only — nothing is written there.
    app.state.win_rates = WinRates()
    # Reads tender documents and looks new items up on the web, with Claude.
    # No key means the enquiry page says so; nothing else depends on it.
    app.state.enquiry_llm = ClaudeEnquiry(settings)
    # Each person's own agents: Haiku, low effort, asked only after the app.
    app.state.agent_model = AgentModel(settings)
    # Mails an agent's owner about their own runs — as them, to them.
    app.state.agent_mailer = GraphMailer(settings, http)
    # Approvers are told a quote is waiting, from the requester's own mailbox.
    app.state.quote_mailer = QuoteMailer(settings, http)
    # Files the supplier documents people upload into OneDrive. Inert
    # unless a drive is configured — see app/quoting/storage.py.
    app.state.quote_drive = QuoteDrive(settings, http)
    # A filed report is mailed to whoever it goes to, from its author's mailbox.
    # A failure to send never fails the filing — see app/reports/router.py.
    app.state.report_mailer = ReportMailer(settings, http)
    # Reads a mailbox for the quote pages, which attach emails to a quote.
    # Shares the HTTP client and inherits the mailer's token cache.
    app.state.mail_reader = MailReader(settings, http)
    # The background half: keep the local copy of the Proposals list current,
    # and the ranking current with it. One instance runs the loop — see
    # app/intake/worker.py — and it is off unless MIRROR_SYNC_ENABLED is set,
    # because a timer that reads a live SharePoint list from a laptop is not
    # what anybody meant.
    app.state.intake_worker = Worker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
    )
    # Started always; the loop decides each tick whether to do anything.
    app.state.intake_worker.start()
    # Asks the holder of a task that went past its due date why, and passes
    # the answer to their team's managers. Off until a super admin turns it
    # on, and scoped by its settings to one team — see app/followups.
    app.state.followup_worker = FollowupWorker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
        mailer=FollowupMailer(settings, http),
    )
    app.state.followup_worker.start()
    # Two days before a task is due, asks its holder where it stands, on a
    # form that can update the task. Off, and with the SharePoint write off,
    # until a super admin turns them on - see app/reminders.
    app.state.reminder_worker = ReminderWorker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
        mailer=ReminderMailer(settings, http),
    )
    app.state.reminder_worker.start()
    # Holds a new task whose BCD is the assignment time, and asks for the real
    # one: the assignee, then the managers after two working hours. Off until
    # a super admin turns it on - see app/bcd.
    app.state.bcd_worker = BcdWorker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
        mailer=BcdMailer(settings, http),
    )
    app.state.bcd_worker.start()
    # Each open task's BCD as an event in its holder's Outlook calendar. Off
    # until a super admin turns it on - see app/taskcalendar.
    app.state.task_calendar = TaskCalendar(settings, http)
    app.state.task_calendar_worker = TaskCalendarWorker(
        factory=get_session_factory(),
        sharepoint=app.state.sharepoint,
        calendar=app.state.task_calendar,
    )
    app.state.task_calendar_worker.start()
    # Reads the open tenders from the Ariba supplier portal — only when a new
    # tender reaches the Proposals mirror, and within a gap and a daily cap.
    # Off unless ARIBA_ENABLED is set. With ARIBA_FIX_BCD it also corrects the
    # BCD of the matching Proposals row — that column only; see app/ariba/bcd.py.
    app.state.ariba_worker = AribaWorker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
        mailer=AribaMailer(settings, http),
    )
    app.state.ariba_worker.start()
    # The CEO's mails in the watched mailbox, read when Graph says one
    # arrived — no timer reads the inbox. Off until a super admin switches it
    # on in its settings; read only. See app/mailwatch.
    app.state.mailwatch_worker = MailWatchWorker(
        factory=get_session_factory(),
        settings=settings,
        reader=app.state.mail_reader,
        model=app.state.agent_model,
        sharepoint=app.state.sharepoint,
    )
    app.state.mailwatch_worker.start()
    # The always-on agents: a task assigned to somebody starts their agent
    # (Nova, for presales), and runs a restart cut off are picked up. Off
    # unless AGENT_TRIGGERS_ENABLED is set — see app/agents/worker.py — and
    # never while agents are paused (app.agents.ENABLED).
    app.state.agent_worker = None
    if AGENTS_ENABLED:
        app.state.agent_worker = AgentWorker(
            factory=get_session_factory(),
            settings=settings,
            deps=lambda: agent_engine.deps_for(app.state, settings, get_session_factory()),
        )
        app.state.agent_worker.start()
    logger.info("started environment=%s", settings.environment)

    try:
        yield
    finally:
        # Stopped before the client closes: a loop mid-request against a
        # closed connection pool is a noisy shutdown for no reason.
        await app.state.intake_worker.stop()
        await app.state.followup_worker.stop()
        await app.state.reminder_worker.stop()
        await app.state.bcd_worker.stop()
        await app.state.task_calendar_worker.stop()
        await app.state.ariba_worker.stop()
        await app.state.mailwatch_worker.stop()
        if app.state.agent_worker is not None:
            await app.state.agent_worker.stop()
        await http.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Required for the session cookie to travel on frontend XHR. Note this
        # forbids the "*" origin wildcard — hence an explicit list.
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """A failure nobody caught, said out loud — with CORS headers.

        Starlette answers these from outside the CORS middleware, so the
        browser gets a 500 with no allow-origin header, refuses to read it,
        and the screen says the API could not be reached. That is what the
        person then reports, and it is wrong: the API answered, with a
        reason. So the reason travels, on a response the browser will read,
        and the traceback goes to the log as it always did.
        """
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        headers: dict[str, str] = {}
        origin = request.headers.get("origin")
        if origin and origin in settings.cors_origins:
            headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            }
        reason = f"{type(exc).__name__}: {exc}".strip(": ")[:600]
        return JSONResponse(
            {"detail": f"The server hit an error it did not expect. {reason}"},
            status_code=500,
            headers=headers,
        )

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(directory_router, prefix=settings.api_prefix)
    app.include_router(roles_router, prefix=settings.api_prefix)
    app.include_router(teams_router, prefix=settings.api_prefix)
    app.include_router(profiles_router, prefix=settings.api_prefix)
    app.include_router(access_router, prefix=settings.api_prefix)
    app.include_router(dashboards_router, prefix=settings.api_prefix)
    app.include_router(proposals_router, prefix=settings.api_prefix)
    app.include_router(projects_router, prefix=settings.api_prefix)
    app.include_router(leave_router, prefix=settings.api_prefix)
    app.include_router(meetings_router, prefix=settings.api_prefix)
    app.include_router(zoho_router, prefix=settings.api_prefix)
    app.include_router(comparison_router, prefix=settings.api_prefix)
    app.include_router(labels_router, prefix=settings.api_prefix)
    app.include_router(assignment_router, prefix=settings.api_prefix)
    app.include_router(analytics_router, prefix=settings.api_prefix)
    app.include_router(quoting_router, prefix=settings.api_prefix)
    app.include_router(enquiries_router, prefix=settings.api_prefix)
    if AGENTS_ENABLED:  # paused: see app/agents/__init__.py
        app.include_router(agents_router, prefix=settings.api_prefix)
    app.include_router(templates_router, prefix=settings.api_prefix)
    app.include_router(hr_router, prefix=settings.api_prefix)
    app.include_router(finance_router, prefix=settings.api_prefix)
    # Admin first: /reports/admin/... must be matched before /reports/{id},
    # which would otherwise try to read "admin" as a report id.
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(notifications_router, prefix=settings.api_prefix)
    app.include_router(followups_router, prefix=settings.api_prefix)
    app.include_router(reminders_router, prefix=settings.api_prefix)
    app.include_router(bcd_router, prefix=settings.api_prefix)
    app.include_router(task_calendar_router, prefix=settings.api_prefix)
    app.include_router(ariba_router, prefix=settings.api_prefix)
    # The webhook first: Graph posts to it unauthenticated, and it must not
    # inherit anything that would refuse Microsoft.
    app.include_router(intake_webhook_router, prefix=settings.api_prefix)
    app.include_router(intake_router, prefix=settings.api_prefix)
    app.include_router(mailwatch_webhook_router, prefix=settings.api_prefix)
    app.include_router(mailwatch_router, prefix=settings.api_prefix)
    app.include_router(reports_admin_router, prefix=settings.api_prefix)
    app.include_router(reports_router, prefix=settings.api_prefix)
    # Mounted at the root, NOT under the API prefix, and holding no
    # authentication dependency of any kind. That separation is the whole
    # of the candidate-facing security model — see app/hr/public.py.
    app.include_router(careers_router)

    # Dev console. Mounted, not merely guarded — in production the route does
    # not exist at all, so there is nothing to accidentally expose.
    if settings.environment == "local":
        from app.sandbox.router import router as sandbox_router

        app.include_router(sandbox_router)
        logger.info("dev sandbox mounted at /sandbox")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        # Deliberately does not touch Postgres: this answers "is the process up",
        # which is what a platform health probe should restart on.
        return {"status": "ok", "app": settings.app_name}

    return app


app = create_app()
