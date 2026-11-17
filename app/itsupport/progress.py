"""A message that shows a search working, as a progress bar.

Graph has no typing dots for a person's account, so a long job (a distributor
search, 15–30 s) sends one message and edits it as it goes (the user's choice
of style, 2026-11-11):

    **Finding suppliers** · Fortinet · the UAE

    ▰▰▰▰▱▱▱▱▱▱  Searching the web & directories…

    ✅ Supplier library · nothing on record
    12s

The bar fills by steps: a finished step counts whole, a running one creeps
towards most of its share as time passes, so the bar moves while a search
runs. Each finished step stays listed with what it found. It is edited every
TICK_SECONDS; Teams marks the message "Edited", the price of it.

The result itself goes as a new message (a new message notifies; an edit does
not), and this one is left full, saying how long it took. An edit Graph
refuses only stops the bar: the job carries on and its result is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from dataclasses import dataclass
from typing import Final, Protocol

logger = logging.getLogger("hamdaz.itsupport")

#: How often the bar moves. Each move is one Graph edit.
TICK_SECONDS: Final = 1.5
#: Cells in the bar.
CELLS: Final = 10
#: A running step creeps towards this share of its part of the bar, over
#: STEP_SECONDS; it never looks finished before it is.
CREEP: Final = 0.85
STEP_SECONDS: Final = 18.0


class _Graph(Protocol):
    async def send(self, chat_id: str, html_body: str) -> dict: ...
    async def edit(self, chat_id: str, message_id: str, html_body: str) -> None: ...


@dataclass(slots=True)
class Step:
    #: Short, for the finished list: "Supplier library".
    name: str
    #: What it is doing, for the line under the bar: "Searching the web".
    doing: str
    started: float
    result: str | None = None


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def bar(filled: float) -> str:
    cells = max(0, min(CELLS, round(filled * CELLS)))
    return "▰" * cells + "▱" * (CELLS - cells)


class Progress:
    def __init__(
        self, graph: _Graph, chat_id: str, title: str, total_steps: int = 3,
        heading: str = "Finding suppliers", below: str = "List below 👇",
    ) -> None:
        self._heading = heading
        self._below = below
        self._graph = graph
        self._chat_id = chat_id
        self._title = title
        self._total = max(1, total_steps)
        self._steps: list[Step] = []
        self._ticker: asyncio.Task[None] | None = None
        self._started = time.monotonic()
        self.message_id: str | None = None

    # ── what it shows ─────────────────────────────────────────────────

    def filled(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        share = 0.0
        for step in self._steps:
            if step.result is not None:
                share += 1
            else:
                share += CREEP * min(1.0, (now - step.started) / STEP_SECONDS)
        return min(1.0, share / max(self._total, len(self._steps)))

    def _doing(self) -> str:
        running = [s.doing for s in self._steps if s.result is None]
        if not running:
            return "Wrapping up…"
        if len(running) == 1:
            return f"{running[0]}…"
        # "Searching the web" + "Searching directories" → "Searching the web & directories"
        first, *rest = running
        verb = first.split(" ", 1)[0]
        tails = [r.split(" ", 1)[1] if r.split(" ", 1)[0] == verb and " " in r else r
                 for r in rest]
        return f"{first} & {' & '.join(tails)}…"

    def render(self, *, done_in: float | None = None) -> str:
        lines = [f"<b>{_e(self._heading)}</b> · {_e(self._title)}", ""]
        if done_in is None:
            lines.append(f"{bar(self.filled())}&nbsp;&nbsp;{_e(self._doing())}")
        else:
            lines.append(f"{bar(1.0)}&nbsp;&nbsp;Done in {done_in:.0f}s")
        lines.append("")
        lines += [f"✅ {_e(s.name)} · {_e(s.result)}" for s in self._steps if s.result is not None]
        if done_in is None:
            lines.append(f"<i>{time.monotonic() - self._started:.0f}s</i>")
        else:
            lines += ["", _e(self._below)]
        return "<br>".join(lines)

    # ── steps ─────────────────────────────────────────────────────────

    async def begin(self, name: str, doing: str) -> str | None:
        """Send the message with its first step running, and start the bar."""
        self.start(name, doing)
        sent = await self._graph.send(self._chat_id, self.render())
        self.message_id = str(sent.get("id") or "") or None
        if self.message_id:
            self._ticker = asyncio.create_task(self._tick(), name="itsupport-progress")
        return self.message_id

    def start(self, name: str, doing: str) -> int:
        """Another step running (more than one may run at once); its index."""
        self._steps.append(Step(name, doing, time.monotonic()))
        return len(self._steps) - 1

    def done(self, result: str, index: int = -1) -> None:
        """A step finished, with what it came to (the latest one by default)."""
        if self._steps:
            self._steps[index].result = result

    async def finish(self) -> None:
        for step in self._steps:
            if step.result is None:
                step.result = "stopped"
        await self._stop_ticker()
        await self._edit(self.render(done_in=time.monotonic() - self._started))

    # ── the edits ─────────────────────────────────────────────────────

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if not await self._edit(self.render()):
                return

    async def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None

    async def _edit(self, body: str) -> bool:
        if not self.message_id:
            return False
        try:
            await self._graph.edit(self._chat_id, self.message_id, body)
            return True
        except Exception as exc:  # noqa: BLE001 - the bar is not worth the job
            logger.info("it support: progress not updated: %s", exc)
            return False
