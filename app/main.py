"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.access.router import router as access_router
from app.analytics.router import router as analytics_router
from app.assignment.router import router as assignment_router
from app.assistant.agent import Assistant
from app.assistant.cache import ActorCache, ConfigCache, PlacesCache
from app.assistant.executor import ToolExecutor
from app.assistant.llm import OpenAIChat
from app.assistant.router import admin_router as assistant_admin_router
from app.assistant.router import router as assistant_router
from app.auth.oidc import EntraOIDC
from app.auth.router import router as auth_router
from app.comparison.extraction import QuoteExtractor
from app.comparison.router import router as comparison_router
from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine
from app.dashboards.router import router as dashboards_router
from app.directory.graph import GraphDirectory
from app.directory.router import router as directory_router
from app.finance.cache import PnlCache
from app.finance.router import router as finance_router
from app.forms.router import router as templates_router
from app.hr.public import router as careers_router
from app.hr.router import router as hr_router
from app.labels.router import router as labels_router
from app.leave.mailer import LeaveMailer
from app.leave.router import router as leave_router
from app.meetings.calendar import GraphCalendar
from app.meetings.router import router as meetings_router
from app.profiles.router import router as profiles_router
from app.projects.router import router as projects_router
from app.proposals.analytics import WorkloadCache
from app.proposals.oversight import TeamTasksCache
from app.proposals.router import router as proposals_router
from app.proposals.sharepoint import SharePointProposals
from app.admin.router import router as admin_router
from app.intake.graph_mail import MailReader
from app.intake.router import router as intake_router
from app.intake.router import webhook_router as intake_webhook_router
from app.intake.worker import Worker
from app.workflows.engine import Services as WorkflowServices
from app.workflows.router import admin_router as workflows_admin_router
from app.workflows.router import router as workflows_router
from app.workflows.worker import WorkflowWorker
from app.notifications.router import router as notifications_router
from app.quoting.mailer import QuoteMailer
from app.reports.brief import Briefer
from app.reports.mailer import ReportMailer
from app.reports.router import admin_router as reports_admin_router
from app.reports.router import router as reports_router
from app.quoting.probability import WinRates
from app.quoting.router import router as quoting_router
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
    # Reads supplier quote documents. Holds no connection of its own; the
    # Anthropic SDK manages that, and an unset key fails at the endpoint rather
    # than at boot so the manual entry path keeps working without one.
    app.state.quote_extractor = QuoteExtractor(settings)
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
    # Approvers are told a quote is waiting, from the requester's own mailbox.
    app.state.quote_mailer = QuoteMailer(settings, http)
    # A filed report is mailed to whoever it goes to, from its author's mailbox.
    # A failure to send never fails the filing — see app/reports/router.py.
    app.state.report_mailer = ReportMailer(settings, http)
    # The assistant. It reaches every module by calling this very app's routes
    # in-process, carrying the caller's own session cookie — so each route's
    # existing guard is the assistant's permission model too, and there is no
    # second copy of it to keep in step. See app/assistant/executor.py.
    #
    # Holds no connection at boot: an unset OpenAI key fails at the first turn
    # with a message a super admin can act on, rather than stopping the app for
    # everyone who never opens the chat.
    # Both stand in front of a database a third of a second away, and both
    # are dropped by the admin routes that change what they hold. See
    # app/assistant/cache.py for why the permission one may be stale.
    app.state.assistant_config = ConfigCache()
    app.state.assistant_actors = ActorCache()
    # Where each person can be sent. Its own cache because the assistant's
    # navigation reads a different thing from its permissions, and reading it
    # uncached costs 2.4 seconds of an ordinary member's turn.
    app.state.assistant_places = PlacesCache()
    #: One client, two callers. The assistant drives a tool loop with it; the
    #: report briefer makes a single call with it. Shared so an OpenAI key, a
    #: base URL or a timeout is configured once and means the same thing to
    #: both, rather than two clients that drift apart on the third setting.
    openai = OpenAIChat(settings)
    # Held on the state as well: the workflow steps that ask the model reach
    # it here, so a flow and a chat use the same client and the same key.
    app.state.openai = openai
    # The one way anything reaches a module: the app's own routes, in-process,
    # carrying the caller's session. Held on the state as well as inside the
    # assistant because navigation-by-name uses it too — see
    # app/assistant/records.py — and reaching into another object's privates
    # for it would be a worse kind of coupling than naming it here.
    app.state.assistant_executor = ToolExecutor(
        app,
        api_prefix=settings.api_prefix,
        cookie_name=settings.session_cookie_name,
        max_chars=settings.assistant_tool_result_max_chars,
        timeout=settings.assistant_tool_timeout_seconds,
    )
    app.state.assistant = Assistant(
        openai,
        app.state.assistant_executor,
        get_session_factory(),
    )
    # Writes the short version of a filed report, for the managers who have
    # several to read. Off until a super admin turns it on: it is the only part
    # of reporting that spends money per report. See app/reports/brief.py.
    app.state.report_briefer = Briefer(openai)
    # Reads the watched mailbox. Shares the HTTP client and inherits the
    # mailer's token cache — same token, no reason for a second copy.
    app.state.mail_reader = MailReader(settings, http)
    # The background half: keep the local copy of the Proposals list current,
    # keep the ranking current with it, and watch the mailbox. Only one
    # instance runs each loop — see app/intake/worker.py — and the loops are
    # off unless somebody turns them on, because a timer that reads a live
    # SharePoint list from a laptop is not what anybody meant.
    app.state.intake_worker = Worker(
        factory=get_session_factory(),
        settings=settings,
        sharepoint=app.state.sharepoint,
        mail=app.state.mail_reader,
        http=http,
    )
    # Started always. Both loops decide for themselves whether to do anything,
    # by reading the intake settings each tick — so turning the intake on
    # through the API is enough, and does not also need an environment
    # variable and a restart. Two sleeping tasks cost nothing; an admin switch
    # that silently does not take effect costs an afternoon.
    app.state.intake_worker.start()
    # Wakes the workflow runs that are waiting on the world — a supplier's
    # reply, a quote's approval. One instance runs it; see app/workflows/worker.py.
    app.state.workflow_worker = WorkflowWorker(
        factory=get_session_factory(),
        services=WorkflowServices(
            settings=settings,
            sharepoint=app.state.sharepoint,
            mail=app.state.mail_reader,
            zoho=app.state.zoho,
            extractor=app.state.quote_extractor,
            llm=openai,
            executor=app.state.assistant_executor,
        ),
    )
    app.state.workflow_worker.start()
    logger.info("started environment=%s", settings.environment)

    try:
        yield
    finally:
        # Stopped before the client closes: a loop mid-request against a
        # closed connection pool is a noisy shutdown for no reason.
        await app.state.intake_worker.stop()
        await app.state.workflow_worker.stop()
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
    app.include_router(templates_router, prefix=settings.api_prefix)
    app.include_router(hr_router, prefix=settings.api_prefix)
    app.include_router(finance_router, prefix=settings.api_prefix)
    # Admin first: /reports/admin/... must be matched before /reports/{id},
    # which would otherwise try to read "admin" as a report id.
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(notifications_router, prefix=settings.api_prefix)
    # The webhook first: Graph posts to it unauthenticated, and it must not
    # inherit anything that would refuse Microsoft.
    app.include_router(intake_webhook_router, prefix=settings.api_prefix)
    app.include_router(intake_router, prefix=settings.api_prefix)
    app.include_router(reports_admin_router, prefix=settings.api_prefix)
    app.include_router(reports_router, prefix=settings.api_prefix)
    app.include_router(assistant_router, prefix=settings.api_prefix)
    app.include_router(assistant_admin_router, prefix=settings.api_prefix)
    app.include_router(workflows_admin_router, prefix=settings.api_prefix)
    app.include_router(workflows_router, prefix=settings.api_prefix)

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
