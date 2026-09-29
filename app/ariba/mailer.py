"""Telling the super admins when the Ariba reader cannot get in.

Two messages. A failed sign-in, every time — the reader has stopped signing in
and will not start again until one of them resumes it, so each of them needs
to know. And a visit that failed some other way, once when the failures begin
rather than on every attempt: the reader keeps trying those on its own.

Sent from the first super admin's mailbox to all of them, as the follow-up
digest sends from its first recipient: mail here always goes out as a person,
and this is the nearest person with a reason to receive it. A failure to send
is logged and returned, never raised — it must not undo the block it reports.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.mail import GraphMailer
from app.followups.mailer import button, facts, heading, logo_attachment, page
from app.models.role import Role, UserRole
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN

logger = logging.getLogger("hamdaz.ariba.mailer")


async def super_admin_emails(session: AsyncSession) -> list[str]:
    rows = await session.scalars(
        select(User.email)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.key == SUPER_ADMIN, User.is_active.is_(True))
        .order_by(User.email)
    )
    return list(dict.fromkeys(e.strip().lower() for e in rows if e))


def _when(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Asia/Dubai")).strftime("%d %b %Y, %H:%M") + " UAE"


class AribaMailer:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._graph = GraphMailer(settings, http)

    def _link(self) -> str:
        return f"{self._settings.followup_link_url.rstrip('/')}/tenders"

    async def _send(self, session: AsyncSession, subject: str, body: str) -> str | None:
        """Send to every super admin. Returns why it could not, or ``None``."""
        try:
            to = await super_admin_emails(session)
            if not to:
                return "no active super admin to tell"
            await self._graph.send(
                sender=to[0],
                recipients=to,
                subject=subject,
                html=page(body),
                attachments=logo_attachment(),
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            logger.warning("ariba alert not sent: %s", exc)
            return f"{type(exc).__name__}: {exc}"[:300]
        logger.info("ariba alert sent: %s", subject)
        return None

    async def sign_in_failed(
        self, session: AsyncSession, *, reason: str, at: datetime
    ) -> str | None:
        body = (
            heading("The Ariba reader could not sign in", eyebrow="Ariba tenders")
            + "<p>Signing in to the Ariba supplier portal failed, so the reader has "
            "<b>stopped signing in</b>. It will not try again until a super admin "
            "resumes it — retrying a failed sign-in is how a supplier account gets locked.</p>"
            + facts(
                [
                    ("Account", self._settings.ariba_username),
                    ("When", _when(at)),
                    ("What happened", reason),
                ]
            )
            + "<p>Until then no tenders are read and no BCD is taken from new visits. "
            "Tenders already read are kept, and BCD is still compared against them.</p>"
            "<p><b>To fix it:</b> sign in to Ariba yourself to see what it wants — a new "
            "password, a verification code, accepted terms. If the password changed, "
            "update <code>ARIBA_PASSWORD</code> on the server and restart it. Then press "
            "<b>Resume sign-in</b> on the Tenders page.</p>"
            + button(self._link(), "Open Ariba tenders")
        )
        return await self._send(session, "Ariba sign-in failed — the reader has stopped", body)

    async def visit_failed(
        self, session: AsyncSession, *, reason: str, at: datetime
    ) -> str | None:
        body = (
            heading("An Ariba visit failed", eyebrow="Ariba tenders")
            + "<p>The reader was signed in but could not read the Events list. It will "
            "try again at its next visit; this message is sent once, when the failures "
            "begin, not for every attempt.</p>"
            + facts([("When", _when(at)), ("What happened", reason)])
            + button(self._link(), "Open Ariba tenders")
        )
        return await self._send(session, "Ariba visit failed", body)
