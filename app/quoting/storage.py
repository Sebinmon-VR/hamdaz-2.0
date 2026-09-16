"""Filing the supplier quotes people upload into the shared document library.

**This is the one place this app writes a file into Microsoft 365**, and it
writes to one library: the Documents library of the Test site, which is already
the only write target this system has. Everything else in SharePoint is read
only — the Proposals list most of all, and nothing here goes near it.

What it does is put the documents a person uploaded against a quote into a
shared folder, so the bid's paperwork lives somewhere a colleague can open
without an account on this system. A shared library rather than somebody's
personal OneDrive on purpose: documents that outlive the person who uploaded
them should not depend on that person's account still existing.

Blank configuration switches filing off, and the files then stay in the database
— which is the system of record either way.

**Filing never fails an upload.** The supplier quote is already saved, already
extracted and already attached to the request by the time this runs; if the
drive refuses, the person gets their comparison and the file is simply not
filed. A quote that could not be attached because a drive was full would be a
bad trade, and the database copy remains the system of record either way.

Layout is one folder per quote inside the configured folder::

    /attachments2/QR-0042 — ADNOC switchgear/Alpha Trading 4471.pdf

Named rather than numbered, because the person opening the drive is looking for
a bid, not a UUID. The quote's id is appended when it has no reference yet, so
two untitled drafts cannot collide.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote as urlquote

import httpx

from app.core.config import Settings

logger = logging.getLogger("hamdaz.quoting")

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
_TOKEN_REFRESH_BUFFER_SECONDS: Final = 120

#: Graph takes a straight PUT up to 4 MB. Anything larger needs an upload
#: session, which is three more round trips; supplier quotations are PDFs and
#: almost never near this, so the simple path is the one that is implemented and
#: the large case is refused rather than half-built.
SIMPLE_UPLOAD_LIMIT: Final = 4 * 1024 * 1024

#: Characters OneDrive will not accept in a file or folder name.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DriveError(Exception):
    """The drive refused, or could not be reached."""


@dataclass(frozen=True, slots=True)
class FiledDocument:
    """Where a document ended up."""

    item_id: str
    #: The link a person opens. Their own access decides what they may see.
    web_url: str
    path: str


def safe_name(value: str, *, fallback: str = "document") -> str:
    """A name OneDrive will accept, that still reads like the original."""
    cleaned = _ILLEGAL.sub("-", (value or "").strip()).strip(". ")
    # Collapse the runs of dashes the substitution leaves behind.
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:120] or fallback


def folder_for(reference: str | None, title: str, request_id: Any) -> str:
    """The per-quote folder name: what a person would call this bid."""
    label = " — ".join(part for part in (reference, title) if (part or "").strip())
    named = safe_name(label, fallback="")
    if named:
        return named
    # No reference and no usable title. The id is ugly but unique, and two
    # untitled drafts sharing a folder would be worse.
    return f"quote-{str(request_id)[:8]}"


class QuoteDrive:
    """Files supplier documents into a OneDrive folder."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._settings.files_to_drive

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
            raise DriveError(
                f"client-credentials token request failed ({response.status_code})"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = (
            time.monotonic()
            + int(payload.get("expires_in", 3600))
            - _TOKEN_REFRESH_BUFFER_SECONDS
        )
        return self._token

    def _drive(self) -> str:
        """The configured document library.

        Taken straight from configuration rather than resolved from a site or a
        user. A drive id is stable for the life of the library, and looking one
        up at write time would mean a document's destination depended on a call
        that could fail — which is how a file ends up somewhere nobody expected.
        """
        drive = self._settings.quote_drive_id.strip()
        if not drive:
            raise DriveError("No document library is configured for quote documents")
        return drive

    async def file_document(
        self,
        *,
        folder: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> FiledDocument:
        """Put one file in ``<configured folder>/<folder>/<filename>``.

        Folders are not created explicitly: addressing a path with ``:/content``
        makes the ones it needs. One request instead of three, and no race
        between two uploads for the same quote arriving together.
        """
        if not self.enabled:
            raise DriveError("No drive is configured for quote documents")
        if len(content) > SIMPLE_UPLOAD_LIMIT:
            raise DriveError(
                f"{filename} is {len(content) // (1024 * 1024)} MB, over the "
                f"{SIMPLE_UPLOAD_LIMIT // (1024 * 1024)} MB single-request limit"
            )

        drive = self._drive()
        token = await self._access_token()
        path = f"{self._settings.quote_drive_folder}/{folder}/{safe_name(filename)}"
        response = await self._http.put(
            f"{GRAPH_BASE}/drives/{drive}/root:/{urlquote(path)}:/content",
            content=content,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type or "application/octet-stream",
            },
        )
        if response.status_code not in (200, 201):
            raise DriveError(
                f"upload of {filename} refused ({response.status_code}): "
                f"{response.text[:160]}"
            )
        item = response.json()
        return FiledDocument(
            item_id=item["id"], web_url=item.get("webUrl", ""), path=path
        )

    async def try_file(
        self,
        *,
        folder: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> FiledDocument | None:
        """File it, and say so in the log if that did not work.

        The caller is in the middle of attaching supplier quotes to a request.
        None of that should come undone because a drive was unreachable, so this
        swallows the failure and returns nothing — see the module docstring.
        """
        if not self.enabled:
            return None
        try:
            filed = await self.file_document(
                folder=folder,
                filename=filename,
                content=content,
                content_type=content_type,
            )
        except (DriveError, httpx.HTTPError):
            logger.exception("could not file %s in the quote drive", filename)
            return None
        logger.info("filed %s at %s", filename, filed.path)
        return filed
