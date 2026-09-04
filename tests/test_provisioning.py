"""Provisioning a directory person as a local user.

The bug this covers: an admin picks a colleague out of the org directory, which
gives an Entra object id, and grants them a role. Before this existed, that was
a 404 unless the person had already signed in — so an organisation could not be
set up in advance, which is the only time anyone sets one up.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.directory.graph import GraphError, OrgUser
from app.directory.provisioning import UserNotResolvableError, resolve_user
from app.models.user import User


def _org_user(oid: str, name: str = "Sujeel", enabled: bool = True, mail: str | None = None):
    return OrgUser(
        object_id=oid,
        display_name=name,
        email=mail if mail is not None else f"{name.lower()}@hamdaz.com",
        user_principal_name=f"{name.lower()}@hamdaz.com",
        job_title=None,
        department=None,
        office_location=None,
        mobile_phone=None,
        account_enabled=enabled,
        user_type="Member",
    )


class _Directory:
    def __init__(self, *people: OrgUser) -> None:
        self.people = {p.object_id: p for p in people}
        self.lookups: list[str] = []

    async def get_user(self, object_id: str) -> OrgUser:
        self.lookups.append(object_id)
        if object_id not in self.people:
            raise GraphError("not found")
        return self.people[object_id]


# ── resolving something already local ──────────────────────────────────


async def test_finds_an_existing_user_by_local_id(db) -> None:
    user = await upsert_user(
        db, EntraIdentity(object_id="oid-1", email="a@hamdaz.com", display_name="A")
    )
    await db.commit()

    directory = _Directory()
    found = await resolve_user(db, directory, str(user.id))

    assert found.id == user.id
    assert directory.lookups == []  # no network call needed


async def test_finds_an_existing_user_by_entra_object_id(db) -> None:
    user = await upsert_user(
        db, EntraIdentity(object_id="oid-1", email="a@hamdaz.com", display_name="A")
    )
    await db.commit()

    directory = _Directory()
    found = await resolve_user(db, directory, "oid-1")

    assert found.id == user.id
    assert directory.lookups == []


# ── provisioning someone new ───────────────────────────────────────────


async def test_provisions_a_person_who_has_never_signed_in(db) -> None:
    oid = str(uuid.uuid4())
    directory = _Directory(_org_user(oid, "Sujeel"))

    user = await resolve_user(db, directory, oid)

    assert user.id is not None
    assert user.entra_object_id == oid
    assert user.email == "sujeel@hamdaz.com"
    assert user.display_name == "Sujeel"
    assert user.last_login_at is None  # they have not actually logged in


async def test_provisioning_uses_the_upn_when_there_is_no_mailbox(db) -> None:
    oid = str(uuid.uuid4())
    directory = _Directory(_org_user(oid, "Service", mail=None))

    user = await resolve_user(db, directory, oid)
    assert user.email == "service@hamdaz.com"


async def test_a_disabled_directory_account_provisions_inactive(db) -> None:
    """Entra is authoritative: do not hand a departed employee an active row."""
    oid = str(uuid.uuid4())
    directory = _Directory(_org_user(oid, "Gone", enabled=False))

    user = await resolve_user(db, directory, oid)
    assert user.is_active is False


async def test_resolving_twice_does_not_duplicate(db) -> None:
    oid = str(uuid.uuid4())
    directory = _Directory(_org_user(oid))

    first = await resolve_user(db, directory, oid)
    await db.commit()
    second = await resolve_user(db, directory, oid)

    assert first.id == second.id
    assert await db.scalar(select(func.count()).select_from(User)) == 1
    assert len(directory.lookups) == 1  # second call was served locally


async def test_signing_in_later_reuses_the_provisioned_row(db) -> None:
    """The whole point of storing the object id: no duplicate on first login."""
    oid = str(uuid.uuid4())
    directory = _Directory(_org_user(oid, "Sujeel"))

    provisioned = await resolve_user(db, directory, oid)
    await db.commit()

    signed_in = await upsert_user(
        db,
        EntraIdentity(object_id=oid, email="sujeel@hamdaz.com", display_name="Sujeel Mohammed Ali"),
    )
    await db.commit()

    assert signed_in.id == provisioned.id
    assert await db.scalar(select(func.count()).select_from(User)) == 1
    assert signed_in.display_name == "Sujeel Mohammed Ali"  # Entra wins on sign-in


# ── refusals ───────────────────────────────────────────────────────────


async def test_an_id_in_neither_place_is_refused(db) -> None:
    with pytest.raises(UserNotResolvableError, match="neither a local user"):
        await resolve_user(db, _Directory(), str(uuid.uuid4()))


async def test_a_malformed_id_is_refused(db) -> None:
    with pytest.raises(UserNotResolvableError, match="not a valid id"):
        await resolve_user(db, _Directory(), "not-a-uuid")


async def test_nothing_is_created_when_resolution_fails(db) -> None:
    with pytest.raises(UserNotResolvableError):
        await resolve_user(db, _Directory(), str(uuid.uuid4()))
    assert await db.scalar(select(func.count()).select_from(User)) == 0
