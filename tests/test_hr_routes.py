"""The HR HTTP surface, and the wall between it and the candidate-facing one.

Two things are worth testing from the outside rather than at the service.

**Who gets in.** HR data is the most sensitive in this system, so every
endpoint is attacked here by somebody who should not reach it — a colleague, a
global admin who is not HR, the subject of a review — rather than trusted to the
dependency being wired up.

**What the public side gives away.** The candidate endpoints are the only
unauthenticated ones in the application. The tests below assert on the whole
serialised body, not on a status code: what matters is that no internal id, no
score and no ERP URL is in it, and a test that only checked for 200 would not
notice any of them appearing.
"""

from __future__ import annotations

import io
import json
import re

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.forms import service as forms_service
from app.forms.catalogue import REVIEW_FIELDS
from app.leave import service as leave_service
from app.roles import service as roles_service
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/hr"

def cv(name: str = "cv.pdf", body: bytes = b"%PDF-1.4 cv"):
    """A fresh upload every call.

    A module-level ``BytesIO`` cannot be shared: httpx consumes it building the
    first multipart body, and every later request then posts an empty file —
    which fails as a missing answer rather than as anything about uploads.
    """
    return ("cv", (name, io.BytesIO(body), "application/pdf"))


def offer():
    return ("file", ("offer.pdf", io.BytesIO(b"%PDF-1.4 offer"), "application/pdf"))

APPLICATION = {
    "candidate_name": "Rania Haddad",
    "candidate_email": "rania@example.com",
    "candidate_phone": "+971500000000",
    "years_experience": "8",
    "qualification": "Masters",
    "notice_period": "1 month",
}


def _as(client, user):
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


async def _person(db, handle: str):
    return await upsert_user(
        db,
        EntraIdentity(
            object_id=f"{handle}@hamdaz.com",
            email=f"{handle}@hamdaz.com",
            display_name=handle.title(),
        ),
    )


@pytest.fixture
async def hr_team(db):
    """The HR team, which is what "being HR" means here."""
    await roles_service.seed_system_roles(db)
    await forms_service.seed_templates(db)
    team = await teams.create_team(db, name="HR", slug="hr")
    await leave_service.update_settings(db, hr_team_slug="hr")
    await db.commit()
    return team


@pytest.fixture
async def hr(db, hr_team):
    user = await _person(db, "hr")
    await teams.set_member_roles(db, team=hr_team, user=user, role_keys=["member"])
    await db.commit()
    return user


@pytest.fixture
async def colleague(db, hr_team):
    """Signed in, on no team. Everything HR is closed to them."""
    user = await _person(db, "engineer")
    await db.commit()
    return user


@pytest.fixture
async def boss(db, hr_team):
    """A global admin who is not on the HR team.

    Deliberately a ``manager``: the point is that organisation-wide authority
    over teams and work is not authority over personnel files.
    """
    user = await _person(db, "boss")
    await roles_service.assign_role(
        db, user_id=user.id, role_key="manager", granted_by_id=user.id
    )
    await db.commit()
    return user


async def _posted_opening(client, hr, **extra):
    created = (
        await _as(client, hr).post(
            f"{API}/openings",
            json={"title": "Senior Estimator", "location": "Abu Dhabi",
                  "summary": "Price the bids.", **extra},
        )
    ).json()
    return (await client.post(f"{API}/openings/{created['id']}/post")).json()


# ── who gets in ────────────────────────────────────────────────────────


async def test_a_colleague_cannot_see_openings(client, colleague):
    assert (await _as(client, colleague).get(f"{API}/openings")).status_code == 403


async def test_a_global_manager_is_not_hr(client, boss):
    """Managing teams is not the same authority as holding the personnel files."""
    response = await _as(client, boss).get(f"{API}/openings")
    assert response.status_code == 403
    assert "HR team" in response.json()["detail"]


async def test_hr_gets_in(client, hr):
    assert (await _as(client, hr).get(f"{API}/openings")).status_code == 200


async def test_an_anonymous_caller_gets_401_not_403(client):
    client.cookies.clear()
    assert (await client.get(f"{API}/openings")).status_code == 401


# ── posting a job ──────────────────────────────────────────────────────


async def test_creating_an_opening_gives_no_link_until_it_is_posted(client, hr):
    created = (
        await _as(client, hr).post(f"{API}/openings", json={"title": "Site Engineer"})
    ).json()
    assert created["status"] == "draft"
    assert created["share_url"] is None, "a link to a draft goes nowhere"
    assert created["template_name"] == "Job application"

    posted = (await client.post(f"{API}/openings/{created['id']}/post")).json()
    assert posted["status"] == "open"
    assert posted["accepts_applications"] is True
    assert "/apply/" in posted["share_url"]
    assert posted["share_url"].split("/apply/")[1] not in posted["slug"]


async def test_the_share_link_is_not_the_slug(client, hr):
    """Guessing the job title must not be enough to reach the form."""
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    assert len(token) >= 32
    assert (await client.get(f"/apply/{posted['slug']}/form")).status_code == 404


async def test_rotating_the_link_kills_the_old_one(client, hr):
    posted = await _posted_opening(client, hr)
    old = posted["share_url"].rsplit("/", 1)[-1]
    rotated = (await client.post(f"{API}/openings/{posted['id']}/rotate-link")).json()
    new = rotated["share_url"].rsplit("/", 1)[-1]

    assert new != old
    assert (await client.get(f"/apply/{old}/form")).status_code == 404
    assert (await client.get(f"/apply/{new}/form")).status_code == 200


# ── the candidate side ─────────────────────────────────────────────────


async def test_the_public_form_carries_no_internal_anything(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]

    client.cookies.clear()
    response = await client.get(f"/apply/{token}/form")
    assert response.status_code == 200
    body = response.json()

    assert body["title"] == "Senior Estimator"
    for leaked in ("id", "template_id", "template_version", "headcount", "reference",
                   "publicly_listed", "created_by_name", "application_count"):
        assert leaked not in body, f"{leaked} must not reach a candidate"

    # And no field tells the candidate how it is marked.
    for field in body["fields"]:
        assert "scoring" not in field
        assert "maps_to" not in field


async def test_the_public_form_sets_no_cookie_and_allows_no_credentials(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]

    client.cookies.clear()
    response = await client.get(f"/apply/{token}/form")
    assert "set-cookie" not in {k.casefold() for k in response.headers}
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_the_hosted_page_contains_no_route_into_the_erp(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]

    client.cookies.clear()
    page = await client.get(f"/apply/{token}")
    assert page.status_code == 200
    html = page.text

    assert "Senior Estimator" in html
    assert "noindex" in html
    # The only URL on the page is where the form posts to, which is this same
    # token. Nothing links to the API, the frontend, or anything else.
    urls = set(re.findall(r'(?:href|src|action)="([^"]+)"', html))
    assert all(token in url for url in urls), urls
    assert "/api/v1" not in html
    assert get_settings().frontend_url not in html


async def test_a_candidate_can_apply_and_hr_sees_it_scored(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]

    client.cookies.clear()
    submitted = await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])
    assert submitted.status_code == 200
    body = submitted.json()
    assert body == {
        "received": True,
        "message": body["message"],
        "candidate_email": "rania@example.com",
    }
    assert "Senior Estimator" in body["message"]

    applications = (await _as(client, hr).get(f"{API}/applications")).json()
    assert len(applications) == 1
    application = applications[0]
    assert application["candidate_name"] == "Rania Haddad"
    assert application["stage"] == "new"
    assert application["score_percent"] is not None
    assert {t["tag"] for t in application["score"]["tags"]} >= {"experience", "qualification"}
    assert [a["file_name"] for a in application["attachments"]] == ["cv.pdf"]


async def test_a_candidate_is_told_nothing_back_about_the_record(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    body = (await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])).json()
    assert "id" not in body and "score" not in body


async def test_applying_without_the_required_cv_says_which_field(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    refused = await client.post(f"/apply/{token}", data=APPLICATION)
    assert refused.status_code == 400
    assert "CV" in refused.json()["detail"]


async def test_an_executable_upload_is_refused(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    refused = await client.post(
        f"/apply/{token}",
        data=APPLICATION,
        files=[cv("cv.exe", b"MZ")],
    )
    assert refused.status_code == 400
    assert "not a type we accept" in refused.json()["detail"]


async def test_a_draft_opening_is_not_reachable_publicly(client, hr):
    created = (
        await _as(client, hr).post(f"{API}/openings", json={"title": "Quiet role"})
    ).json()
    detail = (await client.get(f"{API}/openings/{created['id']}")).json()
    # HR can see the record; nothing in the response hands out a live link.
    assert detail["share_url"] is None


async def test_a_closed_opening_says_so_rather_than_vanishing(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    await client.post(f"{API}/openings/{posted['id']}/close")

    client.cookies.clear()
    body = (await client.get(f"/apply/{token}/form")).json()
    assert body["accepting"] is False
    assert "no longer accepting" in body["closed_message"]
    assert (await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])).status_code == 400


async def test_only_listed_openings_appear_on_the_careers_page(client, hr):
    await _posted_opening(client, hr)
    listed = await _posted_opening(client, hr, title="Site Engineer", publicly_listed=True)

    client.cookies.clear()
    rows = (await client.get("/careers/openings")).json()
    assert [r["title"] for r in rows] == ["Site Engineer"]
    assert listed["share_url"].rsplit("/", 1)[-1] in rows[0]["apply_url"]

    page = await client.get("/careers")
    assert "Site Engineer" in page.text
    assert "Senior Estimator" not in page.text


# ── moving a candidate along ───────────────────────────────────────────


async def test_hr_moves_a_candidate_and_records_who_decided(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])

    application = (await _as(client, hr).get(f"{API}/applications")).json()[0]
    moved = (
        await client.post(
            f"{API}/applications/{application['id']}/stage",
            json={"stage": "shortlisted", "note": "Strong on estimating"},
        )
    ).json()
    assert moved["stage"] == "shortlisted"
    assert moved["decided_by_name"] == "Hr"
    assert moved["stage_note"] == "Strong on estimating"


async def test_a_candidates_file_cannot_be_fetched_through_another_application(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])

    application = (await _as(client, hr).get(f"{API}/applications")).json()[0]
    file_id = application["attachments"][0]["id"]

    ok = await client.get(f"{API}/applications/{application['id']}/files/{file_id}")
    assert ok.status_code == 200
    assert ok.content.startswith(b"%PDF")
    assert ok.headers["content-disposition"].startswith("attachment;")

    import uuid

    wrong = await client.get(f"{API}/applications/{uuid.uuid4()}/files/{file_id}")
    assert wrong.status_code == 404


# ── employee documents ─────────────────────────────────────────────────


async def _file_offer(client, hr, employee, **extra):
    return (
        await _as(client, hr).post(
            f"{API}/people/{employee.id}/documents",
            data={"kind": "offer_letter", "title": "Offer letter", **extra},
            files=[offer()],
        )
    ).json()


async def test_hr_files_an_offer_letter_and_the_employee_can_read_it(
    client, hr, colleague
):
    document = await _file_offer(client, hr, colleague)
    assert document["kind"] == "offer_letter"
    assert document["user_name"] == "Engineer"
    assert document["uploaded_by_name"] == "Hr"

    mine = (await _as(client, colleague).get(f"{API}/me/documents")).json()
    assert [d["id"] for d in mine] == [document["id"]]
    download = await client.get(f"{API}/documents/{document['id']}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    assert download.headers["cache-control"] == "private, no-store"


async def test_a_document_hr_has_not_shared_is_invisible_to_its_subject(
    client, hr, colleague
):
    document = await _file_offer(
        client, hr, colleague, kind="warning", visible_to_employee="false"
    )
    assert (await _as(client, colleague).get(f"{API}/me/documents")).json() == []
    # 404, not 403: that a document exists on somebody is itself not theirs to learn.
    assert (await client.get(f"{API}/documents/{document['id']}")).status_code == 404
    assert (await client.get(f"{API}/documents/{document['id']}/download")).status_code == 404


async def test_a_colleague_cannot_read_somebody_elses_documents(db, client, hr, colleague):
    document = await _file_offer(client, hr, colleague)
    outsider = await _person(db, "nosy")
    await db.commit()
    assert (
        await _as(client, outsider).get(f"{API}/documents/{document['id']}")
    ).status_code == 404
    assert (
        await _as(client, outsider).get(f"{API}/documents?user_id={colleague.id}")
    ).status_code == 403


async def test_only_hr_uploads(client, colleague):
    assert (
        await _as(client, colleague).post(
            f"{API}/people/{colleague.id}/documents",
            data={"kind": "offer_letter"},
            files=[offer()],
        )
    ).status_code == 403


# ── performance reviews ────────────────────────────────────────────────


async def _cycle_with_review(client, hr, subject, reviewer):
    cycle = (
        await _as(client, hr).post(
            f"{API}/review-cycles", json={"name": "2026 mid-year"}
        )
    ).json()
    reviews = (
        await client.post(
            f"{API}/review-cycles/{cycle['id']}/nominations",
            json={"nominations": [
                {"subject_id": str(subject.id), "reviewer_id": str(reviewer.id),
                 "relation": "manager"}
            ]},
        )
    ).json()
    await client.post(f"{API}/review-cycles/{cycle['id']}/open")
    return cycle, reviews[0]


def _full_answers():
    return {
        **{f["key"]: "Outstanding" for f in REVIEW_FIELDS if f.get("type") == "select"},
        "strengths": "Everything",
        "development": "Nothing",
    }


async def test_the_nominated_reviewer_fills_it_in_and_gets_the_questions(
    client, hr, colleague, boss
):
    _, review = await _cycle_with_review(client, hr, colleague, boss)

    mine = (await _as(client, boss).get(f"{API}/reviews/mine")).json()
    assert [r["id"] for r in mine] == [review["id"]]
    assert mine[0]["subject_name"] == "Engineer"

    full = (await client.get(f"{API}/reviews/{review['id']}")).json()
    assert full["fields"], "the form comes with the review"

    submitted = (
        await client.put(
            f"{API}/reviews/{review['id']}",
            json={"answers": _full_answers(), "comment": "A good year", "submit": True},
        )
    ).json()
    assert submitted["status"] == "submitted"
    assert float(submitted["score_percent"]) == 100.0
    assert {t["tag"] for t in submitted["score"]["tags"]} >= {"technical", "communication"}


async def test_hr_cannot_write_somebody_elses_review(client, hr, colleague, boss):
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    refused = await _as(client, hr).put(
        f"{API}/reviews/{review['id']}", json={"answers": _full_answers(), "submit": True}
    )
    assert refused.status_code == 403
    assert "nominated reviewer" in refused.json()["detail"]


async def test_a_subject_sees_that_a_review_exists_but_not_what_it_says(
    client, hr, colleague, boss
):
    cycle, review = await _cycle_with_review(client, hr, colleague, boss)
    await _as(client, boss).put(
        f"{API}/reviews/{review['id']}", json={"answers": _full_answers(), "submit": True}
    )

    about_me = (await _as(client, colleague).get(f"{API}/reviews/about-me")).json()
    assert len(about_me) == 1
    assert about_me[0]["reviewer_name"] == "Boss"
    assert about_me[0]["answers"] is None, "not shared yet"
    assert about_me[0]["score"] is None

    await _as(client, hr).post(f"{API}/review-cycles/{cycle['id']}/sharing?shared=true")
    shared = (await _as(client, colleague).get(f"{API}/reviews/about-me")).json()
    assert shared[0]["answers"], "shared now"


async def test_an_unshared_draft_never_reaches_its_subject(client, hr, colleague, boss):
    cycle, review = await _cycle_with_review(client, hr, colleague, boss)
    await _as(client, hr).post(f"{API}/review-cycles/{cycle['id']}/sharing?shared=true")
    # Saved, not submitted. Sharing the cycle must not expose work in progress.
    await _as(client, boss).put(
        f"{API}/reviews/{review['id']}", json={"answers": {"technical": "Below expectation"}}
    )
    about_me = (await _as(client, colleague).get(f"{API}/reviews/about-me")).json()
    assert about_me[0]["status"] == "draft"
    assert about_me[0]["answers"] is None


async def test_a_third_party_sees_nothing(db, client, hr, colleague, boss):
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    nosy = await _person(db, "nosy")
    await db.commit()
    assert (await _as(client, nosy).get(f"{API}/reviews/{review['id']}")).status_code == 404
    assert (await _as(client, nosy).get(f"{API}/reviews/mine")).json() == []


async def test_somebody_can_read_their_own_performance_but_not_a_colleagues(
    client, hr, colleague, boss
):
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    await _as(client, boss).put(
        f"{API}/reviews/{review['id']}", json={"answers": _full_answers(), "submit": True}
    )

    mine = (await _as(client, colleague).get(f"{API}/me/performance")).json()
    assert mine["reviews"] == 1 and mine["percent"] == 100.0
    assert {t["tag"] for t in mine["tags"]} >= {"technical", "delivery"}

    assert (
        await _as(client, colleague).get(f"{API}/people/{boss.id}/performance")
    ).status_code == 403
    assert (
        await _as(client, hr).get(f"{API}/people/{colleague.id}/performance")
    ).status_code == 200


async def test_a_cycle_cannot_open_empty(client, hr):
    cycle = (
        await _as(client, hr).post(f"{API}/review-cycles", json={"name": "Empty"})
    ).json()
    refused = await client.post(f"{API}/review-cycles/{cycle['id']}/open")
    assert refused.status_code == 400
    assert "Nobody has been nominated" in refused.json()["detail"]


async def test_meta_lists_every_form_hr_can_choose_between(client, hr):
    body = (await _as(client, hr).get(f"{API}/meta")).json()
    assert "offer_letter" in body["document_kinds"]

    applications = body["application_forms"]["templates"]
    names = {t["key"] for t in applications}
    assert {"job_application", "job_application_short", "job_application_technical"} <= names
    # Exactly one default, and it is the canonical one.
    default = [t for t in applications if t["is_default"]]
    assert [t["key"] for t in default] == ["job_application"]

    reviews = {t["key"] for t in body["review_forms"]["templates"]}
    assert {"performance_review", "probation_review", "leadership_review"} <= reviews
    assert body["posting_forms"]["templates"][0]["key"] == "job_posting"


async def test_hr_fills_in_the_posting_form_and_the_advert_reaches_the_candidate(
    client, hr
):
    """The advert is a form a super admin wrote, and the internal half stays in."""
    created = (
        await _as(client, hr).post(
            f"{API}/openings",
            json={
                "title": "Senior Estimator",
                "location": "Abu Dhabi",
                "details": {
                    "summary": "Price the bids that win us work.",
                    "description": "Own estimating end to end.",
                    "requirements": "Five years in MEP estimating.",
                    "salary_range": "AED 18,000-22,000",
                    "benefits": "Visa, insurance, annual ticket.",
                    "reports_to": "Head of Estimating",
                    "hiring_manager": "Sami",
                    "budget_code": "OPS-2026-14",
                    "sourcing_notes": "Try the agency first",
                },
            },
        )
    ).json()
    assert created["posting_template_name"] == "Job posting"
    assert created["posting_fields"], "the form comes with the opening it describes"
    assert created["details"]["budget_code"] == "OPS-2026-14"
    assert created["summary"] == "Price the bids that win us work."

    posted = (await client.post(f"{API}/openings/{created['id']}/post")).json()
    token = posted["share_url"].rsplit("/", 1)[-1]

    client.cookies.clear()
    body = (await client.get(f"/apply/{token}/form")).json()
    published = {d["key"]: d["value"] for d in body["details"]}
    assert published["benefits"] == "Visa, insurance, annual ticket."
    assert published["reports_to"] == "Head of Estimating"
    # The internal half of the form never leaves.
    for internal in ("hiring_manager", "budget_code", "sourcing_notes", "approved_by"):
        assert internal not in published
    # Not merely absent from the keys: nowhere in the response at all.
    assert "Sami" not in json.dumps(body) and "OPS-2026-14" not in json.dumps(body)
    # And the four with columns are not repeated inside details.
    assert "summary" not in published and "salary_range" not in published

    page = await client.get(f"/apply/{token}")
    assert "Visa, insurance, annual ticket." in page.text
    assert "OPS-2026-14" not in page.text
    assert "Sami" not in page.text



async def test_an_opening_cannot_be_posted_with_a_half_written_advert(client, hr):
    created = (
        await _as(client, hr).post(
            f"{API}/openings",
            json={"title": "Half written", "details": {"summary": "Just this"}},
        )
    ).json()
    refused = await client.post(f"{API}/openings/{created['id']}/post")
    assert refused.status_code == 400
    assert "Please fill in" in refused.json()["detail"]


# ── deletion is super admin only ───────────────────────────────────────


@pytest.fixture
async def root(db, hr_team):
    """A super admin who is deliberately NOT on the HR team.

    The point of the fixture: reaching deletion must not depend on HR
    membership, and HR membership must not confer it.
    """
    user = await _person(db, "root")
    await roles_service.assign_role(
        db, user_id=user.id, role_key="super_admin", granted_by_id=user.id
    )
    await db.commit()
    return user


async def _an_opening_with_an_application(client, hr):
    posted = await _posted_opening(client, hr)
    token = posted["share_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    await client.post(f"/apply/{token}", data=APPLICATION, files=[cv()])
    return posted


async def test_hr_cannot_delete_anything(client, hr, colleague):
    """Being on the HR team is explicitly not enough."""
    posted = await _an_opening_with_an_application(client, hr)
    application = (await _as(client, hr).get(f"{API}/applications")).json()[0]
    document = await _file_offer(client, hr, colleague)
    cycle = (
        await _as(client, hr).post(f"{API}/review-cycles", json={"name": "2026"})
    ).json()

    for path in (
        f"{API}/openings/{posted['id']}",
        f"{API}/applications/{application['id']}",
        f"{API}/documents/{document['id']}",
        f"{API}/review-cycles/{cycle['id']}",
        f"{API}/people/{colleague.id}/hr-data",
    ):
        response = await _as(client, hr).delete(path)
        assert response.status_code == 403, path
        assert "super admin" in response.json()["detail"]


async def test_a_colleague_cannot_delete_anything(client, hr, colleague):
    posted = await _an_opening_with_an_application(client, hr)
    assert (
        await _as(client, colleague).delete(f"{API}/openings/{posted['id']}")
    ).status_code == 403


async def test_a_super_admin_deletes_an_opening_and_is_told_what_went(
    client, hr, root
):
    posted = await _an_opening_with_an_application(client, hr)

    response = await _as(client, root).delete(f"{API}/openings/{posted['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["openings"] == 1 and body["applications"] == 1 and body["files"] == 1
    assert body["summary"] == (
        "Deleted 1 job opening, 1 application and 1 candidate file."
    )
    assert (await _as(client, hr).get(f"{API}/openings")).json() == []


async def test_a_super_admin_deletes_a_document_hr_filed(client, hr, colleague, root):
    document = await _file_offer(client, hr, colleague)
    response = await _as(client, root).delete(f"{API}/documents/{document['id']}")
    assert response.status_code == 200
    assert response.json()["summary"] == "Deleted 1 document."
    assert (await _as(client, colleague).get(f"{API}/me/documents")).json() == []


async def test_a_super_admin_purges_one_person(client, hr, colleague, root):
    await _file_offer(client, hr, colleague)
    response = await _as(client, root).delete(f"{API}/people/{colleague.id}/hr-data")
    assert response.status_code == 200
    assert response.json()["documents"] == 1
    # The person themselves is untouched — deactivating is the teams module's job.
    assert (await _as(client, colleague).get(f"{API}/me/documents")).status_code == 200


async def test_hr_can_still_withdraw_an_untouched_nomination(client, hr, colleague, boss):
    """The ordinary correction stays with HR; it destroys nothing."""
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    assert (
        await _as(client, hr).post(f"{API}/reviews/{review['id']}/withdraw")
    ).status_code == 204


async def test_hr_cannot_withdraw_a_submitted_review(client, hr, colleague, boss):
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    await _as(client, boss).put(
        f"{API}/reviews/{review['id']}", json={"answers": _full_answers(), "submit": True}
    )
    refused = await _as(client, hr).post(f"{API}/reviews/{review['id']}/withdraw")
    assert refused.status_code == 400
    assert "already started writing" in refused.json()["detail"]


async def test_a_super_admin_can_delete_a_submitted_review(
    client, hr, colleague, boss, root
):
    """No status guard on the delete: it is done on purpose, and it is logged."""
    _, review = await _cycle_with_review(client, hr, colleague, boss)
    await _as(client, boss).put(
        f"{API}/reviews/{review['id']}", json={"answers": _full_answers(), "submit": True}
    )
    response = await _as(client, root).delete(f"{API}/reviews/{review['id']}")
    assert response.status_code == 200
    assert response.json()["summary"] == "Deleted 1 review."
