"""The workflow engine against the database, with the world stubbed out.

Every block is driven through ``service.start`` / ``service.answer`` /
``advance`` with small flows written for the test, and every service a
block could reach — SharePoint, the mailbox, Zoho, the two models, the route
executor — is a fake handed in through ``Services``. Nothing here leaves the
process except for the test database.

What is pinned above all is the switches: with ``send_email``,
``write_sharepoint`` and ``write_zoho`` off the run reaches its end with a
``held`` account of what it would have done and the fakes see no call.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import fields as dc_fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.assistant.executor import ToolOutcome
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.comparison.extraction import ExtractedItem, ExtractedQuote
from app.core.config import get_settings
from app.core.security import verify
from app.forms import service as forms_service
from app.forms.catalogue import RFQ_EMAIL
from app.models.comparison import QuoteComparison
from app.models.intake import IntakeMessage
from app.models.notification import Notification
from app.models.quoting import QuoteStatus
from app.models.templates import FormTemplate
from app.models.workflow import (
    FileSource,
    RunEventKind,
    RunStatus,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowRunFile,
    WorkflowRunMessage,
)
from app.proposals.sharepoint import ProposalTask
from app.quoting import service as quoting_service
from app.roles import service as roles_service
from app.teams import service as teams_service
from app.workflows import service
from app.workflows.catalogue import PRESALES_KEY, RFQ_BODY, RFQ_SUBJECT
from app.workflows.engine import Services, WorkflowError, advance
from app.workflows.service import WorkflowConflictError
from app.workflows.worker import WorkflowWorker

# ── the fakes ──────────────────────────────────────────────────────────


def make_task(task_id: str = "42", title: str = "Pump spares", **extra: Any) -> ProposalTask:
    """A Proposals row with every field blank but the ones named."""
    values: dict[str, Any] = {f.name: None for f in dc_fields(ProposalTask)}
    values.update(id=task_id, title=title, has_attachments=False)
    values.update(extra)
    return ProposalTask(**values)


class FakeSharePoint:
    """Stands in for the Proposals list. Records every write it is asked for."""

    def __init__(self, task: ProposalTask | None = None, attachments: list[tuple[str, bytes]] | None = None):
        self._task = task
        self.attachments = list(attachments or [])
        self.added: list[tuple[str, str, bytes]] = []
        self.deleted: list[tuple] = []
        self.calls: list[tuple] = []

    async def task(self, task_id: str) -> ProposalTask:
        self.calls.append(("task", task_id))
        if self._task is None:
            raise LookupError(f"no task {task_id}")
        return self._task

    async def attachments_of(self, task_id: str) -> list[dict[str, Any]]:
        self.calls.append(("attachments_of", task_id))
        return [{"file_name": name, "size": len(content)} for name, content in self.attachments]

    async def attachment_content(self, task_id: str, name: str) -> bytes:
        self.calls.append(("attachment_content", task_id, name))
        return dict(self.attachments)[name]

    async def add_attachment(self, task_id: str, name: str, content: bytes) -> None:
        self.added.append((task_id, name, content))

    async def delete_attachment(self, *args: Any) -> None:
        self.deleted.append(args)


class FakeMail:
    """Stands in for the Graph mailbox: outbound sends and inbound attachments."""

    def __init__(self, attachments: list[tuple[str, str, bytes]] | None = None):
        self.sent: list[dict[str, Any]] = []
        self.attachment_calls: list[tuple[str, str]] = []
        self._attachments = list(attachments or [])
        self.error: Exception | None = None

    async def send(self, *, sender: str, recipients: list[str], subject: str, html: str) -> dict:
        if self.error:
            raise self.error
        self.sent.append({"sender": sender, "recipients": list(recipients), "subject": subject, "html": html})
        return {"id": f"msg-{len(self.sent)}"}

    async def attachments(self, mailbox: str, message_id: str) -> list[tuple[str, str, bytes]]:
        self.attachment_calls.append((mailbox, message_id))
        return list(self._attachments)


class FakeZoho:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.contact: dict[str, Any] | None = {"contact_id": "C-1", "contact_name": "ADNOC"}

    async def find_contact(self, name: str) -> dict[str, Any] | None:
        self.calls.append(("find_contact", name))
        return self.contact

    async def create_estimate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_estimate", payload))
        return {"estimate_id": "E-1", "estimate_number": "EST-001"}

    async def estimate_pdf(self, estimate_id: str) -> bytes:
        self.calls.append(("estimate_pdf", estimate_id))
        return b"%PDF-1.4 commercial proposal"

    async def estimate_documents(self, estimate_id: str) -> list[dict[str, Any]]:
        self.calls.append(("estimate_documents", estimate_id))
        return [{"document_id": "D-1", "file_name": "technical.pdf"}]

    async def document(self, estimate_id: str, document_id: str) -> tuple[bytes, str]:
        self.calls.append(("document", estimate_id, document_id))
        return b"%PDF-1.4 technical proposal", "application/pdf"


def quote(supplier: str, *prices: float) -> ExtractedQuote:
    return ExtractedQuote(
        supplier_name=supplier, quote_number=f"Q-{supplier}", quote_date="2026-09-01", currency="AED",
        validity="30 days", delivery_time="2 weeks", payment_terms="30 days", warranty="", incoterms="DDP",
        contact=f"sales@{supplier.lower()}.ae", discount=0, freight=0, tax=0,
        quoted_total=sum(p * 2 for p in prices),
        items=[
            ExtractedItem(
                description=f"Line {i + 1}", part_number=f"PN-{i + 1}", brand="Acme", unit="pcs",
                quantity=2, unit_price=price, line_total=price * 2, lead_time="",
            )
            for i, price in enumerate(prices)
        ],
        note="",
    )


class FakeExtractor:
    """Stands in for the Claude quote reader. One quote per readable, scripted."""

    configured = True

    def __init__(self, quotes: list[ExtractedQuote] | None = None) -> None:
        self.quotes = list(quotes or [])
        self.read: list[list[str]] = []

    async def read_all(self, readables: list) -> list:
        self.read.append([r.file_name for r in readables])
        return [self.quotes[i] if i < len(self.quotes) else quote("Anon", 1) for i in range(len(readables))]

    def _anthropic(self):
        raise RuntimeError("no network in tests")


class FakeLLM:
    configured = True

    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def research(self, **kwargs: Any) -> tuple[str, int, int]:
        self.calls.append(kwargs)
        return self.text, 10, 5


class FakeExecutor:
    """Stands in for the route executor. Plays scripted outcomes and records the call."""

    def __init__(self, *outcomes: ToolOutcome) -> None:
        self.script = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def ok(body: Any, status: int = 200) -> ToolOutcome:
        return ToolOutcome(status, True, json.dumps(body), False, 3, body)

    @staticmethod
    def refused(status: int = 403, detail: str = "Not yours") -> ToolOutcome:
        text = json.dumps({"error": detail, "status": status})
        return ToolOutcome(status, False, text, False, 3, {"detail": detail})

    async def call(self, spec, arguments: dict[str, Any], *, session_cookie: str) -> ToolOutcome:
        self.calls.append({"tool": spec.key, "arguments": arguments, "cookie": session_cookie})
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0] if self.script else self.ok({})


class FakeReader:
    """Stands in for ``DocumentReader``; monkeypatched onto the steps module."""

    payload: dict[str, Any] = {
        "summary": "Two valves for a pump overhaul.",
        "items": [
            {"description": "Gate valve", "part_number": "GV-100", "brand": "KSB", "quantity": 2,
             "unit": "pcs", "specification": "PN16", "source": "spec.csv"},
        ],
        "requirements": ["Delivered to Abu Dhabi"],
        "missing": ["Delivery date"],
        "customer": "",
        "deadline": "2026-10-01",
    }
    calls: list[dict[str, Any]] = []

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def read(self, readables, *, schema, instructions="", typed_items=None):
        FakeReader.calls.append(
            {"files": [r.file_name for r in readables], "schema": schema, "instructions": instructions,
             "typed_items": typed_items}
        )
        return json.loads(json.dumps(self.payload)), 0.0125


CSV = b"description,part_number,qty\nGate valve,GV-100,2\n"


def services(**overrides: Any) -> Services:
    base = dict(
        settings=get_settings(),
        sharepoint=FakeSharePoint(make_task()),
        mail=FakeMail(),
        zoho=FakeZoho(),
        extractor=FakeExtractor(),
        llm=FakeLLM(),
        executor=FakeExecutor(),
        model_key="",
    )
    base.update(overrides)
    return Services(**base)


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def seeded(db):
    await roles_service.seed_system_roles(db)
    await db.commit()


@pytest.fixture
async def owner(db, seeded):
    user = await upsert_user(
        db, EntraIdentity(object_id="amina", email="amina@hamdaz.com", display_name="Amina")
    )
    await db.commit()
    return user


@pytest.fixture
async def team(db, seeded):
    row = await teams_service.create_team(db, name="Presales", slug="presales")
    await db.commit()
    return row


@pytest.fixture
async def switches(db):
    """The WorkflowSettings row. Named so as not to shadow conftest's ``settings``."""
    return await service.get_settings(db)


@pytest.fixture
def reader(monkeypatch):
    FakeReader.calls = []
    monkeypatch.setattr("app.workflows.steps.DocumentReader", FakeReader)
    return FakeReader


async def make_flow(db, owner, steps: list[dict[str, Any]], key: str = "t") -> Any:
    return await service.create_flow(
        db, payload={"key": key, "name": f"Flow {key}", "steps": steps}, actor=owner
    )


async def start(db, flow, owner, switches, svc: Services, subject_id: str = "42") -> WorkflowRun:
    return await service.start(
        db, flow, owner=owner, subject_id=subject_id, subject_label="Pump spares",
        settings=switches, services=svc,
    )


async def events(db, run: WorkflowRun, kind: str | None = None) -> list[WorkflowRunEvent]:
    query = select(WorkflowRunEvent).where(WorkflowRunEvent.run_id == run.id).order_by(WorkflowRunEvent.seq)
    if kind:
        query = query.where(WorkflowRunEvent.kind == kind)
    return list((await db.scalars(query)).all())


async def files(db, run: WorkflowRun) -> list[WorkflowRunFile]:
    return list(
        (await db.scalars(select(WorkflowRunFile).where(WorkflowRunFile.run_id == run.id).order_by(WorkflowRunFile.file_name))).all()
    )


async def messages(db, run: WorkflowRun) -> list[WorkflowRunMessage]:
    return list(
        (await db.scalars(select(WorkflowRunMessage).where(WorkflowRunMessage.run_id == run.id).order_by(WorkflowRunMessage.address))).all()
    )


def review_step(key: str, save_as: str, review_of: str = "task.title") -> dict[str, Any]:
    """A pause: a review the test answers with whatever the next step needs."""
    return {
        "key": key, "kind": "ask_user",
        "config": {"title": f"Check {key}", "mode": "review", "review_of": review_of, "save_as": save_as},
    }


# ── seeding and settings ───────────────────────────────────────────────


async def test_seed_flows_creates_the_presales_flow_once_and_keeps_edits(db, owner, team) -> None:
    assert await service.seed_flows(db) == 1
    flow = await service.get_flow(db, PRESALES_KEY)
    assert flow.is_system is True
    assert flow.enabled is True
    assert flow.team_id == team.id
    assert flow.version == 1
    assert [s["key"] for s in flow.steps][:3] == ["docs", "ask_docs", "extract"]

    await service.update_flow(db, flow, changes={"name": "Our presales", "steps": flow.steps[:2]}, actor=owner)
    assert flow.version == 2
    assert await service.seed_flows(db) == 0
    again = await service.get_flow(db, PRESALES_KEY)
    assert again.name == "Our presales"
    assert len(again.steps) == 2
    assert len(await service.list_flows(db)) == 1

    with pytest.raises(WorkflowConflictError, match="cannot be deleted"):
        await service.delete_flow(db, again)


async def test_the_switches_ship_off(db, switches) -> None:
    assert switches.send_email is False
    assert switches.write_sharepoint is False
    assert switches.write_zoho is False
    assert switches.from_mailbox is None
    assert switches.poll_seconds == 60
    row = await service.update_settings(db, actor_id=None, changes={"send_email": True, "from_mailbox": " rfq@hamdaz.com "})
    assert row.send_email is True and row.from_mailbox == "rfq@hamdaz.com"
    row = await service.update_settings(db, actor_id=None, changes={"from_mailbox": ""})
    assert row.from_mailbox is None


# ── documents → ask → extract ──────────────────────────────────────────

DOCS_FLOW = [
    {"key": "docs", "kind": "documents", "config": {"save_as": "docs"}},
    {
        "key": "ask_docs", "kind": "ask_user",
        "config": {
            "title": "What needs pricing for {{ task.title }}?", "mode": "form",
            "fields": [{"key": "items", "label": "Items", "type": "table"}, {"key": "notes", "label": "Notes", "type": "textarea"}],
            "allow_files": True, "save_as": "answers",
        },
        "when": {"path": "docs.found", "is": False},
    },
    {
        "key": "extract", "kind": "extract",
        "config": {"schema": "requirements", "merge_items_from": "answers.items", "instructions": "UAE firm.", "save_as": "requirements"},
    },
]


async def test_documents_found_skips_the_question_and_reads_them(db, owner, switches, reader) -> None:
    sp = FakeSharePoint(make_task(end_user="ADNOC", bid_closing_date="2026-10-01"), [("spec.csv", CSV)])
    flow = await make_flow(db, owner, DOCS_FLOW)
    run = await start(db, flow, owner, switches, services(sharepoint=sp))

    assert run.status == RunStatus.COMPLETED
    assert run.subject_label == "Pump spares"
    assert run.context["docs"] == {"found": True, "count": 1, "files": ["spec.csv"]}
    assert run.context["task"]["end_user"] == "ADNOC"
    assert run.context["requirements"]["items"][0]["part_number"] == "GV-100"
    # The customer the reader left blank is filled from the task.
    assert run.context["requirements"]["customer"] == "ADNOC"
    assert run.cost_usd == Decimal("0.0125")

    stored = await files(db, run)
    assert [(f.source, f.file_name, f.content) for f in stored] == [(FileSource.SHAREPOINT, "spec.csv", CSV)]
    assert reader.calls == [
        {"files": ["spec.csv"], "schema": reader.calls[0]["schema"], "instructions": "UAE firm.", "typed_items": None}
    ]
    assert reader.calls[0]["schema"]["required"][:2] == ["summary", "items"]

    kinds = [(e.kind, e.step_key) for e in await events(db, run)]
    assert (RunEventKind.STEP_SKIPPED, "ask_docs") in kinds
    assert kinds[0] == (RunEventKind.STARTED, None)
    assert kinds[-1] == (RunEventKind.COMPLETED, None)
    assert run.step_index == 3 and run.pending is None and run.finished_at is not None


async def test_no_documents_asks_then_merges_what_was_typed_and_uploaded(db, owner, switches, reader) -> None:
    flow = await make_flow(db, owner, DOCS_FLOW)
    run = await start(db, flow, owner, switches, services(sharepoint=FakeSharePoint(make_task())))

    assert run.status == RunStatus.WAITING_USER
    assert run.step_index == 1
    assert run.context["docs"] == {"found": False, "count": 0, "files": []}
    assert run.pending["step_key"] == "ask_docs"
    assert run.pending["mode"] == "form"
    assert run.pending["allow_files"] is True
    assert run.pending["title"] == "What needs pricing for Pump spares?"
    assert [f["key"] for f in run.pending["fields"]] == ["items", "notes"]
    assert run.pending["review_value"] is None
    waiting = await events(db, run, RunEventKind.WAITING)
    assert waiting[-1].payload["on"] == "user"

    upload = await service.add_upload(db, run, file_name="list.csv", content=CSV, content_type="text/csv")
    assert upload.source == FileSource.UPLOAD and upload.step_key == "ask_docs" and upload.origin == "Amina"

    typed = {"description": "Gasket", "part_number": "", "brand": "", "quantity": 10, "unit": "pcs"}
    run = await service.answer(
        db, run, user=owner, values={"items": [typed, {"description": "", "quantity": ""}], "notes": "Urgent"},
        value=None, settings=switches, services=services(),
    )
    assert run.status == RunStatus.COMPLETED
    assert run.context["answers"] == {"items": [typed, {"description": "", "quantity": ""}], "notes": "Urgent", "files": ["list.csv"]}
    # The reader saw the upload and the one typed row that said anything.
    assert reader.calls[-1]["files"] == ["list.csv"]
    assert reader.calls[-1]["typed_items"] == [json.dumps(typed, ensure_ascii=False)]
    assert run.context["requirements"]["summary"] == reader.payload["summary"]
    answered = await events(db, run, RunEventKind.ANSWERED)
    assert answered[0].payload == {"mode": "form", "files": ["list.csv"], "fields": ["items", "notes"]}
    assert answered[0].by_user_id == owner.id


async def test_typed_items_alone_need_no_model(db, owner, switches, reader) -> None:
    flow = await make_flow(db, owner, DOCS_FLOW)
    run = await start(db, flow, owner, switches, services(sharepoint=FakeSharePoint(make_task(end_user="DEWA"))))
    run = await service.answer(
        db, run, user=owner, values={"items": [{"description": "Gasket", "quantity": "3", "unit": "pcs"}], "notes": "By Monday"},
        value=None, settings=switches, services=services(),
    )
    assert run.status == RunStatus.COMPLETED
    assert reader.calls == []
    req = run.context["requirements"]
    assert req["items"] == [
        {"description": "Gasket", "part_number": "", "brand": "", "quantity": 3.0, "unit": "pcs",
         "specification": "", "source": "typed in"}
    ]
    assert req["summary"] == "By Monday" and req["requirements"] == ["By Monday"]
    assert req["customer"] == "DEWA"
    assert run.cost_usd == 0


async def test_nothing_to_read_fails_the_run_with_a_sentence(db, owner, switches, reader) -> None:
    flow = await make_flow(db, owner, DOCS_FLOW)
    run = await start(db, flow, owner, switches, services(sharepoint=FakeSharePoint(make_task())))
    run = await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=services())
    assert run.status == RunStatus.FAILED
    assert run.error.startswith("There is nothing to read")
    assert (await events(db, run, RunEventKind.ERROR))[0].step_key == "extract"


async def test_a_missing_task_fails_the_documents_step(db, owner, switches) -> None:
    flow = await make_flow(db, owner, DOCS_FLOW)
    run = await start(db, flow, owner, switches, services(sharepoint=FakeSharePoint(None)))
    assert run.status == RunStatus.FAILED
    assert "Could not read task 42 from SharePoint" in run.error
    run2 = await start(db, flow, owner, switches, services(sharepoint=None), subject_id="43")
    assert run2.status == RunStatus.FAILED and "not configured" in run2.error


# ── ask_user in review mode ────────────────────────────────────────────


async def test_review_shows_the_value_and_keeps_an_edit(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [review_step("check", "checked"), review_step("again", "kept", "checked")])
    run = await start(db, flow, owner, switches, services())
    assert run.status == RunStatus.WAITING_USER
    assert run.pending["mode"] == "review"
    assert run.pending["review_of"] == "task.title"
    assert run.pending["review_value"] == "Pump spares"
    assert run.pending["resume_label"] == "Continue"

    run = await service.answer(db, run, user=owner, values={}, value="Pump spares (edited)", settings=switches, services=services())
    assert run.context["checked"] == "Pump spares (edited)"
    # The second review shows what the first saved; verifying as shown keeps it.
    assert run.pending["step_key"] == "again"
    assert run.pending["review_value"] == "Pump spares (edited)"
    run = await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=services())
    assert run.status == RunStatus.COMPLETED
    assert run.context["kept"] == "Pump spares (edited)"
    notes = [e.payload["note"] for e in await events(db, run, RunEventKind.STEP_COMPLETED)]
    assert notes == ["verified", "verified"]


async def test_answering_a_run_that_is_not_waiting_is_a_conflict(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [{"key": "n", "kind": "notify", "config": {"title": "Hi"}}])
    run = await start(db, flow, owner, switches, services())
    assert run.status == RunStatus.COMPLETED
    with pytest.raises(WorkflowConflictError):
        await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=services())
    with pytest.raises(WorkflowConflictError):
        await service.add_upload(db, run, file_name="x.csv", content=CSV, content_type="text/csv")


# ── agent ──────────────────────────────────────────────────────────────


async def test_agent_asks_the_model_with_the_rendered_prompt(db, owner, switches) -> None:
    llm = FakeLLM(json.dumps({"suppliers": [{"name": "Acme", "email": "s@acme.ae"}], "notes": "ok"}))
    flow = await make_flow(
        db, owner,
        [{"key": "find", "kind": "agent", "config": {"prompt": "Suppliers for {{ task.title }}", "schema": "suppliers", "web_search": True, "save_as": "suppliers"}}],
    )
    run = await start(db, flow, owner, switches, services(llm=llm))
    assert run.status == RunStatus.COMPLETED
    assert run.context["suppliers"]["suppliers"][0]["name"] == "Acme"
    call = llm.calls[0]
    assert call["prompt"] == "Suppliers for Pump spares"
    assert call["web_search"] is True and call["user_key"] == str(owner.id)
    assert call["schema"]["required"] == ["suppliers", "notes"]
    assert (await events(db, run, RunEventKind.STEP_COMPLETED))[0].payload["note"] == "1 result(s)"

    unconfigured = FakeLLM()
    unconfigured.configured = False
    run2 = await start(db, flow, owner, switches, services(llm=unconfigured), subject_id="43")
    assert run2.status == RunStatus.FAILED and "OPENAI_API_KEY" in run2.error


# ── email ──────────────────────────────────────────────────────────────

RECIPIENTS = [
    {"name": "Acme", "email": "sales@acme.ae"},
    {"name": "Bolt", "email": "rfq@bolt.ae"},
    {"name": "Nameless", "email": ""},
]


def email_flow(**config: Any) -> list[dict[str, Any]]:
    return [
        review_step("who", "verified_suppliers"),
        {
            "key": "send", "kind": "email",
            "config": {"to_path": "verified_suppliers", "subject": RFQ_SUBJECT, "body": RFQ_BODY, "save_as": "rfq", **config},
        },
        {"key": "after", "kind": "notify", "config": {"title": "Sent {{ rfq.count }}"}},
    ]


async def test_email_is_held_while_the_switch_is_off(db, owner, switches) -> None:
    mail = FakeMail()
    flow = await make_flow(db, owner, email_flow())
    run = await start(db, flow, owner, switches, services(mail=mail))
    run = await service.answer(db, run, user=owner, values={}, value=RECIPIENTS, settings=switches, services=services(mail=mail))

    assert run.status == RunStatus.COMPLETED
    assert mail.sent == []
    assert run.context["rfq"] == {"sent": [], "held": ["Acme", "Bolt"], "failed": [], "no_address": ["Nameless"], "from": "", "count": 2}
    rows = await messages(db, run)
    assert [(m.direction, m.address, m.state, m.party) for m in rows] == [
        ("out", "rfq@bolt.ae", "held", "Bolt"), ("out", "sales@acme.ae", "held", "Acme"),
    ]
    assert all(run.tag in m.subject for m in rows)
    assert rows[0].body.startswith("Dear Bolt,")
    held = await events(db, run, RunEventKind.HELD)
    assert len(held) == 1 and held[0].payload["what"] == "email" and held[0].payload["to"] == ["Acme", "Bolt"]
    assert await events(db, run, RunEventKind.EMAIL_SENT) == []
    assert (await db.scalar(select(Notification.title).where(Notification.user_id == owner.id))) == "Sent 2"


async def test_email_is_sent_once_per_address_when_the_switch_is_on(db, owner, switches) -> None:
    await forms_service.seed_templates(db)
    template = await db.scalar(select(FormTemplate).where(FormTemplate.key == RFQ_EMAIL))
    assert template is not None and template.kind == RFQ_EMAIL
    # A super admin's edit of the template is what goes out.
    template.fields = [
        {**f, "default": "Quote please: {{ task.title }} [{{ run.tag }}] (from the template)"}
        if f["key"] == "subject" else f
        for f in template.fields
    ]
    await db.flush()
    await service.update_settings(db, actor_id=None, changes={"send_email": True, "from_mailbox": "rfq@hamdaz.com"})

    mail = FakeMail()
    flow = await make_flow(db, owner, email_flow(template_key=RFQ_EMAIL, subject="ignored", body="ignored"))
    run = await start(db, flow, owner, switches, services(mail=mail))
    run = await service.answer(db, run, user=owner, values={}, value=RECIPIENTS, settings=switches, services=services(mail=mail))

    assert run.status == RunStatus.COMPLETED
    assert [m["recipients"] for m in mail.sent] == [["sales@acme.ae"], ["rfq@bolt.ae"]]
    assert all(m["sender"] == "rfq@hamdaz.com" for m in mail.sent)
    assert mail.sent[0]["subject"] == f"Quote please: Pump spares [{run.tag}] (from the template)"
    assert "Dear Acme," in mail.sent[0]["html"] and "<br>" in mail.sent[0]["html"]
    assert run.context["rfq"]["sent"] == ["Acme", "Bolt"]
    assert run.context["rfq"]["no_address"] == ["Nameless"]
    assert run.context["rfq"]["from"] == "rfq@hamdaz.com"
    rows = await messages(db, run)
    assert [(m.state, m.sent_at is not None) for m in rows] == [("sent", True), ("sent", True)]
    sent = await events(db, run, RunEventKind.EMAIL_SENT)
    assert sent[0].payload == {"to": ["Acme", "Bolt"], "from": "rfq@hamdaz.com"}
    assert await events(db, run, RunEventKind.HELD) == []


async def test_email_with_nobody_to_write_to_fails(db, owner, switches) -> None:
    flow = await make_flow(db, owner, email_flow())
    run = await start(db, flow, owner, switches, services())
    run = await service.answer(db, run, user=owner, values={}, value=[{"name": "Nameless"}], settings=switches, services=services())
    assert run.status == RunStatus.FAILED
    assert "No address for: Nameless" in run.error


# ── wait_email ─────────────────────────────────────────────────────────

WAIT_FLOW = [
    {"key": "wait", "kind": "wait_email", "config": {"min_replies": 1, "timeout_days": 14, "save_as": "replies"}},
    {"key": "after", "kind": "notify", "config": {"title": "{{ replies.count }} replied"}},
]


def intake(graph_id: str, subject: str, *, has_attachments: bool = True, sender: str = "sales@acme.ae") -> IntakeMessage:
    return IntakeMessage(
        graph_message_id=graph_id, status="received", subject=subject, body="Please find attached.",
        sender_email=sender, sender_name="Acme Sales", has_attachments=has_attachments,
        received_at=datetime.now(UTC),
    )


async def test_wait_email_takes_the_tagged_reply_and_its_attachments(db, owner, switches) -> None:
    await service.update_settings(db, actor_id=None, changes={"from_mailbox": "rfq@hamdaz.com"})
    mail = FakeMail(attachments=[("acme-quote.csv", "text/csv", CSV)])
    svc = services(mail=mail)
    flow = await make_flow(db, owner, WAIT_FLOW)
    run = await start(db, flow, owner, switches, svc)

    assert run.status == RunStatus.WAITING_EVENT
    assert run.pending is None
    assert run.wake_at is not None and run.wake_at > datetime.now(UTC)
    assert run.wake_at - datetime.now(UTC) <= timedelta(seconds=61)
    assert run.deadline_at is not None and run.deadline_at - datetime.now(UTC) > timedelta(days=13)
    assert run.context["_waits"]["wait"]

    db.add(intake("g-other", "RE: something else", has_attachments=True))
    await db.flush()
    run = await advance(db, run, switches, svc)
    assert run.status == RunStatus.WAITING_EVENT, "an untagged mail is not a reply"
    assert mail.attachment_calls == []
    assert await messages(db, run) == []

    db.add(intake("g-acme", f"RE: Request for quotation [{run.tag}]"))
    await db.flush()
    run = await advance(db, run, switches, svc)
    assert run.status == RunStatus.COMPLETED
    assert mail.attachment_calls == [("rfq@hamdaz.com", "g-acme")]
    inbound = await messages(db, run)
    assert [(m.direction, m.state, m.address, m.party) for m in inbound] == [("in", "received", "sales@acme.ae", "Acme Sales")]
    assert inbound[0].intake_message_id is not None
    stored = await files(db, run)
    assert [(f.source, f.file_name, f.origin, f.content_type) for f in stored] == [(FileSource.EMAIL, "acme-quote.csv", "Acme Sales", "text/csv")]
    assert stored[0].meta["sender"] == "sales@acme.ae"
    assert run.context["replies"]["count"] == 1
    assert run.context["replies"]["replies"][0]["from"] == "sales@acme.ae"
    received = await events(db, run, RunEventKind.EMAIL_RECEIVED)
    assert received[0].payload["files"] == ["acme-quote.csv"]
    assert (await db.scalar(select(Notification.title).where(Notification.user_id == owner.id))) == "1 replied"

    # Advancing again does not count the same mail twice.
    assert len(await messages(db, run)) == 1


async def test_wait_email_gives_up_when_the_deadline_passes_with_nothing(db, owner, switches) -> None:
    flow = await make_flow(db, owner, WAIT_FLOW)
    svc = services()
    run = await start(db, flow, owner, switches, svc)
    assert run.status == RunStatus.WAITING_EVENT
    run.context = {**run.context, "_waits": {"wait": (datetime.now(UTC) - timedelta(days=15)).isoformat()}}
    run = await advance(db, run, switches, svc)
    assert run.status == RunStatus.FAILED
    assert run.error == "No supplier replied within 14 days."
    assert run.wake_at is None and run.finished_at is not None


# ── compare ────────────────────────────────────────────────────────────


async def test_compare_reads_the_reply_and_marks_up_the_cheapest(db, owner, switches) -> None:
    extractor = FakeExtractor([quote("Acme", 100.0, 40.0)])
    svc = services(extractor=extractor)
    flow = await make_flow(
        db, owner,
        [review_step("pause", "ignored"), {"key": "compare", "kind": "compare", "config": {"markup_percent": 10, "save_as": "comparison"}}],
    )
    run = await start(db, flow, owner, switches, svc)
    db.add(
        WorkflowRunFile(
            run_id=run.id, step_key="wait", source=FileSource.EMAIL, file_name="acme-quote.csv",
            content_type="text/csv", size=len(CSV), content=CSV, origin="Acme Sales",
        )
    )
    await db.flush()
    run = await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=svc)

    assert run.status == RunStatus.COMPLETED, run.error
    assert extractor.read == [["acme-quote.csv"]]
    out = run.context["comparison"]
    assert out["single"] is True and out["unreadable"] == []
    assert out["chosen"]["supplier_name"] == "Acme"
    assert [(i["name"], i["rate"], i["cost_rate"], i["quantity"], i["item_code"]) for i in out["items"]] == [
        ("Line 1", 110.0, 100.0, 2.0, "PN-1"), ("Line 2", 44.0, 40.0, 2.0, "PN-2"),
    ]
    saved = await db.scalar(select(QuoteComparison).where(QuoteComparison.id == uuid.UUID(out["comparison_id"])))
    assert saved is not None
    assert saved.reference == run.tag
    assert saved.title == "Supplier quotes for Pump spares"
    assert saved.created_by_id == owner.id
    assert [q.supplier_name for q in saved.quotes] == ["Acme"]
    assert saved.quotes[0].file_bytes == CSV
    assert float(out["suppliers"][0]["total"]) == 280.0


async def test_compare_with_no_reply_file_fails(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [{"key": "compare", "kind": "compare", "config": {"save_as": "comparison"}}])
    run = await start(db, flow, owner, switches, services())
    assert run.status == RunStatus.FAILED
    assert "nothing to compare" in run.error


# ── endpoint and wait_status ───────────────────────────────────────────

ENDPOINT_FLOW = [
    review_step("lines", "items"),
    {
        "key": "draft", "kind": "endpoint",
        "config": {
            "tool": "quote_requests.create",
            "arguments": {"team": "{{ run.team }}", "title": "Quote for {{ task.title }}", "items": "{{ items }}", "n": 1},
            "save_as": "quote",
        },
    },
]


async def test_endpoint_calls_the_route_as_the_owner_with_rendered_arguments(db, owner, team, switches) -> None:
    executor = FakeExecutor(FakeExecutor.ok({"id": "q-1", "status": "draft"}, 201))
    svc = services(executor=executor)
    flow = await service.create_flow(db, payload={"key": "ep", "name": "Endpoint", "team": "presales", "steps": ENDPOINT_FLOW}, actor=owner)
    run = await start(db, flow, owner, switches, svc)
    assert run.team_id == team.id and run.context["run"]["team"] == "presales"
    lines = [{"name": "Valve", "quantity": 2, "rate": 110}]
    run = await service.answer(db, run, user=owner, values={}, value=lines, settings=switches, services=svc)

    assert run.status == RunStatus.COMPLETED
    assert run.context["quote"] == {"id": "q-1", "status": "draft"}
    call = executor.calls[0]
    assert call["tool"] == "quote_requests.create"
    assert call["arguments"] == {"team": "presales", "title": "Quote for Pump spares", "items": lines, "n": 1}
    assert isinstance(call["arguments"]["items"], list)
    claims = verify(call["cookie"], secret=get_settings().session_secret, audience=SESSION_AUDIENCE)
    assert claims["sub"] == str(owner.id)
    assert (await events(db, run, RunEventKind.STEP_COMPLETED))[-1].payload == {"note": "201", "saved_as": "quote"}


async def test_endpoint_refused_by_the_route_fails_the_run(db, owner, switches) -> None:
    svc = services(executor=FakeExecutor(FakeExecutor.refused(403, "Not your team")))
    flow = await make_flow(db, owner, ENDPOINT_FLOW)
    run = await start(db, flow, owner, switches, svc)
    run = await service.answer(db, run, user=owner, values={}, value=[], settings=switches, services=svc)
    assert run.status == RunStatus.FAILED
    assert run.error.startswith("quote_requests.create answered 403")
    assert "Not your team" in run.error
    assert run.step_index == 1


def wait_status_flow(**config: Any) -> list[dict[str, Any]]:
    return [
        {
            "key": "approval", "kind": "wait_status",
            "config": {
                "tool": "quote_requests.get", "arguments": {"request_id": "q-1"}, "status_path": "status",
                "until": "approved,created_in_zoho", "fail_on": "rejected", "poll_minutes": 5, "save_as": "approved",
                **config,
            },
        },
    ]


async def test_wait_status_polls_until_the_status_is_reached(db, owner, switches) -> None:
    executor = FakeExecutor(FakeExecutor.ok({"id": "q-1", "status": "pending_approval"}), FakeExecutor.ok({"id": "q-1", "status": "approved"}))
    svc = services(executor=executor)
    flow = await make_flow(db, owner, wait_status_flow())
    run = await start(db, flow, owner, switches, svc)

    assert run.status == RunStatus.WAITING_EVENT
    assert timedelta(minutes=4) < run.wake_at - datetime.now(UTC) <= timedelta(minutes=5)
    assert run.deadline_at - datetime.now(UTC) > timedelta(days=29)
    assert (await events(db, run, RunEventKind.WAITING))[0].payload["note"] == "still pending_approval"

    run = await advance(db, run, switches, svc)
    assert run.status == RunStatus.COMPLETED
    assert run.context["approved"] == {"id": "q-1", "status": "approved"}
    assert executor.calls[0]["arguments"] == {"request_id": "q-1"}
    assert len(executor.calls) == 2


async def test_wait_status_fails_on_the_failing_value_or_the_deadline(db, owner, switches) -> None:
    flow = await make_flow(db, owner, wait_status_flow())
    run = await start(db, flow, owner, switches, services(executor=FakeExecutor(FakeExecutor.ok({"status": "rejected"}))))
    assert run.status == RunStatus.FAILED and run.error == "It became 'rejected'."

    svc = services(executor=FakeExecutor(FakeExecutor.ok({"status": "draft"})))
    run2 = await start(db, flow, owner, switches, svc, subject_id="43")
    assert run2.status == RunStatus.WAITING_EVENT
    run2.context = {**run2.context, "_waits": {"approval": (datetime.now(UTC) - timedelta(days=31)).isoformat()}}
    run2 = await advance(db, run2, switches, svc)
    assert run2.status == RunStatus.FAILED and run2.error == "Still 'draft' after the wait ran out."


CONTEXT_LOST = (
    "app/workflows/engine.py: a step re-entered in a fresh session (the worker's "
    "tick, /wake, retry) mutates run.context in place before engine._touch copies "
    "it, so SQLAlchemy's committed value is the same already-mutated object and "
    "the shallow copies share the nested _started/_waits dicts; the UPDATE never "
    "includes context. Fix: flag_modified(run, 'context') in _touch (and _fail)."
)


@pytest.mark.xfail(reason=CONTEXT_LOST, strict=True)
@pytest.mark.parametrize("followed", [False, True], ids=["last step", "followed by a step"])
async def test_what_a_woken_step_saves_reaches_the_database(db, owner, switches, session_factory, followed) -> None:
    """The worker wakes a run in a fresh session; what the woken step saved —
    and everything the steps after it saved in the same pass — has to reach
    the database, or the next request reads a context without it."""
    executor = FakeExecutor(FakeExecutor.ok({"status": "pending_approval"}), FakeExecutor.ok({"status": "approved"}))
    svc = services(executor=executor)
    steps = wait_status_flow()
    if followed:
        steps = steps + [{"key": "after", "kind": "notify", "config": {"title": "Approved"}}]
    flow = await make_flow(db, owner, steps)
    run = await start(db, flow, owner, switches, svc)
    assert run.status == RunStatus.WAITING_EVENT
    await db.commit()

    async with session_factory() as fresh:
        woken = await service.get_run(fresh, run.id)
        assert "approved" not in woken.context
        woken = await advance(fresh, woken, await service.get_settings(fresh), svc)
        assert woken.status == RunStatus.COMPLETED
        assert woken.context["approved"] == {"status": "approved"}
        await fresh.commit()

    async with session_factory() as check:
        stored = await service.get_run(check, run.id)
        assert stored.status == RunStatus.COMPLETED
        assert stored.context.get("approved") == {"status": "approved"}


# ── notify ─────────────────────────────────────────────────────────────


async def test_notify_raises_a_notification_for_the_owner(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [{"key": "tell", "kind": "notify", "config": {"title": "Done: {{ task.title }}", "body": "Run {{ run.tag }}"}}])
    run = await start(db, flow, owner, switches, services())
    assert run.status == RunStatus.COMPLETED
    rows = list((await db.scalars(select(Notification).where(Notification.user_id == owner.id))).all())
    assert len(rows) == 1
    note = rows[0]
    assert note.title == "Done: Pump spares"
    assert note.body == f"Run {run.tag}"
    assert note.kind == "workflow" and note.source == "workflow"
    assert note.source_id == f"{run.id}:tell"
    assert note.link == f"/workflows/runs/{run.id}"
    assert (await events(db, run, RunEventKind.NOTIFIED))[0].payload == {"title": "Done: Pump spares"}


# ── zoho_create ────────────────────────────────────────────────────────


async def approved_request(db, owner, team):
    request = await quoting_service.create(
        db,
        payload={
            "title": "Pump spares", "customer_name": "ADNOC", "reference_number": "42",
            "notes": "From the workflow",
            "items": [{"name": "Gate valve", "quantity": 2, "rate": 110, "unit": "pcs", "cost_rate": 100}],
        },
        author=owner, team=team,
    )
    request.status = QuoteStatus.APPROVED
    await db.flush()
    return request


ZOHO_FLOW = [
    review_step("which", "quote"),
    {"key": "zoho", "kind": "zoho_create", "config": {"quote_request_path": "quote.id", "save_as": "zoho"}},
]


async def test_zoho_create_is_held_while_the_switch_is_off(db, owner, team, switches) -> None:
    request = await approved_request(db, owner, team)
    zoho = FakeZoho()
    svc = services(zoho=zoho)
    flow = await make_flow(db, owner, ZOHO_FLOW)
    run = await start(db, flow, owner, switches, svc)
    run = await service.answer(db, run, user=owner, values={}, value={"id": str(request.id)}, settings=switches, services=svc)

    assert run.status == RunStatus.COMPLETED, run.error
    assert zoho.calls == []
    assert run.context["zoho"]["held"] is True
    held = await events(db, run, RunEventKind.HELD)
    assert len(held) == 1 and held[0].payload["what"] == "zoho_estimate"
    would = held[0].payload["would_create"]
    assert would["reference_number"] == "42"
    assert would["notes"] == "From the workflow"
    assert would["line_items"] == [{"name": "Gate valve", "description": None, "rate": 110.0, "quantity": 2.0, "unit": "pcs", "discount": 0.0}]
    assert "customer_id" not in would
    assert request.status == QuoteStatus.APPROVED
    assert await files(db, run) == []


async def test_zoho_create_makes_the_estimate_and_fetches_its_documents(db, owner, team, switches) -> None:
    request = await approved_request(db, owner, team)
    await service.update_settings(db, actor_id=None, changes={"write_zoho": True})
    zoho = FakeZoho()
    svc = services(zoho=zoho)
    flow = await make_flow(db, owner, ZOHO_FLOW)
    run = await start(db, flow, owner, switches, svc)
    run = await service.answer(db, run, user=owner, values={}, value={"id": str(request.id)}, settings=switches, services=svc)

    assert run.status == RunStatus.COMPLETED, run.error
    assert [c[0] for c in zoho.calls] == ["find_contact", "create_estimate", "estimate_pdf", "estimate_documents", "document"]
    assert zoho.calls[0] == ("find_contact", "ADNOC")
    assert zoho.calls[1][1]["customer_id"] == "C-1"
    assert run.context["zoho"] == {"estimate_id": "E-1", "estimate_number": "EST-001", "files": ["CP-EST-001.pdf", "TP-technical.pdf"], "held": False}
    stored = await files(db, run)
    assert [(f.source, f.file_name, f.content_type, f.meta["kind"]) for f in stored] == [
        (FileSource.ZOHO, "CP-EST-001.pdf", "application/pdf", "CP"),
        (FileSource.ZOHO, "TP-technical.pdf", "application/pdf", "TP"),
    ]
    assert request.status == QuoteStatus.CREATED_IN_ZOHO
    assert await events(db, run, RunEventKind.HELD) == []


async def test_zoho_create_refuses_a_request_that_is_not_approved(db, owner, team, switches) -> None:
    request = await approved_request(db, owner, team)
    request.status = QuoteStatus.DRAFT
    await db.flush()
    flow = await make_flow(db, owner, ZOHO_FLOW)
    run = await start(db, flow, owner, switches, services())
    run = await service.answer(db, run, user=owner, values={}, value={"id": str(request.id)}, settings=switches, services=services())
    assert run.status == RunStatus.FAILED and run.error == "The quote request is draft, not approved."
    run2 = await start(db, flow, owner, switches, services(), subject_id="43")
    run2 = await service.answer(db, run2, user=owner, values={}, value={"id": "nope"}, settings=switches, services=services())
    assert run2.status == RunStatus.FAILED and "no quote request" in run2.error


# ── sharepoint_attach ──────────────────────────────────────────────────

ATTACH_FLOW = [
    review_step("pause", "ignored"),
    {"key": "attach", "kind": "sharepoint_attach", "config": {"sources": "zoho", "save_as": "attached"}},
]


async def _zoho_files(db, run: WorkflowRun) -> None:
    for name in ("CP-EST-001.pdf", "TP-technical.pdf"):
        db.add(WorkflowRunFile(run_id=run.id, step_key="zoho", source=FileSource.ZOHO, file_name=name, content_type="application/pdf", size=4, content=b"%PDF"))
    db.add(WorkflowRunFile(run_id=run.id, step_key="ask", source=FileSource.UPLOAD, file_name="list.csv", content_type="text/csv", size=len(CSV), content=CSV))
    await db.flush()


async def test_sharepoint_attach_is_held_while_the_switch_is_off(db, owner, switches) -> None:
    sp = FakeSharePoint(make_task())
    svc = services(sharepoint=sp)
    flow = await make_flow(db, owner, ATTACH_FLOW)
    run = await start(db, flow, owner, switches, svc)
    await _zoho_files(db, run)
    run = await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=svc)
    assert run.status == RunStatus.COMPLETED
    assert sp.added == [] and sp.deleted == []
    assert run.context["attached"]["attached"] == []
    assert sorted(run.context["attached"]["held"]) == ["CP-EST-001.pdf", "TP-technical.pdf"]
    held = await events(db, run, RunEventKind.HELD)
    assert held[0].payload["what"] == "sharepoint_attachments"
    assert sorted(held[0].payload["files"]) == ["CP-EST-001.pdf", "TP-technical.pdf"]


async def test_sharepoint_attach_adds_only_what_the_task_lacks(db, owner, switches) -> None:
    await service.update_settings(db, actor_id=None, changes={"write_sharepoint": True})
    sp = FakeSharePoint(make_task(), attachments=[("TP-technical.pdf", b"old")])
    svc = services(sharepoint=sp)
    flow = await make_flow(db, owner, ATTACH_FLOW)
    run = await start(db, flow, owner, switches, svc)
    await _zoho_files(db, run)
    run = await service.answer(db, run, user=owner, values={}, value=None, settings=switches, services=svc)
    assert run.status == RunStatus.COMPLETED
    assert sp.added == [("42", "CP-EST-001.pdf", b"%PDF")]
    assert sp.deleted == []
    assert run.context["attached"] == {"attached": ["CP-EST-001.pdf"], "already_there": ["TP-technical.pdf"], "held": []}
    assert await events(db, run, RunEventKind.HELD) == []


async def test_sharepoint_attach_with_nothing_to_attach_is_a_no_op(db, owner, switches) -> None:
    await service.update_settings(db, actor_id=None, changes={"write_sharepoint": True})
    sp = FakeSharePoint(make_task())
    flow = await make_flow(db, owner, [ATTACH_FLOW[1]])
    run = await start(db, flow, owner, switches, services(sharepoint=sp))
    assert run.status == RunStatus.COMPLETED
    assert run.context["attached"]["reason"] == "nothing to attach"
    assert sp.calls == []


# ── the run's lifecycle ────────────────────────────────────────────────


async def test_retry_resumes_a_failed_run_at_the_same_step(db, owner, switches) -> None:
    executor = FakeExecutor(FakeExecutor.refused(500, "boom"), FakeExecutor.ok({"id": "q-1"}, 201))
    svc = services(executor=executor)
    flow = await make_flow(db, owner, ENDPOINT_FLOW + [{"key": "after", "kind": "notify", "config": {"title": "ok"}}])
    run = await start(db, flow, owner, switches, svc)
    run = await service.answer(db, run, user=owner, values={}, value=[], settings=switches, services=svc)
    assert run.status == RunStatus.FAILED and run.step_index == 1
    assert run.finished_at is not None

    with pytest.raises(WorkflowConflictError, match="already finished"):
        await service.cancel(db, run, user=owner)

    run = await service.retry(db, run, user=owner, settings=switches, services=svc)
    assert run.status == RunStatus.COMPLETED
    assert run.error is None
    assert run.context["quote"] == {"id": "q-1"}
    retried = await events(db, run, RunEventKind.RETRIED)
    assert retried[0].step_key == "draft" and retried[0].by_user_id == owner.id
    # The step was not started afresh: one start event, two attempts.
    assert len([e for e in await events(db, run, RunEventKind.STEP_STARTED) if e.step_key == "draft"]) == 1
    assert len(executor.calls) == 2

    with pytest.raises(WorkflowConflictError, match="Only a failed run"):
        await service.retry(db, run, user=owner, settings=switches, services=svc)


async def test_one_open_run_per_task_and_cancelling_frees_it(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [review_step("pause", "x")])
    first = await start(db, flow, owner, switches, services())
    assert first.status == RunStatus.WAITING_USER
    with pytest.raises(WorkflowConflictError, match="already going"):
        await start(db, flow, owner, switches, services())
    # Another task is fine.
    other = await start(db, flow, owner, switches, services(), subject_id="43")
    assert other.tag != first.tag and other.tag.startswith("HZ-")

    cancelled = await service.cancel(db, first, user=owner)
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.pending is None and cancelled.finished_at is not None
    assert cancelled.cancelled_by_id == owner.id
    assert (await events(db, first, RunEventKind.CANCELLED))[0].by_user_id == owner.id
    # A stale wake-up does nothing to a cancelled run.
    assert (await advance(db, cancelled, switches, services())).status == RunStatus.CANCELLED

    again = await start(db, flow, owner, switches, services())
    assert again.id != first.id and again.status == RunStatus.WAITING_USER

    assert {r.id for r in await service.list_runs(db, owner_id=owner.id, open_only=True)} == {other.id, again.id}
    assert {r.id for r in await service.list_runs(db, subject_id="42")} == {again.id, first.id}
    assert await service.open_run_counts(db) == {flow.id: 2}
    with pytest.raises(WorkflowConflictError, match="still going"):
        await service.delete_flow(db, flow)


async def test_a_switched_off_flow_cannot_start(db, owner, switches) -> None:
    flow = await make_flow(db, owner, [review_step("pause", "x")])
    await service.update_flow(db, flow, changes={"enabled": False}, actor=owner)
    with pytest.raises(WorkflowError, match="switched off"):
        await start(db, flow, owner, switches, services())
    await service.update_flow(db, flow, changes={"enabled": True}, actor=owner)
    await service.archive_flow(db, flow)
    with pytest.raises(WorkflowError, match="switched off"):
        await start(db, flow, owner, switches, services())
    assert await service.list_flows(db) == []
    assert [f.id for f in await service.list_flows(db, include_archived=True)] == [flow.id]


async def test_flows_for_shows_a_person_their_teams_flows(db, owner, team, switches) -> None:
    anyone = await make_flow(db, owner, [review_step("p", "x")], key="anyone")
    ours = await service.create_flow(db, payload={"key": "ours", "name": "Ours", "team": "presales", "steps": [review_step("p", "x")]}, actor=owner)
    other_team = await teams_service.create_team(db, name="Finance", slug="finance")
    theirs = await service.create_flow(db, payload={"key": "theirs", "name": "Theirs", "team": str(other_team.id), "steps": [review_step("p", "x")]}, actor=owner)
    await teams_service.set_member_roles(db, team=team, user=owner, role_keys=["member"])

    mine = {f.key for f in await service.flows_for(db, user_id=owner.id, is_admin=False)}
    assert mine == {"anyone", "ours"}
    assert {f.key for f in await service.flows_for(db, user_id=owner.id, is_admin=True)} == {"anyone", "ours", "theirs"}
    assert theirs.team_id == other_team.id and ours.team_id == team.id and anyone.team_id is None
    with pytest.raises(WorkflowError, match="No team called"):
        await service.create_flow(db, payload={"key": "x", "name": "X", "team": "nope", "steps": [review_step("p", "x")]}, actor=owner)
    with pytest.raises(WorkflowConflictError, match="already exists"):
        await make_flow(db, owner, [review_step("p", "x")], key="anyone")


async def test_wake_due_returns_only_event_waits_whose_time_has_come(db, owner, switches) -> None:
    pending = FakeExecutor(FakeExecutor.ok({"status": "draft"}))
    waiting_flow = await make_flow(db, owner, wait_status_flow(), key="wait")
    due = await start(db, waiting_flow, owner, switches, services(executor=pending), subject_id="1")
    later = await start(db, waiting_flow, owner, switches, services(executor=pending), subject_id="2")
    asking = await start(db, await make_flow(db, owner, [review_step("p", "x")], key="ask"), owner, switches, services(), subject_id="3")
    assert due.status == later.status == RunStatus.WAITING_EVENT and asking.status == RunStatus.WAITING_USER

    due.wake_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.flush()
    assert [r.id for r in await service.wake_due(db)] == [due.id]

    # A run with no wake time at all is looked at too.
    later.wake_at = None
    await db.flush()
    assert {r.id for r in await service.wake_due(db)} == {due.id, later.id}
    assert asking.id not in {r.id for r in await service.wake_due(db)}


async def test_the_worker_advances_a_due_run(db, owner, switches, session_factory) -> None:
    executor = FakeExecutor(FakeExecutor.ok({"status": "draft"}), FakeExecutor.ok({"status": "approved"}))
    svc = services(executor=executor)
    flow = await make_flow(db, owner, wait_status_flow() + [{"key": "after", "kind": "notify", "config": {"title": "Approved {{ approved.status }}"}}])
    run = await start(db, flow, owner, switches, svc)
    assert run.status == RunStatus.WAITING_EVENT
    run.wake_at = datetime.now(UTC) - timedelta(minutes=1)
    await service.update_settings(db, actor_id=None, changes={"poll_seconds": 45})
    await db.commit()

    worker = WorkflowWorker(factory=session_factory, services=svc)
    assert await worker.tick() == 45
    assert len(executor.calls) == 2

    async with session_factory() as fresh:
        stored = await service.get_run(fresh, run.id)
        assert stored.status == RunStatus.COMPLETED
        # What the woken pass saved into the context is a separate matter:
        # see test_what_a_woken_step_saves_reaches_the_database.
        assert [e.kind for e in stored.events][-1] == RunEventKind.COMPLETED
        title = await fresh.scalar(select(Notification.title).where(Notification.user_id == owner.id))
        assert title == "Approved approved"

    # Nothing is due now; a tick is a no-op that still reports the interval.
    assert await worker.tick() == 45
    assert len(executor.calls) == 2


async def test_step_states_follow_a_real_run(db, owner, switches) -> None:
    flow = await make_flow(db, owner, DOCS_FLOW + [{"key": "after", "kind": "notify", "config": {"title": "x"}}])
    run = await start(db, flow, owner, switches, services(sharepoint=FakeSharePoint(make_task())))
    run = await service.get_run(db, run.id)
    await db.refresh(run, ["events"])
    states = {s["key"]: s["state"] for s in service.step_states(run)}
    assert states == {"docs": "done", "ask_docs": "waiting", "extract": "pending", "after": "pending"}
    assert service.step_states(run)[0]["note"] == "0 file(s) on the task"
