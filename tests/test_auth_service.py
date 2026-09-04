"""Turning a verified identity into exactly one user row."""

from __future__ import annotations

from sqlalchemy import func, select

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.models import User


def _identity(**over) -> EntraIdentity:
    return EntraIdentity(
        **{
            "object_id": "oid-1",
            "email": "person@hamdaz.com",
            "display_name": "A Person",
            **over,
        }
    )


async def test_creates_a_user_on_first_sign_in(db) -> None:
    user = await upsert_user(db, _identity())
    await db.commit()
    assert user.id is not None
    assert user.email == "person@hamdaz.com"
    assert user.is_active is True
    assert user.last_login_at is not None


async def test_second_sign_in_reuses_the_same_row(db) -> None:
    first = await upsert_user(db, _identity())
    await db.commit()
    second = await upsert_user(db, _identity())
    await db.commit()

    assert first.id == second.id
    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def test_email_change_in_entra_flows_through(db) -> None:
    """Entra is authoritative: a rename there must not create a second person."""
    original = await upsert_user(db, _identity())
    await db.commit()
    renamed = await upsert_user(db, _identity(email="new.name@hamdaz.com"))
    await db.commit()

    assert renamed.id == original.id
    assert renamed.email == "new.name@hamdaz.com"
    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def test_display_name_change_flows_through(db) -> None:
    await upsert_user(db, _identity())
    await db.commit()
    updated = await upsert_user(db, _identity(display_name="A Renamed Person"))
    await db.commit()
    assert updated.display_name == "A Renamed Person"


async def test_matches_on_object_id_not_email(db) -> None:
    """Two different people must never merge because an address was reused.

    When someone leaves and their address is handed to a new hire, the new hire
    arrives with a different object id and must get their own row.
    """
    leaver = await upsert_user(db, _identity(object_id="oid-leaver"))
    await db.commit()
    leaver_id = leaver.id

    # Free the address, as an admin would before reassigning it.
    leaver.email = "leaver.archived@hamdaz.com"
    await db.commit()

    joiner = await upsert_user(db, _identity(object_id="oid-joiner"))
    await db.commit()

    assert joiner.id != leaver_id
    assert await db.scalar(select(func.count()).select_from(User)) == 2


async def test_adopts_an_existing_row_that_predates_its_object_id(db) -> None:
    """A user seeded by email before they ever signed in gets claimed, not duplicated."""
    seeded = User(entra_object_id="placeholder", email="person@hamdaz.com", display_name="Seed")
    db.add(seeded)
    await db.commit()

    # Same address, real object id arriving for the first time.
    signed_in = await upsert_user(db, _identity(object_id="oid-real"))
    await db.commit()

    assert signed_in.id == seeded.id
    assert signed_in.entra_object_id == "oid-real"
    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def test_last_login_advances_on_each_sign_in(db) -> None:
    user = await upsert_user(db, _identity())
    await db.commit()
    first_seen = user.last_login_at

    await upsert_user(db, _identity())
    await db.commit()
    assert user.last_login_at >= first_seen
