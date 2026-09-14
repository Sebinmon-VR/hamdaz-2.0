"""Application settings.

Every value the app reads comes from here. Nothing touches ``os.environ`` directly,
so there is exactly one place to look when a deployment misbehaves.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── app ────────────────────────────────────────────────────────────
    environment: Literal["local", "production"] = "local"
    debug: bool = False
    app_name: str = "Hamdaz ERP"
    api_prefix: str = "/api/v1"

    # ── database ───────────────────────────────────────────────────────
    #: Azure Postgres requires TLS, so keep ``?sslmode=require`` on the URL.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    #: Throw a pooled connection away once it is this old, rather than waiting
    #: to find out it is dead.
    #:
    #: ``pool_pre_ping`` alone is not enough. It checks a connection when the
    #: pool hands it out, which catches one that died while idle — but not one
    #: the gateway drops in the seconds between that check and the query, and
    #: that is exactly what a long-lived pool against Azure produces: a 500
    #: reading "server closed the connection unexpectedly" on a request that
    #: did nothing wrong. Azure's gateway cuts idle connections at around five
    #: minutes, so this stays comfortably under it.
    db_pool_recycle_seconds: int = 240

    # ── Entra ID (Microsoft) ───────────────────────────────────────────
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    #: Must byte-for-byte match a Redirect URI on the app registration.
    azure_redirect_uri: str = "http://localhost:8000/api/v1/auth/callback"

    # ── SharePoint (proposals) ─────────────────────────────────────────
    #: The Proposals list is READ ONLY from here. SharePoint is where the team
    #: actually works; this app shows their rows and never writes back.
    sharepoint_site_id: str = (
        "hamdaz1.sharepoint.com,cb8b4419-4f8e-41c9-ad24-377727cf5e79,"
        "9ded8786-1497-489f-a53c-9d316bc9b7e7"
    )
    sharepoint_proposals_list_id: str = "58ac33c7-f42e-4b27-afb0-4af09d90b397"
    #: The list as a person opens it. Every link to a task is this plus the
    #: item id, which is the only construction SharePoint actually documents:
    #:
    #:   {list_url}/DispForm.aspx?ID={id}   the task
    #:   {list_url}/Attachments/{id}        its attachments
    #:
    #: Configured rather than taken from Graph. Graph's ``webUrl`` for a list
    #: item is ``.../Proposals/412_.000`` — an internal handle that does not
    #: render — so deriving a link from it means reshaping a value that was
    #: never a link. This is one string, it changes only if the site is moved,
    #: and it makes the URLs the same whether or not Graph returned anything.
    sharepoint_proposals_list_url: str = (
        "https://hamdaz1.sharepoint.com/sites/ProposalTeam/Lists/Proposals"
    )

    # ── Zoho Books (quotes) ────────────────────────────────────────────
    #: READ ONLY, like SharePoint. Zoho Books is where quotes are actually
    #: written; this app only reads them. The scope on the refresh token should
    #: be ZohoBooks.estimates.READ so that stays true even by accident.
    #:
    #: Deliberately absent from validate_runtime(): Entra must be configured or
    #: nobody can sign in at all, but an unconfigured Zoho should fail at
    #: /api/v1/quotes with a clear message rather than stop the app booting.
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    #: Permanent — Zoho refresh tokens do not expire, and refreshing does not
    #: issue a new one. This value is reused for the life of the integration.
    zoho_refresh_token: str = ""
    zoho_organization_id: str = ""
    #: Only the *accounts* host needs configuring. The API host is whatever the
    #: refresh response hands back as ``api_domain``, so the data centre does
    #: not have to be guessed twice.
    zoho_accounts_url: str = "https://accounts.zoho.com"

    @property
    def zoho_app_base(self) -> str:
        """Where a quote lives in the Zoho Books UI, for deep links.

        Derived from the accounts host so it follows the data centre without a
        second setting: ``accounts.zoho.eu`` implies ``books.zoho.eu``. The API
        host cannot be used here — ``www.zohoapis.com`` serves the API, not the
        app, and a link to it goes nowhere a person can read.
        """
        host = self.zoho_accounts_url.rstrip("/").replace("accounts.zoho", "books.zoho")
        return f"{host}/app/{self.zoho_organization_id}"

    @property
    def zoho_configured(self) -> bool:
        return bool(
            self.zoho_client_id
            and self.zoho_client_secret
            and self.zoho_refresh_token
            and self.zoho_organization_id
        )

    # ── Claude (quote extraction) ──────────────────────────────────────
    #: Create this at console.anthropic.com — it cannot be generated from here.
    anthropic_api_key: str = ""
    #: Required for an *identity-linked* key, which must name the workspace it
    #: acts in; without it every request is a 400. An ordinary organisation key
    #: does not need it, so this stays optional and is only sent when set.
    #: Console -> Settings -> Workspaces, an id beginning ``wrkspc_``.
    anthropic_workspace_id: str = ""
    #: Opus 5 by design. Extraction mistakes on a supplier quote are expensive
    #: in a way the model price difference is not: at roughly twenty
    #: comparisons a month this costs a few dollars, and one misread unit price
    #: costs more than a year of the saving.
    anthropic_model: str = "claude-opus-5"
    #: Extraction only. Left empty it follows ``anthropic_model``.
    #:
    #: Worth setting separately because the two calls are different jobs. Reading
    #: a quote is transcription, and it is output-heavy — a page of line items is
    #: far more tokens out than in, and output is what the bill is made of.
    #: Deciding whether two part numbers are the same item is judgement, is
    #: output-light, and is where a wrong answer costs money. So the cheap model
    #: belongs on extraction, if anywhere, and never on matching.
    #:
    #: claude-sonnet-5 cuts extraction roughly in half; claude-haiku-4-5 by about
    #: three quarters. Both are more likely to slip on a scan or an odd layout,
    #: which is a real trade and therefore yours to make, not a default.
    anthropic_extract_model: str = ""
    #: Reading a document is not a reasoning task. Medium keeps extraction
    #: cheap; the comparison step that has to judge equivalence uses high.
    anthropic_extract_effort: str = "medium"

    @property
    def extract_model(self) -> str:
        """The model that reads documents. Falls back to the main one."""
        return self.anthropic_extract_model or self.anthropic_model

    @property
    def claude_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    # ── OpenAI (the assistant) ─────────────────────────────────────────
    #: The assistant's chat agent runs on the OpenAI API; Claude above is used
    #: only for reading supplier quotes. Create the key at platform.openai.com.
    #: Everything else about the assistant — model, effort, who may use it —
    #: is set by a super admin at runtime, not here.
    openai_api_key: str = ""
    #: Left empty the SDK talks to api.openai.com. Set for a proxy or a
    #: compatible gateway; not needed for direct use.
    openai_base_url: str = ""
    #: One model call, including streaming. Tool calls have their own budget.
    openai_timeout_seconds: float = 120.0
    #: A single tool call — one request to one of this app's own routes.
    #: Generous because SharePoint, Zoho and Graph are behind some of them.
    assistant_tool_timeout_seconds: float = 60.0
    #: A tool result longer than this is cut before the model sees it, so a
    #: full quote list cannot fill the context window in one call.
    assistant_tool_result_max_chars: int = 12_000
    #: What the Proposals mirror embeds rows with, so an incoming email can be
    #: matched to one by meaning rather than by spelling. Small and cheap: the
    #: text being compared is a title and a few notes, and the larger models
    #: buy almost nothing on that. See app/proposals/mirror.py.
    embedding_model: str = "text-embedding-3-small"

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    # ── the background loops ───────────────────────────────────────────
    #: How often the local copy of the Proposals list is refreshed. Every read
    #: that matters — matching an email, counting somebody's workload — goes to
    #: the mirror, so this is the only thing that touches SharePoint on a timer
    #: and its cost is one list read.
    mirror_sync_seconds: int = 120
    #: Off in development and in tests. A background loop that fires against a
    #: live SharePoint list from somebody's laptop is not what anybody meant.
    mirror_sync_enabled: bool = False

    # ── publishing the priority score ──────────────────────────────────
    #: The one place this app writes a ranking outside its own database: the
    #: ``useranalytics`` list, one row per person, rewritten whenever the live
    #: standing changes. Off by default for the same reason the loop above is —
    #: a laptop running the tests must not rewrite a list other tools read.
    analytics_publish_enabled: bool = False
    #: A different site from the Proposals list. The ranking is not the team's
    #: working list and does not belong beside it.
    analytics_site_id: str = (
        "hamdaz1.sharepoint.com,52e0ecb1-8055-49e9-b53e-0a09e104e909,"
        "ba62d842-3d14-4a06-bdf5-ab56c9023b33"
    )
    analytics_list_id: str = "f5795790-d64c-4eb5-a5ff-e8312a94e618"
    analytics_list_url: str = "https://hamdaz1.sharepoint.com/sites/Test/Lists/useranalytics"

    # ── careers (the public application links) ─────────────────────────
    #: The origin this API is reached at from outside, used to build the share
    #: links HR copies. Left empty the links are built from the request itself,
    #: which is right in development and wrong the moment a proxy rewrites the
    #: host — so set it in production.
    public_base_url: str = ""
    #: A separate careers site, if one exists. Set it and the share link points
    #: there instead of at the page this API serves; that site fetches the same
    #: token through the JSON endpoint. Either way the candidate never reaches
    #: an ERP URL.
    careers_url: str = ""
    #: Applications accepted per opening from one address per hour. Best effort
    #: and in-process — it is a brake on a bored person with a form, not a
    #: defence against a distributed flood, and it resets on deploy.
    application_rate_limit: int = 10

    def share_base(self, request_base: str = "") -> str:
        """Where a candidate-facing link points."""
        for candidate in (self.careers_url, self.public_base_url, request_base):
            if candidate:
                return candidate.rstrip("/")
        return ""

    def form_api_base(self, request_base: str = "") -> str:
        """Where the JSON form lives. Never the careers site — that is a client."""
        return (self.public_base_url or request_base or "").rstrip("/")

    # ── machine callers ────────────────────────────────────────────────
    #: Lets a service call this API with a header instead of a user session.
    #: Unset means header auth is off entirely — an empty key must never
    #: authenticate anyone, which is why this is checked rather than compared.
    hamdaz_api_key: str = ""

    # ── session ────────────────────────────────────────────────────────
    #: Signs the session cookie. Rotating it logs everyone out, which is the
    #: correct response to a suspected leak.
    session_secret: str = "change-me"
    session_ttl_minutes: int = 60 * 8
    session_cookie_name: str = "hamdaz_session"
    #: The transient cookie holding OAuth state + PKCE verifier between the
    #: redirect to Microsoft and the callback. Minutes, not hours, by design.
    login_state_cookie_name: str = "hamdaz_login"
    login_state_ttl_minutes: int = 10

    cookie_secure: bool = False
    #: "lax" is correct when the API and frontend share a registrable domain
    #: (localhost:3000 -> localhost:8000 counts). Two different
    #: *.azurewebsites.net hosts do NOT: azurewebsites.net is on the Public
    #: Suffix List, so those are cross-site and need "none" plus cookie_secure.
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None

    @field_validator("cookie_domain", mode="before")
    @classmethod
    def _blank_domain_is_none(cls, value: object) -> object:
        """An empty value means "no domain", not a domain that is empty.

        Leaving a setting blank is how a portal says "unset", and there is no
        way to say it otherwise. Passed through, ``""`` reaches the browser as
        ``Domain=`` on the session cookie, which a browser is entitled to throw
        the whole cookie away over — and a dropped session cookie looks like
        sign-in quietly not working rather than like a configuration mistake.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    #: Mail people about quotes — sent for approval, decided, commented on,
    #: reopened. On by default: an approval nobody is told about waits until
    #: somebody happens to look. Turn it off in a sandbox rather than mailing
    #: colleagues while somebody clicks around.
    notify_by_email: bool = True

    # ── frontend ───────────────────────────────────────────────────────
    #: Where the browser lands after a successful sign-in, and the address the
    #: link in an approval email points at.
    frontend_url: str = "http://localhost:3000"
    #: NoDecode stops pydantic-settings JSON-decoding this before the validator
    #: below runs, which is what lets a plain comma-separated env var work.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        # Env vars arrive as a string; a JSON array also has to keep working.
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @property
    def issuer(self) -> str:
        return f"{self.authority}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"{self.authority}/discovery/v2.0/keys"

    def validate_runtime(self) -> None:
        """Fail fast on configuration that only breaks once a user tries to log in."""
        missing = [
            name
            for name in ("azure_tenant_id", "azure_client_id", "azure_client_secret")
            if not getattr(self, name)
        ]
        if missing:
            raise RuntimeError(f"Entra ID not configured: {', '.join(missing)} unset")

        if self.environment == "production":
            if self.session_secret == "change-me":
                raise RuntimeError("SESSION_SECRET must be set in production")
            if not self.cookie_secure:
                raise RuntimeError("COOKIE_SECURE must be true in production")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            # Browsers silently drop SameSite=None without Secure.
            raise RuntimeError("cookie_samesite='none' requires cookie_secure=true")


@lru_cache
def get_settings() -> Settings:
    return Settings()
