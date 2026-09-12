"""What each block does. One handler per kind, each called until it is done.

A handler takes a ``StepContext`` and returns ``Done``, ``Wait`` or ``Fail``.
It is called again every time the run is woken, so a handler that waits looks
first for what it was waiting on — the person's answer, the supplier's mail,
the status it was polling — and only waits again if it is not there yet.

Handlers that would touch the world check the switch on ``WorkflowSettings``
and, when it is off, record a ``held`` event with exactly what they would have
sent or written, then carry on as if they had. A run on a switched-off
deployment therefore reaches its end with a complete account of what it would
have done, which is what the switches are for.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.assistant.catalogue import MODELS_BY_KEY, TOOLS_BY_KEY
from app.assistant.llm import LLMError
from app.assistant.policy import cost_of
from app.auth.deps import SESSION_AUDIENCE
from app.comparison.documents import DocumentError, prepare
from app.comparison.extraction import ExtractionError
from app.comparison.schemas import ComparisonIn
from app.comparison.schemas import ItemIn as SupplierItemIn
from app.comparison.schemas import QuoteIn as SupplierQuoteIn
from app.core.security import sign
from app.models.comparison import QuoteSource
from app.models.intake import IntakeMessage
from app.models.quoting import QuoteRequest, QuoteStatus
from app.models.templates import FormTemplate, TemplateStatus
from app.models.workflow import FileSource, RunEventKind, WorkflowRunFile, WorkflowRunMessage
from app.notifications import service as notifications
from app.workflows.catalogue import SCHEMAS
from app.workflows.engine import Done, Fail, StepContext, Wait
from app.workflows.reader import DocumentReader, ReaderError
from app.workflows.templating import lookup, render, render_value

logger = logging.getLogger("hamdaz.workflows.steps")

#: How long a run's session lasts when a step calls a route as its owner.
_ACT_AS_MINUTES = 10


# ── helpers ────────────────────────────────────────────────────────────


def _sources(config: dict[str, Any], default: str) -> set[str]:
    raw = str(config.get("sources") or default)
    return {s.strip() for s in raw.split(",") if s.strip()}


async def _files(step: StepContext, sources: set[str]) -> list[WorkflowRunFile]:
    rows = await step.session.scalars(
        select(WorkflowRunFile)
        .where(WorkflowRunFile.run_id == step.run.id, WorkflowRunFile.source.in_(sorted(sources)))
        .order_by(WorkflowRunFile.created_at)
    )
    return list(rows.all())


async def _file_names(step: StepContext) -> set[str]:
    rows = await step.session.scalars(
        select(WorkflowRunFile.file_name).where(WorkflowRunFile.run_id == step.run.id)
    )
    return set(rows.all())


def _add_file(
    step: StepContext, *, source: str, name: str, content: bytes,
    content_type: str | None = None, origin: str | None = None, meta: dict | None = None,
) -> WorkflowRunFile:
    row = WorkflowRunFile(
        run_id=step.run.id, step_key=step.key, source=source, file_name=name[:255],
        content_type=content_type, size=len(content), content=content, origin=origin, meta=meta,
    )
    step.session.add(row)
    return row


def _readables(files: list[WorkflowRunFile]) -> tuple[list, list[str]]:
    readables, failures = [], []
    for f in files:
        try:
            readables.append(prepare(f.file_name, f.content, f.content_type))
        except DocumentError as exc:
            failures.append(f"{f.file_name}: {exc}")
    return readables, failures


def _txt(value: Any, limit: int | None = None) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _model_cost(step: StepContext, input_tokens: int, output_tokens: int) -> None:
    spec = MODELS_BY_KEY.get(step.services.model_key)
    if spec is None:
        return
    row = type("M", (), {})()
    row.input_price = spec.input_price
    row.cached_input_price = spec.cached_input_price
    row.output_price = spec.output_price
    step.run.cost_usd = (step.run.cost_usd or Decimal(0)) + cost_of(
        row, input_tokens=input_tokens, cached_tokens=0, output_tokens=output_tokens
    )


def _owner_cookie(step: StepContext) -> str:
    return sign(
        {"sub": str(step.run.owner_id)},
        secret=step.services.settings.session_secret,
        ttl_minutes=_ACT_AS_MINUTES,
        audience=SESSION_AUDIENCE,
    )


async def _call_route(step: StepContext, tool_key: str, arguments: dict[str, Any]) -> Any:
    """One of the app's routes, as the run's owner. Returns the executor outcome."""
    spec = TOOLS_BY_KEY.get(tool_key)
    if spec is None or spec.is_client:
        raise ValueError(f"{tool_key!r} is not a route the app has.")
    executor = step.services.executor
    if executor is None:
        raise ValueError("The route executor is not available.")
    args = render_value(arguments or {}, step.ctx)
    if not isinstance(args, dict):
        args = {}
    return await executor.call(spec, args, session_cookie=_owner_cookie(step))


# ── documents ──────────────────────────────────────────────────────────


async def documents(step: StepContext) -> Any:
    sp = step.services.sharepoint
    if sp is None:
        return Fail("SharePoint is not configured on this deployment.")
    task_id = step.run.subject_id
    try:
        task = await sp.task(task_id)
    except Exception as exc:  # noqa: BLE001
        return Fail(f"Could not read task {task_id} from SharePoint: {exc}")

    step.ctx["task"] = {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "end_user": task.end_user,
        "bid_closing_date": task.bid_closing_date,
        "due_date": task.due_date,
        "quote_no": task.quote_no,
        "remarks": task.remarks,
        "url": task.web_url,
    }
    if task.title and not step.run.subject_label:
        step.run.subject_label = task.title[:300]

    try:
        listed = await sp.attachments_of(task.id)
    except Exception as exc:  # noqa: BLE001
        return Fail(f"Could not list the task's attachments: {exc}")

    have = await _file_names(step)
    names: list[str] = []
    for entry in listed:
        name = entry.get("file_name") or ""
        if not name:
            continue
        names.append(name)
        if name in have:
            continue
        try:
            content = await sp.attachment_content(task.id, name)
        except Exception as exc:  # noqa: BLE001
            return Fail(f"Could not download {name!r}: {exc}")
        _add_file(step, source=FileSource.SHAREPOINT, name=name, content=content)
    await step.session.flush()
    return Done(
        {"found": bool(names), "count": len(names), "files": names},
        note=f"{len(names)} file(s) on the task",
    )


# ── ask the person ─────────────────────────────────────────────────────


async def ask_user(step: StepContext) -> Any:
    answer = step.answer()
    mode = str(step.config.get("mode") or "form")
    review_of = str(step.config.get("review_of") or "").strip()
    if answer is not None:
        if mode == "review":
            value = answer.get("value")
            if value is None and review_of:
                value = lookup(step.ctx, review_of)
            return Done(value, note="verified")
        values = dict(answer.get("values") or {})
        if answer.get("files"):
            values["files"] = list(answer["files"])
        return Done(values, note="answered")

    pending = {
        "step_key": step.key,
        "title": render(str(step.config.get("title") or step.step.get("name") or ""), step.ctx),
        "message": render(str(step.config.get("message") or ""), step.ctx),
        "mode": mode,
        "fields": list(step.config.get("fields") or []),
        "allow_files": bool(step.config.get("allow_files")),
        "resume_label": str(step.config.get("resume_label") or "Continue"),
        "review_of": review_of or None,
        "review_value": lookup(step.ctx, review_of) if review_of else None,
    }
    return Wait("user", pending=pending, note=pending["title"])


# ── read requirements ──────────────────────────────────────────────────


async def extract(step: StepContext) -> Any:
    files = await _files(step, _sources(step.config, "sharepoint,upload"))
    readables, failures = _readables(files)
    typed = lookup(step.ctx, str(step.config.get("merge_items_from") or "")) if step.config.get("merge_items_from") else None
    typed_items = [i for i in (typed or []) if isinstance(i, dict) and any(v not in (None, "") for v in i.values())]
    typed_lines = [json.dumps(i, ensure_ascii=False) for i in typed_items]

    schema = SCHEMAS.get(str(step.config.get("schema") or "requirements"), SCHEMAS["requirements"])
    reader = DocumentReader(step.services.settings)

    if not readables:
        if not typed_items:
            return Fail(
                "There is nothing to read: no documents on the task or uploaded, and no items typed in."
                + (" " + "; ".join(failures) if failures else "")
            )
        # Nothing to read but the person said what they need. Build the
        # answer from that alone; no model is needed to copy a table.
        items = [
            {
                "description": str(i.get("description") or i.get("name") or "").strip(),
                "part_number": str(i.get("part_number") or ""),
                "brand": str(i.get("brand") or ""),
                "quantity": float(i.get("quantity") or 1),
                "unit": str(i.get("unit") or ""),
                "specification": str(i.get("specification") or i.get("notes") or ""),
                "source": "typed in",
            }
            for i in typed_items
        ]
        notes = str(lookup(step.ctx, "answers.notes") or "")
        return Done(
            {
                "summary": notes or f"{len(items)} item(s) listed by {step.run.owner.display_name}.",
                "items": items,
                "requirements": [notes] if notes else [],
                "missing": [],
                "customer": str(lookup(step.ctx, "task.end_user") or ""),
                "deadline": str(lookup(step.ctx, "task.bid_closing_date") or ""),
            },
            note=f"{len(items)} item(s) typed in",
        )

    try:
        payload, cost = await reader.read(
            readables, schema=schema,
            instructions=str(step.config.get("instructions") or ""),
            typed_items=typed_lines or None,
        )
    except ReaderError as exc:
        return Fail(str(exc))
    step.run.cost_usd = (step.run.cost_usd or Decimal(0)) + Decimal(str(cost))
    if not payload.get("customer"):
        payload["customer"] = str(lookup(step.ctx, "task.end_user") or "")
    if failures:
        payload.setdefault("missing", []).append("Could not read: " + "; ".join(failures))
    return Done(payload, note=f"{len(payload.get('items') or [])} item(s) from {len(readables)} file(s)")


# ── ask the model ──────────────────────────────────────────────────────


async def agent(step: StepContext) -> Any:
    llm = step.services.llm
    if llm is None or not getattr(llm, "configured", False):
        return Fail("The assistant's model is not configured (OPENAI_API_KEY).")
    prompt = render(str(step.config.get("prompt") or ""), step.ctx)
    schema_key = str(step.config.get("schema") or "")
    schema = SCHEMAS.get(schema_key) if schema_key else None
    instructions = str(step.config.get("instructions") or "You help a UAE trading company with its work.")
    try:
        text, inp, out = await llm.research(
            model=step.services.model_key,
            instructions=instructions,
            prompt=prompt,
            schema=schema,
            web_search=bool(step.config.get("web_search")),
            user_key=str(step.run.owner_id),
        )
    except LLMError as exc:
        return Fail(str(exc))
    _model_cost(step, inp, out)
    if schema is not None:
        try:
            parsed = json.loads(text)
        except ValueError:
            return Fail("The model did not answer in the expected shape.")
        count = len(parsed.get("suppliers") or parsed.get("items") or []) if isinstance(parsed, dict) else 0
        return Done(parsed, note=f"{count} result(s)" if count else "answered")
    return Done({"text": text}, note="answered")


# ── send mail ──────────────────────────────────────────────────────────


async def _template_wording(step: StepContext) -> tuple[str | None, str | None]:
    key = str(step.config.get("template_key") or "").strip()
    if not key:
        return None, None
    template = await step.session.scalar(
        select(FormTemplate).where(FormTemplate.key == key, FormTemplate.status == TemplateStatus.ACTIVE)
    )
    if template is None:
        return None, None
    subject = body = None
    for f in template.fields or []:
        if f.get("key") == "subject":
            subject = f.get("default")
        elif f.get("key") == "body":
            body = f.get("default")
    return subject, body


async def email(step: StepContext) -> Any:
    recipients = lookup(step.ctx, str(step.config.get("to_path") or ""))
    if not isinstance(recipients, list):
        return Fail("There is nobody to write to: the recipients list is empty.")
    to_field = str(step.config.get("to_field") or "email")
    name_field = str(step.config.get("name_field") or "name")
    subject_t, body_t = await _template_wording(step)
    subject_t = subject_t or str(step.config.get("subject") or "")
    body_t = body_t or str(step.config.get("body") or "")
    if not subject_t or not body_t:
        return Fail("The mail has no subject or no body.")

    mailer = step.services.mail
    sender = step.settings.from_mailbox or ""
    if not sender:
        from app.intake import service as intake_service

        sender = (await intake_service.get_settings(step.session)).mailbox or ""

    already = {
        m.address for m in await step.session.scalars(
            select(WorkflowRunMessage).where(
                WorkflowRunMessage.run_id == step.run.id,
                WorkflowRunMessage.step_key == step.key,
                WorkflowRunMessage.state == "sent",
            )
        )
    }
    sent, held, failed, skipped = [], [], [], []
    for r in recipients:
        if not isinstance(r, dict):
            continue
        address = str(r.get(to_field) or "").strip()
        name = str(r.get(name_field) or address or "Supplier").strip()
        if not address or "@" not in address:
            skipped.append(name)
            continue
        if address in already:
            continue
        ctx = dict(step.ctx)
        ctx["recipient"] = r
        subject = render(subject_t, ctx)
        body = render(body_t, ctx)
        html = "<p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        message = WorkflowRunMessage(
            run_id=step.run.id, step_key=step.key, direction="out", party=name,
            address=address, subject=subject, body=body, state="held",
        )
        if not step.settings.send_email:
            held.append(name)
            step.session.add(message)
            continue
        if mailer is None or not sender:
            message.state = "failed"
            message.error = "No mailbox to send from."
            failed.append(name)
            step.session.add(message)
            continue
        try:
            await mailer.send(sender=sender, recipients=[address], subject=subject, html=html)
            message.state = "sent"
            message.sent_at = datetime.now(UTC)
            sent.append(name)
        except Exception as exc:  # noqa: BLE001
            message.state = "failed"
            message.error = str(exc)[:500]
            failed.append(name)
        step.session.add(message)
    await step.session.flush()

    if held:
        await step.event(
            RunEventKind.HELD,
            {"what": "email", "to": held, "reason": "The mail switch is off; composed and kept."},
        )
    if sent:
        await step.event(RunEventKind.EMAIL_SENT, {"to": sent, "from": sender})
    if not sent and not held:
        return Fail(
            "No mail could be sent. "
            + ("Failed: " + ", ".join(failed) + ". " if failed else "")
            + ("No address for: " + ", ".join(skipped) + "." if skipped else "")
        )
    return Done(
        {"sent": sent, "held": held, "failed": failed, "no_address": skipped, "from": sender,
         "count": len(sent) + len(held)},
        note=f"sent to {len(sent)}, held {len(held)}",
    )


# ── wait for replies ───────────────────────────────────────────────────


async def wait_email(step: StepContext) -> Any:
    since = step.waited_since()
    poll = timedelta(seconds=max(30, int(step.settings.poll_seconds or 60)))
    timeout_days = float(step.config.get("timeout_days") or 14)
    deadline = since + timedelta(days=timeout_days)
    min_replies = int(step.config.get("min_replies") or 1)

    tag = step.run.tag
    known = {
        m.intake_message_id for m in await step.session.scalars(
            select(WorkflowRunMessage).where(
                WorkflowRunMessage.run_id == step.run.id, WorkflowRunMessage.direction == "in"
            )
        )
        if m.intake_message_id
    }
    pattern = f"%{tag}%"
    fresh = await step.session.scalars(
        select(IntakeMessage)
        .where(
            IntakeMessage.created_at >= step.run.started_at,
            or_(IntakeMessage.subject.ilike(pattern), IntakeMessage.body.ilike(pattern)),
        )
        .order_by(IntakeMessage.received_at)
    )
    mail = step.services.mail
    mailbox = step.settings.from_mailbox or ""
    if not mailbox:
        from app.intake import service as intake_service

        mailbox = (await intake_service.get_settings(step.session)).mailbox or ""

    for msg in fresh.all():
        if msg.id in known:
            continue
        party = msg.sender_name or msg.sender_email
        files: list[str] = []
        if msg.has_attachments and mail is not None and mailbox:
            try:
                for name, ctype, content in await mail.attachments(mailbox, msg.graph_message_id):
                    _add_file(
                        step, source=FileSource.EMAIL, name=name, content=content,
                        content_type=ctype, origin=party,
                        meta={"intake_message_id": str(msg.id), "sender": msg.sender_email},
                    )
                    files.append(name)
            except Exception as exc:  # noqa: BLE001 - the reply still counts
                logger.warning("could not fetch attachments for %s: %s", msg.id, exc)
        step.session.add(
            WorkflowRunMessage(
                run_id=step.run.id, step_key=step.key, direction="in", party=party,
                address=msg.sender_email, subject=msg.subject, body=(msg.body or "")[:20000],
                state="received", intake_message_id=msg.id, received_at=msg.received_at,
            )
        )
        await step.event(
            RunEventKind.EMAIL_RECEIVED,
            {"from": msg.sender_email, "subject": msg.subject, "files": files},
        )
    await step.session.flush()

    replies = list(
        await step.session.scalars(
            select(WorkflowRunMessage).where(
                WorkflowRunMessage.run_id == step.run.id, WorkflowRunMessage.direction == "in"
            ).order_by(WorkflowRunMessage.received_at)
        )
    )
    expected = int((step.ctx.get("rfq") or {}).get("count") or 0)
    enough = len(replies) >= max(1, min_replies)
    if step.config.get("wait_for_all") and expected:
        enough = len(replies) >= expected
    now = datetime.now(UTC)
    if enough or (replies and now >= deadline):
        return Done(
            {
                "count": len(replies),
                "replies": [
                    {"from": m.address, "party": m.party, "subject": m.subject,
                     "received_at": m.received_at.isoformat() if m.received_at else None}
                    for m in replies
                ],
            },
            note=f"{len(replies)} repl{'y' if len(replies) == 1 else 'ies'}",
        )
    if now >= deadline:
        return Fail(f"No supplier replied within {timeout_days:g} days.")
    return Wait(
        "event", wake_at=now + poll, deadline_at=deadline,
        note=f"{len(replies)} of {expected or min_replies} replies",
    )


# ── compare ────────────────────────────────────────────────────────────


async def compare(step: StepContext) -> Any:
    from app.comparison import service as comparison_service

    extractor = step.services.extractor
    if extractor is None:
        return Fail("The quote extractor is not available.")
    files = await _files(step, {FileSource.EMAIL, FileSource.UPLOAD})
    files = [f for f in files if f.source == FileSource.EMAIL or (f.meta or {}).get("supplier_quote")]
    readables, failures = _readables(files)
    if not readables:
        return Fail("No supplier quote arrived as a file, so there is nothing to compare.")

    quotes: list[SupplierQuoteIn] = []
    by_file = {f.file_name: f for f in files}
    for readable, result in zip(readables, await extractor.read_all(readables), strict=True):
        if isinstance(result, ExtractionError):
            failures.append(f"{readable.file_name}: {result}")
            continue
        origin = (by_file.get(readable.file_name).origin if by_file.get(readable.file_name) else None)
        quotes.append(
            SupplierQuoteIn(
                supplier_name=(result.supplier_name or origin or readable.file_name)[:200],
                quote_number=_txt(result.quote_number, 100),
                quote_date=_txt(result.quote_date, 40),
                currency=(result.currency or "AED").upper()[:3],
                validity=_txt(result.validity),
                delivery_time=_txt(result.delivery_time),
                payment_terms=_txt(result.payment_terms),
                warranty=_txt(result.warranty),
                incoterms=_txt(result.incoterms, 60),
                contact=_txt(result.contact, 200),
                discount=_dec(result.discount),
                freight=_dec(result.freight),
                tax=_dec(result.tax),
                quoted_total=_dec(result.quoted_total),
                source=QuoteSource.UPLOAD,
                file_name=readable.file_name,
                extraction_note=_txt(result.note),
                items=[
                    SupplierItemIn(
                        description=item.description,
                        part_number=_txt(item.part_number, 120),
                        brand=_txt(item.brand, 120),
                        unit=_txt(item.unit, 40),
                        quantity=_dec(item.quantity) or Decimal(0),
                        unit_price=_dec(item.unit_price) or Decimal(0),
                        line_total=_dec(item.line_total),
                        lead_time=_txt(item.lead_time),
                    )
                    for item in result.items
                ],
            )
        )
    if not quotes:
        return Fail("No supplier quote could be read. " + "; ".join(failures))

    title = str(lookup(step.ctx, "task.title") or step.run.subject_label or step.run.tag)
    try:
        comparison = await comparison_service.save(
            step.session, extractor,
            ComparisonIn(title=f"Supplier quotes for {title}"[:200], reference=step.run.tag,
                         currency="AED", quotes=quotes),
            author=step.run.owner,
            documents={
                q.file_name: (
                    by_file[q.file_name].content_type or "application/octet-stream",
                    by_file[q.file_name].content,
                )
                for q in quotes
                if q.file_name in by_file
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Fail(f"Could not compare the quotes: {exc}")

    analysis = comparison.analysis or {}
    suppliers = [
        {"quote_id": s.get("quote_id"), "supplier_name": s.get("supplier_name"), "total": s.get("total")}
        for s in analysis.get("suppliers") or []
    ]
    priced = [s for s in suppliers if s.get("total") is not None]
    chosen = min(priced, key=lambda s: Decimal(str(s["total"]))) if priced else (suppliers[0] if suppliers else None)
    markup = Decimal(str(step.config.get("markup_percent") or 0))
    items: list[dict[str, Any]] = []
    if chosen is not None:
        row = next((q for q in comparison.quotes if str(q.id) == str(chosen["quote_id"])), None)
        for line in (row.items if row else []):
            cost = Decimal(line.unit_price or 0)
            rate = (cost * (Decimal(100) + markup) / Decimal(100)).quantize(Decimal("0.01"))
            items.append(
                {
                    "name": (line.description or "Item")[:500],
                    "description": line.lead_time,
                    "item_code": line.part_number,
                    "brand": line.brand,
                    "unit": line.unit,
                    "quantity": float(line.quantity or 0),
                    "rate": float(rate),
                    "cost_rate": float(cost),
                    "source_supplier_quote_id": str(row.id),
                }
            )
    return Done(
        {
            "comparison_id": str(comparison.id),
            "suppliers": suppliers,
            "chosen": chosen,
            "items": items,
            "single": len(quotes) == 1,
            "unreadable": failures,
        },
        note=f"{len(quotes)} quote(s); chose {chosen['supplier_name'] if chosen else 'none'}",
    )


# ── the app's own routes ───────────────────────────────────────────────


async def endpoint(step: StepContext) -> Any:
    try:
        outcome = await _call_route(step, str(step.config.get("tool") or ""), step.config.get("arguments") or {})
    except ValueError as exc:
        return Fail(str(exc))
    if not outcome.ok:
        return Fail(f"{step.config.get('tool')} answered {outcome.status}: {outcome.text[:400]}")
    return Done(outcome.body if outcome.body is not None else {"text": outcome.text}, note=f"{outcome.status}")


async def wait_status(step: StepContext) -> Any:
    since = step.waited_since()
    poll = timedelta(minutes=max(1, int(step.config.get("poll_minutes") or 10)))
    deadline = since + timedelta(days=float(step.config.get("timeout_days") or 30))
    try:
        outcome = await _call_route(step, str(step.config.get("tool") or ""), step.config.get("arguments") or {})
    except ValueError as exc:
        return Fail(str(exc))
    if not outcome.ok:
        return Fail(f"{step.config.get('tool')} answered {outcome.status}: {outcome.text[:400]}")
    body = outcome.body if isinstance(outcome.body, dict) else {}
    value = lookup(body, str(step.config.get("status_path") or "status"))
    wanted = {s.strip() for s in str(step.config.get("until") or "").split(",") if s.strip()}
    failing = {s.strip() for s in str(step.config.get("fail_on") or "").split(",") if s.strip()}
    if str(value) in wanted:
        return Done(body, note=f"{value}")
    if str(value) in failing:
        return Fail(f"It became {value!r}.")
    now = datetime.now(UTC)
    if now >= deadline:
        return Fail(f"Still {value!r} after the wait ran out.")
    return Wait("event", wake_at=now + poll, deadline_at=deadline, note=f"still {value}")


# ── tell the person ────────────────────────────────────────────────────


async def notify(step: StepContext) -> Any:
    title = render(str(step.config.get("title") or ""), step.ctx)
    body = render(str(step.config.get("body") or ""), step.ctx) or None
    await notifications.notify(
        step.session, users=[step.run.owner_id], kind="workflow", title=title[:300], body=body,
        link=f"/workflows/runs/{step.run.id}", source="workflow",
        source_id=f"{step.run.id}:{step.key}",
    )
    await step.event(RunEventKind.NOTIFIED, {"title": title})
    return Done({"title": title}, note="notified")


# ── Zoho Books ─────────────────────────────────────────────────────────


def _estimate_payload(request: QuoteRequest, customer_id: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "customer_id": customer_id,
        "reference_number": request.reference_number,
        "date": request.quote_date.isoformat() if request.quote_date else None,
        "expiry_date": request.expiry_date.isoformat() if request.expiry_date else None,
        "notes": request.notes,
        "terms": request.terms,
        "subject": request.subject,
        "line_items": [
            {
                "name": item.name,
                "description": item.description,
                "rate": float(item.rate or 0),
                "quantity": float(item.quantity or 0),
                "unit": item.unit,
                "discount": float(item.discount or 0),
            }
            for item in request.items
        ],
        "discount": float(request.discount or 0),
        "shipping_charge": float(request.shipping_charge or 0),
        "adjustment": float(request.adjustment or 0),
    }
    custom = []
    if request.cf_bcd:
        custom.append({"api_name": "cf_bcd", "value": request.cf_bcd.isoformat()})
    if request.cf_portal:
        custom.append({"api_name": "cf_portal", "value": request.cf_portal})
    if custom:
        payload["custom_fields"] = custom
    return {k: v for k, v in payload.items() if v is not None}


async def zoho_create(step: StepContext) -> Any:
    raw = lookup(step.ctx, str(step.config.get("quote_request_path") or ""))
    try:
        request_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return Fail("There is no quote request to create in Zoho.")
    # Loaded with its lines up front: the payload below reads ``items``, and
    # a lazy load inside async is a MissingGreenlet rather than a list.
    request = await step.session.scalar(
        select(QuoteRequest)
        .options(selectinload(QuoteRequest.items))
        .where(QuoteRequest.id == request_id)
    )
    if request is None:
        return Fail("The quote request no longer exists.")
    if request.status not in (QuoteStatus.APPROVED, QuoteStatus.CREATED_IN_ZOHO):
        return Fail(f"The quote request is {request.status}, not approved.")

    zoho = step.services.zoho
    customer_id = request.customer_id
    if zoho is not None and not customer_id and step.settings.write_zoho:
        try:
            found = await zoho.find_contact(request.customer_name)
        except Exception as exc:  # noqa: BLE001
            return Fail(f"Could not look the customer up in Zoho: {exc}")
        if found:
            customer_id = str(found.get("contact_id") or "")
    payload = _estimate_payload(request, customer_id)

    if not step.settings.write_zoho:
        await step.event(
            RunEventKind.HELD,
            {"what": "zoho_estimate", "would_create": payload,
             "reason": "The Zoho switch is off; nothing was created."},
        )
        return Done({"held": True, "would_create": payload, "files": []}, note="held: Zoho switch off")
    if zoho is None:
        return Fail("Zoho Books is not configured on this deployment.")
    if not customer_id:
        return Fail(f"Zoho has no customer called {request.customer_name!r}; create the contact first.")

    try:
        estimate = await zoho.create_estimate(payload)
    except Exception as exc:  # noqa: BLE001
        return Fail(f"Zoho refused the estimate: {exc}")
    estimate_id = str(estimate.get("estimate_id") or "")
    number = str(estimate.get("estimate_number") or estimate_id)
    files: list[str] = []
    have = await _file_names(step)
    try:
        pdf = await zoho.estimate_pdf(estimate_id)
        name = f"CP-{number}.pdf"
        if name not in have:
            _add_file(step, source=FileSource.ZOHO, name=name, content=pdf,
                      content_type="application/pdf", meta={"estimate_id": estimate_id, "kind": "CP"})
        files.append(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not fetch the estimate PDF for %s: %s", estimate_id, exc)
    try:
        for doc in await zoho.estimate_documents(estimate_id):
            doc_id = str(doc.get("document_id") or "")
            doc_name = str(doc.get("file_name") or doc_id)
            if not doc_id:
                continue
            content, ctype = await zoho.document(estimate_id, doc_id)
            name = f"TP-{doc_name}"
            if name not in have:
                _add_file(step, source=FileSource.ZOHO, name=name, content=content,
                          content_type=ctype, meta={"estimate_id": estimate_id, "kind": "TP"})
            files.append(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not fetch documents for %s: %s", estimate_id, exc)

    request.status = QuoteStatus.CREATED_IN_ZOHO
    await step.session.flush()
    return Done(
        {"estimate_id": estimate_id, "estimate_number": number, "files": files, "held": False},
        note=f"estimate {number}",
    )


# ── SharePoint ─────────────────────────────────────────────────────────


async def sharepoint_attach(step: StepContext) -> Any:
    sp = step.services.sharepoint
    files = await _files(step, _sources(step.config, "zoho"))
    if not files:
        return Done({"attached": [], "held": [], "reason": "nothing to attach"}, note="nothing to attach")
    if not step.settings.write_sharepoint:
        names = [f.file_name for f in files]
        await step.event(
            RunEventKind.HELD,
            {"what": "sharepoint_attachments", "files": names,
             "reason": "The SharePoint switch is off; nothing was written."},
        )
        return Done({"attached": [], "held": names}, note="held: SharePoint switch off")
    if sp is None:
        return Fail("SharePoint is not configured on this deployment.")
    task_id = step.run.subject_id
    try:
        existing = {e.get("file_name") for e in await sp.attachments_of(task_id)}
    except Exception as exc:  # noqa: BLE001
        return Fail(f"Could not list the task's attachments: {exc}")
    attached, kept = [], []
    for f in files:
        if f.file_name in existing:
            kept.append(f.file_name)
            continue
        try:
            # Add only. Nothing here ever edits or removes what is on the task.
            await sp.add_attachment(task_id, f.file_name, f.content)
            attached.append(f.file_name)
        except Exception as exc:  # noqa: BLE001
            return Fail(f"SharePoint refused {f.file_name!r}: {exc}")
    return Done({"attached": attached, "already_there": kept, "held": []}, note=f"attached {len(attached)}")


HANDLERS = {
    "documents": documents,
    "ask_user": ask_user,
    "extract": extract,
    "agent": agent,
    "email": email,
    "wait_email": wait_email,
    "compare": compare,
    "endpoint": endpoint,
    "wait_status": wait_status,
    "notify": notify,
    "zoho_create": zoho_create,
    "sharepoint_attach": sharepoint_attach,
}
