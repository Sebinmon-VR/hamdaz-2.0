"""SharePoint → Postgres ingest (§8.1).

**Read only.** This module maps SharePoint list items onto ``proposals`` rows. It never writes
back — the connector it uses has no write methods to call.

Idempotent by construction: every row is keyed on ``(source, source_id)``, so re-running a
sync updates rather than duplicates. The legacy version held its delta cursor in a module
global, which is why every gunicorn worker resynced independently and wrote the same updates
several times over.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.sharepoint.client import SharePointReadClient
from app.connectors.sharepoint.guard import SharePointWriteGuard
from app.core.config import Settings, get_settings
from app.core.graph_auth import GraphAuthError, GraphTokenProvider
from app.core.logging import get_logger
from app.models.identity import Team, User
from app.models.platform import ConnectorMode, ConnectorStatus
from app.models.proposals import Proposal, ProposalSource, ProposalStatus

logger = get_logger(__name__)

CONNECTOR_NAME = "sharepoint"

#: SharePoint field → proposal column, verified against the live ``Proposals`` list on
#: /sites/ProposalTeam rather than assumed. Three of the names this used to carry —
#: ``Customer``, ``Reference`` and a plain ``AssignedTo`` — do not exist on that list, so
#: every one of them silently mapped nothing.
#:
#: ``Status`` is deliberately absent: it drives the :class:`ProposalStatus` enum through
#: :func:`_map_status`, and the column named ``SubmissionStatus`` in SharePoint is the one
#: that belongs in ``submission_status``. Conflating the two is what the old map did.
#:
#: Nothing on the list corresponds to ``external_ref``; the reference numbers are embedded in
#: free-text titles and parsing them out is guesswork. ``source_payload`` keeps the entire raw
#: item either way, so no data is lost by leaving it unmapped.
FIELD_MAP: dict[str, str] = {
    "Title": "title",
    "EndUser": "customer_name",
    "BCD": "bcd",
    "SubmissionStatus": "submission_status",
}

#: The person column. Graph renders person fields as ``<Name>LookupId`` holding an ID that is
#: only resolvable against the site's User Information List — never as a name, which is why
#: matching on one could not have worked.
ASSIGNEE_LOOKUP_FIELD = "AssignedToLookupId"

#: SharePoint's Status choice → our enum, keyed on the values the live list actually holds:
#: Completed, In Progress, Not Started, On Hold and a stray "Choice 5".
#:
#: ``Completed`` means pre-sales finished and the proposal went out, so it maps to SUBMITTED.
#: It is emphatically not WON — winning is tracked separately in ``OrderStatus``, which reads
#: Received on a small fraction of these.
#:
#: ``Not Started``, ``On Hold`` and unrecognised values are left to fall through to NEW.
#: Anything unmapped stays NEW rather than guessing, so a new choice added in SharePoint
#: cannot silently close work here.
STATUS_MAP: dict[str, ProposalStatus] = {
    "completed": ProposalStatus.SUBMITTED,
    "submitted": ProposalStatus.SUBMITTED,
    "in progress": ProposalStatus.IN_PROGRESS,
    "ongoing": ProposalStatus.IN_PROGRESS,
    "won": ProposalStatus.WON,
    "lost": ProposalStatus.LOST,
    "cancelled": ProposalStatus.CANCELLED,
    "canceled": ProposalStatus.CANCELLED,
}


async def get_status_row(session: AsyncSession) -> ConnectorStatus:
    row = await session.get(ConnectorStatus, CONNECTOR_NAME)
    if row is None:
        row = ConnectorStatus(
            name=CONNECTOR_NAME, mode=ConnectorMode.READ_ONLY, healthy=True, cursor={}
        )
        session.add(row)
        await session.flush()
    return row


async def sync_proposals(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    client: SharePointReadClient | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Delta-sync the Proposals list into Postgres.

    Returns a summary the developer panel renders. Failures are recorded on
    ``connector_status`` and re-raised, so the job is marked failed rather than silently
    looking healthy.
    """
    settings = settings or get_settings()
    status_row = await get_status_row(session)

    # Guard with writes disabled: this path must not be able to write even by accident.
    guard = SharePointWriteGuard(sandbox_site_id=None, writes_enabled=False)

    owns_client = client is None
    provider: GraphTokenProvider | None = None
    if client is None:
        if not access_token:
            # No caller-supplied token: authenticate as the app itself. The sync runs on a
            # schedule with no user in the request path, so there is no delegated token to
            # borrow — see app/core/graph_auth.py.
            provider = GraphTokenProvider(settings)
            try:
                access_token = await provider.get_token()
            except GraphAuthError as exc:
                await provider.aclose()
                status_row.healthy = False
                status_row.last_error = str(exc)
                status_row.last_error_at = datetime.now(UTC)
                await session.flush()
                logger.error("sharepoint.sync_no_token", error=str(exc))
                raise
        client = SharePointReadClient(access_token=access_token, guard=guard)

    try:
        site_path = next(iter(settings.sharepoint_read_sites), "/sites/ProposalTeam")
        site_id = await client.get_site_id(settings.sharepoint_domain, site_path)
        list_id = await client.get_list_id(site_id, "Proposals")

        cursor = status_row.cursor or {}
        delta_link = cursor.get("delta_link")

        changed, removed_ids, next_delta = await client.delta(site_id, list_id, delta_link)

        team = await _default_team(session)
        if team is None:
            return {"skipped": True, "reason": "no team configured to receive proposals"}

        # Once per sync, not once per item: the directory is a few hundred rows and every
        # proposal in the batch resolves against the same copy.
        people = await client.get_site_users(site_id) if changed else {}

        created = updated = 0
        for item in changed:
            was_new = await _upsert(session, item, team_id=team.id, people=people)
            created += int(was_new)
            updated += int(not was_new)

        archived = await _archive_removed(session, removed_ids)

        status_row.cursor = {"delta_link": next_delta, "site_id": site_id, "list_id": list_id}
        status_row.healthy = True
        status_row.last_success_at = datetime.now(UTC)
        status_row.last_error = None
        status_row.mode = ConnectorMode.READ_ONLY
        await session.flush()

        summary = {
            "created": created,
            "updated": updated,
            "archived": archived,
            "site_path": site_path,
        }
        logger.info("sharepoint.sync_complete", **summary)
        return summary

    except Exception as exc:
        status_row.healthy = False
        status_row.last_error = f"{type(exc).__name__}: {exc}"
        status_row.last_error_at = datetime.now(UTC)
        await session.flush()
        logger.exception("sharepoint.sync_failed", error=str(exc))
        raise
    finally:
        if owns_client and client is not None:
            await client.aclose()
        if provider is not None:
            await provider.aclose()


async def _default_team(session: AsyncSession) -> Team | None:
    """Which team ingested proposals belong to.

    The legacy list has no team column, because the legacy system had one team. Until teams
    are mapped explicitly, everything lands in Pre-Sales.
    """
    team: Team | None = await session.scalar(select(Team).where(Team.slug == "pre-sales"))
    if team is not None:
        return team
    fallback: Team | None = await session.scalar(
        select(Team).where(Team.archived_at.is_(None)).order_by(Team.created_at)
    )
    return fallback


async def _upsert(
    session: AsyncSession,
    item: dict[str, Any],
    *,
    team_id: uuid.UUID,
    people: dict[str, dict[str, Any]],
) -> bool:
    """Insert or update one proposal. Returns True when it was newly created."""
    source_id = str(item.get("id"))
    fields = item.get("fields") or {}

    existing = await session.scalar(
        select(Proposal).where(
            Proposal.source == ProposalSource.SHAREPOINT, Proposal.source_id == source_id
        )
    )

    mapped = _map_fields(fields)
    assignee_id = await _resolve_assignee(session, fields, people)

    if existing is None:
        proposal = Proposal(
            team_id=team_id,
            source=ProposalSource.SHAREPOINT,
            source_id=source_id,
            title=mapped.get("title") or f"Proposal {source_id}",
            status=_map_status(fields.get("Status")),
            source_payload=fields,
            synced_at=datetime.now(UTC),
        )
        for key, value in mapped.items():
            setattr(proposal, key, value)
        if assignee_id is not None:
            proposal.assigned_to = assignee_id
            proposal.assigned_at = datetime.now(UTC)
        session.add(proposal)
        await session.flush()
        return True

    for key, value in mapped.items():
        setattr(existing, key, value)
    existing.status = _map_status(fields.get("Status"), current=existing.status)
    existing.source_payload = fields
    existing.synced_at = datetime.now(UTC)
    if assignee_id is not None and existing.assigned_to != assignee_id:
        existing.previous_owner = existing.assigned_to
        existing.assigned_to = assignee_id
        existing.assigned_at = datetime.now(UTC)
    await session.flush()
    return False


def _map_fields(fields: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for sp_field, column in FIELD_MAP.items():
        if sp_field not in fields:
            continue
        value = fields[sp_field]
        if column == "bcd":
            value = _parse_datetime(value)
            if value is None:
                continue
        mapped[column] = value
    return mapped


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("sharepoint.unparseable_date", value=value)
    return None


def _map_status(raw: Any, *, current: ProposalStatus | None = None) -> ProposalStatus:
    if not isinstance(raw, str) or not raw.strip():
        return current or ProposalStatus.NEW
    return STATUS_MAP.get(raw.strip().lower(), current or ProposalStatus.NEW)


async def _resolve_assignee(
    session: AsyncSession, fields: dict[str, Any], people: dict[str, dict[str, Any]]
) -> uuid.UUID | None:
    """Match the item's person column to one of our users.

    Two hops, because SharePoint gives us neither a name nor an address directly: the item
    carries a lookup ID, the site directory turns that into an email, and the email is what
    identifies a user here. Email first because it is stable — display names get edited.

    Deliberately conservative: anything that does not resolve leaves the proposal unassigned
    rather than guessing, because a wrong assignment is worse than none.
    """
    lookup_id = fields.get(ASSIGNEE_LOOKUP_FIELD)
    if lookup_id in (None, ""):
        return None

    entry = people.get(str(lookup_id))
    if entry is None:
        logger.debug("sharepoint.assignee_not_in_directory", lookup_id=str(lookup_id))
        return None

    email = entry.get("email")
    if email:
        user: User | None = await session.scalar(select(User).where(User.email == email))
        if user is not None:
            return user.id

    display_name = entry.get("display_name")
    if isinstance(display_name, str) and display_name.strip():
        user = await session.scalar(
            select(User).where(User.display_name == display_name.strip())
        )
        if user is not None:
            return user.id

    logger.debug("sharepoint.assignee_unmatched", email=email)
    return None


async def _archive_removed(session: AsyncSession, removed_ids: list[str]) -> int:
    """Cancel proposals whose source item disappeared.

    Cancelled rather than deleted: the row is still referenced by audit entries and
    assignment history, and a SharePoint deletion is not our decision to propagate as a
    hard delete.
    """
    if not removed_ids:
        return 0

    rows = (
        await session.scalars(
            select(Proposal).where(
                Proposal.source == ProposalSource.SHAREPOINT,
                Proposal.source_id.in_(removed_ids),
            )
        )
    ).all()

    for proposal in rows:
        proposal.status = ProposalStatus.CANCELLED
    await session.flush()
    return len(rows)
