"""The one visit to the Ariba supplier portal: read the open events, leave.

Ariba offers suppliers no API for this, so the Events list is read the way a
person reads it — in a real browser (Playwright's Chromium), signed in with the
supplier login. What one visit does, and all it does:

1. Open ``Sourcing.aw`` with the saved session. If Ariba has expired it, sign
   in once — username, Next, password — the same two steps a person takes.
2. In the ADNOC frame, expand **Status: Open** if it is not already expanded.
3. Read each row's Title, ID and End Time, and hand back the refreshed session.

It never clicks into an event, never participates, never downloads. The pace is
a person's, and nothing here disguises the browser: if Ariba ever asks for more
than a password — a captcha, a second factor — that is :class:`SignInRefusedError`,
and the worker stops signing in until somebody has looked.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("hamdaz.ariba.portal")

SOURCING_URL = "https://service.ariba.com/Sourcing.aw"
#: Where a sign-in starts. Sourcing.aw without a session shows an older
#: one-page form instead; the reader always signs in here, on the two-step
#: form a person uses, and then goes back to the Events list.
SIGN_IN_URL = "https://service.ariba.com/Supplier.aw"
#: Either sign-in form: the two-step one (``#userid``) or Sourcing's own.
_SIGN_IN_FORM = "#userid, input[name=UserName]"
#: The frame the Events table is drawn in; its host varies by data centre.
_EVENTS_FRAME = "/Sourcing/Main"
_OPEN_GROUP = 'tr.tableGroupBy:has-text("Status: Open")'
#: Ariba's in-page help widget: a heavy third-party player the reader never uses.
_SKIP_HOSTS = ("walkme.cloud.sap",)

#: Rows under the Open header, up to the next group header. Run in the page.
_READ_OPEN_ROWS = """
(header) => {
  const rows = [];
  let tr = header.nextElementSibling;
  while (tr && !tr.classList.contains('tableGroupBy')) {
    const cells = Array.from(tr.children).map(td => (td.innerText || '').trim());
    if (cells.length >= 3 && /^Doc\\d+/.test(cells[1])) {
      rows.push({title: cells[0], doc_id: cells[1], end_time: cells[2],
                 participated: cells.length >= 5 ? cells[4] : ''});
    }
    tr = tr.nextElementSibling;
  }
  return rows;
}
"""


class PortalError(Exception):
    """The visit did not get as far as the Events list."""


class SignInRefusedError(PortalError):
    """Ariba would not take the password, or asked for more than one."""


@dataclass(slots=True)
class OpenEvent:
    doc_id: str
    title: str
    end_time: datetime | None
    #: The Participated column — whether Ariba holds a response from us.
    #: ``None`` when the cell is neither Yes nor No.
    participated: bool | None = None


@dataclass(slots=True)
class Visit:
    events: list[OpenEvent]
    session_state: dict[str, Any]
    signed_in: bool


def _end_time(raw: str, tz: ZoneInfo) -> datetime | None:
    """``10/08/2026 03:00 PM`` in the browser's zone — month first, as Ariba shows it."""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y %I:%M %p").replace(tzinfo=tz)
    except ValueError:
        return None


async def _pause(low: float = 0.8, high: float = 2.0) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def _events_frame(page, *, wait_seconds: float = 60):
    """The Events frame once it is there, or ``None`` if a sign-in page came instead."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            if _EVENTS_FRAME in frame.url and await frame.locator(_OPEN_GROUP).count():
                return frame
        if await page.locator(_SIGN_IN_FORM).count():
            return None
        await asyncio.sleep(1)
    raise PortalError("the Events list did not appear within a minute")


async def _sign_in(page, username: str, password: str) -> None:
    await page.goto(SIGN_IN_URL, wait_until="load", timeout=90_000)
    await page.locator("#userid").wait_for(timeout=60_000)
    await _pause()
    await page.locator("#userid").press_sequentially(username, delay=70)
    await _pause()
    await page.locator("a.w-login-page-form-btn").first.click()
    field = page.locator("input[type=password]").first
    try:
        await field.wait_for(timeout=30_000)
    except Exception as exc:
        raise SignInRefusedError("Ariba did not ask for a password after the username") from exc
    await _pause()
    await field.press_sequentially(password, delay=70)
    await _pause(0.5, 1.2)
    await page.keyboard.press("Enter")
    # Where a good sign-in lands. Anything else — the sign-in page again, a
    # security check — is a refusal, and is not retried.
    try:
        await page.wait_for_url(re.compile(r"dashboard|Supplier\.aw|Sourcing"), timeout=60_000)
    except Exception as exc:
        raise SignInRefusedError(f"sign-in did not reach the portal (at {page.url[:80]})") from exc
    if await page.locator("input[type=password]").count():
        raise SignInRefusedError("Ariba refused the password")


async def read_open_events(
    *,
    username: str,
    password: str,
    session_state: dict[str, Any] | None,
    timezone: str,
) -> Visit:
    """One visit: the rows under Status: Open, and the session to keep.

    Run on a thread with an event loop of its own. On Windows the app's loop is
    the selector kind psycopg needs (see app/__init__.py), which cannot start
    the browser process; and a minute of browser work has no business on the
    loop that answers requests anyway.
    """

    def run() -> Visit:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _visit(username, password, session_state, timezone)
            )
        finally:
            loop.close()

    return await asyncio.to_thread(run)


async def _visit(
    username: str, password: str, session_state: dict[str, Any] | None, timezone: str
) -> Visit:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - a deployment without it
        raise PortalError("Playwright is not installed on this server") from exc

    tz = ZoneInfo(timezone)
    signed_in = False
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1000},
                locale="en-US",
                timezone_id=timezone,
                storage_state=session_state,
            )
            await context.route(
                re.compile("|".join(re.escape(h) for h in _SKIP_HOSTS)),
                lambda route: route.abort(),
            )
            page = await context.new_page()
            await page.goto(SOURCING_URL, wait_until="load", timeout=90_000)
            frame = await _events_frame(page)
            if frame is None:
                await _pause()
                await _sign_in(page, username, password)
                signed_in = True
                await _pause()
                await page.goto(SOURCING_URL, wait_until="load", timeout=90_000)
                frame = await _events_frame(page)
                if frame is None:
                    raise SignInRefusedError("signed in, but Ariba asked to sign in again")

            header = frame.locator(_OPEN_GROUP).first
            # The header says how many there are — "Status: Open (18)" — and
            # the read is checked against it, because a short read taken as
            # the truth would mark every tender it missed as closed.
            counted = re.search(r"\((\d+)\)", await header.inner_text())
            expected = int(counted.group(1)) if counted else None
            rows = await header.evaluate(_READ_OPEN_ROWS)
            if not rows and expected != 0:
                # Collapsed — which is how a fresh session shows it. One click.
                await _pause()
                await header.locator("a[bh=GAT]").click()
                for _ in range(30):
                    await asyncio.sleep(1)
                    rows = await frame.locator(_OPEN_GROUP).first.evaluate(_READ_OPEN_ROWS)
                    if rows:
                        break
            if expected is not None and len(rows) != expected:
                raise PortalError(f"read {len(rows)} open events, the list says {expected}")

            events = [
                OpenEvent(
                    doc_id=r["doc_id"].strip(),
                    title=r["title"].strip(),
                    end_time=_end_time(r["end_time"], tz),
                    participated={"yes": True, "no": False}.get(
                        (r.get("participated") or "").strip().lower()
                    ),
                )
                for r in rows
            ]
            state = await context.storage_state()
            return Visit(events=events, session_state=state, signed_in=signed_in)
        finally:
            await browser.close()
