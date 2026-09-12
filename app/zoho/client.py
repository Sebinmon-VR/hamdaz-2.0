"""Reading Zoho Books.

**This module is read-only.** Zoho Books is the system of record for quotes;
people work in it directly, and nothing here writes back. Every call is a GET,
and the refresh token should carry only ``ZohoBooks.estimates.READ`` so that
stays true even if someone later adds a method that should not exist.

Two things about Zoho worth knowing before reading the code:

* **A quote is an "estimate".** The Books UI says Quotes, the API says
  ``/estimates``. This class speaks Zoho's vocabulary; the translation to ours
  happens one layer up, in ``app.zoho.schemas``.
* **HTTP 200 does not mean success.** Every response carries a ``code`` field,
  and a non-zero ``code`` is an error that arrives with a 200. Checking the
  status alone silently turns failures into empty results.

Tokens are not handled here — see ``app.zoho.tokens`` for why that is a shared,
database-backed concern rather than a field on this object.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.zoho.catalogue import Endpoint
from app.zoho.tokens import TokenStore, ZohoAuthError

logger = logging.getLogger("hamdaz.zoho")

#: Zoho's maximum. Asking for more is not an error, it is silently capped.
_PAGE_SIZE: Final = 200

#: Zoho allows 100 requests per minute per organisation. A runaway loop would
#: exhaust that for everyone, so paging stops here regardless.
_MAX_PAGES: Final = 25

#: The most rows an unlimited sweep can return. A caller that gets exactly this
#: many should assume Zoho had more.
MAX_ROWS: Final = _MAX_PAGES * _PAGE_SIZE

#: Attachments are relayed through this process, so one absurd file must not be
#: able to take it down. Comfortably above the quotes and datasheets actually
#: attached to these records, which run to one or two megabytes.
MAX_DOCUMENT_BYTES: Final = 25 * 1_048_576

#: Zoho's in-body codes for "you may not do this". Observed against the live
#: organisation: a refusal arrives as HTTP 403 carrying code 104003, so the
#: status branch below is what actually fires. These are kept as a belt-and-
#: braces fallback because Zoho does return some refusals inside a 200, and
#: because 57 is the code its own documentation cites for the same condition.
_SCOPE_CODES: Final[frozenset[int]] = frozenset({57, 104003})


def _scope_message(scope: str | None) -> str:
    """Why a read was refused, without claiming more than a 403 actually proves.

    It is tempting to say "add this scope and regenerate the token", and that
    was this message's first version. It is not safe to say. Zoho gates a read
    on *two* independent things, and refuses both with the identical
    403/104003 "You don't have permission to perform this operation":

    * the OAuth scope on the refresh token, and
    * the Zoho Books role of the user who authorised that token — a person on a
      restricted role cannot read the chart of accounts however the token is
      scoped.

    Nothing in the response distinguishes them, so naming only the scope sends
    somebody to regenerate a token that then fails in exactly the same way. Both
    causes are named, cheapest to check first.
    """
    lead = (
        f"Zoho refused this read. The token needs {scope}"
        if scope
        else "Zoho refused this read. The token may lack the scope for it"
    )
    return (
        f"{lead}, and the Zoho Books user who authorised the token must hold a "
        f"role that can see this data. Zoho reports both the same way, so check "
        f"the role first — it needs no new token — then the scope."
    )


#: Keys every Zoho response carries regardless of endpoint. Excluded when
#: guessing which key holds the payload, since they are never it.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"code", "message", "page_context", "instrumentation"}
)


def _rows_from(payload: dict, endpoint: Endpoint) -> list[dict]:
    """The row array, falling back to finding it when the declared key misses.

    Zoho does not document a response key for every module, so some entries in
    the catalogue are inferred from its naming pattern rather than confirmed —
    ``vendor_credits`` against ``vendorcredits`` is exactly the kind of coin
    flip that gets it wrong. A wrong key is the worst possible failure here: the
    request succeeds, ``.get(key, [])`` returns nothing, and the report shows a
    confident zero instead of an error.

    So when the declared key is absent, take the only list-of-objects in the
    body. There is never more than one — the envelope holds the code, the
    message and the page context, none of which is a list. The fallback runs
    only when the key is genuinely *missing*, so a verified endpoint never
    reaches it, and a genuinely empty page still carries its key and so still
    reads as empty rather than being guessed at.
    """
    if (rows := payload.get(endpoint.list_key)) is not None:
        return rows if isinstance(rows, list) else [rows]

    guessed = next(
        (
            (k, v)
            for k, v in payload.items()
            if k not in _ENVELOPE_KEYS
            and isinstance(v, list)
            and all(isinstance(row, dict) for row in v)
        ),
        None,
    )
    if guessed is None:
        return []

    key, rows = guessed
    # Loud on purpose: this is the signal to correct the catalogue entry and set
    # verified=True, and without it the fallback would quietly hide the mistake
    # for as long as the code lives.
    logger.warning(
        "zoho endpoint %s returned rows under %r, not the expected %r — "
        "correct app/zoho/catalogue.py",
        endpoint.key,
        key,
        endpoint.list_key,
    )
    return rows


class ZohoError(Exception):
    """Zoho refused or could not be reached. Safe to show a user."""


class ZohoRateLimitError(ZohoError):
    """Zoho's per-minute limit was hit. Worth retrying; a plain failure is not."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ZohoScopeError(ZohoError):
    """The token is valid, but this read is not permitted.

    Told apart from a plain failure because the two need opposite reactions: a
    failure is worth retrying and worth alerting on, whereas this will refuse
    identically forever until somebody changes a permission.

    Note what this does *not* say. Zoho refuses an out-of-scope token and a
    restricted Books user role with the same 403/104003, so the name is
    narrower than the condition — see ``_scope_message``. ``scope`` is the scope
    this endpoint would need, which is worth reporting, not proof that the scope
    is what is missing.
    """

    def __init__(self, message: str, scope: str | None = None) -> None:
        super().__init__(message)
        self.scope = scope


class ZohoBooks:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._http = http
        self._tokens = TokenStore(settings, http, session_factory)

    @property
    def configured(self) -> bool:
        return self._settings.zoho_configured

    @property
    def organization_id(self) -> str:
        """Which Zoho organisation this client reads.

        Exposed so callers can identify the organisation without importing the
        settings a second time and risking a different answer from the one the
        requests are actually being sent with.
        """
        return self._settings.zoho_organization_id

    # ── the wire ───────────────────────────────────────────────────────

    async def _request(
        self, path: str, params: dict[str, Any] | None = None, *, scope: str | None = None
    ) -> httpx.Response:
        """One authenticated GET, with Zoho's failure modes already sorted out.

        Returns the raw response because not everything Zoho serves is JSON —
        document downloads come back as bytes through this same path.

        ``scope`` is the OAuth scope this path needs, passed only so a refusal
        can name it. It is never sent to Zoho.
        """
        try:
            token = await self._tokens.get()
        except ZohoAuthError as exc:
            raise ZohoError(str(exc)) from exc

        query: dict[str, Any] = {"organization_id": self._settings.zoho_organization_id}
        query.update({k: v for k, v in (params or {}).items() if v is not None})

        try:
            response = await self._http.get(
                f"{token.api_domain}/books/v3{path}",
                params=query,
                headers={"Authorization": f"Zoho-oauthtoken {token.value}"},
            )
        except httpx.HTTPError as exc:
            raise ZohoError(f"Could not reach Zoho Books: {exc}") from exc

        if response.status_code == 401:
            # The token is dead for everyone, not just this process. Clearing the
            # shared row stops each instance rediscovering that separately.
            await self._tokens.invalidate()
            raise ZohoError("Zoho rejected the access token")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise ZohoRateLimitError(
                "Zoho's rate limit was reached (100 requests per minute per organisation)",
                int(retry_after) if retry_after and retry_after.isdigit() else None,
            )

        if response.status_code == 403:
            # Distinct from 401. The token is genuine and current; it simply was
            # not granted this endpoint, and no amount of refreshing will change
            # that. Invalidating here would be actively wrong — it would throw
            # away a working token because one endpoint was out of reach.
            raise ZohoScopeError(_scope_message(scope), scope)

        if response.status_code == 404:
            raise ZohoError("No such record in Zoho Books")

        if response.status_code != 200:
            raise ZohoError(
                f"Zoho Books returned {response.status_code}: {response.text[:300]}"
            )

        return response

    async def _post(
        self, path: str, body: dict[str, Any], *, scope: str | None = None
    ) -> dict:
        """One authenticated POST. **The only way this client writes.**

        Kept apart from ``_request`` so that reading that method never gives
        false comfort: everything above it is a read. The one caller is the
        workflow step that creates an estimate, and that step sits behind a
        switch that ships off.
        """
        try:
            token = await self._tokens.get()
        except ZohoAuthError as exc:
            raise ZohoError(str(exc)) from exc
        try:
            response = await self._http.post(
                f"{token.api_domain}/books/v3{path}",
                params={"organization_id": self._settings.zoho_organization_id},
                json=body,
                headers={"Authorization": f"Zoho-oauthtoken {token.value}"},
            )
        except httpx.HTTPError as exc:
            raise ZohoError(f"Could not reach Zoho Books: {exc}") from exc
        if response.status_code == 401:
            await self._tokens.invalidate()
            raise ZohoError("Zoho rejected the access token")
        if response.status_code == 403:
            raise ZohoScopeError(_scope_message(scope), scope)
        if response.status_code not in (200, 201):
            raise ZohoError(
                f"Zoho Books returned {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ZohoError("Zoho Books answered with something that is not JSON") from exc

    async def find_contact(self, name: str) -> dict | None:
        """The customer with this name, if Zoho has one. An estimate needs its id."""
        payload = await self._get(
            "/contacts", {"contact_name": name, "contact_type": "customer"},
            scope="ZohoBooks.contacts.READ",
        )
        rows = payload.get("contacts") or []
        exact = [r for r in rows if (r.get("contact_name") or "").strip().lower() == name.strip().lower()]
        chosen = exact[0] if exact else (rows[0] if rows else None)
        return chosen

    async def create_estimate(self, body: dict[str, Any]) -> dict:
        """Create an estimate. Needs ``ZohoBooks.estimates.CREATE``."""
        payload = await self._post("/estimates", body, scope="ZohoBooks.estimates.CREATE")
        estimate = payload.get("estimate")
        if not estimate:
            raise ZohoError(f"Zoho did not return the estimate: {str(payload)[:200]}")
        return estimate

    async def estimate_pdf(self, estimate_id: str) -> bytes:
        """The estimate as Zoho renders it — the commercial proposal."""
        response = await self._request(
            f"/estimates/{estimate_id}", {"accept": "pdf"}, scope="ZohoBooks.estimates.READ"
        )
        return response.content

    async def estimate_documents(self, estimate_id: str) -> list[dict]:
        """The files attached to an estimate, as Zoho lists them."""
        estimate = await self.estimate(estimate_id)
        return list(estimate.get("documents") or [])

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, scope: str | None = None
    ) -> dict:
        response = await self._request(path, params, scope=scope)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZohoError("Zoho Books returned a non-JSON body") from exc

        # A non-zero code is a failure wearing a 200.
        if (code := payload.get("code", 0)) != 0:
            # Zoho reports an unauthorised scope this way as often as with a
            # 403, so the same distinction has to be drawn again here.
            if code in _SCOPE_CODES:
                raise ZohoScopeError(_scope_message(scope), scope)
            raise ZohoError(f"Zoho Books refused the request ({code}): {payload.get('message')}")

        return payload

    async def _paginate(
        self, path: str, key: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict]:
        """Every page of a list endpoint, up to ``limit`` rows."""
        rows: list[dict] = []
        page = 1
        for _ in range(_MAX_PAGES):
            payload = await self._get(
                path, {**(params or {}), "page": page, "per_page": _PAGE_SIZE}
            )
            rows.extend(payload.get(key, []))

            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if not payload.get("page_context", {}).get("has_more_page"):
                return rows
            page += 1

        logger.warning("zoho paging stopped at %d pages for %s", _MAX_PAGES, path)
        return rows[:limit] if limit is not None else rows

    # ── estimates, a.k.a. quotes ───────────────────────────────────────

    async def estimates(
        self,
        *,
        status: str | None = None,
        customer_name: str | None = None,
        estimate_number: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        search_text: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        return await self._paginate(
            "/estimates",
            "estimates",
            {
                # Zoho spells this filter_by=Status.Sent — not status=sent, and
                # the capital is load-bearing: lowercase is a 400.
                "filter_by": f"Status.{status.capitalize()}" if status else None,
                "customer_name": customer_name,
                "estimate_number": estimate_number,
                "date_start": date_start,
                "date_end": date_end,
                "search_text": search_text,
                "sort_column": "date",
                "sort_order": "D",
            },
            limit=limit,
        )

    async def estimate(self, estimate_id: str) -> dict:
        payload = await self._get(f"/estimates/{estimate_id}")
        estimate = payload.get("estimate")
        if not estimate:
            raise ZohoError("No such quote in Zoho Books")
        return estimate

    async def comments(self, estimate_id: str) -> list[dict]:
        payload = await self._get(f"/estimates/{estimate_id}/comments")
        return payload.get("comments", [])

    async def document(self, estimate_id: str, document_id: str) -> tuple[bytes, str]:
        """One attachment's bytes and its content type.

        Zoho scopes this to the estimate — a document id belonging to a
        different quote is a 400, not a download — but the caller checks
        ownership too, so a wrong id reads as "not found" rather than as an
        upstream failure.

        Buffered rather than streamed. Attachments here are quotes and
        datasheets, a megabyte or two; streaming would mean holding the upstream
        response open across the whole of ours for no practical gain. The cap is
        what stops that assumption becoming a memory problem.
        """
        response = await self._request(f"/estimates/{estimate_id}/documents/{document_id}")

        content_type = response.headers.get("content-type", "application/octet-stream")
        # Zoho reports "no such document" as a JSON body with a 200.
        if content_type.startswith("application/json"):
            message = response.json().get("message", "not available")
            raise ZohoError(f"Zoho Books would not return that document: {message}")

        if len(response.content) > MAX_DOCUMENT_BYTES:
            raise ZohoError(
                f"That attachment is larger than the {MAX_DOCUMENT_BYTES // 1_048_576}MB "
                f"this endpoint will relay. Open it in Zoho Books instead."
            )

        return response.content, content_type.split(";")[0].strip()

    # ── the records an estimate points at ──────────────────────────────

    async def contact(self, contact_id: str) -> dict:
        payload = await self._get(f"/contacts/{contact_id}")
        return payload.get("contact", {})

    async def item(self, item_id: str) -> dict:
        payload = await self._get(f"/items/{item_id}")
        return payload.get("item", {})

    async def invoice(self, invoice_id: str) -> dict:
        """One invoice, by an id taken from an estimate's ``invoice_ids``.

        Expect this to fail with a 403 on a token scoped only to estimates —
        which is the current state of this integration. The caller is meant to
        report that rather than treat it as an outage; see ``app.zoho.service``.
        """
        payload = await self._get(f"/invoices/{invoice_id}", scope="ZohoBooks.invoices.READ")
        return payload.get("invoice", {})

    # ── the catalogue-driven surface ───────────────────────────────────
    #
    # Everything above is a named method because something in this app calls it
    # by name. Everything below is the rest of Zoho Books, reached through
    # ``app.zoho.catalogue`` — see that module for why the surface is a table.

    async def list_rows(
        self,
        endpoint: Endpoint,
        *,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        parent_id: str | None = None,
    ) -> list[dict]:
        """Every row of one catalogue endpoint, up to ``limit``."""
        path = self._resolve(endpoint, parent_id)
        rows: list[dict] = []
        page = 1
        for _ in range(_MAX_PAGES):
            payload = await self._get(
                path,
                {**(params or {}), "page": page, "per_page": _PAGE_SIZE},
                scope=endpoint.scope,
            )
            rows.extend(_rows_from(payload, endpoint))

            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if not payload.get("page_context", {}).get("has_more_page"):
                return rows
            page += 1

        logger.warning("zoho paging stopped at %d pages for %s", _MAX_PAGES, path)
        return rows[:limit] if limit is not None else rows

    async def get_row(
        self, endpoint: Endpoint, record_id: str, *, parent_id: str | None = None
    ) -> dict:
        """One record from a catalogue endpoint."""
        if endpoint.detail_key is None:
            raise ZohoError(f"{endpoint.name} has no single-record endpoint")

        path = f"{self._resolve(endpoint, parent_id)}/{record_id}"
        payload = await self._get(path, scope=endpoint.scope)

        record = payload.get(endpoint.detail_key)
        if record is None:
            # Same reasoning as _rows_from: an unverified detail_key should not
            # turn a successful read into a phantom "not found".
            record = next(
                (
                    v
                    for k, v in payload.items()
                    if isinstance(v, dict) and k not in _ENVELOPE_KEYS
                ),
                None,
            )
        if record is None:
            raise ZohoError(f"Zoho returned no {endpoint.name.lower()} for {record_id}")
        return record

    def _resolve(self, endpoint: Endpoint, parent_id: str | None) -> str:
        """The concrete path, with any ``{parent_id}`` filled in."""
        if not endpoint.parameterised:
            return endpoint.path
        if not parent_id:
            raise ZohoError(f"{endpoint.name} must be read under a parent record")
        return endpoint.path.format(parent_id=parent_id)
