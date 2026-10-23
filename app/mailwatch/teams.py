"""Mail watch → Teams: a negotiation from the CEO's mail, posted to a channel.

**Posted as a person.** Graph will not let an application post to a channel on
its own (only a per-team Teams app could), so a super admin connects their own
account once: Microsoft sign-in asking for ``ChannelMessage.Send`` and
``offline_access``, both already admin-consented for this app. The refresh
token is kept encrypted in ``mailwatch_teams`` and turned into an access token
when a post is due. Posts therefore appear as that person.

The sign-in returns through the app's one registered redirect address,
``/auth/callback``; the signed login-state cookie carries ``purpose: teams`` and
the auth router hands those back here (:func:`finish`). Nothing about ordinary
sign-in changes.

**Locked to the test channel.** :data:`CHANNEL_LOCK` is the only channel a post
can go to, resolved by name with the app's own read permission. Moving to the
real channel is a code change, on purpose.

**Once each.** A negotiation mail is posted once; a failure is tried again on
the next pass, three times at most. Only mail received after posting was
switched on is posted — switching on never sends the backlog.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oidc import generate_pkce_pair
from app.core.config import Settings, get_settings
from app.core.mail import GRAPH_BASE, GraphMailer
from app.core.security import sign
from app.mailwatch import assign
from app.mailwatch.assign import TESTING
from app.mailwatch.classify import NEGOTIATION, NEW_TENDER
from app.models.mailwatch import MailWatchTeams, MailWatchTestTask, WatchedMail
from app.proposals.sharepoint import SharePointConsentError, SharePointError, SharePointProposals
from app.quoting.storage import DriveError, QuoteDrive

logger = logging.getLogger("hamdaz.mailwatch.teams")

#: The only channel a post may go to: (team, channel), by display name.
CHANNEL_LOCK: Final = ("Test", "test channel")

#: What the connected account is asked for. Admin consent already covers it.
SCOPES: Final = "openid profile offline_access User.Read ChannelMessage.Send"

#: Marks a login-state cookie as a Teams connection rather than a sign-in.
PURPOSE: Final = "teams"

#: A mail that failed this many times is left alone; the page shows why.
MAX_ATTEMPTS: Final = 3

#: An unused connection is renewed after this; Microsoft's limit is 90 days.
KEEP_ALIVE_AFTER: Final = timedelta(days=7)


class TeamsError(Exception):
    """Something a super admin can be shown as it is."""


# ── the stored connection ───────────────────────────────────────────


def _fernet(settings: Settings) -> Fernet:
    """A key of our own from the session secret: the token in the database is
    useless without the server's configuration as well."""
    digest = hashlib.sha256(b"mailwatch-teams:" + settings.session_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


async def get_connection(session: AsyncSession) -> MailWatchTeams:
    row = await session.get(MailWatchTeams, 1)
    if row is None:
        row = MailWatchTeams(id=1)
        session.add(row)
        await session.flush()
    return row


def is_connected(row: MailWatchTeams) -> bool:
    return bool(row.refresh_token_enc)


# ── connecting: start here, finish in the auth callback ────────────


def start(
    *, settings: Settings, user_id: uuid.UUID, login_state_cookie: str, audience: str
) -> RedirectResponse:
    """Send the super admin to Microsoft to connect their account."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    query = urlencode(
        {
            "client_id": settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": settings.azure_redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    response = RedirectResponse(
        f"{settings.authority}/oauth2/v2.0/authorize?{query}", status_code=307
    )
    response.set_cookie(
        key=login_state_cookie,
        value=sign(
            {"state": state, "verifier": verifier, "purpose": PURPOSE, "user_id": str(user_id)},
            secret=settings.session_secret,
            ttl_minutes=settings.login_state_ttl_minutes,
            audience=audience,
        ),
        max_age=settings.login_state_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )
    return response


def _back(settings: Settings, **query: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{settings.frontend_url}/admin/mail-watch?{urlencode(query)}", status_code=303
    )
    response.delete_cookie(
        key=settings.login_state_cookie_name, httponly=True, secure=settings.cookie_secure,
        samesite=settings.cookie_samesite, domain=settings.cookie_domain, path="/",
    )
    return response


async def finish(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    code: str,
    login_state: dict[str, Any],
) -> RedirectResponse:
    """The code Microsoft sent back: keep the account's refresh token, and
    find the locked channel. The state was already checked by the caller."""
    http: httpx.AsyncClient = request.app.state.http
    response = await http.post(
        f"{settings.authority}/oauth2/v2.0/token",
        data={
            "client_id": settings.azure_client_id,
            "client_secret": settings.azure_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.azure_redirect_uri,
            "code_verifier": login_state.get("verifier", ""),
            "scope": SCOPES,
        },
    )
    if response.status_code != 200:
        logger.warning("teams connect: token exchange failed %s %s", response.status_code, response.text[:300])
        return _back(settings, teams_error="sign_in_failed")
    tokens = response.json()
    if "ChannelMessage.Send" not in tokens.get("scope", "") or not tokens.get("refresh_token"):
        return _back(settings, teams_error="permission_not_granted")

    me = await http.get(
        f"{GRAPH_BASE}/me", params={"$select": "id,displayName,mail,userPrincipalName"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    who = me.json() if me.status_code == 200 else {}

    row = await get_connection(session)
    row.refresh_token_enc = _fernet(settings).encrypt(tokens["refresh_token"].encode()).decode()
    row.account_email = (who.get("mail") or who.get("userPrincipalName") or "").lower() or None
    row.account_name = who.get("displayName")
    row.account_id = who.get("id")
    row.connected_at = datetime.now(UTC)
    with_user = login_state.get("user_id")
    row.connected_by_id = uuid.UUID(with_user) if with_user else None
    row.last_error = None
    try:
        await _resolve_channel(row, request.app.state.mail_reader)
    except TeamsError as exc:
        row.last_error = str(exc)
    await session.commit()
    return _back(settings, teams="connected")


async def _resolve_channel(row: MailWatchTeams, graph: GraphMailer) -> None:
    """Find the locked channel's ids, with the app's own read permission."""
    team_name, channel_name = CHANNEL_LOCK
    token = await graph._access_token()
    headers = {"Authorization": f"Bearer {token}"}
    http = graph._http
    teams = await http.get(
        f"{GRAPH_BASE}/groups",
        params={
            "$filter": f"displayName eq '{team_name}' and resourceProvisioningOptions/Any(x:x eq 'Team')",
            "$select": "id,displayName",
        },
        headers=headers,
    )
    found = teams.json().get("value", []) if teams.status_code == 200 else []
    if not found:
        raise TeamsError(f"No team called {team_name!r} was found.")
    team_id = found[0]["id"]
    channels = await http.get(
        f"{GRAPH_BASE}/teams/{team_id}/channels",
        params={"$select": "id,displayName"}, headers=headers,
    )
    match = next(
        (c for c in channels.json().get("value", [])
         if (c.get("displayName") or "").strip().lower() == channel_name.lower()),
        None,
    ) if channels.status_code == 200 else None
    if match is None:
        raise TeamsError(f"No channel called {channel_name!r} in {team_name!r}.")
    row.team_id, row.team_name = team_id, found[0]["displayName"]
    row.channel_id, row.channel_name = match["id"], match["displayName"]


def disconnect(row: MailWatchTeams) -> None:
    row.refresh_token_enc = None
    row.account_email = row.account_name = row.account_id = None
    row.connected_at = None
    row.post_negotiations = False


# ── posting ─────────────────────────────────────────────────────────


@dataclass
class TaskFile:
    name: str
    url: str | None
    where: str  # "Attachment", or the subfolder it sits in
    modified: str | None


@dataclass
class TaskFiles:
    files: list[TaskFile] = field(default_factory=list)
    folder_url: str | None = None
    #: The task has list attachments that could not be read.
    attachments_hidden: bool = False


@dataclass
class _Cached:
    token: str
    expires: float


class TeamsPoster:
    """Turns the stored refresh token into access tokens, and posts."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        graph: GraphMailer,
        *,
        sharepoint: SharePointProposals | None = None,
        drive: QuoteDrive | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._graph = graph
        self._sharepoint = sharepoint
        self._drive = drive
        self._cached: _Cached | None = None

    async def people(self, emails: list[str]) -> dict[str, dict[str, str]]:
        """Directory entries for the card's people, by lower-case email, so
        Teams can draw them with their photo. Whoever can't be found is left
        out and shown by name."""
        out: dict[str, dict[str, str]] = {}
        wanted = {e.strip().lower() for e in emails if e and e.strip()}
        if not wanted or self._graph is None:
            return out
        try:
            headers = {"Authorization": f"Bearer {await self._graph._access_token()}"}
        except Exception as exc:  # noqa: BLE001 - only the photos on a card
            logger.info("teams card: no directory token: %s", exc)
            return out
        for email in wanted:
            response = await self._http.get(
                f"{GRAPH_BASE}/users/{email}",
                params={"$select": "id,displayName,userPrincipalName"},
                headers=headers,
            )
            if response.status_code == 200:
                out[email] = response.json()
        return out

    async def task_files(self, task: dict[str, Any]) -> TaskFiles:
        """The task's documents, read only: its list attachments, then its
        Proposal Team Channel folder, newest first. Whatever can't be read is
        left out — a post never waits on, or fails for, its file list."""
        out = TaskFiles()
        if self._sharepoint is not None and task.get("item_id"):
            host = "https://" + self._settings.sharepoint_site_id.split(",")[0]
            try:
                try:
                    attached = await self._sharepoint.attachments_of(str(task["item_id"]))
                except SharePointError:
                    # SharePoint fails a call now and then; once more before
                    # the card goes without its attachments.
                    await asyncio.sleep(2)
                    attached = await self._sharepoint.attachments_of(str(task["item_id"]))
                for att in attached:
                    out.files.append(
                        TaskFile(
                            att["file_name"], host + quote(att["server_url"]),
                            "Attached to the task",
                            None,
                        )
                    )
            except (SharePointConsentError, SharePointError) as exc:
                logger.info("teams card: attachments not read: %s", exc)
                # Say on the card that there are some, when the list knows.
                try:
                    found = await self._sharepoint.task(str(task["item_id"]))
                    out.attachments_hidden = bool(found.has_attachments)
                except Exception:  # noqa: BLE001 - only a hint on the card
                    pass
        if self._drive is not None and self._drive.enabled and task.get("title"):
            try:
                folder = await self._drive.find_task_folder(task["title"])
                if folder.how != "created":
                    out.folder_url = folder.web_url
                    found = await self._drive.list_files(folder.name, limit=100)
                    found.sort(key=lambda f: f.get("modified") or "", reverse=True)
                    for f in found:
                        where = f["path"].rpartition("/")[0] or "Task folder"
                        out.files.append(
                            TaskFile(f["name"], f["web_url"], where, f.get("modified"))
                        )
            except DriveError as exc:
                logger.info("teams card: task folder not read: %s", exc)
        return out

    async def _token(self, row: MailWatchTeams) -> str:
        if self._cached and time.monotonic() < self._cached.expires:
            return self._cached.token
        if not row.refresh_token_enc:
            raise TeamsError("No Teams account is connected.")
        try:
            refresh = _fernet(self._settings).decrypt(row.refresh_token_enc.encode()).decode()
        except InvalidToken as exc:
            raise TeamsError("The stored Teams connection can't be read; connect again.") from exc
        response = await self._http.post(
            f"{self._settings.authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": SCOPES,
            },
        )
        if response.status_code != 200:
            error = (response.json() if response.headers.get("content-type", "").startswith("application/json") else {}).get("error", "")
            if error in ("invalid_grant", "interaction_required"):
                raise TeamsError(
                    "The Teams connection has lapsed (password or MFA change, or long unused). "
                    "Connect again."
                )
            raise TeamsError(f"Microsoft refused a Teams token ({response.status_code}).")
        payload = response.json()
        if payload.get("refresh_token"):
            row.refresh_token_enc = (
                _fernet(self._settings).encrypt(payload["refresh_token"].encode()).decode()
            )
        self._cached = _Cached(
            payload["access_token"], time.monotonic() + int(payload.get("expires_in", 3600)) - 300
        )
        return self._cached.token

    def can_read(self, row: MailWatchTeams) -> bool:
        """Whether this server can open the stored connection. One whose
        session secret differs (a laptop sharing the live database) can't, and
        leaves posting to the server that made it."""
        if not row.refresh_token_enc:
            return False
        try:
            _fernet(self._settings).decrypt(row.refresh_token_enc.encode())
        except InvalidToken:
            return False
        return True

    async def keep_alive(self, row: MailWatchTeams) -> bool:
        """Swap the refresh token for a fresh one when it has gone unused for a
        while. Microsoft drops a refresh token left unused for 90 days, and a
        quiet spell with no negotiations must not cost the connection. True
        when it was renewed."""
        if not self.can_read(row):
            return False
        last = row.last_used_at or row.connected_at
        if last is not None and datetime.now(UTC) - last < KEEP_ALIVE_AFTER:
            return False
        self._cached = None
        try:
            await self._token(row)
        except TeamsError as exc:
            row.last_error = str(exc)[:500]
            logger.warning("mail watch Teams keep-alive: %s", exc)
            return False
        row.last_used_at = datetime.now(UTC)
        return True

    async def post(
        self, row: MailWatchTeams, content_html: str, *, card: dict | None = None
    ) -> str:
        """Post to the locked channel; the new message's id. With ``card``, the
        message is that Adaptive Card and ``content_html`` is ignored."""
        team_name, channel_name = CHANNEL_LOCK
        if not row.channel_id or (row.channel_name or "").lower() != channel_name.lower() or (
            (row.team_name or "").lower() != team_name.lower()
        ):
            await _resolve_channel(row, self._graph)
        token = await self._token(row)
        if card is not None:
            attachment_id = uuid.uuid4().hex
            message: dict[str, Any] = {
                "body": {
                    "contentType": "html",
                    "content": f'<attachment id="{attachment_id}"></attachment>',
                },
                "attachments": [
                    {
                        "id": attachment_id,
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": json.dumps(card),
                    }
                ],
            }
        else:
            message = {"body": {"contentType": "html", "content": content_html}}
        response = await self._http.post(
            f"{GRAPH_BASE}/teams/{row.team_id}/channels/{row.channel_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=message,
        )
        if response.status_code == 401:
            self._cached = None
        if response.status_code >= 400:
            raise TeamsError(f"Teams refused the post ({response.status_code}): {response.text[:300]}")
        row.last_used_at = datetime.now(UTC)
        row.last_error = None
        return str(response.json().get("id") or "")


# ── what a negotiation post says ────────────────────────────────────

_AMOUNT = re.compile(r"\b(?:AED|USD|EUR|GBP|SAR|QAR|OMR)\s?[\d,]+(?:\.\d+)?", re.I)
_BOILERPLATE = re.compile(
    r"(You are receiving this email|This message has one or more attachments|Click Here|"
    r"For details about this event|Please visit the)",
    re.I,
)


def ask_of(body: str | None) -> tuple[str, str, list[str]]:
    """(the CEO's own note above the forward, what the customer wrote, amounts)."""
    text = body or ""
    note = ""
    header = re.search(r"^_{5,}|^\s*From:\s", text, re.M)
    if header:
        note = re.sub(r"\s+", " ", text[: header.start()]).strip()
    subject = re.search(r"^\s*Subject:.*$", text, re.M)
    rest = text[subject.end():] if subject else text
    cut = _BOILERPLATE.search(rest)
    if cut:
        rest = rest[: cut.start()]
    rest = re.sub(r"\s+", " ", rest).strip()
    return note[:300], rest[:700], list(dict.fromkeys(_AMOUNT.findall(rest)))


def _e(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


#: Remarks lines shown on the card, newest last, as the list keeps them.
REMARK_LINES: Final = 12

#: Files listed per tab at most; the rest are a click away in the folder.
FILE_ROWS: Final = 6

#: Teams refuses a message over 28 KB; the card keeps under this, leaving
#: room for the message around it.
CARD_BYTES: Final = 26_000

#: Wide clients get columns, narrow ones (phones, a narrow pane) get rows.
WIDE: Final = "atLeast:standard"
NARROW: Final = "atMost:narrow"

_FILE_ICONS: Final = {
    "pdf": "DocumentPdf", "xls": "DocumentTable", "xlsx": "DocumentTable",
    "csv": "DocumentTable", "doc": "DocumentText", "docx": "DocumentText",
    "png": "Image", "jpg": "Image", "jpeg": "Image", "zip": "FolderZip",
    "msg": "Mail", "eml": "Mail",
}


def _v(value: Any) -> str:
    """A card value: plain text, never empty."""
    return str(value).strip() if value not in (None, "") else "—"


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(value: str | None) -> str:
    """An ISO date or time from the list as "21 Oct 2026"."""
    when = _when(value)
    return when.strftime("%d %b %Y") if when else _v(value)


def _text(value: str, **extra: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": value, "wrap": True, **extra}


def _caption(value: str, **extra: Any) -> dict[str, Any]:
    """The grey name above a value, as in "Account / Fabrikam"."""
    return _text(value, isSubtle=True, **extra)


def _heading(value: str, **extra: Any) -> dict[str, Any]:
    return _text(value, weight="Bolder", spacing="Medium", **extra)


def _icon(name: str, *, size: str = "xSmall", color: str = "Default", filled: bool = True,
          **extra: Any) -> dict[str, Any]:
    return {
        "type": "Icon", "name": name, "size": size, "color": color,
        "style": "Filled" if filled else "Regular", **extra,
    }


def _with_icon(icon: dict[str, Any], text: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """An icon and a line of text side by side."""
    return {
        "type": "ColumnSet", **extra,
        "columns": [
            {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
             "items": [icon]},
            {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center",
             "spacing": "Small", "items": [text]},
        ],
    }


def _badge(text: str, *, icon: str, style: str) -> dict[str, Any]:
    return {
        "type": "Badge", "text": text, "icon": icon, "style": style,
        "appearance": "Tint", "shape": "Circular", "size": "Large",
        "fallback": _text(text, weight="Bolder", color=style),
    }


def _persona(person: dict[str, str] | None, name: str | None) -> dict[str, Any]:
    """A person with their Teams photo (Teams fetches it, and opens their
    profile card on a click); just the name when they can't be looked up."""
    plain = _with_icon(_icon("Person", color="Accent"), _text(_v(name), weight="Bolder"))
    if not person:
        return plain
    return {
        "type": "Component", "name": "graph.microsoft.com/user", "view": "compact",
        "properties": {
            "id": person["id"],
            "displayName": person.get("displayName") or _v(name),
            "userPrincipalName": person.get("userPrincipalName") or "",
        },
        "fallback": plain,
    }


def _countdown(bcd: str | None) -> tuple[str, str] | None:
    """(what to say, colour) for the bid closing date, as of now."""
    when = _when(bcd)
    if when is None:
        return None
    days = (when.date() - datetime.now(UTC).date()).days
    if days < 0:
        return (f"Closed {-days} day{'s' if days != -1 else ''} ago", "Attention")
    if days == 0:
        return ("Closes today", "Attention")
    return (f"Closes in {days} day{'s' if days != 1 else ''}", "Warning" if days <= 3 else "Good")


def _facts(facts: list[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Captioned values: one row of columns on a wide card, caption-and-value
    lines on a narrow one."""
    wide = {
        "type": "ColumnSet", "targetWidth": WIDE, "spacing": "Medium",
        "columns": [
            {"type": "Column", "width": "stretch", "items": [_caption(title), *value]}
            for title, value in facts
        ],
    }
    narrow = {
        "type": "Container", "targetWidth": NARROW, "spacing": "Medium",
        "items": [
            {
                "type": "ColumnSet", "spacing": "Small",
                "columns": [
                    {"type": "Column", "width": 2, "items": [_caption(title)]},
                    {"type": "Column", "width": 3, "items": value},
                ],
            }
            for title, value in facts
        ],
    }
    return [wide, narrow]


def _tracker(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Where the tender stands: received → submitted → negotiation → order.
    A negotiation mail puts it at negotiation, so that step is always lit."""
    submitted = "submit" in (task.get("submission_status") or "").lower()
    ordered = bool(task.get("order_status"))
    steps = [
        (f"{task.get('current_type') or 'Enquiry'} received", "done"),
        ("Submitted", "done" if submitted else "missed"),
        ("Negotiation", "done" if ordered else "now"),
        ("Order", "done" if ordered else "next"),
    ]
    looks = {
        "done": ("CheckmarkCircle", "Good", True),
        "now": ("Circle", "Accent", True),
        "missed": ("DismissCircle", "Default", False),
        "next": ("Circle", "Default", False),
    }
    columns: list[dict[str, Any]] = []
    for i, (name, state) in enumerate(steps):
        if i:
            columns.append(
                {
                    "type": "Column", "width": "stretch", "targetWidth": WIDE,
                    "verticalContentAlignment": "Center",
                    "items": [_text("────────", wrap=False, horizontalAlignment="Center",
                                    isSubtle=True)],
                }
            )
        icon, colour, filled = looks[state]
        columns.append(
            {
                "type": "Column", "width": "auto",
                "items": [
                    {**_icon(icon, size="Small", color=colour, filled=filled),
                     "horizontalAlignment": "Center"},
                    _text(name, size="Small", spacing="Small", horizontalAlignment="Center",
                          weight="Bolder" if state == "now" else "Default",
                          color="Accent" if state == "now" else "Default",
                          isSubtle=state in ("next", "missed")),
                ],
            }
        )
    return [_heading("Progress"), {"type": "ColumnSet", "spacing": "Small", "columns": columns}]


def _toggle(show_more: bool) -> dict[str, Any]:
    """The "See more ⌄" / "See less ⌃" link for the customer's message; each
    swaps itself for the other."""
    return {
        "type": "ColumnSet",
        "id": "more" if show_more else "less",
        "isVisible": show_more,
        "selectAction": {
            "type": "Action.ToggleVisibility",
            "targetElements": ["more", "less", "ask-short", "ask-full"],
        },
        "columns": [
            {"type": "Column", "width": "auto",
             "items": [_text("See more" if show_more else "See less", color="Accent")]},
            {"type": "Column", "width": "auto", "spacing": "Small",
             "verticalContentAlignment": "Center",
             "items": [_icon("ChevronDown" if show_more else "ChevronUp", color="Accent",
                             filled=False)]},
        ],
    }


#: Where a list attachment says it came from, as opposed to a folder path.
ATTACHED: Final = "Attached to the task"


def _file_tile(f: TaskFile) -> dict[str, Any]:
    """One file as a clickable tile: its type's icon, its name as a link, and
    where it sits."""
    ext = f.name.rpartition(".")[2].lower() if "." in f.name else ""
    icon = _FILE_ICONS.get(ext, "Document")
    where = ext.upper() or "File"
    if f.where != ATTACHED:
        where += f" · {f.where}"
    if f.modified:
        where += f" · {_day(f.modified)}"
    tile: dict[str, Any] = {
        "type": "Container", "style": "emphasis", "roundedCorners": True, "spacing": "Small",
        "items": [{
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_icon(icon, size="Medium", color="Accent", filled=False)]},
                {
                    "type": "Column", "width": "stretch", "spacing": "Small",
                    "items": [
                        _text(f.name, weight="Bolder", color="Accent" if f.url else "Default",
                              maxLines=2),
                        _caption(where, size="Small", spacing="None"),
                    ],
                },
            ],
        }],
    }
    if f.url:
        tile["selectAction"] = {"type": "Action.OpenUrl", "url": f.url, "tooltip": f.name}
    return tile


def _file_grid(files: list[TaskFile], shown: int) -> list[dict[str, Any]]:
    """Two tiles a row. Built once, not once per width: file links are long,
    and Teams refuses a card over 28 KB."""
    tiles = [_file_tile(f) for f in files[:shown]]
    rows = [
        {
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                {"type": "Column", "width": 1, "items": [pair]}
                for pair in tiles[i:i + 2]
            ]
            # A lone last tile keeps half the width, as in the rows above it.
            + ([{"type": "Column", "width": 1, "items": []}] if i + 1 == len(tiles) else []),
        }
        for i in range(0, len(tiles), 2)
    ]
    if len(files) > shown:
        rows.append(_caption(f"+{len(files) - shown} more — open the task or its folder",
                             size="Small"))
    return rows


def _tabs(tabs: list[tuple[str, str, str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """A tab strip and its panels. Each tab is a pill — filled when chosen,
    tinted otherwise. There is one strip per tab, drawn with that tab chosen,
    so a strip always knows what is showing: a click hides this strip and its
    panel and shows the clicked tab's. All in the card (ToggleVisibility), so
    nothing has to answer — and short ids, as Teams caps a card at 28 KB."""
    out: list[dict[str, Any]] = []
    for a, _ in enumerate(tabs):
        columns = []
        for k, (_, title, icon, _) in enumerate(tabs):
            chosen = k == a
            column: dict[str, Any] = {
                "type": "Column", "width": "auto", "spacing": "Small",
                "items": [{
                    "type": "Badge", "text": title, "icon": icon, "size": "Large",
                    "shape": "Rounded",
                    "style": "Accent" if chosen else "Default",
                    "appearance": "Filled" if chosen else "Tint",
                    "fallback": {"type": "TextBlock", "text": title,
                                 "color": "Accent" if chosen else "Default"},
                }],
            }
            if not chosen:
                column["selectAction"] = {
                    "type": "Action.ToggleVisibility",
                    "targetElements": [f"s{a}", f"p{a}", f"s{k}", f"p{k}"],
                }
            columns.append(column)
        out.append({"type": "ColumnSet", "id": f"s{a}", "isVisible": a == 0,
                    "separator": True, "spacing": "Medium", "columns": columns})
    for k, (_, _, _, panel) in enumerate(tabs):
        out.append({"type": "Container", "id": f"p{k}", "isVisible": k == 0,
                    "spacing": "Small", "items": panel})
    return out


def negotiation_card(
    mail: WatchedMail,
    files: TaskFiles | None = None,
    people: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The card, with as many files as fit under Teams' size limit."""
    shown = FILE_ROWS
    while True:
        card = _negotiation_card(mail, files, people, shown)
        if shown == 0 or len(json.dumps(card, ensure_ascii=False)) <= CARD_BYTES:
            return card
        shown -= 1


def _negotiation_card(
    mail: WatchedMail,
    files: TaskFiles | None,
    people: dict[str, dict[str, str]] | None,
    shown: int,
) -> dict[str, Any]:
    """The Teams post for a negotiation. On top the customer, a badge, and the
    facts that matter (who has it, quote, status, BCD); then how far the
    tender has come; the customer's message; then tabs — the task's
    attachments, its Proposal Team Channel folder, its details, its remarks."""
    task = mail.task or {}
    files = files or TaskFiles()
    people = people or {}
    note, ask, amounts = ask_of(mail.body)
    refs = " · ".join(mail.reference_numbers) or "no reference"
    customer = _v(mail.customer or task.get("customer"))

    body: list[dict[str, Any]] = [
        *_testing_note("Posted to the test channel only; nothing is written anywhere."),
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [
                        _text(customer, size="Large", weight="Bolder"),
                        _caption(f"Negotiation · {refs}", spacing="None"),
                    ],
                },
                {
                    "type": "Column", "width": "auto", "targetWidth": WIDE,
                    "items": [_badge("Negotiation", icon="ArrowSwap", style="Accent")],
                },
            ],
        },
        {**_badge("Negotiation", icon="ArrowSwap", style="Accent"), "targetWidth": NARROW},
    ]

    if task:
        submitted = "submit" in (task.get("submission_status") or "").lower()
        status = _v(task.get("status"))
        if task.get("submission_status"):
            status += f" · {task['submission_status']}"
        countdown = _countdown(task.get("bid_closing_date"))
        bcd = (
            [_text(countdown[0], weight="Bolder", color=countdown[1], spacing="None"),
             _text(_day(task.get("bid_closing_date")), size="Small", isSubtle=True,
                   spacing="None")]
            if countdown else [_text("—", weight="Bolder", spacing="None")]
        )
        body += _facts([
            ("Assigned to", [_persona(people.get((task.get("assignee_email") or "").lower()),
                                      task.get("assignee"))]),
            ("Quote", [_text(_v(task.get("quote_no")), weight="Bolder", spacing="None")]),
            ("Status", [_with_icon(
                _icon("CheckmarkCircle" if submitted else "Clock",
                      color="Good" if submitted else "Warning"),
                _text(status, weight="Bolder", color="Good" if submitted else "Default"),
                spacing="None",
            )]),
            ("BCD", bcd),
        ])
        body += [{"type": "Container", "separator": True, "spacing": "Medium",
                  "items": _tracker(task)}]
    else:
        body.append(
            {
                "type": "Container", "style": "attention", "spacing": "Medium",
                "items": [_with_icon(
                    _icon("Warning", color="Attention"),
                    _text("No task found on the Proposals list for this negotiation — please "
                          "check which task it belongs to.", weight="Bolder"),
                )],
            }
        )

    when = mail.received_at.strftime("%d %b, %H:%M") if mail.received_at else ""
    body.append(
        {
            "type": "ColumnSet", "separator": True, "spacing": "Medium",
            "columns": [
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_icon("Mail", size="Medium", color="Accent")]},
                {
                    "type": "Column", "width": "stretch", "spacing": "Small",
                    "items": [
                        _text(customer, weight="Bolder"),
                        _caption(f"Customer message · {when} UTC", size="Small", spacing="None"),
                    ],
                },
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_toggle(True), _toggle(False)]},
            ],
        }
    )
    message = f"“{ask}”" if ask else "The mail had no message text."
    body += [
        _text(message, id="ask-short", maxLines=3, isSubtle=not ask),
        _text(message, id="ask-full", isVisible=False, isSubtle=not ask),
    ]
    if amounts:
        body.append(_with_icon(_icon("Money", color="Good"),
                               _text("  ·  ".join(amounts), weight="Bolder")))
    if note:
        body.append(
            {
                "type": "Container", "style": "warning", "spacing": "Medium",
                "items": [_text("Note from the CEO", weight="Bolder", size="Small"),
                          _text(note, spacing="Small")],
            }
        )

    tabs: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    if task:
        attached = [f for f in files.files if f.where == ATTACHED]
        in_folder = [f for f in files.files if f.where != ATTACHED]
        if attached:
            panel = _file_grid(attached, shown)
        elif files.attachments_hidden:
            panel = [_with_icon(
                _icon("Info", color="Warning", filled=False),
                _caption("The task has attachments, but they can't be listed here yet — "
                         "open the task to see them."),
            )]
        else:
            panel = [_caption("No attachments on the task.")]
        title = f"Attachments ({len(attached)})" if attached else "Attachments"
        tabs.append(("att", title, "Attach", panel))

        if in_folder:
            panel = _file_grid(in_folder, shown)
        elif files.folder_url:
            panel = [_caption("The task's folder is empty.")]
        else:
            panel = [_caption("No folder for this task in Proposal Team Channel yet.")]
        title = f"Task folder ({len(in_folder)})" if in_folder else "Task folder"
        tabs.append(("folder", title, "Folder", panel))

        tabs.append(("details", "Details", "Info", [{
            "type": "FactSet",
            "facts": [
                {"title": "Task", "value": _v(task.get("title"))},
                {"title": "Stage", "value": _v(task.get("current_type"))},
                {"title": "Negotiation so far", "value": _v(task.get("negotiation"))},
                {"title": "Order status", "value": _v(task.get("order_status"))},
                {"title": "Created", "value": _day(task.get("created_at"))},
            ],
        }]))
    remarks = [line.strip() for line in (task.get("remarks") or "").splitlines() if line.strip()]
    if remarks:
        shown = remarks[-REMARK_LINES:]
        panel = [_text(f"• {line}", spacing="Small") for line in shown]
        if len(remarks) > len(shown):
            panel.insert(0, _caption("Earlier remarks are on the task.", size="Small"))
        tabs.append(("remarks", f"Remarks ({len(remarks)})", "Note", panel))
    if tabs:
        body += _tabs(tabs)

    body += _footer(ai=_ai_helped(mail))
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": body,
    }
    links = []
    if task.get("url"):
        links.append({"type": "Action.OpenUrl", "title": "Open task", "url": task["url"],
                      "iconUrl": "icon:Open"})
    if files.folder_url:
        links.append({"type": "Action.OpenUrl", "title": "Open folder",
                      "url": files.folder_url, "iconUrl": "icon:Folder"})
    if links:
        card["actions"] = links
    return card


# ── what a new-tender post says ─────────────────────────────────────


def new_tender_card(
    mail: WatchedMail,
    task: MailWatchTestTask,
    files: TaskFiles | None = None,
    people: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The card, with as many of the earlier task's files as fit."""
    shown = FILE_ROWS
    while True:
        card = _new_tender_card(mail, task, files, people, shown)
        if shown == 0 or len(json.dumps(card, ensure_ascii=False)) <= CARD_BYTES:
            return card
        shown -= 1


def _new_tender_card(
    mail: WatchedMail,
    task: MailWatchTestTask,
    files: TaskFiles | None,
    people: dict[str, dict[str, str]] | None,
    shown: int,
) -> dict[str, Any]:
    """The Teams post for a new tender: the tender, who it went to and why,
    then tabs — the task as it would go on the list, the team's standing (or,
    on a stage change, the earlier task and its files), the mail's own
    attachments."""
    files = files or TaskFiles()
    people = people or {}
    earlier = mail.task or {}
    staged = task.action == "stage_change"
    customer = _v(mail.customer or task.end_user)
    refs = " · ".join([*mail.reference_numbers, *([mail.ariba_doc] if mail.ariba_doc else [])])
    stage = (mail.stage or "").upper() or "Tender"
    badge = (
        _badge(f"{(task.previous_stage or '?').upper()} → {stage}", icon="ArrowUp",
               style="Warning")
        if staged else _badge("New task", icon="AddCircle", style="Good")
    )

    body: list[dict[str, Any]] = [
        *_testing_note(
            "Posted to the test channel. The task is created in the real Proposals list."
            if _in_real_list(task) else
            "The task is created in the Proposals Test list (Test site), not the real list."
            if task.list_item_id else
            "The task is kept in the app only, not written to any list."
        ),
        {
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [
                        _text(_v(mail.title or task.title), size="Large", weight="Bolder"),
                        _caption(f"{customer} · {refs or 'no reference'}", spacing="None"),
                    ],
                },
                {"type": "Column", "width": "auto", "targetWidth": WIDE, "items": [badge]},
            ],
        },
        {**badge, "targetWidth": NARROW},
    ]

    countdown = _countdown(task.bcd.isoformat() if task.bcd else None)
    bcd = (
        [_text(countdown[0], weight="Bolder", color=countdown[1], spacing="None"),
         _caption(
             task.bcd.strftime("%d %b %Y") if mail.closing_at
             else "Not in the mail — set to today, to be corrected",
             size="Small", spacing="None",
         )]
        if countdown and task.bcd else [_text("Not given", isSubtle=True, spacing="None")]
    )
    why = f"Had the {(task.previous_stage or 'earlier stage').upper()}" if staged else (
        "Priority 1 in Presale"
    )
    body += _facts([
        ("Assigned to", [_persona(people.get((task.assigned_to_email or "").lower()),
                                  task.assigned_to_name)]),
        ("Why", [_with_icon(
            _icon("History" if staged else "Trophy", color="Accent"),
            _text(why, weight="Bolder"), spacing="None",
        )]),
        ("Type", [_text(_v(task.current_type or stage), weight="Bolder", spacing="None")]),
        ("BCD", bcd),
    ])

    note, ask, _ = ask_of(mail.body)
    when = mail.received_at.strftime("%d %b, %H:%M") if mail.received_at else ""
    kind = {"invitation": "Invitation", "release": "Tender release"}.get(mail.kind or "", "Mail")
    body.append(
        {
            "type": "ColumnSet", "separator": True, "spacing": "Medium",
            "columns": [
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_icon("Mail", size="Medium", color="Accent")]},
                {
                    "type": "Column", "width": "stretch", "spacing": "Small",
                    "items": [
                        _text(customer, weight="Bolder"),
                        _caption(f"{kind} · {when} UTC", size="Small", spacing="None"),
                    ],
                },
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_toggle(True), _toggle(False)]},
            ],
        }
    )
    message = ask or (mail.subject or "")
    body += [
        _text(message, id="ask-short", maxLines=3),
        _text(message, id="ask-full", isVisible=False),
    ]
    if note:
        body.append(
            {
                "type": "Container", "style": "warning", "spacing": "Medium",
                "items": [_text("Note from the CEO", weight="Bolder", size="Small"),
                          _text(note, spacing="Small")],
            }
        )

    tabs: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    tabs.append(("task", "Task", "TaskListLtr", [
        _caption(
            f"Created in the {_list_name(task)} list (item {task.list_item_id}):"
            if task.list_item_id else "As it would be created on the Proposals list:",
            size="Small",
        ),
        {
            "type": "FactSet", "spacing": "Small",
            "facts": [
                {"title": "Title", "value": task.title},
                {"title": "Assigned To", "value": _v(task.assigned_to_name)},
                {"title": "Status", "value": _v(task.status)},
                {"title": "CurrentType", "value": _v(task.current_type)},
                {"title": "End User", "value": _v(task.end_user)},
                {"title": "Start Date", "value": _v(task.start_date and
                                                    task.start_date.strftime("%d %b %Y"))},
                {"title": "BCD UAE Time", "value": _v(task.bcd and task.bcd.astimezone(
                    ZoneInfo("Asia/Dubai")).strftime("%d %b %Y %H:%M"))},
                {"title": "Remarks", "value": _v(task.remarks)},
            ],
        },
    ]))

    if staged:
        facts = [
            {"title": "Task", "value": _v(task.previous_title)},
            {"title": "Assigned to", "value": _v(earlier.get("assignee"))},
            {"title": "Status", "value": _v(earlier.get("status"))
             + (f" · {earlier['submission_status']}" if earlier.get("submission_status") else "")},
            {"title": "Stage", "value": _v((task.previous_stage or "").upper())},
            {"title": "Quote no.", "value": _v(earlier.get("quote_no"))},
            {"title": "Created", "value": _day(earlier.get("created_at"))},
        ]
        panel: list[dict[str, Any]] = [
            _text(task.assign_reason, isSubtle=True),
            {"type": "FactSet", "spacing": "Small", "facts": facts},
        ]
        if files.files:
            panel += [_heading(f"Its files ({len(files.files)})"), *_file_grid(files.files, shown)]
        elif files.attachments_hidden:
            panel.append(_caption("It has attachments — open the task to see them.",
                                  size="Small"))
        tabs.append(("earlier", "Earlier task", "History", panel))
    else:
        rows: list[dict[str, Any]] = [_text(task.assign_reason, isSubtle=True)]
        rows += [_standing_row(person) for person in task.standing]
        tabs.append(("standing", "Presale standing", "People", rows))

    if mail.attachment_names:
        tabs.append(("mail-files", f"Mail attachments ({len(mail.attachment_names)})", "Attach", [
            _with_icon(_icon(_FILE_ICONS.get(n.rpartition(".")[2].lower(), "Document"),
                             color="Accent", filled=False), _text(n), spacing="Small")
            for n in mail.attachment_names[:15]
        ]))
    body += _tabs(tabs)

    body += _footer(ai=_ai_helped(mail))
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": body,
    }
    links = []
    if task.list_item_url:
        links.append({"type": "Action.OpenUrl", "title": f"Open in {_list_name(task)}",
                      "url": task.list_item_url, "iconUrl": "icon:Open"})
    if staged and earlier.get("url"):
        links.append({"type": "Action.OpenUrl", "title": "Open earlier task",
                      "url": earlier["url"], "iconUrl": "icon:History"})
    if links:
        card["actions"] = links
    return card


def _standing_row(person: dict[str, Any]) -> dict[str, Any]:
    """One person in the team's standing: rank, name and labels, and what
    shapes their share — open work, capacity, the ceiling on open tasks."""
    first = person.get("rank") == 1
    labels = person.get("labels") or []
    capacity = person.get("capacity")
    limit = person.get("limit")
    share = (f"Capacity {capacity:g}" if capacity is not None else "Capacity —") + " · " + (
        f"Limit {limit} open" if limit is not None else "No limit"
    )
    tags: dict[str, Any] = (
        {
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                {"type": "Column", "width": "auto", "spacing": "Small", "items": [{
                    "type": "Badge", "text": label, "size": "Medium", "shape": "Rounded",
                    "style": "Informative", "appearance": "Tint",
                    "fallback": _caption(label, size="Small"),
                }]}
                for label in labels[:4]
            ],
        }
        if labels else _caption("No labels", size="Small", spacing="None")
    )
    return {
        "type": "Container", "spacing": "Small", "roundedCorners": True,
        "style": "emphasis" if first else "default",
        "items": [{
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [_text(f"#{person.get('rank')}", size="Medium", weight="Bolder",
                                 color="Good" if first else "Default")]},
                {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center",
                 "items": [_text(_v(person.get("name")), weight="Bolder"), tags]},
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [
                     _text(f"{person.get('active', 0)} active · {person.get('open', 0)} open",
                           size="Small", horizontalAlignment="Right"),
                     _caption(share, size="Small", spacing="None", horizontalAlignment="Right"),
                 ]},
            ],
        }],
    }


def _footer(*, ai: bool) -> list[dict[str, Any]]:
    """The note at the foot of every card: who sent it — the app, not a
    person — with the logo, and whether a model had a hand in it."""
    badge = (
        {"type": "Badge", "text": "AI-assisted", "icon": "Sparkle", "size": "Medium",
         "shape": "Rounded", "style": "Informative", "appearance": "Tint",
         "fallback": _caption("AI-assisted", size="Small")}
        if ai else
        {"type": "Badge", "text": "Automated", "icon": "Bot", "size": "Medium",
         "shape": "Rounded", "style": "Subtle", "appearance": "Tint",
         "fallback": _caption("Automated", size="Small")}
    )
    note = (
        "AI-generated message from **Hamdaz ERP**. Please check before acting on it."
        if ai else "Automated message from **Hamdaz ERP**."
    )
    return [{
        "type": "Container", "separator": True, "spacing": "Large",
        "items": [{
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [{"type": "Image", "url": get_settings().teams_card_logo_url,
                            "width": "32px", "altText": "Hamdaz"}]},
                {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center",
                 "spacing": "Small",
                 "items": [_caption(note, size="Small")]},
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
                 "items": [badge]},
            ],
        }],
    }]


def _in_real_list(task: MailWatchTestTask) -> bool:
    return bool(task.list_id) and task.list_id == get_settings().sharepoint_proposals_list_id


def _list_name(task: MailWatchTestTask) -> str:
    return "Proposals" if _in_real_list(task) else "Proposals Test"


def _ai_helped(mail: WatchedMail) -> bool:
    """Whether a model read the mail or picked its task."""
    return mail.decided_by == "model" or mail.match_method == "model"


def _testing_note(detail: str) -> list[dict[str, Any]]:
    """The banner at the top of every card while :data:`TESTING` is on."""
    if not TESTING:
        return []
    return [{
        "type": "Container", "style": "warning", "bleed": True,
        "items": [_with_icon(
            _icon("Beaker", color="Warning"),
            _text(f"**TESTING** — {detail}", size="Small"),
        )],
    }]


def test_html(row: MailWatchTeams) -> str:
    return (
        ("<p><b>TESTING</b></p>" if TESTING else "")
        + "<p><b>Hamdaz ERP — Teams connection test</b></p>"
        f"<p>Posted as {_e(row.account_name)} to {_e(row.team_name)} → {_e(row.channel_name)}. "
        "Negotiation updates from the CEO's mail will appear here.</p>"
    )


async def post_mail(
    session: AsyncSession, mail: WatchedMail, *, poster: TeamsPoster, settings: Settings
) -> str:
    """Post one mail's card now — a negotiation, or a new tender with its
    test task — and record it on the mail."""
    row = await get_connection(session)
    if not is_connected(row):
        raise TeamsError("No Teams account is connected.")
    test_task = None
    if mail.category == NEW_TENDER:
        test_task = await assign.test_task_of(session, mail)
        if test_task is None:
            raise TeamsError("This new tender has no task assigned yet.")
    elif mail.category != NEGOTIATION:
        raise TeamsError("Only negotiations and assigned new tenders are posted to Teams.")
    mail.teams_attempts = (mail.teams_attempts or 0) + 1
    try:
        if test_task is not None:
            files = (
                await poster.task_files(mail.task)
                if mail.task and test_task.action == "stage_change" else None
            )
            people = await poster.people([test_task.assigned_to_email or ""])
            card = new_tender_card(mail, test_task, files, people)
        else:
            files = await poster.task_files(mail.task) if mail.task else None
            people = await poster.people([(mail.task or {}).get("assignee_email") or ""])
            card = negotiation_card(mail, files, people)
        message_id = await poster.post(row, "", card=card)
    except TeamsError as exc:
        mail.teams_error = str(exc)[:500]
        row.last_error = str(exc)[:500]
        raise
    mail.teams_posted_at = datetime.now(UTC)
    mail.teams_message_id = message_id
    mail.teams_error = None
    return message_id


async def post_pending(session: AsyncSession, *, poster: TeamsPoster, settings: Settings) -> int:
    """Post what is due and not yet posted: negotiations received since their
    posting was switched on, and new tenders given a task since assigning was.
    Locked row by row, so two instances never post one twice."""
    row = await get_connection(session)
    if not poster.can_read(row):
        # Not connected, or connected on another server: posting is theirs.
        return 0
    due = []
    if row.post_negotiations and row.posting_from is not None:
        due.append(and_(
            WatchedMail.category == NEGOTIATION, WatchedMail.received_at >= row.posting_from
        ))
    if row.assign_new_tenders and row.assign_from is not None:
        due.append(and_(
            WatchedMail.category == NEW_TENDER,
            WatchedMail.received_at >= row.assign_from,
            WatchedMail.id.in_(select(MailWatchTestTask.mail_id)),
        ))
    if not due:
        return 0
    mails = (
        await session.scalars(
            select(WatchedMail)
            .where(
                or_(*due),
                WatchedMail.teams_posted_at.is_(None),
                WatchedMail.teams_attempts < MAX_ATTEMPTS,
            )
            .order_by(WatchedMail.received_at.asc())
            .limit(10)
            .with_for_update(skip_locked=True)
        )
    ).all()
    posted = 0
    for mail in mails:
        try:
            await post_mail(session, mail, poster=poster, settings=settings)
            posted += 1
        except TeamsError as exc:
            logger.warning("mail watch could not post to Teams: %s", exc)
            if "Connect again" in str(exc):
                break
    return posted
