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


class ZohoError(Exception):
    """Zoho refused or could not be reached. Safe to show a user."""


class ZohoRateLimitError(ZohoError):
    """Zoho's per-minute limit was hit. Worth retrying; a plain failure is not."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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

    # ── the wire ───────────────────────────────────────────────────────

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """One authenticated GET, with Zoho's failure modes already sorted out.

        Returns the raw response because not everything Zoho serves is JSON —
        document downloads come back as bytes through this same path.
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

        if response.status_code == 404:
            raise ZohoError("No such record in Zoho Books")

        if response.status_code != 200:
            raise ZohoError(
                f"Zoho Books returned {response.status_code}: {response.text[:300]}"
            )

        return response

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = await self._request(path, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZohoError("Zoho Books returned a non-JSON body") from exc

        # A non-zero code is a failure wearing a 200.
        if (code := payload.get("code", 0)) != 0:
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
        payload = await self._get(f"/invoices/{invoice_id}")
        return payload.get("invoice", {})
