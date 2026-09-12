"""Bootstrap the role catalogue and the first super admin.

Run after migrating:  ``python -m app.seed``

Idempotent by design — running it twice changes nothing the second time, so it
is safe to wire into a deploy step.

The bootstrap admin is a genuine chicken-and-egg: granting a role requires an
admin, and at first there is none. This script is the only thing that may create
one out of nothing. Everything afterwards goes through the API.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app  # noqa: F401 — sets the Windows event-loop policy
from app.access.service import seed_modules
from app.assistant.service import seed_models as seed_assistant_models
from app.assistant.service import seed_policies as seed_assistant_policies
from app.assistant.service import seed_voice_models as seed_assistant_voice_models
from app.core.config import get_settings
from app.directory.graph import GraphDirectory, GraphError
from app.forms.service import seed_templates
from app.reports.service import seed_templates as seed_report_templates
from app.workflows.service import seed_flows
from app.labels.service import seed_labels
from app.models.user import User
from app.roles.catalogue import BOOTSTRAP_SUPER_ADMIN_EMAIL, SUPER_ADMIN
from app.roles.service import assign_role, count_super_admins, seed_system_roles

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")


async def _find_or_create_user(session: AsyncSession, email: str) -> User:
    """Get the user row for ``email``, creating it from Entra if they have never signed in.

    The bootstrap admin should not have to log in before they can be made an
    admin, so if they are absent locally we look them up in the directory. Their
    Entra object id is what makes the row real rather than a placeholder that
    would collide on their first sign-in.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user

    logger.info("%s has never signed in; looking them up in the directory", email)
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        directory = GraphDirectory(settings, http)
        people = await directory.list_users(include_disabled=True)

    match = next(
        (p for p in people if p.email == email or p.user_principal_name == email), None
    )
    if match is None:
        raise GraphError(f"{email} is not in the organisation directory")

    user = User(
        entra_object_id=match.object_id,
        email=match.email or match.user_principal_name,
        display_name=match.display_name,
    )
    session.add(user)
    await session.flush()
    logger.info("created user row for %s (%s)", user.display_name, user.email)
    return user


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        roles = await seed_system_roles(session)
        logger.info("roles: %s", ", ".join(sorted(r.key for r in roles)))

        modules = await seed_modules(session)
        logger.info("modules: %s", ", ".join(m.key for m in modules))
        labels = await seed_labels(session)
        logger.info("labels: %s", ", ".join(sorted(lbl.key for lbl in labels)))
        templates = await seed_templates(session)
        logger.info("templates: %s", ", ".join(sorted(t.key for t in templates)))
        # Report templates are seeded by the reports module rather than the form
        # catalogue: reports are a consumer of templates, and putting them the
        # other way round would have the generic machinery depend on one of the
        # things built on it.
        reports = await seed_report_templates(session)
        logger.info("report templates: %s", ", ".join(sorted(t.key for t in reports)))
        # The shipped workflows. An edited one is left alone, like a template.
        flows = await seed_flows(session)
        logger.info("workflows seeded: %s", flows)

        # The assistant's catalogue becomes editable rows. Existing switches are
        # never overwritten — a super admin's decision survives every deploy.
        models = await seed_assistant_models(session)
        voice_models = await seed_assistant_voice_models(session)
        module_count, tool_count = await seed_assistant_policies(session)
        logger.info(
            "assistant: %d models, %d voice models, %d modules, %d tools",
            len(models), len(voice_models), module_count, tool_count,
        )

        existing = await count_super_admins(session)
        if existing:
            logger.info("super admin already exists (%d); leaving grants alone", existing)
        else:
            user = await _find_or_create_user(session, BOOTSTRAP_SUPER_ADMIN_EMAIL)
            # granted_by is NULL: nobody granted this one, the system did.
            await assign_role(
                session, user_id=user.id, role_key=SUPER_ADMIN, granted_by_id=None
            )
            logger.info("granted %s to %s", SUPER_ADMIN, user.email)

        await session.commit()

    await engine.dispose()
    logger.info("seed complete")


if __name__ == "__main__":
    asyncio.run(seed())
