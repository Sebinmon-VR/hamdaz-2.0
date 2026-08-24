"""Constraint C2 enforcement — live SharePoint sites are read-only.

See ``docs/PROJECT_PLAN.md`` §8.1. The legacy system has thirteen SharePoint and OneDrive
write paths, several on hot code paths: ``update_user_analytics_in_sharepoint`` fires every
sixty seconds from every gunicorn worker. SharePoint is live, so none of that may happen
here.

Three independent guards protect the live tenant, and this module is guard #2 — the runtime
one. The others are structural (:mod:`app.connectors.sharepoint.client` simply has no write
methods) and CI (a grep for write verbs outside the sandbox module).

The check compares the resolved **site ID**, not the URL string. Site paths are
case-insensitive, accept several equivalent spellings, and are easy to typo; site IDs are
opaque and exact. Comparing strings would be a guard that looks right and isn't.
"""

from __future__ import annotations

from typing import Final

READ_ONLY_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})


class SharePointWriteForbidden(RuntimeError):  # noqa: N818 - name is referenced in docs/PROJECT_PLAN.md §8.1
    """Raised before a socket opens when a write targets anything but the sandbox.

    This is deliberately not an ``HTTPException``. It is not a user-facing condition to be
    rendered as a 4xx — it means our own code tried to do something the project forbids, and
    it should surface as a 500 with a loud trace.
    """

    def __init__(self, method: str, site_id: str, reason: str) -> None:
        self.method = method
        self.site_id = site_id
        self.reason = reason
        super().__init__(
            f"Refusing {method} against SharePoint site {site_id!r}: {reason}. "
            "Live SharePoint is read-only (constraint C2, docs/PROJECT_PLAN.md §8.1)."
        )


class SharePointWriteGuard:
    """Decides whether a given request may proceed.

    Constructed once per connector. ``sandbox_site_id`` is resolved from the sandbox site
    path at startup via a *read* call, then held for the process lifetime.
    """

    __slots__ = ("_sandbox_site_id", "_writes_enabled")

    def __init__(self, *, sandbox_site_id: str | None, writes_enabled: bool) -> None:
        # An empty or whitespace-only ID would compare equal to a caller passing "" and turn
        # the allowlist into a wildcard. Normalise it out of existence instead.
        normalised = (sandbox_site_id or "").strip()
        self._sandbox_site_id: str | None = normalised or None
        self._writes_enabled = writes_enabled

    @property
    def sandbox_site_id(self) -> str | None:
        return self._sandbox_site_id

    def is_write(self, method: str) -> bool:
        return method.strip().upper() not in READ_ONLY_METHODS

    def check(self, method: str, site_id: str) -> None:
        """Raise :class:`SharePointWriteForbidden` unless the request is permitted.

        Reads are always allowed. Writes are allowed only when writes are enabled *and* the
        target is exactly the sandbox site.
        """
        verb = method.strip().upper()
        if not self.is_write(verb):
            return

        target = (site_id or "").strip()
        if not target:
            raise SharePointWriteForbidden(verb, site_id, "no site ID was supplied")

        if not self._writes_enabled:
            raise SharePointWriteForbidden(
                verb, target, "sandbox writes are disabled (sharepoint_sandbox_writes_enabled)"
            )

        if self._sandbox_site_id is None:
            raise SharePointWriteForbidden(
                verb, target, "no sandbox site ID has been resolved, so nothing is writable"
            )

        if target != self._sandbox_site_id:
            raise SharePointWriteForbidden(
                verb,
                target,
                f"only the sandbox site ({self._sandbox_site_id!r}) is writable",
            )

    def describe(self) -> dict[str, object]:
        """Status payload for ``connector_status`` and the developer panel."""
        return {
            "mode": "read_write_sandbox" if self._writes_enabled else "read_only",
            "sandbox_site_id": self._sandbox_site_id,
            "writes_enabled": self._writes_enabled,
            "read_only_methods": sorted(READ_ONLY_METHODS),
        }
