"""The blocks a workflow is built from, and the flow the product ships with.

**Blocks are code; flows are data.** A block is one thing the engine knows how
to do — read the task's documents, ask the person, send a mail, wait for a
reply — with a small config the builder lets a super admin fill in. A flow is
an ordered list of blocks with their configs, kept in the database and
editable without a deploy. The presales flow below is seeded so the module
does something useful on day one, and it is a starting point: a super admin
may reorder it, change any wording, drop a step or add one.

**The ``endpoint`` block is the whole of the app.** It calls one of the
assistant's tools — which is to say one of this app's own routes — as the run's
owner, so a flow can do anything the owner could do on a screen, checked by
the same route the screen would use. Every other block exists because the
thing it does is not a route: reading bytes off SharePoint, waiting weeks for
a mail, asking a person to look at something.

Each block declares its ``config_schema`` for the builder to render and for
``validate_steps`` to check on save, so a run never meets a config it does
not understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.assistant.catalogue import LIVE_TOOLS

# ── what a step's context can carry, for the schemas below ─────────────

#: An item somebody needs priced. The shape every step agrees on: what the
#: documents are read into, what the person types in by hand, what the
#: suppliers are asked for, and what the quote request is raised with.
ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "What it is, as a person would write it."},
        "part_number": {"type": "string", "description": "Manufacturer part or model number, or empty."},
        "brand": {"type": "string", "description": "Brand or manufacturer, or empty."},
        "quantity": {"type": "number", "description": "How many. 1 if not stated."},
        "unit": {"type": "string", "description": "pcs, m, set, lot… or empty."},
        "specification": {
            "type": "string",
            "description": "Technical requirements that matter for pricing, in one line.",
        },
        "source": {"type": "string", "description": "Which document or line this came from."},
    },
    "required": ["description", "part_number", "brand", "quantity", "unit", "specification", "source"],
    "additionalProperties": False,
}

REQUIREMENTS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences: what the customer wants and by when.",
        },
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Conditions that are not line items: certifications, delivery, warranty, site, standards.",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the documents do not say that a supplier would need to know.",
        },
        "customer": {"type": "string", "description": "The customer or end user, or empty."},
        "deadline": {"type": "string", "description": "Submission or bid closing date as YYYY-MM-DD, or empty."},
    },
    "required": ["summary", "items", "requirements", "missing", "customer", "deadline"],
    "additionalProperties": False,
}

SUPPLIER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "website": {"type": "string", "description": "Homepage, or empty."},
        "email": {"type": "string", "description": "A sales or enquiries address found on their site, or empty."},
        "phone": {"type": "string", "description": "With country code, or empty."},
        "city": {"type": "string", "description": "Emirate or city, or empty."},
        "role": {
            "type": "string",
            "enum": ["manufacturer", "distributor", "reseller", "unknown"],
        },
        "items_covered": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which of the requested items they can supply, by description.",
        },
        "evidence": {"type": "string", "description": "The page or fact that says they supply this, in one line."},
        "confidence": {"type": "number", "description": "0 to 1."},
    },
    "required": ["name", "website", "email", "phone", "city", "role", "items_covered", "evidence", "confidence"],
    "additionalProperties": False,
}

SUPPLIERS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "suppliers": {"type": "array", "items": SUPPLIER_SCHEMA},
        "notes": {"type": "string", "description": "Anything the person should know before contacting them."},
    },
    "required": ["suppliers", "notes"],
    "additionalProperties": False,
}

#: Named schemas an ``agent`` or ``extract`` block may ask for by key, so the
#: builder offers a list rather than a JSON editor.
SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "requirements": REQUIREMENTS_SCHEMA,
    "suppliers": SUPPLIERS_SCHEMA,
}


# ── the blocks ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConfigField:
    key: str
    label: str
    #: text, textarea, number, boolean, select, json, tool, path, fields
    type: str
    required: bool = False
    help: str = ""
    options: tuple[str, ...] = ()
    default: Any = None


@dataclass(frozen=True, slots=True)
class Block:
    """One kind of step, as the builder shows it and the engine runs it."""

    kind: str
    name: str
    description: str
    fields: tuple[ConfigField, ...] = field(default_factory=tuple)
    #: Whether a run stops on this block until something happens.
    waits: str | None = None  # "user" | "event" | None
    #: Which switch on WorkflowSettings the block's side effect sits behind.
    switch: str | None = None

    def config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": f.key, "label": f.label, "type": f.type, "required": f.required,
                "help": f.help, "options": list(f.options), "default": f.default,
            }
            for f in self.fields
        ]


_SAVE_AS = ConfigField(
    "save_as", "Save result as", "text",
    help="The context key later steps read this block's output from.",
)

BLOCKS: Final[tuple[Block, ...]] = (
    Block(
        "documents",
        "Read the task's documents",
        "Loads the subject task from SharePoint and downloads its attachments "
        "onto the run. Says whether any were found, so the next step can ask "
        "the person for them when there are none.",
        (_SAVE_AS,),
    ),
    Block(
        "ask_user",
        "Ask the person",
        "Stops the run with a question. A form of fields, files, or a thing to "
        "review and mark verified. The answer is saved and the run carries on.",
        (
            ConfigField("title", "Title", "text", required=True),
            ConfigField("message", "Message", "textarea", help="Placeholders allowed: {{ requirements.summary }}."),
            ConfigField(
                "mode", "Mode", "select", options=("form", "review"), default="form",
                help="form: ask for the fields below. review: show a context value for the person to check and verify.",
            ),
            ConfigField("review_of", "Review this context key", "path", help="For review mode: e.g. suppliers.suppliers."),
            ConfigField("fields", "Fields", "fields", help="For form mode: what to ask for."),
            ConfigField("allow_files", "Allow file uploads", "boolean", default=False),
            ConfigField("resume_label", "Button label", "text", default="Continue"),
            _SAVE_AS,
        ),
        waits="user",
    ),
    Block(
        "extract",
        "Read requirements from the documents",
        "Reads every document on the run (from the task, from the person, or "
        "both) with the extraction model and writes out the items and the "
        "conditions a supplier would need. Merges items the person typed in.",
        (
            ConfigField("schema", "Shape", "select", options=tuple(SCHEMAS), default="requirements"),
            ConfigField("instructions", "Extra instructions", "textarea"),
            ConfigField("merge_items_from", "Also take items from", "path", help="e.g. answers.items"),
            ConfigField("sources", "Which files", "text", default="sharepoint,upload", help="Comma-separated file sources."),
            _SAVE_AS,
        ),
    ),
    Block(
        "agent",
        "Ask the model",
        "One model call over the context, with an optional web search and an "
        "optional shape for the answer. This is where suppliers are found.",
        (
            ConfigField("prompt", "Prompt", "textarea", required=True, help="Placeholders allowed."),
            ConfigField("schema", "Answer shape", "select", options=("",) + tuple(SCHEMAS), default=""),
            ConfigField("web_search", "Search the web", "boolean", default=False),
            ConfigField("instructions", "System instructions", "textarea"),
            _SAVE_AS,
        ),
    ),
    Block(
        "email",
        "Send an email",
        "Composes a mail per recipient from a subject and body template and "
        "sends it from the intake mailbox, tagged so a reply is recognised. "
        "Held, not sent, while the mail switch is off.",
        (
            ConfigField("to_path", "Recipients from", "path", required=True, help="A list in the context, e.g. answers.value."),
            ConfigField("to_field", "Address field", "text", default="email"),
            ConfigField("name_field", "Name field", "text", default="name"),
            ConfigField("subject", "Subject", "text", required=True),
            ConfigField("body", "Body", "textarea", required=True, help="{{ recipient.name }} is the one being written to."),
            ConfigField("template_key", "Or take subject and body from template", "text", help="A form template of kind rfq_email."),
            _SAVE_AS,
        ),
        switch="send_email",
    ),
    Block(
        "wait_email",
        "Wait for replies",
        "Waits for mail carrying the run's tag to arrive in the intake mailbox, "
        "and pulls the attachments onto the run. Carries on when enough have "
        "replied, or when the wait runs out.",
        (
            ConfigField("min_replies", "Carry on after this many replies", "number", default=1),
            ConfigField("timeout_days", "Give up after (days)", "number", default=14),
            ConfigField("wait_for_all", "Wait for every recipient", "boolean", default=False),
            _SAVE_AS,
        ),
        waits="event",
    ),
    Block(
        "compare",
        "Compare the supplier quotes",
        "Reads the quotes that came back, compares them like with like, and "
        "keeps the comparison. With one reply there is nothing to compare and "
        "that one is taken.",
        (
            ConfigField("markup_percent", "Mark up the chosen prices by (%)", "number", default=0),
            _SAVE_AS,
        ),
    ),
    Block(
        "endpoint",
        "Call the app",
        "Calls one of the app's own routes as the run's owner, with arguments "
        "filled from the context. Anything the owner could do on a screen.",
        (
            ConfigField("tool", "Route", "tool", required=True),
            ConfigField("arguments", "Arguments", "json", help="Placeholders allowed; a whole value may be a placeholder."),
            _SAVE_AS,
        ),
    ),
    Block(
        "wait_status",
        "Wait for a status",
        "Polls one of the app's routes until a field reaches one of the wanted "
        "values — a quote approved, say — or one of the failing values.",
        (
            ConfigField("tool", "Route", "tool", required=True),
            ConfigField("arguments", "Arguments", "json"),
            ConfigField("status_path", "Field to watch", "text", default="status"),
            ConfigField("until", "Carry on when it is", "text", required=True, help="Comma-separated."),
            ConfigField("fail_on", "Stop when it is", "text", help="Comma-separated."),
            ConfigField("poll_minutes", "Check every (minutes)", "number", default=10),
            ConfigField("timeout_days", "Give up after (days)", "number", default=30),
            _SAVE_AS,
        ),
        waits="event",
    ),
    Block(
        "notify",
        "Tell the person",
        "Raises an in-app notification for the run's owner.",
        (
            ConfigField("title", "Title", "text", required=True),
            ConfigField("body", "Body", "textarea"),
        ),
    ),
    Block(
        "zoho_create",
        "Create the quote in Zoho Books",
        "Creates the estimate from an approved quote request, then fetches the "
        "commercial proposal PDF and any documents on it onto the run. Held "
        "while the Zoho switch is off.",
        (
            ConfigField("quote_request_path", "Quote request id from", "path", required=True),
            _SAVE_AS,
        ),
        switch="write_zoho",
    ),
    Block(
        "sharepoint_attach",
        "Attach files to the task",
        "Adds files the run holds to the subject task's attachments. Adds only "
        "— never edits or removes what is there. Held while the SharePoint "
        "switch is off.",
        (
            ConfigField("sources", "Which files", "text", default="zoho", help="Comma-separated file sources."),
            _SAVE_AS,
        ),
        switch="write_sharepoint",
    ),
)

BLOCKS_BY_KIND: Final[dict[str, Block]] = {b.kind: b for b in BLOCKS}


def tool_choices() -> list[dict[str, str]]:
    """The routes an ``endpoint`` block may call, for the builder's picker."""
    return [
        {"key": t.key, "label": t.label, "module": t.module_key, "method": t.method, "path": t.path}
        for t in LIVE_TOOLS
        if not t.is_client and t.module_key != "app"
    ]


class StepError(ValueError):
    """A step definition the engine could not run."""


def validate_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check a flow's steps and hand back a normalised copy.

    Keys are unique and URL-safe, every kind exists, every required field is
    present, and an ``endpoint`` names a real tool. Runs copy the result, so
    a flow that passes here is one every run can follow.
    """
    if not isinstance(steps, list) or not steps:
        raise StepError("A workflow needs at least one step.")
    tools = {t["key"] for t in tool_choices()}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise StepError(f"Step {index + 1} is not an object.")
        key = str(raw.get("key") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        if not key or not key.replace("_", "").replace("-", "").isalnum():
            raise StepError(f"Step {index + 1} needs a short key made of letters, digits, _ or -.")
        if key in seen:
            raise StepError(f"Two steps are called {key!r}.")
        seen.add(key)
        block = BLOCKS_BY_KIND.get(kind)
        if block is None:
            raise StepError(f"Step {key!r}: no block called {kind!r}.")
        config = raw.get("config") or {}
        if not isinstance(config, dict):
            raise StepError(f"Step {key!r}: config must be an object.")
        for f in block.fields:
            if f.required and config.get(f.key) in (None, ""):
                raise StepError(f"Step {key!r}: {f.label} is required.")
        if kind in ("endpoint", "wait_status") and config.get("tool") not in tools:
            raise StepError(f"Step {key!r}: {config.get('tool')!r} is not a route the app has.")
        when = raw.get("when")
        if when is not None and not isinstance(when, dict):
            raise StepError(f"Step {key!r}: when must be an object like {{path, is}}.")
        out.append(
            {
                "key": key,
                "kind": kind,
                "name": str(raw.get("name") or block.name)[:160],
                "config": config,
                "when": when,
            }
        )
    return out


# ── the presales flow ──────────────────────────────────────────────────

PRESALES_KEY: Final = "presales_rfq"

#: The mail the flow sends to each supplier. Also seeded as a form template of
#: kind ``rfq_email`` (see ``app.forms.catalogue``) so the wording is a super
#: admin's to change in the templates screen; the step falls back to this
#: when that template is not there.
RFQ_SUBJECT: Final = "Request for quotation — {{ task.title }} [{{ run.tag }}]"
RFQ_BODY: Final = """\
Dear {{ recipient.name }},

We are preparing an offer for {{ requirements.customer }} and would like your best quotation for the following:

{{ requirements.items | bullets }}

Requirements:
{{ requirements.requirements | bullets }}

Please quote in AED, delivered to the UAE, stating lead time, validity and payment terms. Please keep the reference [{{ run.tag }}] in the subject of your reply so it reaches the right file.

Kind regards,
{{ owner.name }}
Hamdaz Technologies
"""


def _step(key: str, kind: str, name: str, config: dict[str, Any], when: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "kind": kind, "name": name, "config": config, "when": when}


PRESALES_STEPS: Final[list[dict[str, Any]]] = [
    _step("docs", "documents", "Read the task's documents", {"save_as": "docs"}),
    _step(
        "ask_docs", "ask_user", "Ask for the requirement documents",
        {
            "title": "What needs pricing?",
            "message": (
                "The task \"{{ task.title }}\" has no documents attached in SharePoint. "
                "Upload the requirement documents, or list the items directly."
            ),
            "mode": "form",
            "fields": [
                {
                    "key": "items", "label": "Items", "type": "table", "required": False,
                    "columns": [
                        {"key": "description", "label": "Description", "type": "text"},
                        {"key": "part_number", "label": "Part no.", "type": "text"},
                        {"key": "brand", "label": "Brand", "type": "text"},
                        {"key": "quantity", "label": "Qty", "type": "number"},
                        {"key": "unit", "label": "Unit", "type": "text"},
                    ],
                },
                {"key": "notes", "label": "Anything else a supplier should know", "type": "textarea"},
            ],
            "allow_files": True,
            "resume_label": "Continue",
            "save_as": "answers",
        },
        when={"path": "docs.found", "is": False},
    ),
    _step(
        "extract", "extract", "Read requirements",
        {
            "schema": "requirements",
            "sources": "sharepoint,upload",
            "merge_items_from": "answers.items",
            "instructions": "The company is a UAE trading and contracting firm quoting for a customer.",
            "save_as": "requirements",
        },
    ),
    _step(
        "confirm_requirements", "ask_user", "Check the requirements",
        {
            "title": "Is this what needs pricing?",
            "message": "{{ requirements.summary }}\n\nMissing from the documents: {{ requirements.missing | bullets }}",
            "mode": "review",
            "review_of": "requirements",
            "resume_label": "Verified",
            "save_as": "requirements",
        },
    ),
    _step(
        "find_suppliers", "agent", "Find suppliers in the UAE",
        {
            "prompt": (
                "Find suppliers or distributors in the United Arab Emirates who can supply these items:\n"
                "{{ requirements.items | bullets }}\n\n"
                "Conditions: {{ requirements.requirements | bullets }}\n\n"
                "Prefer authorised distributors and manufacturers' regional offices. For each, give a sales "
                "or enquiries email address found on their own website — never invent one; leave it empty "
                "if you cannot find it. Five to eight suppliers at most, the most relevant first."
            ),
            "schema": "suppliers",
            "web_search": True,
            "instructions": "You research suppliers for a UAE trading company. Only report what you actually found.",
            "save_as": "suppliers",
        },
    ),
    _step(
        "verify_suppliers", "ask_user", "Verify the suppliers",
        {
            "title": "Send the request for quotation to these suppliers?",
            "message": "Check the addresses. Remove anyone who should not be asked, add anyone missing, then verify.",
            "mode": "review",
            "review_of": "suppliers.suppliers",
            "resume_label": "Verified — send",
            "save_as": "verified_suppliers",
        },
    ),
    _step(
        "send_rfq", "email", "Send the request for quotation",
        {
            "to_path": "verified_suppliers",
            "to_field": "email",
            "name_field": "name",
            "template_key": "rfq_email",
            "subject": RFQ_SUBJECT,
            "body": RFQ_BODY,
            "save_as": "rfq",
        },
    ),
    _step(
        "wait_replies", "wait_email", "Wait for supplier replies",
        {"min_replies": 1, "timeout_days": 14, "wait_for_all": False, "save_as": "replies"},
    ),
    _step("compare", "compare", "Compare the quotes", {"markup_percent": 0, "save_as": "comparison"}),
    _step(
        "draft_quote", "endpoint", "Draft the quote request",
        {
            "tool": "quote_requests.create",
            "arguments": {
                "team": "{{ run.team }}",
                "title": "{{ task.title }}",
                "customer_name": "{{ requirements.customer }}",
                "reference_number": "{{ task.id }}",
                "items": "{{ comparison.items }}",
                "notes": "Prepared by the presales workflow {{ run.tag }} from {{ comparison.chosen.supplier_name }}.",
            },
            "save_as": "quote",
        },
    ),
    _step(
        "review_quote", "ask_user", "Review the quote request",
        {
            "title": "The quote request is drafted",
            "message": (
                "Open it, check the lines and prices, and send it for approval from the quote page. "
                "Come back here once it is sent."
            ),
            "mode": "review",
            "review_of": "quote",
            "resume_label": "I have sent it for approval",
            "save_as": "quote_reviewed",
        },
    ),
    _step(
        "wait_approval", "wait_status", "Wait for approval",
        {
            "tool": "quote_requests.get",
            "arguments": {"request_id": "{{ quote.id }}"},
            "status_path": "status",
            "until": "approved,created_in_zoho",
            "fail_on": "rejected",
            "poll_minutes": 10,
            "timeout_days": 30,
            "save_as": "approved_quote",
        },
    ),
    _step(
        "notify_approved", "notify", "Tell the owner it is approved",
        {"title": "Quote approved — {{ task.title }}", "body": "The quote request was approved. The workflow is waiting for you to allow the Zoho quote."},
    ),
    _step(
        "allow_zoho", "ask_user", "Create the quote in Zoho?",
        {
            "title": "Create this quote in Zoho Books?",
            "message": "The quote request {{ approved_quote.reference }} is approved. Creating it in Zoho Books makes it the customer's quote.",
            "mode": "review",
            "review_of": "approved_quote",
            "resume_label": "Create in Zoho",
            "save_as": "zoho_allowed",
        },
    ),
    _step("zoho", "zoho_create", "Create in Zoho Books", {"quote_request_path": "quote.id", "save_as": "zoho"}),
    _step("attach", "sharepoint_attach", "Attach CP and TP to the task", {"sources": "zoho", "save_as": "attached"}),
    _step(
        "done", "notify", "Tell the owner it is done",
        {"title": "Presales workflow finished — {{ task.title }}", "body": "The quote is in Zoho Books and its documents are on the task."},
    ),
]


@dataclass(frozen=True, slots=True)
class FlowSpec:
    key: str
    name: str
    description: str
    team_slug: str | None
    trigger: str
    steps: list[dict[str, Any]]


FLOWS: Final[tuple[FlowSpec, ...]] = (
    FlowSpec(
        PRESALES_KEY,
        "Presales: from task to Zoho quote",
        "Reads the task's documents (or asks for them), works out what needs pricing, "
        "finds UAE suppliers, sends them a request for quotation, waits for replies, "
        "compares them, drafts the quote request, waits for its approval, creates the "
        "quote in Zoho Books and attaches the CP and TP to the task.",
        "presales",
        "manual",
        PRESALES_STEPS,
    ),
)
