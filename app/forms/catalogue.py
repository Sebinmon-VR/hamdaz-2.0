"""The templates the product ships with.

Seeded like the module and label catalogues: code, not user data, because a
module refers to its own template by ``kind``. A super admin may edit any of it
afterwards — rename fields, add their own, change what is required — and the
seed will not undo that.

The quote template below is not invented. Every ``maps_to`` was read off a live
Zoho Books estimate, including the three custom fields this organisation
actually uses: ``cf_bcd`` (BCD, the bid closing date), ``cf_portal`` and
``cf_quote_creater``. So a field here is a field Zoho will accept when the
integration is built, rather than a guess that has to be reconciled later.
"""

from __future__ import annotations

from typing import Any, Final

from app.models.templates import FieldType

#: What a module asks for when it wants "the current quote form".
QUOTE_REQUEST: Final = "quote_request"
#: What HR fills in to create an opening. The advert, as a form rather than
#: as fixed columns — a super admin decides what a job posting here has to
#: say, and can add "hiring manager" or "budget code" without a deploy.
JOB_POSTING: Final = "job_posting"
#: The form a candidate fills in. HR picks which template an opening uses; a
#: super admin decides what it asks and what each answer is worth.
JOB_APPLICATION: Final = "job_application"
#: The form a nominated reviewer fills in about somebody.
PERFORMANCE_REVIEW: Final = "performance_review"

#: Variants. Both KINDS above may have several templates; HR chooses which
#: one an opening or a review cycle uses. The template whose key equals its
#: kind is the canonical one, and is what a module falls back to when nobody
#: has chosen — see ``app.hr.service.default_template``.
#: The mail a workflow sends a supplier asking for a quotation. Two fields
#: whose *defaults* are the subject and body; the workflow's email step reads
#: them, so the wording is a super admin's to change here rather than a
#: developer's. Placeholders are the workflow's — see app/workflows/templating.
RFQ_EMAIL: Final = "rfq_email"

SHORT_APPLICATION: Final = "job_application_short"
TECHNICAL_APPLICATION: Final = "job_application_technical"
PROBATION_REVIEW: Final = "probation_review"
LEADERSHIP_REVIEW: Final = "leadership_review"


def field(
    key: str,
    label: str,
    kind: FieldType,
    *,
    section: str,
    required: bool = False,
    maps_to: str | None = None,
    help: str | None = None,
    options: list[str] | None = None,
    default: Any = None,
    columns: list[dict] | None = None,
    scoring: dict[str, Any] | None = None,
    internal: bool = False,
) -> dict[str, Any]:
    """One field.

    ``maps_to`` is the Zoho estimate field it becomes, where there is one.
    ``scoring`` makes the field count towards a score — see
    ``app.forms.scoring`` for the shape and for why tags matter more than the
    total. ``internal`` keeps an answer off anything candidate-facing.
    """
    spec: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": kind.value,
        "section": section,
        "required": required,
    }
    if maps_to:
        spec["maps_to"] = maps_to
    if help:
        spec["help"] = help
    if options:
        spec["options"] = options
    if default is not None:
        spec["default"] = default
    if columns:
        spec["columns"] = columns
    if scoring:
        spec["scoring"] = scoring
    if internal:
        # Only written when true. An absent key reads as public, which is
        # the right default for an advert and matches every field that
        # predates the flag.
        spec["internal"] = True
    return spec


_LINE_COLUMNS: Final[list[dict]] = [
    field("name", "Item", FieldType.TEXT, section="line", required=True, maps_to="name"),
    field("item_code", "Code / SKU", FieldType.TEXT, section="line", maps_to="item_code"),
    field("description", "Description", FieldType.TEXTAREA, section="line", maps_to="description"),
    field("quantity", "Qty", FieldType.NUMBER, section="line", required=True,
          maps_to="quantity", default=1),
    field("unit", "Unit", FieldType.TEXT, section="line", maps_to="unit",
          help="each, set, metre, box"),
    field("rate", "Unit price", FieldType.CURRENCY, section="line", required=True,
          maps_to="rate", help="Zoho calls the unit price 'rate'"),
    field("discount", "Discount", FieldType.CURRENCY, section="line", maps_to="discount"),
    field("tax_name", "Tax", FieldType.TEXT, section="line", maps_to="tax_name"),
    field("tax_percentage", "Tax %", FieldType.PERCENT, section="line",
          maps_to="tax_percentage"),
    field("cost_rate", "Our cost", FieldType.CURRENCY, section="line",
          help="Not sent to Zoho. Kept so an approver can see the margin."),
]

QUOTE_SECTIONS: Final[list[dict]] = [
    {"key": "customer", "name": "Customer", "help": "Who the quote is for."},
    {"key": "quote", "name": "Quote details", "help": "Dates, references, currency."},
    {"key": "items", "name": "Items", "help": "What is being quoted."},
    {"key": "charges", "name": "Charges and discounts", "help": "Applied to the whole quote."},
    {"key": "terms", "name": "Terms and notes", "help": "What the customer sees."},
    {"key": "suppliers", "name": "Supplier quotes",
     "help": "Attach what suppliers sent, if several quoted the same requirement."},
]

QUOTE_FIELDS: Final[list[dict]] = [
    # ── customer ───────────────────────────────────────────────────────
    field("customer_name", "Customer", FieldType.TEXT, section="customer", required=True,
          maps_to="customer_name", help="Matched to a Zoho contact when it is created."),
    field("customer_id", "Zoho contact id", FieldType.TEXT, section="customer",
          maps_to="customer_id", help="Left blank until the customer is matched."),
    field("contact_person", "Contact person", FieldType.TEXT, section="customer",
          maps_to="contact_persons"),
    field("place_of_supply", "Place of supply", FieldType.TEXT, section="customer",
          maps_to="place_of_supply", help="Emirate code, e.g. AB, DU. Drives VAT treatment."),

    # ── quote details ──────────────────────────────────────────────────
    field("title", "Title", FieldType.TEXT, section="quote", required=True,
          help="For finding it here. Not sent to Zoho."),
    field("reference_number", "Customer reference", FieldType.TEXT, section="quote",
          maps_to="reference_number", help="Their PO or enquiry number."),
    field("quote_date", "Quote date", FieldType.DATE, section="quote", maps_to="date"),
    field("expiry_date", "Valid until", FieldType.DATE, section="quote",
          maps_to="expiry_date", help="How long the price holds."),
    field("currency", "Currency", FieldType.SELECT, section="quote", required=True,
          maps_to="currency_code", default="AED",
          options=["AED", "USD", "EUR", "GBP", "SAR", "QAR", "OMR", "KWD", "BHD", "INR"]),
    field("salesperson_name", "Salesperson", FieldType.TEXT, section="quote",
          maps_to="salesperson_name"),
    field("cf_bcd", "Bid closing date (BCD)", FieldType.DATE, section="quote",
          maps_to="cf_bcd",
          help="The date the bid closes. The same date the Proposals list calls BCD."),
    field("cf_portal", "Portal", FieldType.TEXT, section="quote", maps_to="cf_portal",
          help="Which client portal the enquiry came through."),
    field("cf_quote_creater", "Quote creator", FieldType.TEXT, section="quote",
          maps_to="cf_quote_creater", help="A dropdown in Zoho."),
    field("tax_treatment", "Tax treatment", FieldType.SELECT, section="quote",
          maps_to="tax_treatment", default="vat_registered",
          options=["vat_registered", "vat_not_registered", "gcc_vat_registered",
                   "gcc_vat_not_registered", "non_gcc", "dz_vat_registered"]),

    # ── items ──────────────────────────────────────────────────────────
    field("items", "Line items", FieldType.TABLE, section="items", required=True,
          maps_to="line_items", columns=_LINE_COLUMNS,
          help="One row per priced line. Totals are computed, never typed."),

    # ── charges ────────────────────────────────────────────────────────
    field("discount", "Discount on the whole quote", FieldType.CURRENCY, section="charges",
          maps_to="discount"),
    field("shipping_charge", "Shipping", FieldType.CURRENCY, section="charges",
          maps_to="shipping_charge"),
    field("adjustment", "Adjustment", FieldType.CURRENCY, section="charges",
          maps_to="adjustment", help="Rounding, or anything the other fields do not cover."),

    # ── terms ──────────────────────────────────────────────────────────
    field("subject", "Subject", FieldType.TEXTAREA, section="terms",
          maps_to="subject_content"),
    field("payment_terms", "Payment terms", FieldType.TEXT, section="terms",
          help="e.g. 30 days net, 50% advance."),
    field("delivery_terms", "Delivery terms", FieldType.TEXT, section="terms",
          help="e.g. 4 weeks ex-stock."),
    field("notes", "Notes to the customer", FieldType.TEXTAREA, section="terms",
          maps_to="notes"),
    field("terms", "Terms and conditions", FieldType.TEXTAREA, section="terms",
          maps_to="terms"),

    # ── supplier quotes ────────────────────────────────────────────────
    field("multiple_supplier_quotes", "Several suppliers quoted this", FieldType.CHECKBOX,
          section="suppliers", default=False,
          help="Turn on to attach supplier quotes and compare them. An approver "
               "then chooses which supplier wins."),
    field("supplier_files", "Supplier quote documents", FieldType.FILE, section="suppliers",
          help="PDF, image, XLSX, CSV or DOCX. Read automatically — locally where "
               "the document allows it, and by Claude where it does not."),
]



# ── HR ─────────────────────────────────────────────────────────────────
#
# Both templates below are *scored* — see ``app.forms.scoring``. The scoring
# blocks are what turn a form into an assessment, and the tags are what make the
# result readable: "strong technically, weak on delivery" is actionable in a way
# that "68%" is not.
#
# The candidate-facing template is deliberately short. Every extra question on a
# public form is a candidate who does not finish it, and anything HR can find out
# at interview does not belong on the application.

APPLICATION_SECTIONS: Final[list[dict]] = [
    {"key": "about", "name": "About you", "help": "How we reach you."},
    {"key": "experience", "name": "Experience", "help": "What you have done."},
    {"key": "fit", "name": "This role", "help": "Why this one."},
    {"key": "documents", "name": "Documents", "help": "Your CV, and anything else."},
]

APPLICATION_FIELDS: Final[list[dict]] = [
    # The first three keys are also lifted onto columns of their own on the
    # application row — see app.models.hr.JobApplication — so leave them alone.
    field("candidate_name", "Full name", FieldType.TEXT, section="about", required=True),
    field("candidate_email", "Email", FieldType.TEXT, section="about", required=True,
          help="Where we reply. Please check it."),
    field("candidate_phone", "Phone", FieldType.TEXT, section="about"),
    field("location", "Where you are based", FieldType.TEXT, section="about"),
    field("notice_period", "Notice period", FieldType.SELECT, section="about",
          options=["Immediately", "2 weeks", "1 month", "2 months", "3 months or more"],
          scoring={"tags": ["availability"], "max": 5,
                   "option_scores": {"Immediately": 5, "2 weeks": 4, "1 month": 3,
                                     "2 months": 2, "3 months or more": 1}}),

    field("years_experience", "Years of relevant experience", FieldType.NUMBER,
          section="experience", help="Round to the nearest year.",
          scoring={"tags": ["experience"], "max": 10, "weight": 1.5}),
    field("current_role", "Current or most recent role", FieldType.TEXT,
          section="experience"),
    field("current_employer", "Current or most recent employer", FieldType.TEXT,
          section="experience"),
    field("qualification", "Highest qualification", FieldType.SELECT,
          section="experience",
          options=["School", "Diploma", "Bachelors", "Masters", "Doctorate"],
          scoring={"tags": ["qualification"], "max": 5,
                   "option_scores": {"School": 1, "Diploma": 2, "Bachelors": 3,
                                     "Masters": 4, "Doctorate": 5}}),
    field("has_licence", "Hold a UAE driving licence", FieldType.CHECKBOX,
          section="experience", default=False,
          scoring={"tags": ["logistics"], "max": 2}),

    field("why_this_role", "Why this role", FieldType.TEXTAREA, section="fit",
          help="A few sentences is plenty."),
    field("expected_salary", "Expected monthly salary (AED)", FieldType.NUMBER,
          section="fit"),
    field("earliest_start", "Earliest start date", FieldType.DATE, section="fit"),

    field("cv", "CV", FieldType.FILE, section="documents", required=True,
          help="PDF or Word. One file."),
    field("other_documents", "Anything else", FieldType.FILE, section="documents",
          help="Certificates, a portfolio, a covering letter."),
]

REVIEW_SECTIONS: Final[list[dict]] = [
    {"key": "delivery", "name": "Delivery", "help": "What they got done."},
    {"key": "quality", "name": "Quality and skill", "help": "How well."},
    {"key": "working", "name": "Working with others", "help": "How they are to work with."},
    {"key": "summary", "name": "Summary", "help": "In your own words."},
]

#: The scale every rated question on the review form uses. One scale across the
#: whole form is the point: answers marked out of different maximums cannot be
#: compared with each other, and comparing them is what a review is for.
_RATING: Final[list[str]] = [
    "Well below expectation",
    "Below expectation",
    "Meets expectation",
    "Above expectation",
    "Outstanding",
]
_RATING_SCORES: Final[dict[str, int]] = {name: n for n, name in enumerate(_RATING, start=1)}


def _rated(key: str, label: str, section: str, tags: list[str], *,
           weight: float = 1.0, help: str | None = None) -> dict[str, Any]:
    """A five-point rated question feeding one or more competency tags."""
    return field(key, label, FieldType.SELECT, section=section, options=list(_RATING),
                 help=help,
                 scoring={"tags": tags, "max": 5, "weight": weight,
                          "option_scores": dict(_RATING_SCORES)})


REVIEW_FIELDS: Final[list[dict]] = [
    _rated("volume", "Volume of work delivered", "delivery", ["delivery"]),
    _rated("deadlines", "Meeting agreed dates", "delivery", ["delivery", "reliability"],
           weight=1.5,
           help="Weighted higher than the rest: a date missed silently costs more "
                "than one missed loudly."),
    _rated("ownership", "Sees work through without being chased", "delivery",
           ["reliability", "initiative"]),

    _rated("technical", "Technical skill in their own area", "quality", ["technical"],
           weight=1.5),
    _rated("accuracy", "Accuracy of the work", "quality", ["technical", "quality"]),
    _rated("improvement", "Improved something without being asked", "quality",
           ["initiative"]),

    _rated("communication", "Communicates clearly", "working", ["communication"]),
    _rated("collaboration", "Works well across teams", "working",
           ["communication", "teamwork"]),
    _rated("client_facing", "Represents us well to clients", "working",
           ["communication", "client_facing"],
           help="Leave blank for a role with no client contact. A blank is not a "
                "zero and will not count against them."),

    field("strengths", "What they are best at", FieldType.TEXTAREA, section="summary",
          required=True),
    field("development", "Where they should grow", FieldType.TEXTAREA, section="summary",
          required=True),
    field("recommend_promotion", "Ready for more responsibility", FieldType.CHECKBOX,
          section="summary", default=False),
]



# ── HR: the variants ───────────────────────────────────────────────────
#
# Same two kinds, different forms. What makes them worth having separately is
# that the questions genuinely differ by the job: asking a site labourer to
# rate their systems design is noise, and asking a network engineer nothing
# technical throws away the only thing the form could have told you.
#
# Every one of them is editable by a super admin afterwards, and HR picks which
# an opening uses. Nothing here is a hardcoded workflow.

SHORT_SECTIONS: Final[list[dict]] = [
    {"key": "about", "name": "About you", "help": "How we reach you."},
    {"key": "work", "name": "Work", "help": "What you have done."},
    {"key": "documents", "name": "Documents"},
]

#: Deliberately eight questions. This is the form for a role that gets two
#: hundred applications, where every extra field is a candidate who gives up
#: half way and a row HR has to read.
SHORT_FIELDS: Final[list[dict]] = [
    field("candidate_name", "Full name", FieldType.TEXT, section="about", required=True),
    field("candidate_email", "Email", FieldType.TEXT, section="about", required=True,
          help="Where we reply. Please check it."),
    field("candidate_phone", "Phone", FieldType.TEXT, section="about", required=True,
          help="A number we can call or message."),
    field("location", "Where you are based", FieldType.TEXT, section="about"),

    field("years_experience", "Years doing this kind of work", FieldType.NUMBER,
          section="work",
          scoring={"tags": ["experience"], "max": 10, "weight": 1.5}),
    field("can_start", "When you can start", FieldType.SELECT, section="work",
          options=["Immediately", "Within a week", "Within a month", "Later"],
          scoring={"tags": ["availability"], "max": 5,
                   "option_scores": {"Immediately": 5, "Within a week": 4,
                                     "Within a month": 3, "Later": 1}}),
    field("has_licence", "Hold a UAE driving licence", FieldType.CHECKBOX,
          section="work", default=False,
          scoring={"tags": ["logistics"], "max": 2}),

    field("cv", "CV", FieldType.FILE, section="documents",
          help="If you have one. Not required."),
]

TECHNICAL_SECTIONS: Final[list[dict]] = [
    {"key": "about", "name": "About you", "help": "How we reach you."},
    {"key": "experience", "name": "Experience"},
    {"key": "technical", "name": "Technical",
     "help": "Rate yourself honestly. We check at interview, and a claim that "
             "does not survive that costs you more than a modest answer would."},
    {"key": "fit", "name": "This role"},
    {"key": "documents", "name": "Documents"},
]

#: The self-rating scale. One scale across the section, for the same reason the
#: review form uses one: answers marked out of different maximums cannot be
#: compared with each other.
_SKILL: Final[list[str]] = ["None", "Basic", "Working", "Strong", "Expert"]
_SKILL_SCORES: Final[dict[str, int]] = {name: n for n, name in enumerate(_SKILL)}


def _skill(key: str, label: str, tags: list[str], *, help: str | None = None) -> dict[str, Any]:
    """A five-point self-rated technical skill. ``None`` is worth zero, not blank."""
    return field(key, label, FieldType.SELECT, section="technical", options=list(_SKILL),
                 help=help,
                 scoring={"tags": tags, "max": 4,
                          "option_scores": dict(_SKILL_SCORES)})


TECHNICAL_FIELDS: Final[list[dict]] = [
    field("candidate_name", "Full name", FieldType.TEXT, section="about", required=True),
    field("candidate_email", "Email", FieldType.TEXT, section="about", required=True),
    field("candidate_phone", "Phone", FieldType.TEXT, section="about"),
    field("location", "Where you are based", FieldType.TEXT, section="about"),
    field("notice_period", "Notice period", FieldType.SELECT, section="about",
          options=["Immediately", "2 weeks", "1 month", "2 months", "3 months or more"],
          scoring={"tags": ["availability"], "max": 5,
                   "option_scores": {"Immediately": 5, "2 weeks": 4, "1 month": 3,
                                     "2 months": 2, "3 months or more": 1}}),

    field("years_experience", "Years of relevant experience", FieldType.NUMBER,
          section="experience",
          scoring={"tags": ["experience"], "max": 12, "weight": 1.5}),
    field("current_role", "Current or most recent role", FieldType.TEXT,
          section="experience"),
    field("current_employer", "Current or most recent employer", FieldType.TEXT,
          section="experience"),
    field("qualification", "Highest qualification", FieldType.SELECT,
          section="experience",
          options=["School", "Diploma", "Bachelors", "Masters", "Doctorate"],
          scoring={"tags": ["qualification"], "max": 5,
                   "option_scores": {"School": 1, "Diploma": 2, "Bachelors": 3,
                                     "Masters": 4, "Doctorate": 5}}),
    field("certifications", "Certifications you hold", FieldType.TEXTAREA,
          section="experience",
          help="One per line, with the year. Blank if none."),

    _skill("skill_networking", "Networking and infrastructure", ["technical", "networking"]),
    _skill("skill_security", "Security", ["technical", "security"]),
    _skill("skill_cloud", "Cloud platforms", ["technical", "cloud"]),
    _skill("skill_scripting", "Scripting and automation", ["technical", "automation"]),
    _skill("skill_documentation", "Writing technical documents", ["communication"],
           help="Designs, method statements, handover notes."),
    field("worked_on", "The project you are proudest of", FieldType.TEXTAREA,
          section="technical",
          help="What it was, what you did on it, and what was hard about it."),

    field("expected_salary", "Expected monthly salary (AED)", FieldType.NUMBER,
          section="fit"),
    field("earliest_start", "Earliest start date", FieldType.DATE, section="fit"),
    field("why_this_role", "Why this role", FieldType.TEXTAREA, section="fit"),

    field("cv", "CV", FieldType.FILE, section="documents", required=True,
          help="PDF or Word. One file."),
    field("other_documents", "Certificates or portfolio", FieldType.FILE,
          section="documents"),
]

PROBATION_SECTIONS: Final[list[dict]] = [
    {"key": "settling", "name": "Settling in"},
    {"key": "work", "name": "The work"},
    {"key": "decision", "name": "Recommendation",
     "help": "The point of this form. A probation review that does not end in a "
             "recommendation is an appraisal nobody asked for."},
]

#: Short by design: three months is not long enough to judge somebody on twelve
#: questions, and pretending otherwise makes the score look more authoritative
#: than it is.
PROBATION_FIELDS: Final[list[dict]] = [
    _rated("understands_role", "Understands what the job is", "settling",
           ["onboarding"]),
    _rated("fits_in", "Works well with the people around them", "settling",
           ["teamwork"]),
    _rated("asks", "Asks when they are stuck rather than guessing", "settling",
           ["initiative", "reliability"],
           help="Weighted like the rest, but the single best early signal."),

    _rated("quality", "Quality of what they produce", "work", ["quality"],
           weight=1.5),
    _rated("pace", "Getting through the work", "work", ["delivery"]),
    _rated("independence", "Needs less supervision than at the start", "work",
           ["initiative"], weight=1.5),

    field("recommendation", "Recommendation", FieldType.SELECT, section="decision",
          required=True,
          options=["Confirm", "Extend probation", "End employment"],
          help="Extending is a real answer. Confirming somebody you are unsure "
               "about is how a problem becomes permanent."),
    field("extend_until", "If extending, until when", FieldType.DATE,
          section="decision"),
    field("evidence", "What this is based on", FieldType.TEXTAREA, section="decision",
          required=True,
          help="Specific things that happened, not impressions."),
    field("support_needed", "What they need from us", FieldType.TEXTAREA,
          section="decision",
          help="Often the honest answer to a weak probation is that we did not "
               "give somebody what they needed."),
]

LEADERSHIP_SECTIONS: Final[list[dict]] = [
    {"key": "team", "name": "Their team",
     "help": "A manager is judged on the team, not on their own output."},
    {"key": "judgement", "name": "Judgement and ownership"},
    {"key": "delivery", "name": "Delivery"},
    {"key": "summary", "name": "Summary"},
]

LEADERSHIP_FIELDS: Final[list[dict]] = [
    _rated("develops", "Develops the people who report to them", "team",
           ["people_leadership"], weight=2,
           help="Weighted double. It is the part of a management job that "
                "nothing else substitutes for, and the part easiest to skip."),
    _rated("retains", "People want to keep working for them", "team",
           ["people_leadership"], weight=1.5),
    _rated("hard_conversations", "Has the difficult conversation rather than "
           "waiting", "team", ["people_leadership", "courage"]),
    _rated("shares_credit", "Credit goes to the team, blame is taken", "team",
           ["integrity"]),

    _rated("decisions", "Decides with incomplete information", "judgement",
           ["judgement"], weight=1.5),
    _rated("escalates", "Escalates the right things at the right time",
           "judgement", ["judgement", "communication"]),
    _rated("owns_failure", "Owns what goes wrong in their area", "judgement",
           ["integrity", "reliability"]),

    _rated("team_delivery", "Their team delivers what it committed to",
           "delivery", ["delivery"], weight=1.5),
    _rated("planning", "Plans realistically rather than optimistically",
           "delivery", ["delivery", "judgement"]),
    _rated("cross_team", "Works well with other teams", "delivery",
           ["teamwork", "communication"]),

    field("strengths", "What they are best at", FieldType.TEXTAREA,
          section="summary", required=True),
    field("development", "Where they should grow", FieldType.TEXTAREA,
          section="summary", required=True),
    field("team_health", "Anything worrying about their team", FieldType.TEXTAREA,
          section="summary",
          help="Read before the score. A number cannot say that two good people "
               "are about to resign."),
]



# ── HR: the job posting ────────────────────────────────────────────────
#
# The advert HR writes, as a form. Everything on it is shown to candidates by
# default, because a job posting is a public document — that is what it is for.
# A field marked ``internal`` is the exception: it is HR's own, kept on the
# opening and never sent to the candidate side. Marking is opt-in and the
# default is public, which is the safe way round for an advert and the reason
# the flag is named for the rarer case.
#
# The fixed columns on ``job_openings`` are still the ones the system itself
# needs — title, status, dates, the share token. This form is everything a
# human wants to say about the job, and it changes without a migration.

POSTING_SECTIONS: Final[list[dict]] = [
    {"key": "role", "name": "The role", "help": "What candidates see first."},
    {"key": "detail", "name": "The detail", "help": "What the job actually is."},
    {"key": "package", "name": "Package and terms"},
    {"key": "internal", "name": "Internal",
     "help": "Ours. None of this reaches the advert."},
]

POSTING_FIELDS: Final[list[dict]] = [
    field("summary", "One-line summary", FieldType.TEXT, section="role",
          help="The sentence that makes somebody read the rest."),
    field("about_us", "About the company", FieldType.TEXTAREA, section="role",
          help="Kept on the form rather than hardcoded, so it can be tuned per "
               "advert without a deploy."),

    field("description", "What you would be doing", FieldType.TEXTAREA,
          section="detail", required=True),
    field("requirements", "What we are looking for", FieldType.TEXTAREA,
          section="detail", required=True),
    field("nice_to_have", "Nice to have", FieldType.TEXTAREA, section="detail",
          help="Kept separate from requirements on purpose: a list that mixes "
               "the two is a list good candidates rule themselves out of."),
    field("reports_to", "Reports to", FieldType.TEXT, section="detail"),
    field("travel", "Travel expected", FieldType.SELECT, section="detail",
          options=["None", "Occasional", "Regular", "Site based"]),

    field("salary_range", "Salary range", FieldType.TEXT, section="package",
          help="Free text: 'AED 8,000-10,000', 'Competitive', 'DOE'. Publishing "
               "a range gets better applicants and fewer wasted interviews."),
    field("benefits", "Benefits", FieldType.TEXTAREA, section="package",
          help="Visa, insurance, ticket, leave. One per line."),
    field("hours", "Working hours", FieldType.TEXT, section="package"),
    field("start_date", "Ideal start date", FieldType.DATE, section="package"),

    field("hiring_manager", "Hiring manager", FieldType.TEXT, section="internal",
          internal=True),
    field("budget_code", "Budget code", FieldType.TEXT, section="internal",
          internal=True),
    field("approved_by", "Approved by", FieldType.TEXT, section="internal",
          internal=True),
    field("sourcing_notes", "Where to advertise", FieldType.TEXTAREA,
          section="internal", internal=True,
          help="Boards, agencies, referral notes. Never published."),
]


#: The templates seeded on first run.
RFQ_SECTIONS: Final[list[dict]] = [
    {"key": "mail", "name": "The mail", "help": "Sent once per supplier, from the intake mailbox."},
]

RFQ_FIELDS: Final[list[dict]] = [
    field(
        "subject", "Subject", FieldType.TEXT, section="mail", required=True,
        default="Request for quotation — {{ task.title }} [{{ run.tag }}]",
        help="Keep [{{ run.tag }}] in it: that is how a reply is matched to the task.",
    ),
    field(
        "body", "Body", FieldType.TEXTAREA, section="mail", required=True,
        default=(
            "Dear {{ recipient.name }},\n\n"
            "We are preparing an offer for {{ requirements.customer }} and would like your "
            "best quotation for the following:\n\n"
            "{{ requirements.items | bullets }}\n\n"
            "Requirements:\n{{ requirements.requirements | bullets }}\n\n"
            "Please quote in AED, delivered to the UAE, stating lead time, validity and "
            "payment terms. Please keep the reference [{{ run.tag }}] in the subject of "
            "your reply so it reaches the right file.\n\n"
            "Kind regards,\n{{ owner.name }}\nHamdaz Technologies"
        ),
        help=(
            "Placeholders: {{ recipient.name }}, {{ task.title }}, {{ requirements.customer }}, "
            "{{ requirements.items | bullets }}, {{ requirements.requirements | bullets }}, "
            "{{ run.tag }}, {{ owner.name }}."
        ),
    ),
]

TEMPLATES: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": RFQ_EMAIL,
        "name": "Request for quotation (email)",
        "kind": RFQ_EMAIL,
        "description": (
            "What the presales workflow writes to a supplier. Not a form anybody "
            "fills in: the defaults of its two fields are the subject and body the "
            "workflow sends, so the wording lives here where a super admin can "
            "change it."
        ),
        "sections": RFQ_SECTIONS,
        "fields": RFQ_FIELDS,
        "grants": (),
    },
    {
        "key": JOB_POSTING,
        "name": "Job posting",
        "kind": JOB_POSTING,
        "description": (
            "What HR fills in to create a job opening. Everything on it is "
            "published on the advert except the fields marked internal, so a "
            "super admin can change what a job posting here says without a "
            "developer."
        ),
        "sections": POSTING_SECTIONS,
        "fields": POSTING_FIELDS,
        # No grants: HR reaches this through the HR module, not through the
        # "forms you can fill in" list.
        "grants": (),
    },
    {
        "key": QUOTE_REQUEST,
        "name": "Quote request",
        "kind": QUOTE_REQUEST,
        "description": (
            "The form presales fills in to raise a customer quote. Every field "
            "that carries a `maps_to` was taken from a live Zoho Books estimate, "
            "so the integration later is a mapping rather than a rewrite."
        ),
        "sections": QUOTE_SECTIONS,
        "fields": QUOTE_FIELDS,
    },
    {
        "key": JOB_APPLICATION,
        "name": "Job application",
        "kind": JOB_APPLICATION,
        "description": (
            "What a candidate fills in on the public link for an opening. Kept "
            "short on purpose. The scored answers give HR a first ordering of a "
            "pile of applications — it is a sort, not a decision."
        ),
        "sections": APPLICATION_SECTIONS,
        "fields": APPLICATION_FIELDS,
        # No grants: candidates are not users, and this form is reached through
        # an opening's share link rather than through the grant machinery. A
        # blanket grant would only make it appear in every colleague's list of
        # forms they can fill in, which is not a thing anybody should do.
        "grants": (),
    },
    {
        "key": PERFORMANCE_REVIEW,
        "name": "Performance review",
        "kind": PERFORMANCE_REVIEW,
        "description": (
            "Filled in by whoever HR nominates for a review cycle, including the "
            "person themselves. Every rated question is on one five-point scale, "
            "and each feeds named competencies rather than only a total."
        ),
        "sections": REVIEW_SECTIONS,
        "fields": REVIEW_FIELDS,
        # No grants, for the same reason: who may fill one in is the nomination
        # HR made, not a team grant.
        "grants": (),
    },
    {
        "key": SHORT_APPLICATION,
        "name": "Job application (short)",
        "kind": JOB_APPLICATION,
        "description": (
            "Eight questions, for a role that gets a large pile of applications "
            "and where most of what matters is found out at interview. Scores on "
            "experience, availability and licence only."
        ),
        "sections": SHORT_SECTIONS,
        "fields": SHORT_FIELDS,
        "grants": (),
    },
    {
        "key": TECHNICAL_APPLICATION,
        "name": "Job application (technical)",
        "kind": JOB_APPLICATION,
        "description": (
            "For engineering roles. Adds a self-rated skills section whose "
            "answers feed named competencies, so a pile of applications can be "
            "read by skill rather than only by total."
        ),
        "sections": TECHNICAL_SECTIONS,
        "fields": TECHNICAL_FIELDS,
        "grants": (),
    },
    {
        "key": PROBATION_REVIEW,
        "name": "Probation review",
        "kind": PERFORMANCE_REVIEW,
        "description": (
            "End of probation. Six rated questions and a recommendation to "
            "confirm, extend or end — deliberately short, because three months "
            "does not support a longer judgement than that."
        ),
        "sections": PROBATION_SECTIONS,
        "fields": PROBATION_FIELDS,
        "grants": (),
    },
    {
        "key": LEADERSHIP_REVIEW,
        "name": "Performance review (managers)",
        "kind": PERFORMANCE_REVIEW,
        "description": (
            "For people who manage others. Judges them on their team rather than "
            "on their own output, and weights developing people highest — it is "
            "the part of the job nothing else substitutes for."
        ),
        "sections": LEADERSHIP_SECTIONS,
        "fields": LEADERSHIP_FIELDS,
        "grants": (),
    },
)

BY_KEY: Final[dict[str, dict[str, Any]]] = {t["key"]: t for t in TEMPLATES}
