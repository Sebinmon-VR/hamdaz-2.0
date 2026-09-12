"""Reading a mailbox through Graph, and being told when it changes.

Two ways in, on purpose:

* **delta** — ask what has changed since last time, on a timer. Simple, needs
  no public address, and is the only thing that works on a laptop. It is also
  the safety net: Microsoft's own guidance is not to depend on change
  notifications alone, because they are dropped.
* **change notifications** — Graph posts to us when mail arrives, so the
  pipeline reacts in seconds rather than at the next poll. Needs a public HTTPS
  endpoint, survives about three days, and has to be renewed.

Both end up calling the same processor with a message id, and the id is unique
on the intake row, so a notification and a poll racing each other is a no-op
rather than two tasks.

This module reads. It never marks anything read, moves it, or replies — the
mailbox belongs to a person and an intake that quietly tidied their inbox would
be a bug with an apology attached.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.core.mail import GRAPH_BASE, GraphMailer, MailError

logger = logging.getLogger("hamdaz.intake.mail")

#: What the pipeline actually reads. Asking for the whole message brings the
#: full HTML body and every header, which is megabytes over a busy inbox and
#: none of it is used.
MESSAGE_FIELDS = (
    "id,conversationId,internetMessageId,subject,bodyPreview,receivedDateTime,"
    "from,sender,hasAttachments,webLink,isDraft"
)

#: The body is trimmed before it reaches a model. A forwarded tender with forty
#: quoted replies underneath is mostly somebody else's signature block, and the
#: part that says what this email is about is at the top.
BODY_LIMIT = 6000

#: Graph refuses a mail subscription longer than about three days.
SUBSCRIPTION_MINUTES = 4230

#: How many messages a delta page asks for. Graph's own default is around ten,
#: which turns any real mailbox into dozens of round trips.
PAGE_SIZE = 50

#: Pages per poll. Fifty times this is far more than a minute of mail, so in
#: steady state the budget is never reached; it exists so that a first pass
#: over a long inbox is spread across polls instead of blocking one.
MAX_PAGES = 20


def _address(part: dict[str, Any] | None) -> tuple[str | None, str | None]:
    holder = ((part or {}).get("emailAddress")) or {}
    email = (holder.get("address") or "").strip().lower() or None
    return email, (holder.get("name") or "").strip() or None


class MailReader(GraphMailer):
    """Reads a mailbox. Inherits the token cache rather than keeping a second.

    Subclassing the mailer looks odd until you notice they need exactly the
    same thing — an application token for Graph, refreshed before it expires.
    A second copy of that is a second thing to get wrong.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        super().__init__(settings, http)

    async def _graph(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._http.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                # Asks Graph to keep serving the page even if some property is
                # unavailable, rather than failing the whole call.
                "Prefer": 'outlook.body-content-type="text"',
            },
        )
        if response.status_code == 403:
            raise MailError(
                "Graph refused to read the mailbox. The app registration needs "
                "the Mail.Read application permission, granted by an admin."
            )
        if response.status_code == 404:
            raise MailError("No such mailbox. Check the address in the intake settings.")
        if response.status_code >= 400:
            raise MailError(f"Graph returned {response.status_code} reading mail")
        return response.json()

    async def delta(
        self, mailbox: str, *, delta_link: str | None, since: datetime | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Messages that have arrived or changed, and the cursor for next time.

        With no cursor this starts from ``since`` rather than from the
        beginning of the mailbox. A newly configured intake must not wake up
        and process a year of history — which, with task creation on, would
        mean a year of tasks.
        """
        if delta_link:
            url: str | None = delta_link
            params: dict[str, str] | None = None
        else:
            url = f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages/delta"
            # ``$top`` matters more than it looks. Delta pages default to about
            # ten items, so without it a mailbox of any size is dozens of round
            # trips to get through — which is how the page budget below gets
            # exhausted before Graph ever hands over a delta cursor.
            params = {"$select": MESSAGE_FIELDS, "$top": str(PAGE_SIZE)}
            if since is not None:
                params["$filter"] = (
                    f"receivedDateTime ge {since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
                )

        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        # Bounded rather than "until done": a first pass over a large mailbox
        # would otherwise hold the loop for minutes.
        for _ in range(MAX_PAGES):
            payload = await self._graph(url, params)
            messages.extend(payload.get("value", []))
            params = None
            if next_link := payload.get("@odata.nextLink"):
                url = next_link
                # Kept as the cursor. If the budget runs out before Graph
                # offers a delta link, this is what lets the next poll carry
                # on from here — returning None instead would start the whole
                # mailbox again every time and never reach the end of it.
                cursor = next_link
                continue
            cursor = payload.get("@odata.deltaLink") or cursor
            break
        return messages, cursor

    async def message(self, mailbox: str, message_id: str) -> dict[str, Any]:
        """One message, with its body. Used when a notification names an id."""
        return await self._graph(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}",
            {"$select": f"{MESSAGE_FIELDS},body"},
        )

    async def attachments(
        self, mailbox: str, message_id: str
    ) -> list[tuple[str, str | None, bytes]]:
        """The files on one message: ``(name, content type, bytes)`` each.

        Only file attachments — an item or a reference attachment is a link
        to something elsewhere, and a supplier's quote is a file. Read for the
        workflows that wait on a reply, so what the supplier sent lands on the
        run rather than staying in a mailbox somebody has to go and look in.
        """
        import base64

        payload = await self._graph(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/attachments",
            {"$select": "id,name,contentType,size,isInline,@odata.type"},
        )
        out: list[tuple[str, str | None, bytes]] = []
        for entry in payload.get("value", []):
            if entry.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            if entry.get("isInline"):
                continue
            # The listing omits the bytes; each file is one more call. Fine
            # for a quote or three, which is what a reply carries.
            full = await self._graph(
                f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}"
                f"/attachments/{entry['id']}"
            )
            raw = full.get("contentBytes")
            if not raw:
                continue
            out.append(
                (
                    str(entry.get("name") or "attachment"),
                    entry.get("contentType"),
                    base64.b64decode(raw),
                )
            )
        return out

    async def body_of(self, mailbox: str, message_id: str) -> str:
        """The plain-text body, trimmed to what a model needs to read."""
        payload = await self._graph(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}", {"$select": "body"}
        )
        content = ((payload.get("body") or {}).get("content") or "").strip()
        return content[:BODY_LIMIT]

    # ── change notifications ───────────────────────────────────────────

    async def subscribe(
        self, mailbox: str, *, notification_url: str, secret: str
    ) -> dict[str, Any]:
        """Ask Graph to tell us when mail arrives.

        Graph validates the endpoint synchronously before the subscription is
        created: it posts a token and expects it echoed back within seconds. So
        this call fails outright if the URL is not reachable from the internet,
        which is the honest outcome — a subscription that silently never fires
        is worse than one that refuses to be created.
        """
        token = await self._access_token()
        expires = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        response = await self._http.post(
            f"{GRAPH_BASE}/subscriptions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "changeType": "created",
                "notificationUrl": notification_url,
                "resource": f"/users/{mailbox}/mailFolders('inbox')/messages",
                "expirationDateTime": f"{expires:%Y-%m-%dT%H:%M:%S.0000000Z}",
                # Comes back on every notification and is checked. A webhook
                # that acts on whatever posts to it is an open door.
                "clientState": secret,
            },
        )
        if response.status_code >= 400:
            raise MailError(
                f"Graph refused the subscription ({response.status_code}): "
                f"{response.text[:300]}"
            )
        return response.json()

    async def renew(self, subscription_id: str) -> dict[str, Any]:
        """Push the expiry out. Mail subscriptions last about three days."""
        token = await self._access_token()
        expires = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        response = await self._http.patch(
            f"{GRAPH_BASE}/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"expirationDateTime": f"{expires:%Y-%m-%dT%H:%M:%S.0000000Z}"},
        )
        if response.status_code >= 400:
            raise MailError(f"Could not renew the subscription ({response.status_code})")
        return response.json()

    async def unsubscribe(self, subscription_id: str) -> None:
        token = await self._access_token()
        await self._http.delete(
            f"{GRAPH_BASE}/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"},
        )


def summarise_message(raw: dict[str, Any]) -> dict[str, Any]:
    """One Graph message, reduced to what the intake row holds."""
    email, name = _address(raw.get("from") or raw.get("sender"))
    received = raw.get("receivedDateTime")
    when: datetime | None = None
    if received:
        try:
            when = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
        except ValueError:
            when = None
    body = ((raw.get("body") or {}).get("content") or raw.get("bodyPreview") or "").strip()
    return {
        "graph_message_id": raw.get("id") or "",
        "conversation_id": raw.get("conversationId"),
        "internet_message_id": raw.get("internetMessageId"),
        "received_at": when,
        "sender_email": email,
        "sender_name": name,
        "subject": (raw.get("subject") or "").strip() or None,
        "body": body[:BODY_LIMIT] or None,
        "has_attachments": bool(raw.get("hasAttachments")),
        "web_link": raw.get("webLink"),
    }


def sender_allowed(
    email: str | None, *, addresses: list[str], domains: list[str]
) -> bool:
    """Whether mail from this address is looked at.

    An empty configuration admits **nobody**. That is the opposite of the usual
    convention and it is deliberate: the alternative reading — that a blank box
    means everyone — turns an unconfigured intake into one that reads every
    message in somebody's inbox and creates tasks from it.
    """
    if not email:
        return False
    address = email.strip().lower()
    if address in {a.strip().lower() for a in addresses if a.strip()}:
        return True

    domain = address.rpartition("@")[2]
    for raw in domains:
        wanted = raw.strip().lower().lstrip("@")
        if not wanted:
            continue
        # The domain itself, or anything beneath it — ``adnoc.ae`` admits
        # ``mail.adnoc.ae``. Anchored on the dot rather than a plain suffix
        # test, because ``notadnoc.ae`` ends with ``adnoc.ae`` and is somebody
        # else entirely.
        if domain == wanted or domain.endswith(f".{wanted}"):
            return True
    return False
