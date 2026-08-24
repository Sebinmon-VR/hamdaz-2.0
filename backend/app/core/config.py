"""Typed application settings.

Every knob the app has lives here. Nothing reads ``os.environ`` directly — that is how the
legacy system ended up with configuration scattered across nine modules and a ``.env`` file
with thirty commented-out keys.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── app ────────────────────────────────────────────────────────────
    environment: Environment = Environment.LOCAL
    debug: bool = False
    app_name: str = "Hamdaz 2.0"
    api_v1_prefix: str = "/api/v1"

    # ── database ───────────────────────────────────────────────────────
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://hamdaz:hamdaz@localhost:5432/hamdaz"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ── redis / celery ─────────────────────────────────────────────────
    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    # ── auth (Azure AD / Entra ID) ─────────────────────────────────────
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_redirect_uri: str = "http://localhost:3000/api/v1/auth/callback"
    #: Where the browser is sent after a successful sign-in. The callback is an API
    #: endpoint; landing a human on its JSON body is not a sign-in flow.
    frontend_url: str = "http://localhost:3000"

    jwt_secret: str = "change-me-in-production"  # noqa: S105 - placeholder; rejected in prod below
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 8  # one working day
    session_cookie_name: str = "hamdaz_session"

    # ── SharePoint (C2: live sites are READ ONLY) ──────────────────────
    sharepoint_domain: str = "hamdaz1.sharepoint.com"
    #: The ONLY site path Hamdaz 2.0 may write to. See docs/PROJECT_PLAN.md §8.1.1.
    sharepoint_sandbox_site_path: str = "/sites/sandbox"
    sharepoint_sandbox_list: str = "sandboxlist"
    #: Live sites, ingested read-only. Never writable, regardless of this list.
    sharepoint_read_sites: Annotated[tuple[str, ...], NoDecode] = (
        "/sites/ProposalTeam",
        "/sites/Test",
    )
    #: Master switch. Even when true, writes are confined to the sandbox site.
    sharepoint_sandbox_writes_enabled: bool = False

    # ── outbound side effects (§8.2) ───────────────────────────────────
    #: When false, outbound mail is captured to the outbox table instead of sent.
    outbound_email_enabled: bool = False

    # ── observability ──────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ── cors ───────────────────────────────────────────────────────────
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ("http://localhost:3000",)

    @field_validator("sharepoint_read_sites", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept a comma-separated env var, or a JSON array, for tuple fields.

        These fields carry ``NoDecode`` so pydantic-settings hands the raw string here
        instead of trying ``json.loads`` on it first. Without that, the natural
        ``SHAREPOINT_READ_SITES=/sites/A,/sites/B`` is a startup crash.
        """
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                try:
                    return tuple(str(x) for x in json.loads(text))
                except json.JSONDecodeError:
                    pass
            return tuple(part.strip() for part in text.split(",") if part.strip())
        return v

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    def model_post_init(self, _: object, /) -> None:
        # Fail fast rather than shipping the placeholder secret to production.
        if self.is_production and self.jwt_secret == "change-me-in-production":  # noqa: S105
            raise ValueError("jwt_secret must be set in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
