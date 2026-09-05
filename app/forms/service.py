"""Managing templates, and deciding who may use one.

Two rules run through everything here.

**Only a super admin writes.** Creating, editing, archiving and granting are all
theirs. A template decides what the business records, so it is not something a
team adjusts for itself — and the alternative, letting each team edit the form
they fill in, produces records that cannot be compared with each other.

**Access is two independent questions.** Which *team* may use a template, and
what *standing* is needed inside it. A grant leaves either side open by omission:
no team means every team, no roles means anyone on the granted team. A template
with no grants at all is usable by nobody but a super admin, which is the right
default for something just created — a form should appear because somebody
decided it should, not because it was saved.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forms import scoring
from app.forms.catalogue import TEMPLATES
from app.models.team import Team, TeamMembership
from app.models.templates import FormTemplate, TemplateGrant, TemplateStatus
from app.models.user import User

logger = logging.getLogger("hamdaz.forms")

#: The only role that may create or change a template.
TEMPLATE_ADMINS = frozenset({"super_admin"})


class TemplateError(Exception):
    """A template operation was refused. Safe to show a user."""


class TemplateNotFoundError(TemplateError):
    pass


class TemplatePermissionError(TemplateError):
    pass


@dataclass(frozen=True, slots=True)
class Usable:
    allowed: bool
    reason: str


def require_admin(roles: set[str]) -> None:
    if not roles & TEMPLATE_ADMINS:
        raise TemplatePermissionError(
            "Only a super admin may create or change a form template — a template "
            "decides what the business records, so it is not a per-team setting."
        )


# ── the catalogue ──────────────────────────────────────────────────────


async def seed_templates(session: AsyncSession) -> list[FormTemplate]:
    """Create the shipped templates. Idempotent; admin edits survive."""
    existing = {
        t.key: t for t in (await session.scalars(select(FormTemplate))).all()
    }
    for spec in TEMPLATES:
        if spec["key"] in existing:
            continue  # an admin owns it now
        template = FormTemplate(
            key=spec["key"],
            name=spec["name"],
            kind=spec["kind"],
            description=spec.get("description"),
            fields=spec["fields"],
            sections=spec.get("sections", []),
            # Shipped active: a form nobody can use until somebody publishes it
            # would be a puzzle rather than a safeguard.
            status=TemplateStatus.ACTIVE,
        )
        # Usable by every team, by anyone, until a super admin narrows it —
        # unless the spec says otherwise. A template whose access is decided
        # somewhere else entirely ships with no grants, because appearing in
        # everybody's "forms you can fill in" list would be a lie about it.
        template.grants = [
            TemplateGrant(
                team_id=g.get("team_id"), allowed_roles=list(g.get("allowed_roles") or [])
            )
            for g in spec.get("grants", ({"team_id": None, "allowed_roles": []},))
        ]
        session.add(template)
        existing[spec["key"]] = template
    await session.flush()
    return list(existing.values())


async def all_templates(
    session: AsyncSession, *, include_archived: bool = False, kind: str | None = None
) -> list[FormTemplate]:
    query = select(FormTemplate).order_by(FormTemplate.kind, FormTemplate.name)
    if not include_archived:
        query = query.where(FormTemplate.status != TemplateStatus.ARCHIVED)
    if kind:
        query = query.where(FormTemplate.kind == kind)
    return list((await session.scalars(query)).all())


async def get(session: AsyncSession, key_or_id: str | uuid.UUID) -> FormTemplate:
    """By key or id — both turn up in URLs."""
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(str(key_or_id))
    except (ValueError, AttributeError):
        as_uuid = None

    template = None
    if as_uuid is not None:
        template = await session.get(FormTemplate, as_uuid)
    if template is None:
        template = await session.scalar(
            select(FormTemplate)
            .where(FormTemplate.key == str(key_or_id))
            .order_by(FormTemplate.version.desc())
        )
    if template is None:
        raise TemplateNotFoundError(f"No template {str(key_or_id)!r}")
    return template


async def active_for(session: AsyncSession, kind: str) -> FormTemplate | None:
    """The template a module should use for its own kind of form."""
    return await session.scalar(
        select(FormTemplate)
        .where(FormTemplate.kind == kind, FormTemplate.status == TemplateStatus.ACTIVE)
        .order_by(FormTemplate.version.desc())
    )


# ── writing ────────────────────────────────────────────────────────────


def _validate_fields(fields: list[Any]) -> list[dict]:
    """Enough checking that a saved template can actually be rendered."""
    if not isinstance(fields, list):
        raise TemplateError("fields must be a list")

    seen: set[str] = set()
    out: list[dict] = []
    for index, raw in enumerate(fields):
        if not isinstance(raw, dict):
            raise TemplateError(f"Field {index} is not an object")
        key = str(raw.get("key") or "").strip()
        if not key:
            raise TemplateError(f"Field {index} has no key")
        if key in seen:
            # Two fields with one key means one silently overwrites the other on
            # every submission.
            raise TemplateError(f"Two fields share the key {key!r}")
        seen.add(key)
        if not raw.get("label"):
            raise TemplateError(f"Field {key!r} has no label")
        if not raw.get("type"):
            raise TemplateError(f"Field {key!r} has no type")
        if raw.get("scoring"):
            # Checked here, not when somebody fills the form in: a scoring block
            # that cannot produce a number is a mistake by the admin writing the
            # template, and they are the only person able to fix it.
            try:
                raw = {
                    **raw,
                    "scoring": scoring.validate_block(key, str(raw["type"]), raw["scoring"]),
                }
            except scoring.ScoringError as exc:
                raise TemplateError(str(exc)) from exc
        out.append(raw)
    return out


async def create(
    session: AsyncSession,
    *,
    roles: set[str],
    actor: User,
    key: str,
    name: str,
    kind: str,
    fields: list[Any],
    sections: list[Any] | None = None,
    description: str | None = None,
) -> FormTemplate:
    require_admin(roles)
    key = key.strip().casefold().replace(" ", "_")
    if not key:
        raise TemplateError("A template needs a key")
    if await session.scalar(select(FormTemplate).where(FormTemplate.key == key)):
        raise TemplateError(f"A template {key!r} already exists")

    template = FormTemplate(
        key=key,
        name=name.strip(),
        kind=(kind or key).strip(),
        description=description,
        fields=_validate_fields(fields),
        sections=sections or [],
        # Draft, so a half-finished form cannot be filled in by somebody who
        # then loses the work when it changes under them.
        status=TemplateStatus.DRAFT,
        # The object, not the id: the response names its author, and an
        # unloaded ``created_by`` makes that a lazy SELECT from inside
        # serialisation — a MissingGreenlet rather than a name.
        created_by=actor,
        updated_by_id=actor.id,
        # Initialised for the same reason. Left out, a freshly created template
        # has a collection SQLAlchemy would lazily fetch, and touching it
        # outside an await is a MissingGreenlet rather than an empty list.
        grants=[],
    )
    session.add(template)
    await session.flush()
    return template


async def update(
    session: AsyncSession,
    template: FormTemplate,
    *,
    roles: set[str],
    actor: User,
    **changes: Any,
) -> FormTemplate:
    require_admin(roles)
    if template.status == TemplateStatus.ARCHIVED:
        raise TemplateError(
            f"{template.key!r} is archived. Restore it before editing, or the "
            f"records made from it stop matching their own definition."
        )

    if changes.get("name"):
        template.name = str(changes["name"]).strip()[:160]
    if "description" in changes:
        template.description = changes["description"]
    if changes.get("kind"):
        template.kind = str(changes["kind"]).strip()[:64]
    if (fields := changes.get("fields")) is not None:
        template.fields = _validate_fields(fields)
    if (sections := changes.get("sections")) is not None:
        template.sections = sections

    template.updated_by_id = actor.id
    await session.flush()
    return template


async def publish(
    session: AsyncSession, template: FormTemplate, *, roles: set[str], actor: User
) -> FormTemplate:
    require_admin(roles)
    if not template.fields:
        raise TemplateError("A template with no fields cannot be published")
    template.status = TemplateStatus.ACTIVE
    template.updated_by_id = actor.id
    await session.flush()
    return template


async def archive(
    session: AsyncSession, template: FormTemplate, *, roles: set[str], actor: User
) -> FormTemplate:
    """Retire it, keeping it readable.

    Archiving rather than deleting because a form somebody submitted last month
    is unreadable if the definition behind it has gone.
    """
    require_admin(roles)
    template.status = TemplateStatus.ARCHIVED
    template.updated_by_id = actor.id
    await session.flush()
    return template


async def restore(
    session: AsyncSession, template: FormTemplate, *, roles: set[str], actor: User
) -> FormTemplate:
    require_admin(roles)
    template.status = TemplateStatus.ACTIVE
    template.updated_by_id = actor.id
    await session.flush()
    return template


async def delete(session: AsyncSession, template: FormTemplate, *, roles: set[str]) -> None:
    """Only a template nothing was ever filled in from.

    Anything else is archived instead — see :func:`archive`.
    """
    require_admin(roles)
    from app.models.quoting import QuoteRequest  # local: avoids a cycle

    if template.kind == "quote_request" and await session.scalar(
        select(QuoteRequest).limit(1)
    ):
        raise TemplateError(
            f"{template.key!r} has been used. Archive it instead — deleting it "
            f"would leave existing records without a definition to read them by."
        )
    await session.delete(template)
    await session.flush()


# ── who may use one ────────────────────────────────────────────────────


async def set_grants(
    session: AsyncSession,
    template: FormTemplate,
    *,
    roles: set[str],
    grants: list[dict[str, Any]],
) -> FormTemplate:
    """Replace the grants wholesale.

    Replaced rather than merged: access is a single statement of who may use
    this, and a partial update makes "who can see it now" a question nobody can
    answer without replaying the edits.
    """
    require_admin(roles)
    # The Team objects, not just their ids. A grant built from an id alone has
    # an unloaded ``team``, and naming the team in the response would then be a
    # lazy SELECT from inside serialisation — a MissingGreenlet, not a name.
    # Resolving them here also turns an unknown team into a refusal a caller can
    # read, rather than a foreign key violation at flush.
    wanted = {g.get("team_id") for g in grants if g.get("team_id") is not None}
    teams = {}
    if wanted:
        rows = await session.scalars(select(Team).where(Team.id.in_(wanted)))
        teams = {t.id: t for t in rows}
        missing = wanted - teams.keys()
        if missing:
            raise TemplateError(
                "No such team: " + ", ".join(sorted(str(m) for m in missing))
            )

    template.grants = [
        TemplateGrant(
            team=teams.get(g.get("team_id")),
            allowed_roles=[str(r).strip().casefold() for r in (g.get("allowed_roles") or [])],
            note=g.get("note"),
        )
        for g in grants
    ]
    await session.flush()
    return template


async def may_use(
    session: AsyncSession,
    template: FormTemplate,
    *,
    user: User,
    roles: set[str],
) -> Usable:
    """Whether this person may fill this form in, and why not if they may not."""
    if roles & TEMPLATE_ADMINS:
        return Usable(True, "Super admins may use any template.")
    if template.status != TemplateStatus.ACTIVE:
        return Usable(
            False,
            f"This template is {template.status} and is not available to fill in.",
        )
    if not template.grants:
        return Usable(
            False,
            "Nobody has been given this template yet. A super admin grants it to "
            "a team.",
        )

    memberships = list(
        (
            await session.scalars(
                select(TeamMembership).where(TeamMembership.user_id == user.id)
            )
        ).all()
    )
    my_teams = {m.team_id for m in memberships}

    for grant in template.grants:
        if grant.team_id is not None and grant.team_id not in my_teams:
            continue
        wanted = {str(r).casefold() for r in (grant.allowed_roles or [])}
        if not wanted:
            return Usable(True, "Granted to your team.")

        held = set(roles)
        for membership in memberships:
            if grant.team_id in (None, membership.team_id) and membership.role:
                held.add(membership.role.key)
        if held & wanted:
            return Usable(True, f"Granted to {sorted(held & wanted)[0]!r}.")

    return Usable(
        False,
        "Your team does not have this template, or it needs a role you do not hold.",
    )


async def usable_by(
    session: AsyncSession, *, user: User, roles: set[str], kind: str | None = None
) -> list[FormTemplate]:
    """Every template this person may actually fill in."""
    out = []
    for template in await all_templates(session, kind=kind):
        if (await may_use(session, template, user=user, roles=roles)).allowed:
            out.append(template)
    return out
