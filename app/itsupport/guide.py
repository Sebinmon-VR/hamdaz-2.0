"""How things are done in the ERP, for "how do I…" in the chat (feature howto).

Written from the app's own pages (the frontend's routes), so each answer names
the real page and links it. Kept to what the pages are for; the exact buttons
are the page's to show. Matched by words, rules only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Entry:
    words: tuple[str, ...]
    title: str
    path: str
    steps: str


GUIDE: Final[tuple[Entry, ...]] = (
    Entry(("costing", "quote request", "quotation", "price", "selling", "margin", "raise",
           "create quote", "new quote"),
          "Make a costing sheet (quote request)", "/quote-requests/new",
          "Start a new quote request, from a Proposals task where there is one (it carries "
          "the task's details and folder). Add the supplier quotes, pick the supplier per "
          "line, set the margin and the landed-cost charges, and check the totals. It saves "
          "as a draft until you send it for approval. I can also build one here in the chat "
          "from the supplier quotes you send me."),
    Entry(("approval", "approve", "submit", "send for approval", "queue", "reviewer"),
          "Send a costing for approval, or approve one", "/quote-requests/queue",
          "On the costing's own page, send it for approval when it's complete; the approvers "
          "are told. Approvers see what's waiting in the queue, and approve, reject or send "
          "it back with a note. Sending for approval is done in the app, not from the chat."),
    Entry(("supplier quote", "upload quote", "attach quote", "quotes"),
          "Add a supplier's quote", "/quotes",
          "Upload the supplier's quote (PDF, Excel or a scan) on the quote request, or on the "
          "Quotes page; the app reads the lines, prices and charges. You can also send it to "
          "me here and I'll read it."),
    Entry(("compare", "comparison", "cheapest"),
          "Compare supplier quotes", "/comparisons/new",
          "Start a comparison with two or more supplier quotes: it lines items up and shows "
          "the cheapest per line and overall. A costing sheet with several quotes compares "
          "them too."),
    Entry(("enquiry analysis", "analyse", "analyze", "rfq", "tender document", "requirements",
           "boq"),
          "Read a tender's requirements", "/enquiries",
          "Open the task on the enquiries page and run the analysis on its documents: it "
          "lists the items, quantities and conditions. Or send me the RFQ here."),
    Entry(("my tasks", "tasks", "proposals", "assigned"),
          "See your tasks", "/proposals/my-tasks",
          "Your Proposals tasks with BCDs and status. The task itself is edited on the "
          "Proposals list in SharePoint."),
    Entry(("reminder", "status update", "update status", "where it stands"),
          "Answer a status reminder", "/reminders",
          "Each reminder opens a form with the task's Status, Submission Status, Remarks and "
          "Working notes; change what's different and send. You can also just tell me here: "
          "\"1952 submitted\"."),
    Entry(("reason", "late", "follow up", "follow-up", "overdue", "why"),
          "Give the reason a task is late", "/followups",
          "The follow-up asks why a task went past its BCD unsubmitted; answer on its page "
          "(or use the task's Remarks). Or tell me here: \"1991 waiting for the supplier\"."),
    Entry(("bcd", "closing date", "placeholder"),
          "Fix a task's BCD", "/proposals/my-tasks",
          "The BCD is set on the Proposals list in SharePoint (the task's edit form). A BCD "
          "check asks when it still shows the assignment time; confirm it there if it's "
          "really right."),
    Entry(("leave", "holiday", "sick", "half day", "casual", "time off", "vacation"),
          "Apply for leave", "/leave/request",
          "Request leave with its type (annual, sick, emergency, unpaid, casual), the dates "
          "and a half day if it's one day. See yours on the Leave page and who's off on the "
          "leave calendar (/leave/calendar)."),
    Entry(("website enquiry", "website", "contact form", "lead"),
          "Handle a website enquiry", "/website-enquiries",
          "Enquiries from hamdaz.com land here; open one, assign it, set its status and add "
          "notes."),
    Entry(("report", "eod", "dashboard", "analytics"),
          "Reports and the dashboard", "/reports",
          "Team reports are on the Reports page; your own overview is on the dashboard."),
    Entry(("directory", "colleague", "phone", "contact", "who is"),
          "Find a colleague", "/directory",
          "The directory lists everyone with their team and contact details."),
    Entry(("notification", "alerts", "bell"),
          "Notifications", "/notifications",
          "Everything the app has told you is under notifications."),
)


def _score(entry: Entry, text: str) -> int:
    return sum(len(w) for w in entry.words if re.search(rf"\b{re.escape(w)}", text))


def answer(topic: str, base: str) -> str:
    text = (topic or "").casefold()
    ranked = sorted(GUIDE, key=lambda e: -_score(e, text))
    hits = [e for e in ranked if _score(e, text)][:2]
    if not hits:
        return ("Not in the guide. Pages: " + "; ".join(f"{e.title} {base}{e.path}"
                                                        for e in GUIDE))
    return "\n".join(f"{e.title}: {e.steps} Page: {base}{e.path}" for e in hits)
