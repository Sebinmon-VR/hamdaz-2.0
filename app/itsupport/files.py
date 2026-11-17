"""Files sent in the chat: fetched, opened, and given a first look.

Two kinds come with a Teams message: a **file** (an attachment of type
``reference``, kept in the sender's OneDrive and shared with the chat) and a
**picture** pasted into the text (an ``<img>`` pointing at the message's
hosted content). Both are fetched through the account (``graph``), then
opened by the ERP's own document reader (``app.comparison.documents``):
PDF, Excel, Word, CSV and images.

The first look decides nothing by itself, it gathers what the goal needs:

* **what the file seems to be**: the name first (``enquiries.reading``'s
  rule: an RFQ's name wins over a quotation word), then the words on its
  first pages: a supplier's "Quotation No / Valid until / Unit price" against
  a customer's "Request for Quotation / Scope of work / Bill of quantities";
* **which task it is for**: an RFQ, tender or PR number in the file's name or
  first pages that matches one of the person's open tasks.

Nothing is stored: the bytes stay in memory for the conversation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from app.comparison.documents import DocumentError, Readable, prepare
from app.enquiries import reading as enquiry_reading
from app.itsupport.graph import FilesNotAllowedError, FileTooBigError, GraphError, ITSupportGraph
from app.proposals.sharepoint import ProposalTask
from app.reminders.service import is_done

logger = logging.getLogger("hamdaz.itsupport")

_IMG = re.compile(r"<img[^>]+src=\"([^\"]+/hostedContents/[^\"]+)\"", re.IGNORECASE)
#: Words a supplier's quotation carries, and words a customer's request does.
_QUOTE_WORDS = re.compile(
    r"\b(quotation\s*(no|number|#|ref)|our\s+(offer|quote)|offer\s+no|proforma|"
    r"valid(ity)?\s+(until|till|for)|price\s+validity|unit\s+price|total\s+price|"
    r"we\s+are\s+pleased\s+to\s+(offer|quote)|thank\s+you\s+for\s+your\s+enquiry)",
    re.IGNORECASE,
)
_REQUIREMENT_WORDS = re.compile(
    r"\b(request\s+for\s+(quotation|proposal)|rfq\s*(no|number|#)|tender\s*(no|number)|"
    r"scope\s+of\s+work|bill\s+of\s+quantit|technical\s+specification|"
    r"bidders?\s+shall|closing\s+date|submission\s+deadline|purchase\s+requisition)",
    re.IGNORECASE,
)
#: An RFQ, tender or PR number: six digits or more.
_NUMBER = re.compile(r"\b\d{6,12}\b")
#: How much of a file's text the first look reads.
LOOK_CHARS: Final = 6000


@dataclass(slots=True)
class ChatFile:
    name: str
    url: str
    #: A picture pasted into the message rather than a file attached to it.
    pasted: bool = False


@dataclass(slots=True)
class Opened:
    name: str
    readable: Readable | None = None
    #: Why it could not be opened, in words for the person.
    problem: str | None = None
    #: "requirement", "supplier_quote" or "" when the first look can't say.
    looks_like: str = ""
    #: The person's tasks this file names (by number), best first.
    task_ids: list[str] = field(default_factory=list)
    #: The file as sent, for handing on to the ERP (an upload as the person).
    data: bytes | None = None
    media_type: str = "application/octet-stream"

    @property
    def text(self) -> str:
        return (self.readable.text or "") if self.readable else ""


def in_message(message: dict[str, Any]) -> list[ChatFile]:
    """The files and pasted pictures of one Teams message."""
    out = []
    for a in message.get("attachments") or []:
        if a.get("contentType") == "reference" and a.get("contentUrl"):
            out.append(ChatFile(a.get("name") or "file", a["contentUrl"]))
    body = (message.get("body") or {}).get("content") or ""
    for index, url in enumerate(_IMG.findall(body), start=1):
        out.append(ChatFile(f"picture-{index}.png", url.replace("&amp;", "&"), pasted=True))
    return out


async def fetch_and_open(graph: ITSupportGraph, f: ChatFile) -> Opened:
    try:
        data = await (graph.hosted_content(f.url) if f.pasted else graph.shared_file(f.url))
    except FilesNotAllowedError:
        raise
    except FileTooBigError as exc:
        return Opened(f.name, problem=f"it's too big for me to open ({exc})")
    except GraphError as exc:
        logger.warning("it support: fetching %s failed: %s", f.name, exc)
        return Opened(f.name, problem="I couldn't download it")
    try:
        # Scans are kept as they are, for Claude to look at (no OCR on this
        # machine's way, the same choice the enquiry analysis makes).
        readable = prepare(f.name, data, "image/png" if f.pasted else None, ocr=False)
    except DocumentError as exc:
        return Opened(f.name, problem=str(exc), data=data)
    return Opened(f.name, readable, data=data, media_type=readable.media_type)


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def first_look(opened: Opened, tasks: list[ProposalTask]) -> None:
    """Fill in what ``opened`` seems to be and which task it names."""
    head = opened.text[:LOOK_CHARS]
    by_name = enquiry_reading.guess_kind(opened.name)
    quote_hits = len(_QUOTE_WORDS.findall(head))
    requirement_hits = len(_REQUIREMENT_WORDS.findall(head))
    if by_name:
        opened.looks_like = by_name
    elif quote_hits > requirement_hits:
        opened.looks_like = "supplier_quote"
    elif requirement_hits > quote_hits:
        opened.looks_like = "requirement"
    named = _numbers(opened.name) | _numbers(head)
    scored = []
    for task in tasks:
        if is_done(task):
            continue  # an old, finished task with the same RFQ number
        common = named & _numbers(task.title or "")
        if common:
            # A number in the file's own name counts double.
            scored.append((len(common) + len(_numbers(opened.name) & common), task.id))
    opened.task_ids = [tid for _, tid in sorted(scored, reverse=True)]


def what_for(opened: list[Opened], first_name: str) -> str:
    """The question for files sent with no words, by rules, no model: what
    they look like, which task they name, and what could be done with them."""
    ok = [o for o in opened if not o.problem]
    if not ok:
        why = "; ".join(f"{o.name}: {o.problem}" for o in opened)
        return f"I couldn't open that, {first_name}: {why}. Could you send it again?"
    kinds = {o.looks_like for o in ok}
    tasks = sorted({t for o in ok for t in o.task_ids[:1]})
    for_task = f" for {tasks[0]}" if len(tasks) == 1 else ""
    many = len(ok) > 1
    if kinds == {"supplier_quote"}:
        seen = f"{len(ok)} supplier quotes" if many else "a supplier quote"
        offer = (f"Want me to compare them for the costing{for_task}, put them on the costing "
                 "sheet, or something else?" if many else
                 f"Want me to put it on the costing{for_task}, compare it with the other quotes, "
                 "or something else?")
    elif kinds == {"requirement"}:
        seen = "the customer's RFQ" + ("s" if many else "")
        offer = "Want me to go through what they need, find suppliers for it, or something else?"
    else:
        seen = f"{len(ok)} files" if many else ok[0].name
        offer = "What would you like me to do with " + ("them?" if many else "it?")
    gone = [o.name for o in opened if o.problem]
    tail = f" (I couldn't open {', '.join(gone)}.)" if gone else ""
    return f"Got it 👍 looks like {seen}{for_task}. {offer}{tail}"


def describe(opened: Opened) -> str:
    """One file, for the goal step: name, first look, start of its words."""
    if opened.problem:
        return f"- {opened.name}: could not be opened ({opened.problem})"
    r = opened.readable
    assert r is not None
    shape = "text" if r.kind == "text" else f"a {r.kind} (scan or picture, no text layer)"
    looks = {"requirement": "looks like a customer's request (RFQ/tender/BOQ)",
             "supplier_quote": "looks like a supplier's quotation"}.get(opened.looks_like,
                                                                         "kind unclear")
    tasks = f"; names task(s) {', '.join(opened.task_ids[:3])}" if opened.task_ids else ""
    start = " ".join(opened.text[:700].split())
    return f"- {opened.name}: {shape}; {looks}{tasks}\n  starts: {start or '(no text)'}"
