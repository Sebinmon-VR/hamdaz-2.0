"""IT Support's connection to the ERP: its own API, called as the person chatting.

The user's ask (2026-11-13): "we need all the advanced features and every
feature in the ERP; simply a connection is needed", "role based access for
each module". So the agent does not get a hand-made copy of each feature: it
calls **this app's own API**, in process, through the same routes, checks and
services the web app uses, with a short-lived session made for **the person
in the chat**. New ERP features are reachable the day they exist.

**Role-based, per module.** Two checks, both must pass:

1. **The person's own access** (``Access``): the modules the ERP's access
   screen gives them (``app.access.service.effective_access``: their teams'
   grants and their global roles; a super admin has all). A route whose
   module they can't open in the app isn't shown to the model or called.
   The route's own role checks then still decide every call, as for a click.
2. **What the agent may do in that module** (``MODES``, changed with
   IT_SUPPORT_MODULE_MODES): ``off`` (not from the chat at all), ``read`` or
   ``write``. Finance and payroll are off and hidden whatever the setting
   (the user, 2026-11-13: "no finance or accounts module needed").

**Changes are never made when the model asks.** A POST/PUT/PATCH is held as a
plan, the person is asked in one line, and only their "yes" runs it
(``brain``). Some are not made from a chat at all, whatever the mode:

* deletes, of anything;
* anything that sends mail or hands work on: supplier emails, sending a quote
  for approval, reviews, negotiation (the user's standing rule: supplier mail
  only after review in the app, never from Teams).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx
from fastapi import FastAPI

from app.auth.deps import SESSION_AUDIENCE
from app.core.config import Settings
from app.core.security import sign

logger = logging.getLogger("hamdaz.itsupport")

API: Final = "/api/v1"
#: Never shown, never called: signing in, the public site's own forms,
#: finance and payroll (not even read), and the agent's own settings.
HIDDEN: Final = ("/api/v1/auth", "/apply", "/careers", "/website", "/health", "/sandbox",
                 "/api/v1/finance", "/api/v1/payroll", "/api/v1/it-support")

#: Each API area's ERP module (``app.access.catalogue``). ``PERSONAL``: the
#: person's own things, open to everyone it answers. ``ADMIN``: areas no
#: module covers, for super admins only.
PERSONAL: Final = "personal"
ADMIN: Final = "admin"
AREA_MODULE: Final[dict[str, str]] = {
    "proposals": "proposals", "task-calendar": "proposals", "labels": "proposals",
    "followups": "proposals", "reminders": "proposals", "bcd-checks": "proposals",
    "intake": "proposals",
    "quote-requests": "quote_requests",
    "quotes": "quotes",
    "comparisons": "quote_comparison",
    "enquiries": "enquiry_analysis",
    "website-enquiries": "website_enquiries",
    "assignment": "assignment",
    "directory": "directory", "users": "directory",
    "teams": "teams",
    "leave": "leave",
    "projects": "projects",
    "reports": "reports", "analytics": "reports",
    "hr": "hr",
    "templates": "templates",
    "roles": "roles", "access": "roles",
    "notifications": PERSONAL, "dashboards": PERSONAL, "widgets": PERSONAL,
    "modules": PERSONAL,
    "ariba": ADMIN, "mailwatch": ADMIN, "meetings": ADMIN, "admin": ADMIN,
}
#: What the agent may do in each module, before IT_SUPPORT_MODULE_MODES.
#: ``write`` only where a change is the presales work itself (a costing, its
#: supplier quotes, an enquiry analysis). The Proposals list stays read: its
#: writes go to SharePoint (the user's rule: never write to SharePoint).
MODES: Final[dict[str, str]] = {
    "quote_requests": "write", "quotes": "write", "quote_comparison": "write",
    "enquiry_analysis": "write", PERSONAL: "write",
    "proposals": "read", "dashboard": "read", "directory": "read", "teams": "read",
    "leave": "read", "projects": "read", "reports": "read", "assignment": "read",
    "website_enquiries": "read", "templates": "read", "hr": "read", "roles": "read",
    "user_admin": "read", ADMIN: "read",
    "finance": "off", "payroll": "off",
}
#: Path words that mean mail goes to somebody, or a hand-off the app keeps.
NEVER_FROM_CHAT: Final = ("supplier-emails", "/submit", "/reviews", "/negotiate", "/send",
                          "/mail", "/approve")
#: What a reply may bring back to the model, in characters.
MAX_RESULT: Final = 14_000


def modes(settings: Settings) -> dict[str, str]:
    """``MODES`` with IT_SUPPORT_MODULE_MODES ("hr=off,leave=write") on top.
    Finance and payroll stay off."""
    out = dict(MODES)
    for pair in settings.it_support_module_modes.split(","):
        module, _, mode = pair.partition("=")
        module, mode = module.strip(), mode.strip().lower()
        if module and mode in ("off", "read", "write"):
            out[module] = mode
    out["finance"] = out["payroll"] = "off"
    return out


@dataclass(frozen=True, slots=True)
class Access:
    """What one person's chat may reach: their modules in the ERP (as its
    access screen gives them) and whether they are a super admin."""

    modules: frozenset[str]
    is_admin: bool = False

    def key(self) -> str:
        return ("admin:" if self.is_admin else "") + ",".join(sorted(self.modules))


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return self.status < 400

    def for_model(self) -> str:
        text = self.body if isinstance(self.body, str) else json.dumps(
            self.body, default=str, ensure_ascii=False, separators=(",", ":")
        )
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + f"… [cut: {len(text) - MAX_RESULT} more characters; ask " \
                                      "for less, e.g. a filter or one record]"
        return f"HTTP {self.status}\n{text}"


def area_of(path: str) -> str:
    return path.removeprefix(API + "/").split("/", 1)[0].split("?", 1)[0]


def module_of(path: str) -> str | None:
    return AREA_MODULE.get(area_of(path))


def mode_for(path: str, access: Access, mode_of: dict[str, str]) -> str:
    """``off``, ``read`` or ``write``: what the chat may do on this path for
    this person."""
    if not path.startswith(API) or path.startswith(HIDDEN):
        return "off"
    module = module_of(path)
    if module is None:
        return "off"
    if module == ADMIN:
        reachable = access.is_admin
    elif module == PERSONAL:
        reachable = True
    else:
        reachable = access.is_admin or module in access.modules
    return mode_of.get(module, "read") if reachable else "off"


def blocked(method: str, path: str, access: Access, mode_of: dict[str, str]) -> str | None:
    """Why this call may not be made from the chat; None when it may."""
    method = method.upper()
    mode = mode_for(path, access, mode_of)
    if mode == "off":
        return "that's not a module they have, or not one you handle from the chat"
    if method == "GET":
        return None
    if method == "DELETE":
        return "deleting is done in the app"
    if any(word in path for word in NEVER_FROM_CHAT):
        return "this sends mail or hands the work on, which is done in the app after a review"
    if mode != "write":
        return f"changes in {module_of(path)} are made in the app, not from the chat"
    return None


class ERP:
    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        self.mode_of = modes(settings)
        self._catalogues: dict[str, str] = {}
        self._spec: dict[str, Any] | None = None

    def reload_modes(self) -> None:
        """The modes read again, after the settings page changed them."""
        mode_of = modes(self._settings)
        if mode_of != self.mode_of:
            self.mode_of = mode_of
            self._catalogues.clear()

    def spec(self) -> dict[str, Any]:
        if self._spec is None:
            self._spec = self._app.openapi()
        return self._spec

    def blocked(self, method: str, path: str, access: Access) -> str | None:
        path = API + path if not path.startswith(API) else path
        return blocked(method, path, access, self.mode_of)

    def catalogue(self, access: Access, module: str | None = None) -> str:
        """Every route this person's chat may use (in one module, or all), a
        line each: method, path, summary, and [in the app only] where a change
        is made in the app."""
        key = f"{access.key()}|{module or ''}"
        if key not in self._catalogues:
            lines = []
            for path, ops in sorted(self.spec()["paths"].items()):
                if mode_for(path, access, self.mode_of) == "off":
                    continue
                if module is not None and module_of(path) != module:
                    continue
                for method, op in ops.items():
                    if method not in ("get", "post", "put", "patch"):
                        continue
                    rule = "" if blocked(method, path, access, self.mode_of) is None \
                        else "  [in the app only]"
                    summary = (op.get("summary") or "").strip()
                    lines.append(f"{method.upper()} {path.removeprefix(API)} — {summary}{rule}")
            self._catalogues[key] = "\n".join(lines)
        return self._catalogues[key]

    def modules_said(self, access: Access) -> str:
        """The person's modules and what the chat may do in each, for the model."""
        names = sorted(self.mode_of) if access.is_admin else sorted(access.modules | {PERSONAL})
        parts = [f"{m} ({self.mode_of.get(m, 'read')})" for m in names
                 if self.mode_of.get(m, "read") != "off" and m != ADMIN]
        return ", ".join(parts)

    def how(self, method: str, path: str, access: Access) -> str:
        """A route's parameters and body, for the model to call it right."""
        path = API + path if not path.startswith(API) else path
        if mode_for(path, access, self.mode_of) == "off":
            return "That's not a module they have, or not one you handle from the chat."
        op = (self.spec()["paths"].get(path) or {}).get(method.lower())
        if op is None:
            return f"No {method.upper()} {path.removeprefix(API)} in the ERP. Check the catalogue."
        params = [
            f"{p['name']} ({p['in']}{', required' if p.get('required') else ''})"
            f"{': ' + p['description'][:120] if p.get('description') else ''}"
            for p in op.get("parameters") or []
        ]
        body = ""
        content = ((op.get("requestBody") or {}).get("content") or {})
        for kind, media in content.items():
            schema = self._resolve(media.get("schema") or {}, depth=0)
            body = f"\nBody ({kind}):\n{json.dumps(schema, ensure_ascii=False)[:5000]}"
        description = (op.get("description") or "").strip()[:1500]
        return (f"{method.upper()} {path.removeprefix(API)} — {op.get('summary', '')}\n"
                f"{description}\nParameters: {'; '.join(params) or 'none'}{body}")

    def _resolve(self, schema: Any, depth: int) -> Any:
        """``$ref``s replaced by what they point at, a few levels down."""
        if depth > 4:
            return "…"
        if isinstance(schema, dict):
            if "$ref" in schema:
                name = schema["$ref"].rsplit("/", 1)[-1]
                target = self.spec().get("components", {}).get("schemas", {}).get(name, {})
                return self._resolve(target, depth + 1)
            keep = ("type", "properties", "items", "required", "enum", "description", "anyOf",
                    "maxLength", "format", "default")
            return {k: self._resolve(v, depth + 1) for k, v in schema.items() if k in keep}
        if isinstance(schema, list):
            return [self._resolve(v, depth + 1) for v in schema]
        return schema

    async def call(
        self, user_id: uuid.UUID, method: str, path: str, *, access: Access, query: str = "",
        body: Any = None, files: list[tuple[str, bytes, str]] | None = None,
        form: dict[str, str] | None = None,
    ) -> Answer:
        """One request to the ERP as ``user_id``, through the app itself."""
        path = API + path if not path.startswith(API) else path
        if (why := blocked(method, path, access, self.mode_of)) is not None:
            return Answer(403, f"Not from the chat: {why}.")
        cookie = sign({"sub": str(user_id)}, secret=self._settings.session_secret,
                      ttl_minutes=10, audience=SESSION_AUDIENCE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://erp",
            cookies={self._settings.session_cookie_name: cookie}, timeout=600,
        ) as client:
            kwargs: dict[str, Any] = {}
            if files:
                kwargs["files"] = [("files", f) for f in files]
                kwargs["data"] = form or {}
            elif body is not None:
                kwargs["json"] = body
            url = path + (f"?{query.lstrip('?')}" if query else "")
            try:
                response = await client.request(method.upper(), url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the route crashed; say what, carry on
                logger.exception("it support: ERP %s %s crashed", method.upper(), path)
                return Answer(500, f"The ERP crashed on this: {type(exc).__name__}: {exc}"[:2000])
        try:
            payload: Any = response.json()
        except ValueError:
            kind = response.headers.get("content-type", "")
            payload = response.text if "text" in kind or "json" in kind else \
                f"(a {kind or 'binary'} file, {len(response.content):,} bytes)"
        logger.info("it support: ERP %s %s as %s -> %s", method.upper(), path, user_id,
                    response.status_code)
        return Answer(response.status_code, payload)


async def access_for(session: Any, user_id: uuid.UUID) -> Access:
    """The person's modules, exactly as the ERP's access screen gives them."""
    from app.access.service import effective_access, may_administer
    from app.roles.service import global_role_keys

    roles = await global_role_keys(session, user_id)
    found = await effective_access(session, user_id=user_id, global_roles=roles)
    return Access(frozenset(m["key"] for m in found["modules"]), is_admin=may_administer(roles))
