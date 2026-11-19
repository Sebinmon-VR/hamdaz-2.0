"""Links into the web app, checked before a reply goes out.

The user, 2026-11-19: "the link the agent is giving sometimes are wrong". The
model wrote ``/tasks`` (no such page: a 404) and the bare new-costing page
where the task's own was meant. So every link to the app in a reply is
matched against the app's real pages (the frontend's routes, below); one
that isn't a page loses its link and stays as words, rather than sending
someone to a 404. Links elsewhere (SharePoint, Teams calls) pass as they are.
"""

from __future__ import annotations

import logging
import re
from typing import Final

logger = logging.getLogger("hamdaz.itsupport")

#: The web app's pages (``src/app/(app)/**/page.tsx`` in the frontend), a
#: segment in brackets standing for any one segment. Keep in step with it.
PAGES: Final = (
    "/admin/access", "/admin/ai-usage", "/admin/ariba", "/admin/bcd", "/admin/calendar",
    "/admin/console", "/admin/followups", "/admin/it-support", "/admin/mail-watch",
    "/admin/mirror", "/admin/permissions", "/admin/reminders", "/admin/reports",
    "/admin/roles", "/admin/roles/assignments", "/admin/templates", "/admin/templates/[key]",
    "/admin/users/[id]", "/agents", "/agents/[id]", "/agents/new", "/assignment/labels",
    "/assignment/policy", "/assignment/runs/[id]", "/assignment/user-analytics", "/bcd/[id]",
    "/comparisons", "/comparisons/[id]", "/comparisons/new", "/dashboard", "/directory",
    "/directory/[id]", "/enquiries", "/enquiries/[taskId]", "/followups", "/followups/[id]",
    "/hr/applications", "/hr/applications/[id]", "/hr/me", "/hr/my-reviews", "/hr/openings",
    "/hr/openings/[id]", "/hr/people", "/hr/people/[id]", "/hr/reviews", "/hr/reviews/[id]",
    "/hr/staff", "/leave", "/leave/calendar", "/leave/request", "/leave/requests",
    "/leave/settings", "/meetings", "/notifications", "/payroll", "/projects",
    "/projects/[id]", "/projects/[id]/plan", "/projects/activity", "/projects/board",
    "/projects/portfolio", "/proposals/my-tasks", "/quote-requests", "/quote-requests/[id]",
    "/quote-requests/new", "/quote-requests/queue", "/quotes", "/quotes/[id]", "/reminders",
    "/reminders/[id]", "/reports", "/reports/[id]", "/reports/new", "/reports/overview",
    "/settings", "/teams", "/teams/[slug]", "/teams/[slug]/dashboard", "/teams/[slug]/members",
    "/teams/[slug]/projects", "/teams/[slug]/proposals", "/teams/[slug]/reports", "/tenders",
    "/website-enquiries", "/website-enquiries/[id]", "/",
)


def _pattern(page: str) -> str:
    return "".join("/[^/?#]+" if re.fullmatch(r"\[[^\]]+\]", seg) else "/" + re.escape(seg)
                   for seg in page.strip("/").split("/") if seg) or "/"


_PAGE: Final = re.compile("(?:" + "|".join(_pattern(p) for p in PAGES) + r")/?(?:[?#].*)?")
#: "[label](url)" in a reply.
_MD: Final = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+)\)")


def is_page(url: str, base: str) -> bool:
    """Whether a link into the app is one of its pages. Not the app's: True."""
    base = base.rstrip("/")
    if not base or not url.startswith(base):
        return True
    path = url[len(base):] or "/"
    if not path.startswith("/"):
        return True  # another host that only starts the same
    return _PAGE.fullmatch(path) is not None


def check(text: str, base: str) -> str:
    """The reply with every link to a page the app doesn't have made words."""
    def one(found: re.Match[str]) -> str:
        label, url = found.group(1), found.group(2)
        if is_page(url, base):
            return found.group(0)
        logger.warning("it support: dropped a link to a page the app doesn't have: %s", url)
        return label

    text = _MD.sub(one, text)
    # A bare address into the app that isn't a page goes too.
    base = base.rstrip("/")
    if base:
        bare = re.compile(re.escape(base) + r"[^\s<>\"\])]*")
        def plain(m: re.Match[str]) -> str:
            url = m.group(0).rstrip(".,;:!?")
            return m.group(0) if is_page(url, base) else "the app" + m.group(0)[len(url):]

        text = bare.sub(plain, text)
    return text
