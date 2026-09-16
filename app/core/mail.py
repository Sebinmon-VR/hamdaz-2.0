"""Sending mail through Graph, as the person the message is about.

Graph's ``/users/{id}/sendMail`` with an application token sends *as* that
person, so a notification arrives from a colleague rather than from a robot and
a reply goes where a reply should go.

The transport lives here because more than one module needs it, and a second
copy of a token cache is a second thing to get wrong.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.core.config import Settings

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
_TOKEN_REFRESH_BUFFER_SECONDS: Final = 120

logger = logging.getLogger("hamdaz.mail")


#: Graph takes an inline attachment up to about 4 MB, counted across the whole
#: encoded message. Held well under it, because base64 adds a third and the body
#: has to fit too.
MAX_ATTACHMENT_BYTES: Final = 3_000_000


@dataclass(frozen=True, slots=True)
class Attachment:
    """One file to hang on a message."""

    name: str
    content: bytes
    content_type: str = "application/octet-stream"


class MailError(Exception):
    """The message could not be sent."""


class GraphMailer:
    """Token handling and one send. What to say is the caller's business."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        response = await self._http.post(
            f"{self._settings.authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "grant_type": "client_credentials",
                "scope": GRAPH_SCOPE,
            },
        )
        if response.status_code != 200:
            raise MailError(f"token request failed ({response.status_code})")
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = (
            time.monotonic() + int(payload.get("expires_in", 3600))
            - _TOKEN_REFRESH_BUFFER_SECONDS
        )
        return self._token

    async def send(
        self,
        *,
        sender: str,
        recipients: list[str],
        subject: str,
        html: str,
        attachments: list[Attachment] | None = None,
    ) -> dict[str, Any]:
        """Send as ``sender``, optionally with files.

        Attachments go inline in the sendMail body as base64, which Graph caps
        at roughly 4 MB for the whole message. That is ample for what this sends
        — a generated workbook is tens of kilobytes — and the alternative, an
        upload session against a draft, is three more round trips for a case
        nothing here has. An attachment that would blow the cap is dropped with
        a warning rather than failing the mail: the approver being told a quote
        is waiting matters more than the copy of it they could have opened.
        """
        if not recipients:
            raise MailError("There is nobody to send this to")
        if not sender:
            raise MailError("There is no Entra account to send from")

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [
                {"emailAddress": {"address": address}} for address in recipients
            ],
        }
        if attachments:
            kept = []
            for item in attachments:
                if len(item.content) > MAX_ATTACHMENT_BYTES:
                    logger.warning(
                        "attachment %s dropped: %d bytes is over the sendMail limit",
                        item.name,
                        len(item.content),
                    )
                    continue
                kept.append(
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": item.name,
                        "contentType": item.content_type,
                        "contentBytes": base64.b64encode(item.content).decode(),
                    }
                )
            if kept:
                message["attachments"] = kept

        token = await self._access_token()
        response = await self._http.post(
            f"{GRAPH_BASE}/users/{sender}/sendMail",
            json={
                "message": message,
                # It is their own mail; it belongs in their Sent Items.
                "saveToSentItems": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code not in (200, 202):
            raise MailError(
                f"sendMail returned {response.status_code}: {response.text[:200]}"
            )
        return {"sent": True, "recipients": recipients}
