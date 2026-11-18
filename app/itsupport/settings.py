"""IT Support's settings row: read, changed on the admin page, applied.

The worker and the rest of ``app.itsupport`` read their settings as
``settings.it_support_*`` attributes. Rather than teach each of them about a
table, the worker keeps **its own copy** of the app's Settings and ``apply``
writes the row over it every ``REFRESH_SECONDS`` — so a person added on the
page is answered within half a minute, without a restart. Values the page does
not hold (poll speed, token file, providers) stay as the environment says.

The row is made on first read, seeded from the environment, so the first save
of the page keeps whatever the laptop's .env already said.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.access.catalogue import BY_KEY
from app.core.config import Settings
from app.itsupport import features as features_mod
from app.itsupport.erp import ADMIN, MODES, PERSONAL
from app.models.itsupport import ITSupportSettings
from app.models.user import User

logger = logging.getLogger("hamdaz.itsupport")

#: How often the worker reads the row again (and writes its heartbeat).
REFRESH_SECONDS: Final = 30.0
#: A heartbeat younger than this means that machine is answering.
ALIVE_SECONDS: Final = 90.0
#: This machine, as the heartbeat names it.
#: This process, as the lease names it: machine and process, since a server
#: may run several processes of the app on one machine.
HOST: Final = f"{socket.gethostname()}:{os.getpid()}"

MODELS: Final = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
EFFORTS: Final = ("low", "medium", "high")
#: Where a change from the chat is the presales work itself (erp.MODES).
WRITABLE: Final = frozenset(k for k, v in MODES.items() if v == "write")
#: Off whatever is saved (the user, 2026-11-13: no finance or accounts).
ALWAYS_OFF: Final = frozenset({"finance", "payroll"})
_LABELS: Final = {PERSONAL: "Their own things (notifications, dashboards)",
                  ADMIN: "Admin areas (super admins only)"}
_CLOCK: Final = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ITSupportSettingsError(ValueError):
    pass


def module_rows() -> list[dict[str, Any]]:
    """The modules the page lists: key, name, the default mode, the most it
    may be set to."""
    out = []
    for key, default in MODES.items():
        spec = BY_KEY.get(key)
        top = "off" if key in ALWAYS_OFF else "write" if key in WRITABLE else "read"
        out.append({"key": key, "name": _LABELS.get(key) or (spec.name if spec else key),
                    "default": default, "max": top})
    order = {"write": 0, "read": 1, "off": 2}
    return sorted(out, key=lambda m: (order[m["max"]], m["name"].casefold()))


def _pairs(text: str) -> list[tuple[str, str]]:
    out = []
    for pair in text.split(","):
        key, _, value = pair.partition("=")
        if key.strip():
            out.append((key.strip(), value.strip()))
    return out


def seed(settings: Settings) -> dict[str, Any]:
    """The row as the environment would have it."""
    names = {k.lower(): v for k, v in _pairs(settings.it_support_names)}
    emails = [e.strip().lower() for e in settings.it_support_allowed_emails.split(",")
              if e.strip() and e.strip() != "*"]
    return {
        "enabled": True,
        "people": [{"email": e, "name": names.get(e, "")} for e in dict.fromkeys(emails)],
        "show_timing": settings.it_support_show_timing,
        "due_enabled": settings.it_support_due_enabled,
        "due_from": settings.it_support_due_from,
        "due_until": settings.it_support_due_until,
        "due_days": sorted({int(d) for d in settings.it_support_due_days.split(",")
                            if d.strip().isdigit()}),
        "due_test_to": settings.it_support_due_test_to.strip().lower(),
        "brief_enabled": settings.it_support_brief_enabled,
        "brief_at": settings.it_support_brief_at,
        "brain_model": settings.it_support_brain_model,
        "brain_effort": settings.it_support_brain_effort,
        "module_modes": {k: v.lower() for k, v in _pairs(settings.it_support_module_modes)},
        "features": {},
    }


async def get_row(session: AsyncSession, settings: Settings) -> ITSupportSettings:
    row = await session.get(ITSupportSettings, 1)
    if row is None:
        row = ITSupportSettings(id=1, **seed(settings))
        session.add(row)
        await session.flush()
    return row


def _clean_people(values: Any) -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for raw in values or []:
        if isinstance(raw, str):
            raw = {"email": raw}
        email = str(raw.get("email") or "").strip().lower()
        if not email:
            continue
        if "@" not in email:
            raise ITSupportSettingsError(f"{email} is not an email address.")
        out.setdefault(email, {"email": email, "name": str(raw.get("name") or "").strip()[:60]})
    return list(out.values())


def _clean_modes(values: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, mode in (values or {}).items():
        mode = str(mode).strip().lower()
        if key not in MODES:
            raise ITSupportSettingsError(f"{key} is not a module.")
        if mode not in ("off", "read", "write"):
            raise ITSupportSettingsError(f"{key}: {mode} is not off, read or write.")
        if key in ALWAYS_OFF and mode != "off":
            raise ITSupportSettingsError("Finance and payroll stay off for the agent.")
        if mode == "write" and key not in WRITABLE:
            raise ITSupportSettingsError(
                f"{key} can be read from the chat, not changed: its changes are made in the app."
            )
        if mode != MODES[key]:
            out[key] = mode  # only what differs from the default is kept
    return out


_EDITABLE: Final = frozenset({
    "enabled", "people", "show_timing", "due_enabled", "due_from", "due_until", "due_days",
    "due_test_to", "brain_model", "brain_effort", "module_modes", "brief_enabled", "brief_at",
    "features",
})


async def update(session: AsyncSession, settings: Settings, *, actor_id: uuid.UUID,
                 changes: dict[str, Any]) -> ITSupportSettings:
    """Only the keys given change."""
    row = await get_row(session, settings)
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise ITSupportSettingsError(f"Not a setting: {', '.join(sorted(unknown))}.")
    if "people" in changes:
        changes["people"] = _clean_people(changes["people"])
    for key in ("due_from", "due_until", "brief_at"):
        if key in changes:
            changes[key] = str(changes[key]).strip()
            if not _CLOCK.match(changes[key]):
                raise ITSupportSettingsError("Times are HH:MM, e.g. 09:00.")
    if ("due_from" in changes or "due_until" in changes) and \
            changes.get("due_from", row.due_from) >= changes.get("due_until", row.due_until):
        raise ITSupportSettingsError("Office hours must end after they start.")
    if "due_days" in changes:
        days = sorted({int(d) for d in changes["due_days"] or []})
        if any(not 0 <= d <= 6 for d in days):
            raise ITSupportSettingsError("Days are 0 (Monday) to 6 (Sunday).")
        changes["due_days"] = days
    if "due_test_to" in changes:
        changes["due_test_to"] = str(changes["due_test_to"] or "").strip().lower()
    if changes.get("brain_model") is not None and changes["brain_model"] not in MODELS:
        raise ITSupportSettingsError(f"The model is one of {', '.join(MODELS)}.")
    if changes.get("brain_effort") is not None and changes["brain_effort"] not in EFFORTS:
        raise ITSupportSettingsError(f"The effort is one of {', '.join(EFFORTS)}.")
    if "module_modes" in changes:
        changes["module_modes"] = _clean_modes(changes["module_modes"])
    if "features" in changes:
        try:
            # The switches sent change; the others keep what was saved.
            if not isinstance(changes["features"], dict):
                raise ValueError("Features are a map of name to on/off.")
            changes["features"] = features_mod.clean(
                {**(row.features or {}), **changes["features"]})
        except ValueError as exc:
            raise ITSupportSettingsError(str(exc)) from exc
    people = changes.get("people", row.people)
    test_to = changes.get("due_test_to", row.due_test_to)
    if test_to and test_to not in {p["email"] for p in people}:
        raise ITSupportSettingsError(
            "Test check-ins can only go to somebody in the list it answers."
        )
    for key, value in changes.items():
        if value is not None:
            setattr(row, key, value)
    row.updated_by_id = actor_id
    await session.flush()
    return row


async def accounts(session: AsyncSession, emails: list[str]) -> set[str]:
    """Which of these have an ERP account (the agent works the ERP as them)."""
    if not emails:
        return set()
    found = await session.scalars(
        select(func.lower(User.email)).where(func.lower(User.email).in_(emails)))
    return set(found.all())


def apply(row: ITSupportSettings, settings: Settings) -> None:
    """Write the row over this copy of the settings, as the environment
    values the rest of app.itsupport reads."""
    people = row.people or []
    settings.it_support_allowed_emails = ",".join(p["email"] for p in people)
    settings.it_support_names = ",".join(f"{p['email']}={p['name']}" for p in people
                                         if p.get("name"))
    settings.it_support_show_timing = row.show_timing
    settings.it_support_due_enabled = row.due_enabled
    settings.it_support_due_from = row.due_from
    settings.it_support_due_until = row.due_until
    settings.it_support_due_days = ",".join(str(d) for d in row.due_days or [])
    settings.it_support_due_test_to = row.due_test_to
    settings.it_support_brief_enabled = row.brief_enabled
    settings.it_support_brief_at = row.brief_at
    settings.it_support_brain_model = row.brain_model
    settings.it_support_brain_effort = row.brain_effort
    settings.it_support_module_modes = ",".join(f"{k}={v}" for k, v in
                                                (row.module_modes or {}).items())
    settings.it_support_features = dict(row.features or {})
    trial_overrides(settings)


def trial_overrides(settings: Settings) -> None:
    """A trial on this machine (IT_SUPPORT_TRIAL_ONLY) over whatever the page
    says: only those people, every unasked message to the first of them, the
    trial's features and brief time."""
    people = trial_people(settings)
    if not people:
        return
    settings.it_support_allowed_emails = ",".join(people)
    settings.it_support_due_test_to = people[0]
    wanted = settings.it_support_trial_features.strip()
    keys = [f.key for f in features_mod.CATALOGUE] if wanted.lower() == "all" else \
        [k.strip() for k in wanted.split(",") if k.strip() in features_mod.BY_KEY]
    settings.it_support_features = {**settings.it_support_features, **{k: True for k in keys}}
    if _CLOCK.match(settings.it_support_trial_brief_at.strip()):
        settings.it_support_brief_at = settings.it_support_trial_brief_at.strip()


def trial_people(settings: Settings) -> list[str]:
    return [e.strip().lower() for e in settings.it_support_trial_only.split(",")
            if "@" in e]


async def load(factory: async_sessionmaker[AsyncSession], settings: Settings,
               ) -> ITSupportSettings | None:
    """The row, made if missing; None while its table does not exist yet
    (the migration not run), so the environment values stand."""
    try:
        async with factory() as session:
            row = await get_row(session, settings)
            await session.commit()
            return row
    except DBAPIError as exc:
        if "itsupport_settings" in str(exc):
            return None
        raise


def other_machine(row: ITSupportSettings, now: datetime | None = None) -> str | None:
    """Another process holding the lease right now, if one is (for the page)."""
    now = now or datetime.now(UTC)
    if (row.running_on and row.running_on != HOST and row.last_seen_at is not None
            and (now - row.last_seen_at).total_seconds() < ALIVE_SECONDS):
        return row.running_on
    return None


#: Taken only when nobody else holds it: free, stale, or already ours. One
#: statement, so two processes starting together can't both win.
_CLAIM: Final = text("""
    UPDATE itsupport_settings
       SET running_on = :me, last_seen_at = now(),
           signed_in_as = COALESCE(:who, signed_in_as), last_error = :error
     WHERE id = 1
       AND (running_on = :me OR running_on IS NULL OR last_seen_at IS NULL
            OR last_seen_at < now() - make_interval(secs => :alive))
 RETURNING id""")
#: A problem recorded only where it doesn't talk over the process answering.
_NOTE: Final = text("""
    UPDATE itsupport_settings SET last_error = :error
     WHERE id = 1
       AND (running_on = :me OR running_on IS NULL OR last_seen_at IS NULL
            OR last_seen_at < now() - make_interval(secs => :alive))""")


async def claim(factory: async_sessionmaker[AsyncSession], *, signed_in_as: str | None,
                error: str | None) -> bool:
    """The lease of the one process that answers: taken or renewed, with
    this process's sign-in and last problem; False while another holds it."""
    try:
        async with factory() as session:
            got = (await session.execute(_CLAIM, {
                "me": HOST, "who": signed_in_as, "error": error, "alive": ALIVE_SECONDS,
            })).first()
            await session.commit()
            return got is not None
    except DBAPIError as exc:
        logger.warning("it support: the lease could not be taken: %s", exc)
        return False


async def note_problem(factory: async_sessionmaker[AsyncSession], error: str) -> None:
    """Why this process can't answer (not connected, wrong account), for the
    page, without claiming the lease or overwriting the one that holds it."""
    try:
        async with factory() as session:
            await session.execute(_NOTE, {"me": HOST, "error": f"{HOST}: {error}",
                                          "alive": ALIVE_SECONDS})
            await session.commit()
    except DBAPIError as exc:
        logger.warning("it support: the problem was not recorded: %s", exc)


async def release(factory: async_sessionmaker[AsyncSession]) -> None:
    """Let the lease go on a clean stop, so another process takes over now
    rather than after ALIVE_SECONDS."""
    try:
        async with factory() as session:
            await session.execute(text(
                "UPDATE itsupport_settings SET last_seen_at = NULL "
                "WHERE id = 1 AND running_on = :me"), {"me": HOST})
            await session.commit()
    except DBAPIError as exc:
        logger.warning("it support: the lease was not let go: %s", exc)
