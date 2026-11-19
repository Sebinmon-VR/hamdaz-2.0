"""Status and reasons from the chat: "1 submitted, 2 on hold waiting for price".

The user's shortlist (2026-11-18, phase 1): people update a task's Status,
Submission Status and Remarks, or say why it's late, by replying to IT
Support in Teams, picking the task by its number in the ask, its id, or its
tender number. **Rules first**: a reply the rules read fully is filed at once
with no model; anything else goes to the brain, which has the same filing as
its ``update_task`` tool.

Filed through the ERP's own routes, as the person (``erp.OWN_WRITES``, only
while the ``status_updates`` feature is on):

* a status: ``POST /reminders/update``, kept as an answer to the task's
  reminder and reported to the team's managers, like the web form. The
  Proposals list is written only when the reminder's ``write_sharepoint`` is
  on (it is off), so the reply says the list still needs it;
* a reason for a late task with a question waiting: ``POST
  /followups/{id}/reason``; "submitted" to that question: ``false-positive``
  plus the status, so the follow-up takes their word next time;
* a note with no question waiting goes into the task's Remarks, on a new line
  dated the team's way ("18/11: waiting for price").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.proposals.sharepoint import ProposalTask

UAE: Final = ZoneInfo("Asia/Dubai")

#: Words for a Status or Submission Status, longest first ("not submitted"
#: before "submitted"). Values are the list's own choices.
STATUS_WORDS: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    (re.compile(r"\bnot\s+submit+ed\b|\bnot\s+going\s+to\s+submit\b|\bregret+ed\b|"
                r"\bdeclined?\b|\bno\s+bid\b", re.I), "submission", "Not Submitted"),
    (re.compile(r"\b(?:already\s+)?submit+ed\b|\bsubmision\s+done\b|\bsubmit+ed\s+it\b|"
                r"\bsent\s+(?:the\s+)?(?:bid|quote|offer)\b", re.I), "submission", "Submitted"),
    (re.compile(r"\bcompleted?\b|\bfinished\b", re.I), "status", "Completed"),
    (re.compile(r"\bon\s*hold\b|\bhold(?:ing)?\b|\bpaused\b", re.I), "status", "On Hold"),
    (re.compile(r"\bin\s*progress\b|\bongoing\b|\bworking\s+on\s+it\b|\bstill\s+working\b",
                re.I), "status", "In Progress"),
    (re.compile(r"\bnot\s+started\b|\byet\s+to\s+start\b", re.I), "status", "Not Started"),
)
#: "use my remarks" / "same as remarks" / just "remarks".
_REMARKS: Final = re.compile(r"^\W*(?:use\s+(?:my\s+|the\s+)?|same\s+as\s+(?:the\s+)?|see\s+)?"
                             r"remarks?\W*$", re.I)
#: A question about a task, not an update ("is 1952 submitted?").
_QUESTION: Final = re.compile(
    r"\?|^\s*(?:is|are|was|were|did|does|do|has|have|had|what|when|who|whom|which|why|how|"
    r"can|could|should|would|will|shall|any|show|tell|check|list|find|give)\b", re.I)
#: Where one item's answer ends and the next begins.
_SPLIT: Final = re.compile(r"[\n;]+|,\s*(?=(?:no\.?\s*|#)?\d)|\band\s+(?=(?:no\.?\s*|#)?\d)",
                           re.I)
#: A list number at the start of a piece: "1", "1.", "1)", "#1", "no 1".
_LEAD: Final = re.compile(r"^\s*(?:no\.?\s*|#|item\s+)?(\d{1,2})(?:[.):\-]|\s|$)", re.I)
#: A task id or tender number anywhere: 3+ digits.
_LONG_NUMBER: Final = re.compile(r"(?<![\d.])(\d{3,12})(?![\d.])")
_ALL: Final = re.compile(r"^\s*(?:all|both|everything|all\s+of\s+them|all\s+these)\b", re.I)
#: Filler around the note ("it's", "-", "because").
_FILLER: Final = re.compile(r"^[\s,:;.\-–—]*(?:is|it'?s|its|was|are|they'?re|because|bcz|bcoz|"
                            r"coz|as|since|due\s+to|reason)?[\s,:;.\-–—]*", re.I)


@dataclass(slots=True)
class Item:
    """One task in an ask, by its number there."""

    number: int
    task_id: str
    title: str = ""
    #: The row it stands for, where the answer goes (a BCD check's id).
    ref: str = ""


@dataclass(slots=True)
class Asked:
    """The list IT Support asked about: "status" (where do these stand) or
    "reason" (what held these up), with when (UTC)."""

    kind: str
    items: list[Item]
    at: datetime


@dataclass(slots=True)
class Update:
    task_id: str
    status: str | None = None
    submission: str | None = None
    note: str = ""
    use_remarks: bool = False

    @property
    def empty(self) -> bool:
        return not (self.status or self.submission or self.note or self.use_remarks)


@dataclass(slots=True)
class Reading:
    updates: list[Update] = field(default_factory=list)
    #: Pieces the rules couldn't place: the brain takes the whole message.
    unsure: bool = False


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION.search(text))


def _words_of(piece: str) -> tuple[str | None, str | None, str]:
    """Status, submission and the rest of the words."""
    status = submission = None
    rest = piece
    for pattern, kind, value in STATUS_WORDS:
        if pattern.search(rest):
            if kind == "status" and status is None:
                status = value
            elif kind == "submission" and submission is None:
                submission = value
            rest = pattern.sub(" ", rest, count=1)
    rest = re.sub(r"\s{2,}", " ", rest)
    rest = _FILLER.sub("", rest).strip(" ,.;:-–—")
    return status, submission, rest


def read(text: str, *, asked: Asked | None, tasks: list[ProposalTask]) -> Reading:
    """A reply read by rules. Each piece names its task (by its number in
    the ask, its id or its tender number, or all of them); a piece the rules
    can't tie to one task makes the reading unsure."""
    out = Reading()
    text = (text or "").strip()
    if not text or looks_like_question(text):
        out.unsure = True
        return out
    by_id = {t.id: t for t in tasks}
    numbered = {i.number: i.task_id for i in (asked.items if asked else [])}
    pieces = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    for piece in pieces:
        targets: list[str] = []
        body = piece
        if asked and _ALL.match(piece):
            targets = [i.task_id for i in asked.items]
            body = _ALL.sub("", piece, count=1)
        elif (lead := _LEAD.match(piece)) and int(lead.group(1)) in numbered:
            targets = [numbered[int(lead.group(1))]]
            body = piece[lead.end():]
        else:
            for number in _LONG_NUMBER.findall(piece):
                if number in by_id:
                    targets = [number]
                else:
                    matched = [t.id for t in tasks if number in (t.title or "")]
                    if len(matched) == 1:
                        targets = matched
                if targets:
                    body = piece.replace(number, " ", 1)
                    break
        if not targets and asked and len(asked.items) == 1 and len(pieces) == 1:
            targets = [asked.items[0].task_id]
        if not targets:
            out.unsure = True
            continue
        if _REMARKS.match(body):
            out.updates += [Update(t, use_remarks=True) for t in targets]
            continue
        status, submission, rest = _words_of(body)
        if not (status or submission or rest):
            out.unsure = True
            continue
        out.updates += [Update(t, status, submission, rest) for t in targets]
    if not out.updates:
        out.unsure = True
    return out


def remarks_with(existing: str | None, note: str, now: datetime) -> str:
    """The task's Remarks with ``note`` on a new line, dated the way the
    team writes them ("18/11: waiting for price")."""
    line = f"{now.astimezone(UAE):%d/%m}: {note.strip()}"
    existing = (existing or "").rstrip()
    return f"{existing}\n{line}" if existing else line


# ── filing, as the person ──────────────────────────────────────────────


@dataclass(slots=True)
class Filed:
    task_id: str
    said: str
    ok: bool = True
    #: A Status, Submission Status or Remarks was filed: the list wants it too.
    on_list: bool = False


async def file(erp: Any, *, user_id: Any, access: Any, update: Update,
               task: ProposalTask | None, pending_reason: dict[str, str],
               now: datetime) -> Filed:
    """One update filed through the ERP as the person. ``pending_reason``:
    task id → the follow-up still waiting for its reason."""
    followup_id = pending_reason.get(update.task_id)
    said: list[str] = []
    if followup_id and update.submission == "Submitted":
        answer = await erp.call(user_id, "POST", f"/followups/{followup_id}/false-positive",
                                access=access, body={"note": "Said in Teams: submitted."})
        if not answer.ok:
            return Filed(update.task_id, _why(answer), ok=False)
    elif followup_id and (update.note or update.use_remarks):
        answer = await erp.call(user_id, "POST", f"/followups/{followup_id}/reason",
                                access=access, body={"reason": update.note,
                                                     "use_remarks": update.use_remarks})
        if not answer.ok:
            return Filed(update.task_id, _why(answer), ok=False)
        said.append("reason sent" if not update.use_remarks else "reason sent from your Remarks")
        if not (update.status or update.submission):
            return Filed(update.task_id, ", ".join(said))
    values: dict[str, str] = {}
    if update.status:
        values["status"] = update.status
    if update.submission:
        values["submission_status"] = update.submission
    if update.note and not followup_id:
        values["remarks"] = remarks_with(task.remarks if task else None, update.note, now)
    if update.use_remarks and not followup_id:
        return Filed(update.task_id, "nothing asked about it, so its Remarks stand as they are")
    if values:
        answer = await erp.call(user_id, "POST", "/reminders/update", access=access,
                                body={"task_id": update.task_id, "values": values})
        if not answer.ok:
            if said:
                return Filed(update.task_id, f"{', '.join(said)}; the status didn't go: "
                             f"{_why(answer)}", ok=False)
            return Filed(update.task_id, _why(answer), ok=False)
        if update.status:
            said.append(update.status)
        if update.submission:
            said.append(update.submission)
        if "remarks" in values:
            said.append(f"noted \"{update.note}\"")
        return Filed(update.task_id, ", ".join(said), on_list=True)
    return Filed(update.task_id, ", ".join(said) or "nothing to change")


def _why(answer: Any) -> str:
    body = answer.body
    if isinstance(body, dict):
        body = body.get("detail") or body
    text = body if isinstance(body, str) else str(body)
    return text.removeprefix("Not from the chat: ")[:200]


def reply(filed: list[Filed], *, list_written: bool) -> str:
    """What IT Support says back, in a line or two."""
    done = [f for f in filed if f.ok]
    failed = [f for f in filed if not f.ok]
    parts = []
    if done:
        parts.append("Done: " + "; ".join(f"{f.task_id} {f.said}" for f in done) + ".")
    for f in failed:
        parts.append(f"{f.task_id} didn't go through: {f.said}")
    if not list_written and any(f.on_list for f in done):
        parts.append("The Proposals list itself isn't changed, so update it there too when "
                     "you get a sec.")
    return " ".join(parts) or "Hmm, nothing to change there."


# ── the ask in the chat, read back after a restart ─────────────────────

#: An ask's line: "1. 1952 · title, …".
_ASK_LINE: Final = re.compile(r"^\s*(\d{1,2})\.\s+(\d{2,7})\s+·\s*(.*)$", re.M)
#: The ask's kind, from its wording.
_REASON_ASK: Final = re.compile(r"held (?:these|it|them) up|past (?:their|its) BCD", re.I)


#: A team list's row, sent as a table and read back cell by cell:
#: "1 | 1958 | 6000156515 · PR 4001754766 | Vishnu | …" (``talk.plain``).
_TEAM_ROW: Final = re.compile(r"^\s*(\d{1,2})\s*\|\s*(\d{2,7})\s*\|\s*([^|\n]*)", re.M)
#: The team's list before it was a table: "Presale has 8 due today:", with
#: "— holder —" on each line. Its numbers are the team's, not their own.
_STATUS_HINT: Final = re.compile(r'Reply like "1 submitted"')
#: The costing ask ("shows submitted, but I can't find a costing sheet").
_COSTING_ASK: Final = re.compile(r"can't find a costing sheet")
_OLD_TEAM: Final = re.compile(r"\bhas \d+ due today:|^Past BCD, still open:", re.M)


def asked_in(text: str, at: datetime) -> Asked | None:
    """An ask found in one of IT Support's own messages (its numbered lines,
    or a team list's numbered rows)."""
    rows = [Item(int(n), task_id, title.strip()) for n, task_id, title in _TEAM_ROW.findall(text)]
    if rows:
        # A person's own ask is a table too; how it says to answer tells it
        # from the team's list.
        if _COSTING_ASK.search(text):
            return Asked("costing", rows, at)
        if _REASON_ASK.search(text):
            return Asked("reason", rows, at)
        if _STATUS_HINT.search(text):
            return Asked("status", rows, at)
        return Asked("team", rows, at)
    items = [Item(int(n), task_id, title.strip()) for n, task_id, title in _ASK_LINE.findall(text)]
    if not items:
        return None
    if _OLD_TEAM.search(text):
        return Asked("team", items, at)
    return Asked("reason" if _REASON_ASK.search(text) else "status", items, at)
