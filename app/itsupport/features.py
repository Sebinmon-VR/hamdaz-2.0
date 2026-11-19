"""The agent's features, each with its switch on the settings page.

The user's shortlist (2026-11-18), built together and switched one by one:
anything that **writes** or **sends a message nobody asked for** ships off,
so a push changes nothing until a super admin turns it on (and a trial goes
to one chat with "Send check-ins to" on the page). Answers to questions,
which only read, ship on.

The worker's settings copy carries the saved switches as
``it_support_features``; :func:`on` is the one place they are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class Feature:
    key: str
    name: str
    #: What it does, in a line, for the page.
    about: str
    group: str
    default: bool
    #: It messages people unasked (shown on the page; the trial redirect applies).
    sends: bool = False
    #: It changes something in the ERP.
    writes: bool = False


CATALOGUE: Final[tuple[Feature, ...]] = (
    # ── status and daily asks ──
    Feature("status_updates", "Status and reasons from chat",
            "People update a task's Status, Submission Status and Remarks, or say why it's "
            "late, by replying in Teams. Kept in the ERP and reported as the web form does; "
            "the Proposals list itself is not written.", "Status and daily asks", False,
            writes=True),
    Feature("daily_asks", "Daily asks in Teams",
            "At the status reminder's time and the follow-up's time, IT Support asks in Teams "
            "about the same tasks the emails ask about, numbered so a reply can pick them.",
            "Status and daily asks", False, sends=True),
    Feature("risk_notes", "Risk notes in the morning ask",
            "The morning ask flags a task closing soon with no costing yet, or a supplier "
            "quote that expires before the BCD.", "Status and daily asks", True),
    Feature("reason_tags", "Reason tags",
            "Each late-task reason is tagged (supplier, customer, approval, workload, portal) "
            "by rules, and the daily report counts them.", "Status and daily asks", True),
    Feature("team_brief", "Team's day, for super admins",
            "At the morning brief's time, a super admin or manager gets the team's tasks due "
            "today (holder, BCD, status, who's away) and what's past its BCD and still open, "
            "numbered, instead of a count.", "Status and daily asks", False, sends=True),
    Feature("connect_people", "Connect with a teammate",
            "Someone who oversees a team can ask IT Support to message a teammate about a "
            "task (after their yes; the answer comes back to them) or get a Teams call link.",
            "Status and daily asks", False, sends=True),
    # ── alerts ──
    Feature("bcd_asks", "Placeholder BCDs in Teams",
            "When a task's BCD is still the assignment time, IT Support asks its holder in "
            "Teams, with the link to fix it on the list.", "Alerts", False, sends=True),
    Feature("bcd_moved", "BCD moved",
            "Tells a task's holder when its BCD moves on the list.", "Alerts", False,
            sends=True),
    Feature("new_task", "New task heads-up",
            "Tells the holder about a task just assigned to them: BCD, end user, and whether "
            "the RFQ files are on it.", "Alerts", False, sends=True),
    Feature("approval_pings", "Approval results",
            "Tells whoever sent a costing for approval when it's approved, rejected or sent "
            "back.", "Alerts", False, sends=True),
    Feature("costing_ask", "Submitted without a costing",
            "When a task shows Submitted and there's no costing for it, neither in the ERP nor "
            "as a costing file on the task (its list attachments or task folder), IT Support "
            "asks its holder for it, once per task.", "Alerts", False, sends=True),
    # ── answers (read only) ──
    Feature("task_files", "A task's files in chat",
            "Asked for a task's files (\"send me the RFQ of 1958\"), IT Support gives links to "
            "the files in its task folder and on its Proposals list item.", "Answers", True),
    Feature("tender_lookup", "Ask about any tender",
            "One answer about a tender by number or name: the task, its costing, quotes and "
            "analysis.", "Answers", True),
    Feature("past_bids", "Past bids and who knows this",
            "Searches past tasks, and says who has bid on similar ones.", "Answers", True),
    Feature("price_memory", "Price memory",
            "What we were quoted before for an item, by whom and when.", "Answers", True),
    Feature("quote_checks", "Quote checks",
            "A supplier quote read in chat is checked: validity against the BCD, currency, "
            "charges left out.", "Answers", True),
    Feature("howto", "How do I… in the ERP",
            "Answers how to do things in the ERP, with the link to the page.", "Answers", True),
    Feature("currency", "Currency at Zoho's rate",
            "Converts amounts at the rate Zoho Books uses.", "Answers", True),
    Feature("projects", "Customer POs in chat",
            "Answers about customer POs from the project tracker.", "Answers", True),
    Feature("whos_in", "Who's in today", "Answers who's on leave today or this week.",
            "Answers", True),
    # ── orders, enquiries, people ──
    Feature("delivery_watch", "PO delivery dates",
            "Tells a PO's owner when its delivery date is near and it isn't delivered.",
            "Orders, enquiries, people", False, sends=True),
    Feature("website_enquiries", "Website enquiries",
            "Tells the person a hamdaz.com enquiry is assigned to, with a summary.",
            "Orders, enquiries, people", False, sends=True),
    Feature("leave_apply", "Apply for leave from chat",
            "People file a leave request by asking; approval stays in the app.",
            "Orders, enquiries, people", False, writes=True),
    Feature("leave_handover", "Leave handover",
            "When someone is away, their tasks closing today or tomorrow are raised with the "
            "team's lead or manager.", "Orders, enquiries, people", False, sends=True),
    # ── admin ──
    Feature("feedback", "👎 feedback",
            "A 👎 on one of its replies is kept, with the conversation, in the activity log.",
            "Admin", True),
)
BY_KEY: Final = {f.key: f for f in CATALOGUE}


def trial(settings: Settings) -> bool:
    """A trial on this machine (IT_SUPPORT_TRIAL_ONLY): see ``settings``."""
    return bool(getattr(settings, "it_support_trial_only", "").strip())


def on(settings: Settings, key: str) -> bool:
    """Whether a feature is on: as saved on the page, else its default."""
    saved = getattr(settings, "it_support_features", None) or {}
    if key in saved:
        return bool(saved[key])
    feature = BY_KEY.get(key)
    return bool(feature and feature.default)


def clean(values: object) -> dict[str, bool]:
    """Saved switches from the page: known keys, true or false. Only what
    differs from the default is kept."""
    if not isinstance(values, dict):
        raise ValueError("Features are a map of name to on/off.")
    out: dict[str, bool] = {}
    for key, value in values.items():
        if key not in BY_KEY:
            raise ValueError(f"{key} is not a feature.")
        if not isinstance(value, bool):
            raise ValueError(f"{key}: on or off (true or false).")
        if value != BY_KEY[key].default:
            out[key] = value
    return out


def rows(saved: dict[str, bool] | None) -> list[dict[str, object]]:
    """The switches as the page lists them."""
    saved = saved or {}
    return [{"key": f.key, "name": f.name, "about": f.about, "group": f.group,
             "default": f.default, "on": bool(saved.get(f.key, f.default)), "sends": f.sends,
             "writes": f.writes} for f in CATALOGUE]
