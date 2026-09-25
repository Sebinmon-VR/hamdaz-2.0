"""Filing a quote's documents into the task's folder in the Proposal Team library.

**This is the one place this app writes a file into Microsoft 365**, and it
writes to one library: the Proposal Team Channel folder of the ProposalTeam
site's Documents library, where the proposal team already keeps a folder per
task. Everything else in SharePoint is read only — the Proposals *list* most
of all, and nothing here goes near it. A file into a folder is not a row into
a list.

What it does is put every document uploaded against a quote — the supplier's
quotation, the customer's RFQ, a courier quote, the selling & costing report
the system renders on submit — into the folder the team already uses for that
task, so the bid's paperwork is where a colleague expects to find it, opened
with their own access and no account on this system.

**The library is the store.** No bytes are kept in the database. An upload that
cannot be filed fails and says why, because there is nowhere else for the file
to be. The one exception is the report on submit, which is filed on a best
effort: a quote that could not reach its approvers because a folder was locked
would be the worse failure.

Layout, inside the configured root folder::

    Proposal Team Channel/
      6000150626 AP Connect for ADNOC Central Laboratory/     ← the task's folder
        Quote request QT-001720/                               ← this quote's
          Supplier quote — router-switch 4471.pdf
          Customer RFQ — RFQ 6000150626.pdf
          QT-001720 Selling & Costing Report pass 1.pdf
      _Quotes without a task/
        Quote request 3f2a9c1d/ ...

The task folder is found by the task's title, then by the event number at the
front of it (the team renames folders, and punctuation drifts), and made from
the title when neither finds one. ``filing.py`` decides which task a quote
belongs to; this module only knows about folders and files.
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

#: Graph takes a straight PUT up to 4 MB. Anything larger goes through an
#: upload session in chunks — a scanned tender runs to tens of megabytes.
SIMPLE_UPLOAD_LIMIT: Final = 4 * 1024 * 1024
#: Chunks must be a multiple of 320 KiB; 26 of them is a shade over 8 MB.
UPLOAD_CHUNK: Final = 26 * 327_680
#: Past this a file is a mistake rather than a document.
MAX_UPLOAD_BYTES: Final = 60 * 1024 * 1024

#: Characters OneDrive will not accept in a file or folder name.
_ILLEGAL = re.compile(r'[<>:"/\\|?*#%\x00-\x1f]')

#: The event or document number the team leads a task title with —
#: "6000150626 AP Connect…", "RFQ 6000151176 supply of…", "Doc334974796 …".
_LEADING_NUMBER = re.compile(r"\b((?:Doc)?\d{6,12})\b", re.I)


class DriveError(Exception):
    """The drive refused, or could not be reached. Safe to show a user."""


@dataclass(frozen=True, slots=True)
class FiledDocument:
    """Where a document ended up."""

    item_id: str
    #: The link a person opens. Their own access decides what they may see.
    web_url: str
    path: str


@dataclass(frozen=True, slots=True)
class FolderMatch:
    """Which folder a task's documents go into, and how it was found."""

    #: The folder name, relative to the library's root folder.
    name: str
    #: exact — a folder named after the title; number — a folder beginning
    #: with the same event number; created — none found, named from the title.
    how: str
    web_url: str | None = None


def safe_name(value: str, *, fallback: str = "document") -> str:
    """A name OneDrive will accept, that still reads like the original."""
    cleaned = _ILLEGAL.sub("-", (value or "").strip()).strip(". ")
    # Collapse the runs of dashes the substitution leaves behind.
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


def leading_number(title: str | None) -> str | None:
    """The event number a task title starts with, if it starts with one."""
    if not title:
        return None
    head = title.strip()[:40]
    match = _LEADING_NUMBER.search(head)
    return match.group(1) if match else None


def quote_folder_name(reference: str | None, request_id: Any) -> str:
    """This quote's own folder inside the task's: what a person would call it."""
    label = (reference or "").strip()
    if not label:
        label = str(request_id)[:8]
    return safe_name(f"Quote request {label}", fallback="Quote request")


def document_file_name(kind_label: str, original: str) -> str:
    """"Supplier quote — router-switch 4471.pdf": the kind first, so a folder
    with a dozen files reads as a list of what they are."""
    return safe_name(f"{kind_label} — {original}", fallback="document")


class QuoteDrive:
    """Files a quote's documents into the Proposal Team Channel library."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._settings.files_to_drive

    @property
    def root(self) -> str:
        """The library's folder everything goes under — "Proposal Team Channel"."""
        return self._settings.quote_drive_folder.strip("/ ")

    @property
    def unlinked_root(self) -> str:
        return self._settings.quote_drive_unlinked_folder.strip("/ ")

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

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}"}

    def _path_url(self, path: str, suffix: str = "") -> str:
        return f"{GRAPH_BASE}/drives/{self._drive()}/root:/{urlquote(path)}:{suffix}"

    # ── finding the task's folder ──────────────────────────────────────

    async def find_task_folder(self, title: str | None) -> FolderMatch:
        """The folder for a task, by its title.

        Three tries, in order of confidence. A folder named exactly as the
        title. Then a folder under the root beginning with the same event
        number — folders get renamed, and "RFQ 6000151176 supply of…" and
        "6000151176 RFQ 6000151176" are the same task. Then a new folder named
        from the title, which the first upload creates by addressing its path.

        A task with no title at all cannot be matched and goes to the unlinked
        folder; the caller decides that.
        """
        if not self.enabled:
            raise DriveError("No drive is configured for quote documents")
        wanted = safe_name(title or "", fallback="")
        if not wanted:
            raise DriveError("The task has no title to name a folder after")

        headers = await self._headers()
        response = await self._http.get(
            self._path_url(f"{self.root}/{wanted}"), headers=headers
        )
        if response.status_code == 200:
            item = response.json()
            if "folder" in item:
                return FolderMatch(item.get("name") or wanted, "exact", item.get("webUrl"))
        elif response.status_code not in (404,):
            raise DriveError(
                f"could not look for the task folder ({response.status_code}): "
                f"{response.text[:160]}"
            )

        number = leading_number(title)
        if number:
            found = await self._search_folder(number, headers)
            if found is not None:
                return found

        return FolderMatch(wanted, "created", None)

    async def _search_folder(self, number: str, headers: dict[str, str]) -> FolderMatch | None:
        """A folder directly under the root whose name starts with ``number``."""
        response = await self._http.get(
            self._path_url(self.root, f"/search(q='{urlquote(number)}')"),
            headers=headers,
            params={"$select": "id,name,folder,webUrl,parentReference", "$top": "50"},
        )
        if response.status_code != 200:
            logger.info("folder search for %s answered %s", number, response.status_code)
            return None
        root_path = f"/{self.root}".casefold()
        candidates = []
        for item in response.json().get("value", []):
            if "folder" not in item:
                continue
            parent = str(item.get("parentReference", {}).get("path", ""))
            # ".../root:/Proposal Team Channel" — directly under the root only.
            if not parent.casefold().endswith(root_path):
                continue
            name = str(item.get("name", ""))
            if leading_number(name) == number or name.casefold().startswith(number.casefold()):
                candidates.append(item)
        if not candidates:
            return None
        # The most recently changed one, when the team made two.
        best = max(candidates, key=lambda i: str(i.get("lastModifiedDateTime", "")))
        return FolderMatch(str(best["name"]), "number", best.get("webUrl"))

    async def folder_url(self, path: str) -> str | None:
        """The link to a folder, once something has been put in it."""
        response = await self._http.get(self._path_url(path), headers=await self._headers())
        if response.status_code != 200:
            return None
        return response.json().get("webUrl")

    # ── putting a file there ───────────────────────────────────────────

    async def file_document(
        self,
        *,
        folder: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> FiledDocument:
        """Put one file at ``<root>/<folder>/<filename>``.

        Folders are not created explicitly: addressing a path with ``:/content``
        makes the ones it needs. One request instead of three, and no race
        between two uploads for the same quote arriving together.

        Small files go in one request. Larger ones go through an upload
        session, in chunks, which is how a forty-page scanned tender arrives.
        """
        if not self.enabled:
            raise DriveError("No drive is configured for quote documents")
        if len(content) > MAX_UPLOAD_BYTES:
            raise DriveError(
                f"{filename} is {len(content) // (1024 * 1024)} MB, over the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        path = f"{self.root}/{folder.strip('/')}/{safe_name(filename)}"
        headers = await self._headers()
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            item = await self._put_small(path, content, content_type, headers)
        else:
            item = await self._put_large(path, content, headers)
        filed = FiledDocument(item_id=item["id"], web_url=item.get("webUrl", ""), path=path)
        logger.info("filed %s at %s", filename, filed.path)
        return filed

    async def _put_small(self, path, content, content_type, headers) -> dict[str, Any]:
        response = await self._http.put(
            self._path_url(path, "/content"),
            content=content,
            headers={**headers, "Content-Type": content_type or "application/octet-stream"},
        )
        if response.status_code not in (200, 201):
            raise DriveError(
                f"upload of {path.rsplit('/', 1)[-1]} refused ({response.status_code}): "
                f"{response.text[:160]}"
            )
        return response.json()

    async def _put_large(self, path, content, headers) -> dict[str, Any]:
        opened = await self._http.post(
            self._path_url(path, "/createUploadSession"),
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if opened.status_code not in (200, 201):
            raise DriveError(
                f"upload session for {path.rsplit('/', 1)[-1]} refused "
                f"({opened.status_code}): {opened.text[:160]}"
            )
        upload_url = opened.json()["uploadUrl"]
        total = len(content)
        item: dict[str, Any] | None = None
        for start in range(0, total, UPLOAD_CHUNK):
            end = min(start + UPLOAD_CHUNK, total)
            chunk = content[start:end]
            response = await self._http.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end - 1}/{total}",
                },
            )
            if response.status_code in (200, 201):
                item = response.json()
            elif response.status_code != 202:
                raise DriveError(
                    f"chunk {start}-{end - 1} refused ({response.status_code}): "
                    f"{response.text[:160]}"
                )
        if item is None:
            raise DriveError("the upload session ended without the file being created")
        return item

    async def delete_item(self, item_id: str) -> None:
        """Remove a file this app filed. Only ever called for an item this app
        recorded, never for anything a person put in the folder."""
        response = await self._http.delete(
            f"{GRAPH_BASE}/drives/{self._drive()}/items/{item_id}",
            headers=await self._headers(),
        )
        if response.status_code not in (204, 404):
            raise DriveError(f"delete refused ({response.status_code}): {response.text[:160]}")

    async def try_file(
        self,
        *,
        folder: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> FiledDocument | None:
        """File it, and say so in the log if that did not work.

        For the report on submit: none of the submission should come undone
        because a folder was unreachable, so this swallows the failure and
        returns nothing. Uploads use :meth:`file_document` and fail loudly.
        """
        if not self.enabled:
            return None
        try:
            return await self.file_document(
                folder=folder, filename=filename, content=content, content_type=content_type
            )
        except (DriveError, httpx.HTTPError):
            logger.exception("could not file %s in the quote drive", filename)
            return None
