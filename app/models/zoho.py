"""The Zoho Books access token, shared by every process.

Zoho's OAuth model has two ceilings that make a per-process cache wrong:

* **10 active access tokens per refresh token.** Asking for an eleventh does not
  fail — it silently invalidates the oldest. A process that mints its own token
  is therefore capable of logging out another process.
* **10 access token requests per 10 minutes.** A burst of restarts, or a handful
  of instances starting together, exhausts that on its own.

An in-memory cache is per *process*: every restart and every extra worker takes
another of those ten slots. So the token is kept here instead, and the refresh
path takes a row lock — see ``app.zoho.tokens``. Steady state is one refresh per
hour for the whole deployment, however many processes are running.

The refresh token itself is *not* stored. It never expires, is never reissued by
a refresh, and lives in configuration where the other credentials live.

One row, ``id`` fixed at 1, following ``LeaveSettings``. There is exactly one
Zoho organisation, and a table that can only ever hold one row should say so.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped


class ZohoToken(Base, Timestamped):
    __tablename__ = "zoho_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: A live credential. Never log it, and never put it in a response.
    access_token: Mapped[str] = mapped_column(Text, nullable=False)

    #: Handed back by the refresh rather than configured, so the data centre is
    #: discovered once instead of guessed in two places. Stored because it is
    #: needed on every subsequent call and only arrives with a refresh.
    api_domain: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Already includes the safety margin — a token is treated as expired a
    #: little before Zoho would, so one cannot lapse mid-flight.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        # Deliberately no token material.
        return f"<ZohoToken domain={self.api_domain} expires_at={self.expires_at}>"
