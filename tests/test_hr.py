"""The HR service, and the scoring underneath it.

The scoring tests come first and are the longest, because scoring is the part
of this module that is quietly wrong rather than loudly broken. A permission
bug throws a 403 somebody notices; a scoring bug produces a plausible number
that ends up in a decision about somebody's job.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.forms import scoring
from app.forms import service as forms_service
from app.forms.catalogue import (
    JOB_APPLICATION,
    JOB_POSTING,
    PERFORMANCE_REVIEW,
    REVIEW_FIELDS,
)
from app.hr import service
from app.hr.documents import Upload, UploadError, accept, safe_name
from app.models.hr import ApplicationStage, CycleStatus, DocumentKind, OpeningStatus, ReviewStatus
from app.models.templates import TemplateStatus

# ── scoring ────────────────────────────────────────────────────────────


RATED = [
    {"key": "a", "label": "A", "type": "select", "options": ["lo", "hi"],
     "scoring": {"tags": ["skill"], "max": 5, "option_scores": {"lo": 1, "hi": 5}}},
    {"key": "b", "label": "B", "type": "select", "options": ["lo", "hi"],
     "scoring": {"tags": ["skill", "pace"], "max": 5, "weight": 2,
                 "option_scores": {"lo": 1, "hi": 5}}},
    {"key": "n", "label": "N", "type": "number", "scoring": {"tags": ["pace"], "max": 10}},
    {"key": "c", "label": "C", "type": "checkbox", "scoring": {"tags": ["extra"], "max": 2}},
    {"key": "free", "label": "Free", "type": "textarea"},
]


def test_score_totals_and_tags():
    result = scoring.score(RATED, {"a": "hi", "b": "lo", "n": 8, "c": True})
    # a: 5*1, b: 1*2, n: 8*1, c: 2*1 = 17 out of 5 + 10 + 10 + 2 = 27
    assert result.points == pytest.approx(17.0)
    assert result.max == pytest.approx(27.0)
    assert result.percent == pytest.approx(63.0)
    assert result.answered == 4 and result.skipped == 0

    # b feeds both skill and pace at its full weight, not half to each.
    assert result.tags["skill"].points == pytest.approx(7.0)
    assert result.tags["skill"].max == pytest.approx(15.0)
    assert result.tags["pace"].points == pytest.approx(10.0)


def test_unanswered_is_not_a_zero():
    """The whole point of the design: a skipped question changes nothing."""
    both = scoring.score(RATED, {"a": "hi", "b": "hi"})
    one = scoring.score(RATED, {"a": "hi"})
    assert both.percent == one.percent == 100.0
    assert one.skipped == 3


def test_nothing_scored_is_none_not_zero():
    result = scoring.score(RATED, {"free": "words"})
    assert result.percent is None, "no answers must not read as a bad score"


def test_checkbox_false_is_an_answer_worth_nothing():
    result = scoring.score(RATED, {"c": False})
    assert result.answered == 1
    assert result.points == 0.0 and result.max == 2.0
    assert result.percent == 0.0


def test_number_is_clamped_to_its_ceiling():
    assert scoring.score(RATED, {"n": 500}).points == 10.0
    assert scoring.score(RATED, {"n": -5}).points == 0.0


def test_option_the_template_no_longer_offers_is_skipped():
    """An edited template must not invent a zero out of an old answer."""
    result = scoring.score(RATED, {"a": "middling"})
    assert result.answered == 0 and result.skipped == 4
    assert result.percent is None


def test_combine_weights_by_questions_answered_not_by_percentage():
    """Two reviewers, one thorough and one not. The thorough one counts more."""
    thorough = scoring.score(RATED, {"a": "lo", "b": "lo", "n": 0, "c": False}).as_dict()
    brief = scoring.score(RATED, {"c": True}).as_dict()
    combined = scoring.combine([thorough, brief])
    # 3 + 2 points out of 27 + 2 — not the mean of 11.1% and 100%.
    assert combined["percent"] == pytest.approx(17.2, abs=0.1)


def test_tags_of_lists_every_tag_in_field_order():
    assert scoring.tags_of(RATED) == ["skill", "pace", "extra"]
    assert scoring.is_scored(RATED) is True
    assert scoring.is_scored([{"key": "x", "label": "X", "type": "text"}]) is False


def test_scoring_on_free_text_is_refused():
    with pytest.raises(scoring.ScoringError, match="cannot be scored"):
        scoring.validate_block("notes", "textarea", {"tags": ["x"]})


def test_select_needs_option_scores():
    with pytest.raises(scoring.ScoringError, match="option_scores"):
        scoring.validate_block("pick", "select", {"tags": ["x"]})


def test_scoring_needs_a_tag():
    with pytest.raises(scoring.ScoringError, match="at least one tag"):
        scoring.validate_block("n", "number", {"max": 5})


def test_shipped_review_form_scores_every_rated_question():
    tags = scoring.tags_of(REVIEW_FIELDS)
    assert "technical" in tags and "communication" in tags
    perfect = {
        f["key"]: "Outstanding" for f in REVIEW_FIELDS if f.get("type") == "select"
    }
    assert scoring.score(REVIEW_FIELDS, perfect).percent == 100.0


async def test_a_super_admin_cannot_save_an_unusable_scoring_block(db, actor):
    with pytest.raises(forms_service.TemplateError, match="cannot be scored"):
        await forms_service.create(
            db,
            roles={"super_admin"},
            actor=actor,
            key="broken",
            name="Broken",
            kind="broken",
            fields=[{"key": "why", "label": "Why", "type": "textarea",
                     "scoring": {"tags": ["x"]}}],
        )


# ── fixtures ───────────────────────────────────────────────────────────


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
async def actor(db):
    return await _person(db, "hr")


@pytest.fixture
async def templates(db):
    """The shipped forms, keyed by kind.

    Several templates share a kind — the application variants — so this keeps
    the canonical one, which is the one whose key equals its kind.
    """
    seeded = await forms_service.seed_templates(db)
    await db.flush()
    by_kind = {}
    for template in seeded:
        if template.kind not in by_kind or template.key == template.kind:
            by_kind[template.kind] = template
    return by_kind


@pytest.fixture
async def opening(db, actor, templates):
    return await service.create_opening(
        db,
        actor=actor,
        title="Senior Estimator",
        location="Abu Dhabi",
        summary="Price the bids.",
    )


# ── openings ───────────────────────────────────────────────────────────


async def test_an_opening_starts_as_a_draft_with_a_token(db, opening):
    assert opening.status == OpeningStatus.DRAFT
    assert opening.slug == "senior-estimator"
    assert len(opening.public_token) >= 32
    assert opening.accepts_applications is False, "a draft accepts nothing"


async def test_two_openings_with_one_title_get_different_slugs(db, actor, templates):
    second = await service.create_opening(db, actor=actor, title="Senior Estimator")
    third = await service.create_opening(db, actor=actor, title="Senior Estimator")
    assert {second.slug, third.slug} == {"senior-estimator", "senior-estimator-2"}


async def test_posting_makes_it_accept(db, opening, actor):
    posted = await service.post_opening(db, opening, actor=actor)
    assert posted.status == OpeningStatus.OPEN
    assert posted.posted_at is not None
    assert posted.accepts_applications is True


async def test_a_closing_date_in_the_past_cannot_be_posted(db, opening, actor):
    opening.closes_on = date.today() - timedelta(days=1)
    with pytest.raises(service.HRError, match="already passed"):
        await service.post_opening(db, opening, actor=actor)


async def test_a_closing_date_is_inclusive(db, opening, actor):
    opening.closes_on = date.today()
    await service.post_opening(db, opening, actor=actor)
    assert opening.accepts_applications is True, "the closing day is still a day"


async def test_only_a_job_application_template_can_back_an_opening(db, actor, templates):
    with pytest.raises(service.HRError, match="needs a 'job_application'"):
        await service.create_opening(
            db, actor=actor, title="Wrong form",
            template_id=templates[PERFORMANCE_REVIEW].id,
        )


async def test_the_form_cannot_be_swapped_once_it_is_posted(db, opening, actor, templates):
    await service.post_opening(db, opening, actor=actor)
    other = await forms_service.create(
        db, roles={"super_admin"}, actor=actor, key="alt_application",
        name="Alt", kind=JOB_APPLICATION,
        fields=[{"key": "candidate_name", "label": "Name", "type": "text", "required": True},
                {"key": "candidate_email", "label": "Email", "type": "text", "required": True}],
    )
    await forms_service.publish(db, other, roles={"super_admin"}, actor=actor)
    with pytest.raises(service.HRError, match="only be changed while the opening is a draft"):
        await service.update_opening(db, opening, actor=actor, template_id=other.id)


async def test_rotating_the_token_revokes_the_old_link(db, opening, actor):
    await service.post_opening(db, opening, actor=actor)
    old = opening.public_token
    await service.rotate_token(db, opening, actor=actor)
    assert opening.public_token != old
    with pytest.raises(service.NotFoundError):
        await service.by_token(db, old)
    assert (await service.by_token(db, opening.public_token)).id == opening.id


async def test_a_draft_is_not_findable_by_its_token(db, opening):
    """Indistinguishable from a wrong token, so tokens cannot be probed."""
    with pytest.raises(service.NotFoundError):
        await service.by_token(db, opening.public_token)


async def test_by_token_refuses_a_short_token(db):
    with pytest.raises(service.NotFoundError):
        await service.by_token(db, "senior-estimator")


async def test_only_listed_openings_reach_the_careers_list(db, opening, actor, templates):
    await service.post_opening(db, opening, actor=actor)
    assert await service.listed_openings(db) == []

    listed = await service.create_opening(db, actor=actor, title="Site Engineer",
                                          publicly_listed=True)
    await service.post_opening(db, listed, actor=actor)
    assert [o.id for o in await service.listed_openings(db)] == [listed.id]


# ── applications ───────────────────────────────────────────────────────


ANSWERS = {
    "candidate_name": "Rania Haddad",
    "candidate_email": "Rania@Example.com",
    "candidate_phone": "+971 50 000 0000",
    "years_experience": 8,
    "qualification": "Masters",
    "notice_period": "1 month",
}
CV = Upload(file_name="cv.pdf", content_type="application/pdf", content=b"%PDF-1.4 cv")


async def _open(db, opening, actor):
    return await service.post_opening(db, opening, actor=actor)


async def test_an_application_is_scored_and_the_email_lowercased(db, opening, actor):
    await _open(db, opening, actor)
    application = await service.submit_application(
        db, opening, answers=ANSWERS, uploads=[("cv", CV)]
    )
    assert application.candidate_email == "rania@example.com"
    assert application.stage == ApplicationStage.NEW
    assert application.score_percent is not None
    tags = {t["tag"] for t in application.score["tags"]}
    assert {"experience", "qualification", "availability"} <= tags
    assert [a.file_name for a in application.attachments] == ["cv.pdf"]


async def test_a_missing_required_answer_is_refused_by_name(db, opening, actor):
    await _open(db, opening, actor)
    with pytest.raises(service.HRError, match="CV"):
        await service.submit_application(db, opening, answers=ANSWERS, uploads=[])


async def test_a_bad_email_is_refused(db, opening, actor):
    await _open(db, opening, actor)
    with pytest.raises(service.HRError, match="email address"):
        await service.submit_application(
            db, opening, answers={**ANSWERS, "candidate_email": "not-an-email"},
            uploads=[("cv", CV)],
        )


async def test_a_closed_opening_refuses_applications(db, opening, actor):
    await _open(db, opening, actor)
    await service.close_opening(db, opening, actor=actor)
    with pytest.raises(service.HRError, match="no longer accepting"):
        await service.submit_application(db, opening, answers=ANSWERS, uploads=[("cv", CV)])


async def test_resubmission_replaces_while_it_is_still_new(db, opening, actor):
    await _open(db, opening, actor)
    first = await service.submit_application(db, opening, answers=ANSWERS, uploads=[("cv", CV)])
    again = await service.submit_application(
        db, opening,
        answers={**ANSWERS, "years_experience": 12},
        uploads=[("cv", Upload("cv2.pdf", "application/pdf", b"%PDF-1.4 better"))],
    )
    assert again.id == first.id, "one application per person per opening"
    assert again.answers["years_experience"] == 12
    assert [a.file_name for a in again.attachments] == ["cv2.pdf"]


async def test_resubmission_is_refused_once_hr_has_moved_them(db, opening, actor):
    await _open(db, opening, actor)
    application = await service.submit_application(
        db, opening, answers=ANSWERS, uploads=[("cv", CV)]
    )
    await service.move_stage(
        db, application, actor=actor, stage=ApplicationStage.SHORTLISTED
    )
    with pytest.raises(service.HRError, match="being looked at"):
        await service.submit_application(db, opening, answers=ANSWERS, uploads=[("cv", CV)])


async def test_hiring_links_the_candidate_to_the_employee(db, opening, actor):
    await _open(db, opening, actor)
    application = await service.submit_application(
        db, opening, answers=ANSWERS, uploads=[("cv", CV)]
    )
    employee = await _person(db, "rania")
    await service.hire(db, application, actor=actor, user_id=employee.id,
                       close_opening_too=True)
    assert application.stage == ApplicationStage.HIRED
    assert application.hired_user_id == employee.id
    assert opening.status == OpeningStatus.FILLED


async def test_hiring_somebody_with_no_account_says_so(db, opening, actor):
    import uuid

    await _open(db, opening, actor)
    application = await service.submit_application(
        db, opening, answers=ANSWERS, uploads=[("cv", CV)]
    )
    with pytest.raises(service.NotFoundError, match="signs in for the first time"):
        await service.hire(db, application, actor=actor, user_id=uuid.uuid4())


# ── documents ──────────────────────────────────────────────────────────


OFFER = Upload("offer.pdf", "application/pdf", b"%PDF-1.4 offer")


async def test_an_offer_letter_is_filed_against_a_person(db, actor):
    employee = await _person(db, "sami")
    document = await service.add_document(
        db, actor=actor, user_id=employee.id, upload=OFFER,
        kind=DocumentKind.OFFER_LETTER, title="Offer — Sami",
    )
    assert document.kind == DocumentKind.OFFER_LETTER
    assert document.size_bytes == len(OFFER.content)
    assert document.visible_to_employee is True, "people can read their own offer letter"
    assert [d.id for d in await service.list_documents(db, user_id=employee.id)] == [document.id]


async def test_any_other_document_is_the_same_row(db, actor):
    employee = await _person(db, "sami")
    for kind in (DocumentKind.VISA, DocumentKind.CERTIFICATE, DocumentKind.OTHER):
        await service.add_document(db, actor=actor, user_id=employee.id, upload=OFFER, kind=kind)
    assert len(await service.list_documents(db, user_id=employee.id)) == 3


async def test_expiring_documents_can_be_found(db, actor):
    employee = await _person(db, "sami")
    await service.add_document(
        db, actor=actor, user_id=employee.id, upload=OFFER, kind=DocumentKind.VISA,
        expires_on=date.today() + timedelta(days=20),
    )
    await service.add_document(
        db, actor=actor, user_id=employee.id, upload=OFFER, kind=DocumentKind.VISA,
        expires_on=date.today() + timedelta(days=400),
    )
    soon = await service.list_documents(db, expiring_within_days=60)
    assert len(soon) == 1


async def test_an_expiry_before_the_issue_date_is_refused(db, actor):
    employee = await _person(db, "sami")
    with pytest.raises(service.HRError, match="before the issue date"):
        await service.add_document(
            db, actor=actor, user_id=employee.id, upload=OFFER,
            issued_on=date(2026, 5, 1), expires_on=date(2026, 4, 1),
        )


def test_uploads_are_checked_by_extension_not_by_what_the_client_claimed():
    with pytest.raises(UploadError, match="not a type we accept"):
        accept("payload.exe", b"MZ", "application/pdf")
    assert accept("cv.PDF", b"%PDF", None).content_type == "application/pdf"


def test_a_file_name_cannot_be_a_path():
    assert safe_name("../../etc/passwd") == "etcpasswd" or "/" not in safe_name(
        "../../etc/passwd"
    )
    assert "\\" not in safe_name(r"C:\Users\x\offer.pdf")
    assert safe_name(r"C:\Users\x\offer.pdf").endswith("offer.pdf")


def test_an_oversized_file_is_refused():
    from app.hr.documents import MAX_FILE_BYTES

    with pytest.raises(UploadError, match="limit is"):
        accept("big.pdf", b"x" * (MAX_FILE_BYTES + 1), "application/pdf")


# ── review cycles ──────────────────────────────────────────────────────


@pytest.fixture
async def cycle(db, actor, templates):
    return await service.create_cycle(db, actor=actor, name="2026 mid-year")


async def test_a_cycle_cannot_open_with_nobody_nominated(db, cycle):
    with pytest.raises(service.HRError, match="Nobody has been nominated"):
        await service.open_cycle(db, cycle)


async def test_nominating_and_opening(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    opened = await service.open_cycle(db, cycle)
    assert opened.status == CycleStatus.OPEN


async def test_a_self_review_is_labelled_as_one(db, cycle):
    person = await _person(db, "ali")
    review = await service.nominate(db, cycle, subject_id=person.id, reviewer_id=person.id)
    assert review.is_self_review is True
    assert str(review.relation) == "self"


async def test_the_same_reviewer_cannot_be_nominated_twice(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    with pytest.raises(service.HRError, match="already reviewing"):
        await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)


async def test_a_deactivated_person_cannot_be_nominated(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "gone")
    reviewer.is_active = False
    await db.flush()
    with pytest.raises(service.HRError, match="deactivated"):
        await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)


async def test_a_draft_review_is_not_validated_but_a_submission_is(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)

    half = await service.save_review(db, review, answers={"technical": "Outstanding"})
    assert half.status == ReviewStatus.DRAFT

    with pytest.raises(service.HRError, match="Please fill in"):
        await service.save_review(db, review, answers={"technical": "Outstanding"}, submit=True)


async def test_submitting_freezes_the_score(db, cycle, actor, templates):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)

    answers = {
        **{f["key"]: "Outstanding" for f in REVIEW_FIELDS if f.get("type") == "select"},
        "strengths": "Everything",
        "development": "Nothing",
    }
    submitted = await service.save_review(db, review, answers=answers, submit=True)
    assert submitted.status == ReviewStatus.SUBMITTED
    assert float(submitted.score_percent) == 100.0
    frozen = dict(submitted.score)

    # The template is reworded afterwards. The record must not move.
    template = templates[PERFORMANCE_REVIEW]
    await forms_service.update(
        db, template, roles={"super_admin"}, actor=actor,
        fields=[*template.fields, {"key": "extra", "label": "Extra", "type": "number",
                                   "scoring": {"tags": ["new"], "max": 5}}],
    )
    await db.refresh(submitted)
    assert dict(submitted.score) == frozen


async def test_reopening_clears_the_stale_score(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)
    await service.save_review(
        db, review,
        answers={**{f["key"]: "Outstanding" for f in REVIEW_FIELDS if f.get("type") == "select"},
                 "strengths": "x", "development": "y"},
        submit=True,
    )
    await service.reopen_review(db, review)
    assert review.status == ReviewStatus.DRAFT
    assert review.score == {} and review.score_percent is None


async def test_a_submitted_review_cannot_be_reopened_into_a_closed_cycle(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)
    await service.close_cycle(db, cycle)
    with pytest.raises(service.HRError, match="Reopen the cycle first"):
        await service.reopen_review(db, review)


async def test_a_started_review_cannot_be_withdrawn(db, cycle):
    """Withdrawing is un-asking somebody, not throwing their work away."""
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)
    await service.save_review(
        db, review,
        answers={**{f["key"]: "Meets expectation" for f in REVIEW_FIELDS
                    if f.get("type") == "select"},
                 "strengths": "x", "development": "y"},
        submit=True,
    )
    with pytest.raises(service.HRError, match="already started writing"):
        await service.withdraw_nomination(db, review)


async def test_an_untouched_nomination_can_be_withdrawn(db, cycle):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.withdraw_nomination(db, review)
    assert await service.list_reviews(db, cycle_id=cycle.id) == []


async def test_performance_combines_only_submitted_reviews(db, cycle):
    subject = await _person(db, "ali")
    kind = await _person(db, "mona")
    harsh = await _person(db, "omar")

    one = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=kind.id)
    two = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=harsh.id)
    await service.open_cycle(db, cycle)

    selects = [f["key"] for f in REVIEW_FIELDS if f.get("type") == "select"]
    await service.save_review(
        db, one,
        answers={**{k: "Outstanding" for k in selects}, "strengths": "x", "development": "y"},
        submit=True,
    )
    # Left as a draft, so it must not count.
    await service.save_review(db, two, answers={k: "Well below expectation" for k in selects})

    summary = await service.performance_for(db, subject.id)
    assert summary["reviews"] == 1
    assert summary["percent"] == 100.0
    assert summary["self_reviews"] == 0


async def test_a_review_cycle_needs_a_review_template(db, actor, templates):
    with pytest.raises(service.HRError, match="needs a 'performance_review'"):
        await service.create_cycle(
            db, actor=actor, name="Wrong", template_id=templates[JOB_APPLICATION].id
        )


async def test_an_archived_template_cannot_back_a_cycle(db, actor, templates):
    template = templates[PERFORMANCE_REVIEW]
    template.status = TemplateStatus.ARCHIVED
    await db.flush()
    with pytest.raises(service.HRError, match="Only an active template"):
        await service.create_cycle(db, actor=actor, name="Late", template_id=template.id)


# ── the job posting form ───────────────────────────────────────────────
#
# The advert is a form a super admin writes, not a set of columns a developer
# chose. These tests are about the two things that makes load-bearing: that a
# draft may be incomplete and a posted advert may not, and that a field marked
# internal never reaches a candidate.


POSTING = {
    "summary": "Price the bids that win us work.",
    "description": "Own estimating end to end.",
    "requirements": "Five years in MEP estimating.",
    "salary_range": "AED 18,000-22,000",
    "benefits": "Visa, insurance, annual ticket.",
    "hiring_manager": "Sami",
    "budget_code": "OPS-2026-14",
}


async def test_the_advert_comes_off_the_posting_form(db, actor, templates):
    opening = await service.create_opening(
        db, actor=actor, title="Senior Estimator", details=POSTING
    )
    assert opening.posting_template is not None
    assert opening.posting_template.kind == JOB_POSTING
    assert opening.details["benefits"] == "Visa, insurance, annual ticket."
    # The four with a column of their own are mirrored onto it, so the list and
    # the careers page read a column rather than a JSON blob.
    assert opening.summary == "Price the bids that win us work."
    assert opening.requirements == "Five years in MEP estimating."
    assert opening.salary_range == "AED 18,000-22,000"


async def test_a_draft_advert_may_be_incomplete(db, actor, templates):
    """Refusing to save half an advert is how a draft stops being a draft."""
    opening = await service.create_opening(
        db, actor=actor, title="Half written", details={"summary": "Just this"}
    )
    assert opening.details == {"summary": "Just this"}


async def test_posting_refuses_an_incomplete_advert(db, actor, templates):
    opening = await service.create_opening(
        db, actor=actor, title="Half written", details={"summary": "Just this"}
    )
    with pytest.raises(service.HRError, match="Please fill in"):
        await service.post_opening(db, opening, actor=actor)

    await service.update_opening(db, opening, actor=actor, details=POSTING)
    posted = await service.post_opening(db, opening, actor=actor)
    assert posted.status == OpeningStatus.OPEN


async def test_a_live_advert_cannot_be_edited_into_an_incomplete_one(db, actor, templates):
    opening = await service.create_opening(
        db, actor=actor, title="Senior Estimator", details=POSTING
    )
    await service.post_opening(db, opening, actor=actor)
    with pytest.raises(service.HRError, match="Please fill in"):
        await service.update_opening(
            db, opening, actor=actor, details={"summary": "Oops"}
        )


async def test_an_unknown_posting_answer_is_dropped_not_stored(db, actor, templates):
    """A stale tab posting a field the form no longer has must not leave it behind."""
    opening = await service.create_opening(
        db, actor=actor, title="Senior Estimator",
        details={**POSTING, "secret_note": "do not publish"},
    )
    assert "secret_note" not in opening.details


async def test_only_a_job_posting_template_can_describe_an_opening(db, actor, templates):
    with pytest.raises(service.HRError, match="needs a 'job_posting'"):
        await service.create_opening(
            db, actor=actor, title="Wrong form",
            posting_template_id=templates[PERFORMANCE_REVIEW].id,
        )


async def test_the_canonical_template_is_the_default_not_the_newest(db, actor, templates):
    """With several forms of a kind, "newest" is not a decision. key == kind is."""
    variants = await service.templates_of(db, JOB_APPLICATION)
    assert len(variants) > 1, "the shipped catalogue has application variants"
    default = await service.default_template(db, JOB_APPLICATION)
    assert default is not None and default.key == JOB_APPLICATION


async def test_hr_can_choose_a_variant(db, actor, templates):
    short = next(
        t for t in await service.templates_of(db, JOB_APPLICATION)
        if t.key == "job_application_short"
    )
    opening = await service.create_opening(
        db, actor=actor, title="Site labourer", template_id=short.id
    )
    assert opening.template_id == short.id


# ── deletion ───────────────────────────────────────────────────────────
#
# Only a super admin reaches any of this; the routes enforce that and are
# tested there. What is tested here is what a deletion actually takes with it,
# because the answer is not obvious and getting it wrong destroys the wrong
# records silently.


async def test_deleting_an_opening_takes_its_applications_and_their_files(
    db, opening, actor
):
    await service.post_opening(db, opening, actor=actor)
    await service.submit_application(db, opening, answers=ANSWERS, uploads=[("cv", CV)])
    await service.submit_application(
        db, opening,
        answers={**ANSWERS, "candidate_email": "other@example.com"},
        uploads=[("cv", CV)],
    )

    removed = await service.delete_opening(db, opening, actor=actor)
    assert removed.openings == 1
    assert removed.applications == 2
    assert removed.files == 2
    assert await service.list_applications(db) == []


async def test_deleting_an_opening_does_not_destroy_the_offer_letter(db, opening, actor):
    """The contract of somebody hired through it is not the opening's to delete."""
    await service.post_opening(db, opening, actor=actor)
    application = await service.submit_application(
        db, opening, answers=ANSWERS, uploads=[("cv", CV)]
    )
    employee = await _person(db, "rania")
    document = await service.add_document(
        db, actor=actor, user_id=employee.id, upload=OFFER,
        kind=DocumentKind.OFFER_LETTER, source_application_id=application.id,
    )

    await service.delete_opening(db, opening, actor=actor)
    kept = await service.get_document(db, document.id)
    assert kept.id == document.id
    # SET NULL happens in the database, so the row has to be re-read: the
    # identity map still holds the id the application had before it went, and
    # asserting on that would be testing SQLAlchemy's cache rather than the
    # constraint.
    await db.refresh(kept)
    assert kept.source_application_id is None


async def test_deleting_an_application_takes_only_its_own_files(db, opening, actor):
    await service.post_opening(db, opening, actor=actor)
    one = await service.submit_application(db, opening, answers=ANSWERS, uploads=[("cv", CV)])
    await service.submit_application(
        db, opening,
        answers={**ANSWERS, "candidate_email": "other@example.com"},
        uploads=[("cv", CV)],
    )

    removed = await service.delete_application(db, one, actor=actor)
    assert removed.as_dict()["applications"] == 1 and removed.files == 1
    survivors = await service.list_applications(db)
    assert [a.candidate_email for a in survivors] == ["other@example.com"]


async def test_deleting_a_cycle_takes_every_review_in_it(db, cycle, actor):
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)
    await service.save_review(
        db, review,
        answers={**{f["key"]: "Outstanding" for f in REVIEW_FIELDS
                    if f.get("type") == "select"},
                 "strengths": "x", "development": "y"},
        submit=True,
    )

    removed = await service.delete_cycle(db, cycle, actor=actor)
    assert removed.cycles == 1 and removed.reviews == 1
    assert await service.list_reviews(db, subject_id=subject.id) == []


async def test_a_super_admin_may_delete_a_submitted_review(db, cycle, actor):
    """Unlike withdrawing, this has no status guard — it is done on purpose."""
    subject, reviewer = await _person(db, "ali"), await _person(db, "mona")
    review = await service.nominate(db, cycle, subject_id=subject.id, reviewer_id=reviewer.id)
    await service.open_cycle(db, cycle)
    await service.save_review(
        db, review,
        answers={**{f["key"]: "Outstanding" for f in REVIEW_FIELDS
                    if f.get("type") == "select"},
                 "strengths": "x", "development": "y"},
        submit=True,
    )
    removed = await service.delete_review(db, review, actor=actor)
    assert removed.reviews == 1


async def test_purging_a_person_keeps_the_reviews_they_wrote_about_others(
    db, cycle, actor
):
    """Erasing a leaver must not gut a colleague's appraisal."""
    leaver = await _person(db, "leaver")
    colleague = await _person(db, "stayer")

    about_them = await service.nominate(
        db, cycle, subject_id=leaver.id, reviewer_id=colleague.id
    )
    by_them = await service.nominate(
        db, cycle, subject_id=colleague.id, reviewer_id=leaver.id
    )
    await service.add_document(
        db, actor=actor, user_id=leaver.id, upload=OFFER, kind=DocumentKind.CONTRACT
    )

    removed = await service.purge_person(db, leaver.id, actor=actor)
    assert removed.documents == 1
    assert removed.reviews == 1, "only the one about them"

    assert await service.list_documents(db, user_id=leaver.id) == []
    remaining = await service.list_reviews(db, cycle_id=cycle.id)
    assert [r.id for r in remaining] == [by_them.id]
    assert about_them.id not in {r.id for r in remaining}


async def test_purging_somebody_who_does_not_exist_says_so(db, actor):
    import uuid as _uuid

    with pytest.raises(service.NotFoundError, match="No such person"):
        await service.purge_person(db, _uuid.uuid4(), actor=actor)
