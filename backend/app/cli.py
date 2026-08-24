"""Operational commands.

    uv run python -m app.cli seed                # reconcile registry + default labels
    uv run python -m app.cli bootstrap <email>   # first super admin (refuses if one exists)
    uv run python -m app.cli grant-admin <email> # promote an existing user to super admin
    uv run python -m app.cli users               # who exists, and what they can reach
    uv run python -m app.cli check               # verify registries and constraints
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.compat import run as run_async
from app.core.config import get_settings
from app.core.db import dispose_engine, init_engine, session_scope
from app.core.logging import configure_logging
from app.core.rbac import PERMISSIONS, SYSTEM_ROLES, validate_registry
from app.core.rules.registry import DECISION_POINTS
from app.models.identity import Membership, Role, Team, User, UserStatus
from app.services.label_service import seed_default_labels
from app.services.seed import seed_all


async def _seed() -> int:
    async with session_scope() as session:
        result = await seed_all(session)
        labels = await seed_default_labels(session)

    print(f"permissions written : {result['permissions']}")
    print(f"role grants written : {result['role_grants']}")
    print(f"labels created      : {labels}")
    return 0


async def _bootstrap(email: str) -> int:
    """Create the first team and super admin.

    Idempotent, and refuses to run once a super admin exists — bootstrapping a second one
    would be a privilege-escalation path that bypasses the admin panel entirely.
    """
    async with session_scope() as session:
        await seed_all(session)
        await seed_default_labels(session)

        existing_admin = await session.scalar(
            select(Membership)
            .join(Role, Role.id == Membership.role_id)
            .where(Role.key == "super_admin")
            .limit(1)
        )
        if existing_admin is not None:
            print("A super admin already exists. Use the admin panel to add more.")
            return 1

        team = await session.scalar(select(Team).where(Team.slug == "pre-sales"))
        if team is None:
            team = Team(
                slug="pre-sales",
                name="Pre-Sales",
                description="Proposals and bid management.",
                enabled_modules=["proposals", "quotes", "reports", "leave"],
                settings={},
            )
            session.add(team)
            await session.flush()

        user = await session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            user = User(
                email=email.lower(),
                display_name=email.split("@")[0],
                status=UserStatus.ACTIVE,
                joined_at=datetime.now(UTC),
            )
            session.add(user)
            await session.flush()
        else:
            user.status = UserStatus.ACTIVE

        role = await session.scalar(
            select(Role).where(Role.key == "super_admin", Role.team_id.is_(None))
        )
        if role is None:
            print("super_admin role missing — run `seed` first.", file=sys.stderr)
            return 1

        session.add(
            Membership(
                user_id=user.id, team_id=team.id, role_id=role.id, joined_at=datetime.now(UTC)
            )
        )

    print(f"Super admin  : {email}")
    print(f"Team         : {team.name} ({team.slug})")
    print("They can now sign in with Microsoft and administer the system.")
    return 0


async def _grant_admin(email: str) -> int:
    """Make an existing user a super admin.

    The escape hatch ``bootstrap`` deliberately lacks: it refuses to run once any super admin
    exists, which strands you if the first one was created for the wrong address — for
    instance bootstrapping one email and then signing in with another.

    This runs against the database directly, so it is available to anyone who already holds
    the database credentials. That is the same trust level ``bootstrap`` assumes.
    """
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            print(f"No user with the email {email!r}.", file=sys.stderr)
            print("They must sign in once before they can be promoted.", file=sys.stderr)
            return 1

        role = await session.scalar(
            select(Role).where(Role.key == "super_admin", Role.team_id.is_(None))
        )
        if role is None:
            print("super_admin role missing - run `seed` first.", file=sys.stderr)
            return 1

        team = await session.scalar(select(Team).where(Team.archived_at.is_(None)))
        if team is None:
            print("No team exists - run `bootstrap` first.", file=sys.stderr)
            return 1

        user.status = UserStatus.ACTIVE

        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.team_id == team.id
            )
        )
        if membership is None:
            session.add(
                Membership(
                    user_id=user.id,
                    team_id=team.id,
                    role_id=role.id,
                    joined_at=datetime.now(UTC),
                )
            )
            action = "added to"
        else:
            membership.role_id = role.id
            action = "already in"

    print(f"{email} is now a super admin ({action} {team.name}).")
    print("Sign out and back in for the change to take effect.")
    return 0


async def _users() -> int:
    """List every account, its status, and where it can reach.

    The first thing to check when someone says they cannot get in.
    """
    async with session_scope() as session:
        users = list((await session.scalars(select(User).order_by(User.email))).all())
        if not users:
            print("No users yet. Nobody has signed in, and bootstrap has not run.")
            return 0

        memberships = list(
            (
                await session.scalars(
                    select(Membership).options(
                        selectinload(Membership.team), selectinload(Membership.role)
                    )
                )
            ).all()
        )

        by_user: dict[uuid.UUID, list[str]] = {}
        for m in memberships:
            if m.team is not None and m.role is not None:
                by_user.setdefault(m.user_id, []).append(f"{m.team.slug}:{m.role.key}")

        width = max(len(u.email) for u in users)
        print(f"{'EMAIL'.ljust(width)}  {'STATUS'.ljust(11)}  TEAMS AND ROLES")
        print("-" * (width + 40))
        for user in users:
            held = sorted(by_user.get(user.id, []))
            teams = ", ".join(held) or "-- no team, cannot see anything --"
            print(f"{user.email.ljust(width)}  {user.status.value.ljust(11)}  {teams}")

    print()
    print("A user with no team sees nothing, however they signed in. That is deliberate:")
    print("signing in proves identity, an admin grants access.")
    return 0


def _check() -> int:
    """Verify the registries agree with themselves. No database needed."""
    settings = get_settings()
    problems: list[str] = []

    try:
        validate_registry()
    except Exception as exc:
        problems.append(f"permission registry: {exc}")

    for dp in DECISION_POINTS:
        if not dp.facts:
            problems.append(f"decision point {dp.key} declares no facts")
        if not dp.actions:
            problems.append(f"decision point {dp.key} declares no actions")

    # C2 is a constraint, not a preference; surface its state on every check.
    if settings.sharepoint_sandbox_writes_enabled:
        print(
            "NOTE: SharePoint sandbox writes are ENABLED. Only /sites/sandbox is writable; "
            "live sites remain read-only."
        )
    if settings.is_production and settings.outbound_email_enabled is False:
        problems.append("outbound email is disabled in production — is that intended?")

    print(f"permissions     : {len(PERMISSIONS)}")
    print(f"system roles    : {len(SYSTEM_ROLES)}")
    print(f"decision points : {len(DECISION_POINTS)}")
    print(f"environment     : {settings.environment.value}")
    print(f"sharepoint      : read-only (sandbox {settings.sharepoint_sandbox_site_path})")

    if problems:
        print("\nPROBLEMS:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nAll registries consistent.")
    return 0


def main() -> int:
    configure_logging(get_settings())

    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    command = sys.argv[1]

    if command == "check":
        return _check()

    init_engine()
    try:
        if command == "seed":
            return run_async(_seed())
        if command == "bootstrap":
            if len(sys.argv) < 3:
                print("usage: python -m app.cli bootstrap <email>", file=sys.stderr)
                return 2
            return run_async(_bootstrap(sys.argv[2]))
        if command == "grant-admin":
            if len(sys.argv) < 3:
                print("usage: python -m app.cli grant-admin <email>", file=sys.stderr)
                return 2
            return run_async(_grant_admin(sys.argv[2]))
        if command == "users":
            return run_async(_users())
    finally:
        run_async(dispose_engine())

    print(f"unknown command {command!r}", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
