"""Who is emailed an approval request, as a super admin sets it.

Only the mail list is under test: who may decide a quote is unchanged. The
mailer is the recording stub; nothing is sent.
"""

from __future__ import annotations

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.quoting import service
from app.roles import service as roles_service
from tests.test_quoting_routes import (  # noqa: F401 - the fixtures come along
    API,
    _as,
    _priced,
    quoting,
    requester,
    team,
)

SETTINGS = f"{API}/approval-settings"


async def _person(db, email: str, *global_roles: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in global_roles:
        await roles_service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


async def test_only_a_super_admin_reads_or_changes_them(quoting, requester) -> None:
    assert (await _as(quoting, requester).get(SETTINGS)).status_code == 403
    assert (await quoting.put(SETTINGS, json={"notify_ceo": True})).status_code == 403


async def test_the_ceo_is_off_until_switched_on(quoting, db, requester, team) -> None:
    ceo = await _person(db, "ceo-rules@hamdaz.com", "ceo")
    admin = await _person(db, "admin-rules@hamdaz.com", "super_admin")

    before = {p.id for p in await service.approvers_to_notify(db, team.id)}
    assert ceo.id not in before

    saved = await _as(quoting, admin).put(
        SETTINGS,
        json={
            "notify_team_approvers": True,
            "notify_team_managers": True,
            "notify_managers": True,
            "notify_ceo": True,
            "notify_super_admins": False,
            "extra_emails": ["Approvals@Hamdaz.com", "approvals@hamdaz.com"],
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["notify_ceo"] is True
    assert body["extra_emails"] == ["approvals@hamdaz.com"]
    assert body["updated_by_name"] == "admin-rules"
    preview = next(t for t in body["preview"] if t["team_id"] == str(team.id))
    assert "ceo-rules" in preview["recipients"]
    assert "approvals@hamdaz.com" in preview["recipients"]
    # The super admin who saved it is not on the list: that switch is off.
    assert "admin-rules" not in preview["recipients"]

    db.expire_all()
    after = await service.approvers_to_notify(db, team.id)
    assert ceo.id in {p.id for p in after}
    assert "approvals@hamdaz.com" in {p.email for p in after}


async def test_a_bad_address_is_refused(quoting, db) -> None:
    admin = await _person(db, "admin-bad@hamdaz.com", "super_admin")
    response = await _as(quoting, admin).put(SETTINGS, json={"extra_emails": ["not an email"]})
    assert response.status_code == 400
    assert "not an email address" in response.json()["detail"]


async def test_the_approval_mail_follows_the_settings(quoting, db, requester, team) -> None:
    admin = await _person(db, "admin-mail@hamdaz.com", "super_admin")
    await _as(quoting, admin).put(
        SETTINGS, json={"notify_managers": False, "extra_emails": ["approvals@hamdaz.com"]}
    )
    quote_id = await _priced(quoting, requester, team)
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    sent = await quoting.post(f"{API}/{quote_id}/submit")

    assert sent.status_code == 200, sent.text
    assert "approvals@hamdaz.com" in mailer.sent[-1]["recipients"]
