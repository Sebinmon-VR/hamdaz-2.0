"""The loop: each allowed person's chat with IT Support, checked and answered.

On start: who the account is, who may be answered, and each one's chat with
the account (one Graph call finds it). What is in those chats already is
marked seen, so starting never answers the backlog. Then, every
IT_SUPPORT_POLL_SECONDS: the chat's newest messages; anything new from the
person is answered once, in one reply, with the chat as its memory. A message
the account itself sent (the agent, or a person typing as IT Support) is
never answered.

With the brain on (IT_SUPPORT_BRAIN_ENABLED and a provider configured), the
chat works like a colleague's, **rules first, models last**:

* a few messages in a row are **gathered** and answered as one
  (IT_SUPPORT_GATHER_SECONDS after the last);
* a **file sent alone** waits IT_SUPPORT_FILE_WAIT_SECONDS for them to type
  what it's for; with still no words, it says what the file looks like and
  which task it names and asks what to do (``files.what_for``, no model).
  A distributor ask the rules can read starts the finder, no model either.
  Anything else goes to the brain: free models first, Claude last
  (``brain``), with tools working the ERP as the person.
  The files stay with the chat (``FILES_KEPT_SECONDS``), so the next
  message, and any later one, can use them;
* a change is asked about first and kept as a plan; their next message runs
  it on a yes, or changes or drops it;
* one turn at a time per chat: messages sent while it works are answered
  next;
* work that takes a while gets a short "on it" line first.

Without the brain, the older paths answer: a distributor ask starts the
search (our quotes, then the web; see ``distributors``), files go through the
goal step, anything else the plain chat (``talk``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.model import AgentModel, Spent
from app.comparison.extraction import QuoteExtractor
from app.core.config import Settings
from app.core.llm import TextModel
from app.enquiries.claude import ClaudeEnquiry
from app.itsupport import brief as brief_mod
from app.itsupport import distributors, docs, due, memory, smalltalk, talk, work
from app.itsupport import files as files_mod
from app.itsupport import goal as goal_mod
from app.itsupport import settings as settings_mod
from app.itsupport.brain import Brain, Person, Plan, Turn
from app.itsupport.erp import ERP, Access, access_for
from app.itsupport.graph import FilesNotAllowedError, GraphError, ITSupportGraph, NotConnectedError
from app.itsupport.progress import Progress
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.itsupport")

DUBAI = ZoneInfo("Asia/Dubai")
#: After a failed check, wait this long before the next.
BACKOFF_SECONDS = 10.0
#: How often the background jobs (tasks, notes, check-ins) look for work.
BACKGROUND_SECONDS = 60.0
#: How long files waiting on an answer ("is this the RFQ or a quote?") and an
#: offer waiting on a yes ("want me to find suppliers?") are kept.
WAITING_SECONDS = 30 * 60
#: How long files sent in the chat stay at hand for later messages.
FILES_KEPT_SECONDS = 3 * 60 * 60
#: However long the messages keep coming, a turn starts after this.
GATHER_AT_MOST = 30.0
#: How long a person's modules are trusted before being read again.
ACCESS_SECONDS = 5 * 60
#: The typing dots while an answer is thought out; the message then becomes
#: the reply (Dots). Still text: moving them by editing went no faster than
#: Graph takes an edit (~0.7 s a step), too slow to look like typing, and a
#: picture made the notification say "sent an image".
DOTS = "৹৹৹"
#: A message this short, with no files or pending question, is small talk:
#: answered at low effort (3 s instead of 10 s for a "hi").
SMALL_TALK_WORDS = 6
#: A silence this long ends a conversation: the model sees only the one
#: going on now (``this_conversation``).
CONVERSATION_GAP = timedelta(hours=2)
#: A yes to the offer just made.
_YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|please|pls|go\s+ahead|do\s+it|go\s+for\s+it)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Chat:
    chat_id: str
    person_id: str
    email: str
    first_name: str
    seen: set[str] = field(default_factory=set)
    #: When the chat last moved (monotonic), and whether the notes kept about
    #: the person are behind it; they are brought up to date once it is quiet.
    active_at: float = 0.0
    notes_behind: bool = False
    #: Files sent, waiting on the answer to the question asked about them.
    waiting_files: tuple[list[files_mod.Opened], float] | None = None
    #: Suppliers offered to be found for the items just read, waiting on a yes.
    offer: tuple[distributors.Ask, float] | None = None
    #: The brain's chat: messages not answered yet, when the first and the
    #: last of them came (monotonic), and whether a turn is running.
    pending: list[dict[str, Any]] = field(default_factory=list)
    first_in: float = 0.0
    last_in: float = 0.0
    busy: bool = False
    #: Files sent in the chat, by name, with when they came.
    files: dict[str, tuple[files_mod.Opened, float]] = field(default_factory=dict)
    #: Changes the brain asked about, waiting on their yes.
    plan: tuple[Plan, float] | None = None
    #: Until when an agreed job that failed on a step may be finished
    #: without asking again (monotonic; 0 = none).
    agreed_until: float = 0.0
    #: Their teams in the ERP, (handle, name), read with their modules.
    teams: list[tuple[str, str]] = field(default_factory=list)
    #: Their name in Teams, for what to call them when the page names none.
    display_name: str | None = None


def _words(message: dict[str, Any]) -> str:
    """What they typed in a message, without the stand-in for a file."""
    text = talk.plain(message)
    return "" if text == "(sent a file or a picture)" else text.strip()


def _small_talk(words: str, batch: list[dict[str, Any]], files: dict[str, Any],
                plan_note: str) -> bool:
    """A greeting, a thanks, an "ok": a few words, nothing sent with it, no
    question of ours waiting on the answer."""
    return (len(words.split()) <= SMALL_TALK_WORDS and not plan_note
            and not any(files_mod.in_message(m) for m in batch)
            and not re.search(r"\d{3,}", words))


def this_conversation(newest_first: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages of the conversation going on now: back to the last
    silence of ``CONVERSATION_GAP``. What was said before it (Friday's search)
    is not brought up again; the notes carry what matters from it."""
    out: list[dict[str, Any]] = []
    later: datetime | None = None
    for message in newest_first:
        when = _when(message.get("createdDateTime"))
        if later is not None and when is not None and later - when > CONVERSATION_GAP:
            break
        out.append(message)
        later = when or later
    return out


class Dots:
    """Typing dots while an answer is thought out, edited into the first
    thing said after them, so the reply appears where the dots were. Graph
    can't show Teams' own typing dots for a user account; this is the
    nearest. Once something is said, no more dots this turn."""

    def __init__(self, graph: ITSupportGraph, chat: Chat) -> None:
        self._graph, self._chat = graph, chat
        self.message_id: str | None = None
        self.done = False
        self._lock = asyncio.Lock()

    async def show(self) -> None:
        async with self._lock:
            if self.done or self.message_id is not None:
                return
            sent = await self._graph.send(self._chat.chat_id, DOTS)
            self.message_id = str(sent.get("id"))
            self._chat.seen.add(self.message_id)

    async def say(self, html_body: str) -> bool:
        """Put this where the dots are. False when there are none (or the
        edit failed and they were taken away): send it as a new message."""
        async with self._lock:
            self.done = True
            message_id, self.message_id = self.message_id, None
            if message_id is None:
                return False
            try:
                await self._graph.edit(self._chat.chat_id, message_id, html_body)
                return True
            except (GraphError, httpx.HTTPError) as exc:
                logger.warning("it support: the dots could not become the reply: %s", exc)
                with contextlib.suppress(Exception):
                    await self._graph.delete_own(self._chat.chat_id, message_id)
                return False

    async def close(self) -> None:
        """The turn ended without saying anything after them: take them away."""
        async with self._lock:
            self.done = True
            message_id, self.message_id = self.message_id, None
        if message_id is not None:
            with contextlib.suppress(Exception):
                await self._graph.delete_own(self._chat.chat_id, message_id)


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class ITSupportWorker:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        model: TextModel,
        factory: async_sessionmaker[AsyncSession] | None = None,
        finder: AgentModel | None = None,
        sharepoint: SharePointProposals | None = None,
        *,
        app: FastAPI | None = None,
    ) -> None:
        # Its own copy: the settings page's row is written over it every
        # settings_mod.REFRESH_SECONDS, and the rest of the app keeps its own.
        settings = settings.model_copy()
        self._settings = settings
        self._graph = ITSupportGraph(settings, http, factory)
        self._model = model
        self._factory = factory
        self._finder = finder or AgentModel(settings)
        self._sharepoint = sharepoint
        self._task: asyncio.Task[None] | None = None
        self._background: asyncio.Task[None] | None = None
        #: Each person's tasks on the Proposals list, by email, kept fresh in
        #: the background so a reply never waits on SharePoint.
        self._work: dict[str, work.Work] = {}
        self._memory = memory.Memory(settings.it_support_memory_file)
        self._asked = due.Asked(settings.it_support_due_asked_file)
        self._due_checked = 0.0
        #: The ERP's own readers: tenders through the enquiry analysis's
        #: Claude reader, supplier quotes through the comparison's.
        self._claude = ClaudeEnquiry(settings)
        self._extractor = QuoteExtractor(settings, model)
        #: Distributor searches still running, kept so they are not collected.
        self._searches: set[asyncio.Task[None]] = set()
        self.me_id: str | None = None
        #: The brain, when it can run: Claude with the ERP as the person.
        self._erp = ERP(app, settings) if app is not None else None
        self._brain = Brain(settings, self._erp, self._finder, self._claude, self._extractor,
                            http=http) if self._erp is not None else None
        #: Each person's ERP account and modules, by email, with when read.
        self._people: dict[str, tuple[uuid.UUID, Access, float]] = {}
        #: The chats it answers; changed in place when the page changes the
        #: people, so the loops holding it see the change.
        self._chats: list[Chat] = []
        self._me_email: str | None = None
        #: Whether it answers now: the page's switch is on and no other
        #: machine is answering. Off, the loop still runs, to see it come on.
        self.answering = False
        self._refreshed = 0.0
        #: The last problem, for the heartbeat; cleared once it is written.
        self._error: str | None = None

    @property
    def thinking(self) -> bool:
        """Whether the brain answers (else the older chat paths)."""
        return self._brain is not None and self._brain.configured and self._factory is not None

    def start(self) -> None:
        if not self._settings.it_support_chat_enabled:
            return
        self._task = asyncio.create_task(self._run(), name="itsupport-chat")

    async def stop(self) -> None:
        for task in [self._task, self._background, *self._searches]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._task is not None and self._factory is not None and self.answering:
            await settings_mod.release(self._factory)

    async def _run(self) -> None:
        """Signed in (waiting for a Connect if need be), then the loop. A
        sign-in that stops working (disconnected, refused) goes back to
        waiting; it never ends the worker."""
        while True:
            await self._sign_in()
            try:
                await self._loop()
            except NotConnectedError as exc:
                logger.error("it support: %s", exc)
                self.answering = False
                self._graph.forget()
                if self._factory is not None:
                    await settings_mod.note_problem(self._factory, str(exc))

    async def _loop(self) -> None:
        await self._refresh()
        chats = self._chats
        if self._background is None or self._background.done():
            self._background = asyncio.create_task(
                self._background_jobs(chats), name="itsupport-background"
            )
        while True:
            if time.monotonic() - self._refreshed >= settings_mod.REFRESH_SECONDS:
                try:
                    await self._refresh()
                except NotConnectedError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the last settings stand
                    logger.exception("it support: reading the settings failed")
                    self._error = f"reading the settings: {exc}"
            for chat in list(chats) if self.answering else []:
                try:
                    await self.check(chat)
                except NotConnectedError:
                    raise
                except (GraphError, httpx.HTTPError) as exc:
                    logger.warning("it support: checking %s failed: %s", chat.email, exc)
                    self._error = f"checking {chat.email}'s chat: {exc}"
                    await asyncio.sleep(BACKOFF_SECONDS)
                except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad message
                    logger.exception("it support: a reply failed")
                    self._error = f"a reply to {chat.email} failed: {exc}"
                    await asyncio.sleep(BACKOFF_SECONDS)
            await asyncio.sleep(self._settings.it_support_poll_seconds)

    async def _sign_in(self) -> None:
        """Wait until the sign-in works and is the IT Support account: a
        Connect on the settings page is picked up within REFRESH_SECONDS."""
        s = self._settings
        said = None
        while True:
            problem = None
            try:
                me = await self._graph.me()
                email = (me.get("mail") or me.get("userPrincipalName") or "").lower()
                if email == s.it_support_email.lower():
                    self._me_email, self.me_id = email, me["id"]
                    logger.info("it support: signed in as %s (sign-in from the %s)", email,
                                "settings page" if self._graph.source == "db" else "local file")
                    return
                problem = f"signed in as {email}, not {s.it_support_email}; connect again"
            except NotConnectedError as exc:
                problem = str(exc)
            except (GraphError, httpx.HTTPError) as exc:
                logger.warning("it support: could not start yet: %s", exc)
            self._graph.forget()
            if problem is not None and problem != said:
                logger.error("it support: %s", problem)
                if self._factory is not None:
                    await settings_mod.note_problem(self._factory, problem)
                said = problem
            await asyncio.sleep(settings_mod.REFRESH_SECONDS)

    async def _refresh(self) -> None:
        """The settings page's row over this copy, the lease (only the
        process holding it answers), and the chats made to match the people.
        Without the table yet (migration not run), the environment's values
        stand."""
        self._refreshed = time.monotonic()
        row = None
        if self._factory is not None:
            row = await settings_mod.load(self._factory, self._settings)
        was = self.answering
        if row is None:
            self.answering = True  # no settings table yet: the environment decides
        else:
            if row.sign_in is None and self._graph.source == "db":
                # Disconnected on the page: stop using the sign-in in hand.
                raise NotConnectedError("disconnected on the settings page")
            settings_mod.apply(row, self._settings)
            if self._erp is not None:
                self._erp.reload_modes()
            error, self._error = self._error, None
            held = await settings_mod.claim(self._factory, signed_in_as=self._me_email,
                                            error=error)
            if not held:
                if was:
                    logger.warning("it support: another process (%s) is answering; this one "
                                   "stays quiet so nobody gets two replies", row.running_on)
                self.answering = False
                return
            self.answering = row.enabled
            if was != self.answering:
                logger.info("it support: %s", "answering" if self.answering else
                            "switched off on the settings page")
        await self._sync_chats(mark_seen=self.answering and not was)

    async def _sync_chats(self, *, mark_seen: bool) -> None:
        """A chat for everyone it should answer, none for anyone else; with
        ``mark_seen`` (switched back on), what was said meanwhile is left."""
        s = self._settings
        wanted = [e.strip().lower() for e in s.it_support_allowed_emails.split(",")
                  if e.strip() and e.strip() != "*"]
        for chat in list(self._chats):
            if chat.email not in wanted:
                self._chats.remove(chat)
                logger.info("it support: no longer answering %s", chat.email)
        by_email = {c.email: c for c in self._chats}
        for email in wanted:
            chat = by_email.get(email)
            if chat is not None:
                # A name changed on the page reaches the next reply.
                chat.first_name = talk.first_name(s, email, chat.display_name)
                if mark_seen:
                    chat.seen.update(m["id"] for m in await self._messages(chat, top=50))
                continue
            try:
                chat = await self._open_chat(email)
            except (GraphError, httpx.HTTPError) as exc:
                logger.warning("it support: can't open a chat with %s: %s", email, exc)
                self._error = f"can't open a chat with {email}: {exc}"
                continue
            self._chats.append(chat)
            logger.info("it support: answering %s as %s, every %ss", email, self._me_email,
                        s.it_support_poll_seconds)

    async def _open_chat(self, email: str) -> Chat:
        """Their chat with the account, what is in it already marked seen."""
        assert self.me_id is not None
        s = self._settings
        person = await self._graph.user(email)
        chat = Chat(
            await self._graph.chat_with(self.me_id, person["id"]), person["id"],
            email, talk.first_name(s, email, person.get("displayName")),
        )
        chat.display_name = person.get("displayName")
        history = await self._messages(chat, top=50)
        chat.seen = {m["id"] for m in history}
        # A chat that went on while nothing was kept (or before memory
        # existed) is folded into the notes first.
        chat.notes_behind = bool(history) and (
            history[0]["id"] != self._memory.last_seen(chat.email)
        )
        return chat

    async def check(self, chat: Chat) -> bool:
        """Answer what is new in one chat; whether anything was."""
        s = self._settings
        newest_first = await self._messages(chat, top=s.it_support_history_messages)
        new = [m for m in newest_first if m["id"] not in chat.seen]
        chat.seen.update(m["id"] for m in new)
        asked = [
            m for m in new
            if m.get("messageType") == "message" and not m.get("deletedDateTime")
            and ((m.get("from") or {}).get("user") or {}).get("id") == chat.person_id
        ]
        if self.thinking:
            return self._gather(chat, asked)
        if not asked:
            return False
        started = time.monotonic()
        chat.active_at, chat.notes_behind = started, True
        assert self.me_id is not None
        if self._documents_turn(chat, asked, newest_first):
            return True
        ask = distributors.read_ask(talk.plain(asked[0]))
        if ask is not None and ask.items:
            what = ask.label.removeprefix("these ")
            if len(what) > 40:
                what = what[:38].rstrip() + "…"
            progress = Progress(
                self._graph, chat.chat_id, f"{what} · {ask.place.removeprefix('the ')}"
            )
            sent_id = await progress.begin("Supplier library", "Checking our supplier library")
            chat.seen.add(str(sent_id))
            search = asyncio.create_task(self._find(chat, ask, progress), name="itsupport-find")
            self._searches.add(search)
            search.add_done_callback(self._searches.discard)
            logger.info("it support: %s asked for %s distributors in %s", chat.email,
                        ask.label, ask.place)
            return True
        spoken = await talk.reply(
            self._model, first_name=chat.first_name, history=list(reversed(newest_first)),
            me_id=self.me_id, now=datetime.now(DUBAI), order=s.it_support_chat_providers,
            notes=self._memory.notes(chat.email) if s.it_support_memory_enabled else "",
            context=work.describe(self._work.get(chat.email), datetime.now(DUBAI))
            if self._sharepoint is not None else "",
        )
        text = spoken.text.strip()[:4000]
        asked_at = _when(asked[0].get("createdDateTime"))
        body = talk.to_html(text)
        if s.it_support_show_timing:
            body += (
                f"<br><br><i>(model {spoken.total_ms / 1000:.1f}s · "
                f"{spoken.who or 'no model'})</i>"
            )
        sent = await self._graph.send(chat.chat_id, body)
        chat.seen.add(str(sent.get("id")))
        sent_at = _when(sent.get("createdDateTime"))
        waited = (sent_at - asked_at).total_seconds() if sent_at and asked_at else None
        logger.info(
            "it support: answered %s - %.1fs from their message to the reply in Teams "
            "(model first words %s ms, done %d ms, this pass %.1fs, %s)",
            chat.email, waited if waited is not None else -1, spoken.first_ms, spoken.total_ms,
            time.monotonic() - started, spoken.who or "no model",
        )
        return True

    # ── the brain: gathered messages, one turn at a time ──────────────

    def _gather(self, chat: Chat, asked: list[dict[str, Any]]) -> bool:
        """Keep new messages until they stop coming, then start one turn.
        Called on every poll; whether anything new came."""
        now = time.monotonic()
        if asked:
            # Graph lists newest first; the turn reads them oldest first.
            chat.pending.extend(reversed(asked))
            chat.last_in = now
            chat.first_in = chat.first_in or now
            chat.active_at, chat.notes_behind = now, True
        if not chat.pending or chat.busy:
            return bool(asked)
        s = self._settings
        files_only = all(files_mod.in_message(m) and not _words(m) for m in chat.pending)
        wait = s.it_support_file_wait_seconds if files_only else s.it_support_gather_seconds
        if now - chat.last_in < wait and now - chat.first_in < GATHER_AT_MOST:
            return bool(asked)
        batch, chat.pending, chat.first_in = chat.pending, [], 0.0
        chat.busy = True
        self._spawn(self._think(chat, batch), "itsupport-think")
        return True

    async def _person(self, chat: Chat) -> tuple[uuid.UUID, Access] | None:
        """Their ERP account and the modules it opens, read again every few
        minutes so a change on the access screen reaches the chat."""
        known = self._people.get(chat.email)
        if known is not None and time.monotonic() - known[2] < ACCESS_SECONDS:
            return known[0], known[1]
        assert self._factory is not None
        async with self._factory() as session:
            user_id = await session.scalar(
                select(User.id).where(func.lower(User.email) == chat.email.lower()))
            if user_id is None:
                return None
            access = await access_for(session, user_id)
            # Their teams: a costing raised without a task is filed under one.
            rows = await session.execute(
                select(Team.slug, Team.name).join(TeamMembership, TeamMembership.team_id == Team.id)
                .where(TeamMembership.user_id == user_id).distinct().order_by(Team.name))
            chat.teams = [(slug, name) for slug, name in rows.all()]
        self._people[chat.email] = (user_id, access, time.monotonic())
        logger.info("it support: %s reaches %s%s", chat.email,
                    ", ".join(sorted(access.modules)) or "no modules",
                    " (super admin)" if access.is_admin else "")
        return user_id, access

    async def _open_sent(self, chat: Chat, batch: list[dict[str, Any]],
                         tasks: list) -> list[str]:
        """Files in these messages fetched, opened and kept with the chat;
        what went wrong, for the context."""
        problems: list[str] = []
        now = time.monotonic()
        chat.files = {n: (o, t) for n, (o, t) in chat.files.items()
                      if now - t < FILES_KEPT_SECONDS}
        for f in (f for m in batch for f in files_mod.in_message(m)):
            try:
                opened = await files_mod.fetch_and_open(self._graph, f)
            except FilesNotAllowedError as exc:
                logger.error("it support: chat files need Files.Read.All; run "
                             "python -m app.itsupport.connect again (%s)", exc)
                problems.append(f"{f.name}: you can't open files sent in chat yet (a "
                                "permission is still pending); ask them to paste the text")
                continue
            files_mod.first_look(opened, tasks)
            name, n = opened.name, 2
            while name in chat.files:
                stem, dot, ext = opened.name.rpartition(".")
                name = f"{stem} ({n}).{ext}" if dot else f"{opened.name} ({n})"
                n += 1
            opened.name = name
            chat.files[name] = (opened, now)
        return problems

    async def _think(self, chat: Chat, batch: list[dict[str, Any]]) -> None:
        """One turn of the brain for the messages gathered."""
        assert self._brain is not None and self._erp is not None and self.me_id is not None
        started = time.monotonic()
        dots: Dots | None = None
        try:
            found = await self._person(chat)
            if found is None:
                await self._say(chat, talk.to_html(
                    "Hmm, I can't find your account in the ERP, so I can't look anything up "
                    "for you yet. Could you ask an admin to check it?"))
                logger.warning("it support: no ERP user for %s", chat.email)
                return
            user_id, access = found
            known = self._work.get(chat.email)
            tasks = known.tasks if known else []
            before = set(chat.files)
            problems = await self._open_sent(chat, batch, tasks)
            words = " ".join(w for w in (_words(m) for m in batch) if w).strip()

            # System first: what rules settle never reaches a model.
            if not words and chat.plan is None and (new := [
                    o for n, (o, _) in chat.files.items() if n not in before]):
                await self._say(chat, talk.to_html(files_mod.what_for(new, chat.first_name)))
                logger.info("it support: %s sent %d file(s) alone; asked what for (rules)",
                            chat.email, len(new))
                return
            if not words and problems:
                await self._say(chat, talk.to_html(
                    "I can't open files sent in chat yet, I'm still waiting on the permission "
                    "for that. Could you paste the text in for now?"))
                return
            ask = distributors.read_ask(words) if words and len(batch) == 1 else None
            if ask is not None and ask.items and chat.plan is None:
                logger.info("it support: %s asked for %s distributors in %s (rules)",
                            chat.email, ask.label, ask.place)
                self._spawn(self._find_for(chat, ask), "itsupport-find")
                return
            # Small talk ("hi", "test", "thanks", "ok", "bye") gets a colleague's
            # one line, no model, when nothing is waiting on their answer (an
            # "ok" to a plan is a yes, handled below).
            waiting = (chat.plan is not None or time.monotonic() < chat.agreed_until
                       or chat.waiting_files is not None or chat.offer is not None)
            if words and not waiting and set(chat.files) == before:
                recent = [talk.plain(m) for m in await self._messages(chat, top=12)
                          if ((m.get("from") or {}).get("user") or {}).get("id") == self.me_id]
                if (line := smalltalk.reply(words, chat.first_name, recent)) is not None:
                    await self._say(chat, talk.to_html(line))
                    logger.info("it support: small talk from %s answered by rules", chat.email)
                    return
            person = Person(user_id, chat.email, chat.first_name, tasks, access)
            files = {n: o for n, (o, _) in chat.files.items()}

            # A change asked about last turn: their answer decides it.
            plan_note = ""
            # A job they agreed to that isn't finished (a step was rejected):
            # this turn may finish it without asking again.
            agreed = time.monotonic() < chat.agreed_until
            chat.agreed_until = 0.0
            if agreed:
                plan_note = ("They already agreed to the job above and it isn't finished: a "
                             "step was rejected. erp_change now does it at once. Fix what was "
                             "wrong (their message may give the missing detail) and finish "
                             "it, then send the result. If their message is a no or stop, "
                             "don't.")
            if chat.plan is not None:
                plan, made = chat.plan
                chat.plan = None
                if time.monotonic() - made < WAITING_SECONDS and _YES.match(words):
                    ran = await self._brain.run_plan(person, plan, files)
                    agreed = agreed or plan.failed
                    plan_note = ("They said yes to what you asked, and it ran:\n" + ran +
                                 "\nTell them what was done and the result from these replies "
                                 "(for a costing: suppliers, totals, cheapest, what couldn't be "
                                 "read), with the link. Don't run the done steps again.")
                    if plan.failed:
                        plan_note += (" A step failed: they already agreed, so erp_change "
                                      "now does it at once. Fix what was wrong (look it up) "
                                      "and finish the job now, without asking, then send the "
                                      "result. Only if the fix needs their choice, ask that.")
                    logger.info("it support: %s said yes; ran %d step(s)%s", chat.email,
                                len(plan.steps), " (a step failed)" if plan.failed else "")
                else:
                    plan_note = (f"You had asked them to confirm: {plan.said()}. Nothing was "
                                 "done. Their message below answers that: on a no, drop it; "
                                 "if they changed it, make the plan again as they want it.")

            newest_first = this_conversation(await self._messages(
                chat, top=self._settings.it_support_history_messages))
            history = talk.turns(list(reversed(newest_first)), self.me_id)
            small = _small_talk(words, batch, files, plan_note)
            context = self._context(chat, person, known, files, batch, problems, plan_note,
                                    small_talk=small)

            dots = Dots(self._graph, chat)

            async def say(text: str) -> None:
                await self._say(chat, talk.to_html(text.strip()[:1000]), dots)

            async def find(ask: distributors.Ask) -> None:
                self._spawn(self._find_for(chat, ask), "itsupport-find")

            async def send_file(name: str, url: str, attachment_id: str) -> None:
                sent = await self._graph.send_file(
                    chat.chat_id, talk.to_html("Here's the costing sheet 👇"), name=name,
                    url=url, attachment_id=attachment_id)
                chat.seen.add(str(sent.get("id")))
                logger.info("it support: sent %s to %s", name, chat.email)

            turn = Turn(person, files, on_find=find, on_say=say, on_file=send_file,
                        agreed=agreed,
                        effort="low" if small else None)

            async def hold(dots: Dots) -> None:
                # Any answer still being thought out after a moment, as typing
                # dots would be; small talk only once it starts real work (a
                # lookup). Not cancelled: it ends by itself once anything is
                # said, so the dots can't be left half sent.
                while not dots.done:
                    if (turn.working or not small) and (
                            time.monotonic() - started
                            >= self._settings.it_support_dots_seconds):
                        with contextlib.suppress(Exception):
                            await dots.show()
                        return
                    await asyncio.sleep(0.25)

            self._spawn(hold(dots), "itsupport-dots")
            text = await self._brain.answer(turn, chat=history, context=context)
            if agreed and (not turn.done or turn.done[-1].endswith("failed")):
                # Still not finished: their next message may carry what's missing.
                chat.agreed_until = time.monotonic() + WAITING_SECONDS
            if turn.done:
                logger.info("it support: done at once for %s: %s", chat.email,
                            "; ".join(turn.done))
            if turn.plan.steps:
                chat.plan = (turn.plan, time.monotonic())
                logger.info("it support: asked %s to confirm: %s", chat.email, turn.plan.said())
            body = talk.to_html(text.strip()[:4000])
            if self._settings.it_support_show_timing:
                body += (f"<br><br><i>({time.monotonic() - started:.1f}s · "
                         f"{turn.who or 'no model'})</i>")
            await self._say(chat, body, dots)
            logger.info("it support: brain answered %s in %.1fs (%d message(s), %d file(s) "
                        "at hand)", chat.email, time.monotonic() - started, len(batch),
                        len(files))
        except NotConnectedError as exc:
            logger.error("it support: %s", exc)
        except Exception as exc:  # noqa: BLE001 - say so rather than go quiet
            logger.exception("it support: the brain failed for %s", chat.email)
            known_person = self._people.get(chat.email)
            # A super admin is told what broke, to fix it; others just the apology.
            detail = f" ({type(exc).__name__}: {str(exc)[:200]})" if (
                known_person and known_person[1].is_admin) else ""
            with contextlib.suppress(Exception):
                await self._say(chat, talk.to_html(
                    "Sorry, something went wrong on my side there. Can you send that again?"
                    + detail), dots)
        finally:
            if dots is not None:
                self._spawn(dots.close(), "itsupport-dots-close")
            chat.busy = False

    def _context(self, chat: Chat, person: Person, known: work.Work | None,
                 files: dict[str, files_mod.Opened], batch: list[dict[str, Any]],
                 problems: list[str], plan_note: str, *, small_talk: bool = False) -> str:
        """What the brain knows this turn, beyond the chat itself."""
        assert self._erp is not None
        now = datetime.now(DUBAI)
        new_files = {f.name for m in batch for f in files_mod.in_message(m)}
        lines = [
            f"Now: {work.both(now)}. You're chatting with {chat.first_name} ({chat.email}).",
            f"Their ERP modules (what you may do in each from the chat): "
            f"{self._erp.modules_said(person.access) or 'none'}"
            + (". They're a super admin." if person.access.is_admin else "."),
            "Their teams (handle: name): " + (
                "; ".join(f"{slug}: {name}" for slug, name in chat.teams) or "none"),
            work.describe(known, now) if self._sharepoint is not None else "",
        ]
        # Small talk gets no notes: shown them, the model reads old work out
        # on every "hi" (tried 2026-11-17, the prompt alone didn't stop it).
        if self._settings.it_support_memory_enabled and not small_talk and (
                notes := self._memory.notes(chat.email)):
            lines.append(f"What you remember about them:\n{notes}")
        if files:
            lines.append("Files in this chat (newest last; use their names in tools):\n" +
                         "\n".join(files_mod.describe(o) for o in files.values()))
        if new_files and not any(_words(m) for m in batch):
            lines.append("They just sent file(s) with no words. Say briefly what you see and "
                         "ask what they want done with it; don't start the work yet.")
        if problems:
            lines.append("Couldn't open: " + "; ".join(problems))
        if plan_note:
            lines.append(plan_note)
        return "\n\n".join(line for line in lines if line)

    async def _find(self, chat: Chat, ask: distributors.Ask, progress: Progress) -> None:
        """Our quotes, then the web, shown step by step; the list posted into
        the chat."""
        started = time.monotonic()
        ours: list[dict] = []
        library = "nothing on record"
        if self._factory is not None:
            try:
                async with self._factory() as session:
                    ours = await distributors.from_our_quotes(session, ask)
                if ours:
                    library = f"{len(ours)} supplier(s) quoted us before"
            except Exception:  # noqa: BLE001 - the web part can still answer
                logger.exception("it support: reading our supplier quotes failed")
                library = "couldn't be read just now"
        progress.done(library)
        web: list[dict] = []
        listed: list[dict] = []
        note: str | None = None
        spent, spent_dirs = Spent(), Spent()
        if not self._finder.configured:
            note = "I can't search the web right now, so this is only from our own quotes."
        else:
            known = [s["name"] for s in ours]
            sites = distributors.directories_for(ask.place)
            names = ", ".join(distributors.DIRECTORY_NAMES.get(d, d) for d in sites[:2])
            on_web = progress.start("Web", "Searching the web")
            in_dirs = progress.start(f"Directories ({names}…)", "Searching directories")

            async def step(search, index: int, spent_here: Spent) -> list[dict]:
                try:
                    found = await search(self._finder, ask, known, spent_here)
                except Exception as exc:  # noqa: BLE001 - the other step still answers
                    logger.warning("it support: a search failed: %s", exc)
                    progress.done("didn't come back", index)
                    return []
                progress.done(f"{len(found)} found" if found else "nothing found", index)
                return found

            web, listed = await asyncio.gather(
                step(distributors.from_web, on_web, spent),
                step(distributors.from_directories, in_dirs, spent_dirs),
            )
            if not web and not listed and not ours:
                note = ("The searches didn't turn anyone up this time. Try the brand's exact "
                        "name, or ask me again in a bit?")
            logger.info("it support: web searched for %s", spent.queries)
            logger.info("it support: directories searched for %s", spent_dirs.queries)
            spent.input_tokens += spent_dirs.input_tokens
            spent.output_tokens += spent_dirs.output_tokens
            spent.searches += spent_dirs.searches
        await progress.finish()
        listed = distributors.only_new(listed, web)
        took = time.monotonic() - started
        body = distributors.reply_html(ask, ours, web, note, listed)
        if self._settings.it_support_show_timing:
            body += (
                f"<br><br><i>({took:.0f}s · {spent.searches} web searches · about "
                f"${spent.cost(self._finder.model):.3f})</i>"
            )
        try:
            sent = await self._graph.send(chat.chat_id, body)
            chat.seen.add(str(sent.get("id")))
        except (GraphError, httpx.HTTPError) as exc:
            logger.warning("it support: the distributor list could not be sent: %s", exc)
            return
        logger.info(
            "it support: %s distributors for %s: %d from our quotes, %d from the web, %d from "
            "directories in %.0fs (%d searches, about $%.3f)", ask.label, chat.email, len(ours),
            len(web), len(listed), took,
            spent.searches, spent.cost(self._finder.model),
        )

    # ── in the background: tasks, notes, due check-ins ─────────────────

    async def _background_jobs(self, chats: list[Chat]) -> None:
        while True:
            for job in (self._refresh_work, self._update_notes, self._morning_briefs,
                        self._due_check_ins)\
                    if self.answering else ():
                try:
                    await job(list(chats))
                except NotConnectedError as exc:
                    logger.error("it support: %s", exc)
                    return
                except Exception:  # noqa: BLE001 - one job failing leaves the others
                    logger.exception("it support: %s failed", job.__name__)
            await asyncio.sleep(BACKGROUND_SECONDS)

    async def _refresh_work(self, chats: list[Chat]) -> None:
        """Each person's tasks, read again once older than
        IT_SUPPORT_WORK_REFRESH_MINUTES."""
        if self._sharepoint is None:
            return
        minutes = self._settings.it_support_work_refresh_minutes
        stale = datetime.now(UTC) - timedelta(minutes=minutes)
        for chat in chats:
            known = self._work.get(chat.email)
            if known is not None and known.read_at is not None and known.read_at > stale:
                continue
            try:
                self._work[chat.email] = await work.read(
                    self._sharepoint, self._factory, chat.email
                )
            except Exception as exc:  # noqa: BLE001 - the last read stands
                logger.warning("it support: reading %s's tasks failed: %s", chat.email, exc)

    async def _update_notes(self, chats: list[Chat]) -> None:
        """Fold a conversation that has gone quiet into the notes kept about
        the person."""
        s = self._settings
        if not s.it_support_memory_enabled or self.me_id is None:
            return
        quiet = s.it_support_memory_idle_minutes * 60
        for chat in chats:
            if not chat.notes_behind or time.monotonic() - chat.active_at < quiet:
                continue
            history = await self._messages(chat, top=s.it_support_history_messages)
            turns = talk.turns(list(reversed(history)), self.me_id)
            if not history or not turns:
                chat.notes_behind = False
                continue
            notes = await memory.update(
                self._model, notes=self._memory.notes(chat.email), chat=turns,
                now=datetime.now(DUBAI), order=s.it_support_chat_providers,
            )
            if notes is None:
                continue  # no model answered; tried again next round
            self._memory.keep(chat.email, notes, history[0]["id"], datetime.now(DUBAI))
            chat.notes_behind = False
            logger.info("it support: notes on %s brought up to date (%d lines)", chat.email,
                        notes.count("\n") + 1)

    async def _morning_briefs(self, chats: list[Chat]) -> None:
        """Each person's first message of the working day, once, when there
        is something due. Off unless the settings page turns it on; with a
        trial address set, every brief goes into that one chat. See ``brief``."""
        s = self._settings
        if not s.it_support_brief_enabled or self._sharepoint is None or self.me_id is None:
            return
        now = datetime.now(UTC)
        day = brief_mod.due_now(s, now)
        if day is None:
            return
        test_to = s.it_support_due_test_to.strip().lower()
        by_email = {c.email: c for c in chats}
        for chat in chats:
            key = brief_mod.sent_key(chat.email, day)
            if self._asked.has(key):
                continue
            known = self._work.get(chat.email)
            if known is None or known.read_at is None or now - known.read_at > timedelta(
                    minutes=30):
                continue  # tasks not read yet this morning; next round
            if self._factory is not None:
                from app.mailwatch.availability import away_reason

                async with self._factory() as session:
                    away = await away_reason(session, chat.email)
                if away:
                    self._asked.mark_key(key, now)
                    logger.info("it support: no brief for %s, away (%s)", chat.email, away)
                    continue
            plan = brief_mod.plan(known, now=now, settings=s)
            if plan.empty:
                self._asked.mark_key(key, now)
                continue
            target = by_email.get(test_to) if test_to else chat
            if target is None:
                logger.warning("it support: the trial address %s is not a chat it answers",
                               test_to)
                return
            spoken = await brief_mod.write(
                self._model, first_name=chat.first_name, brief=plan, now=now,
                order=s.it_support_chat_providers, context=work.describe(known, now),
            )
            text = spoken.text
            if target is not chat:
                text = f"(test: this is {chat.first_name}'s morning brief) {text}"
            await self._say(target, talk.to_html(text))
            self._asked.mark_key(key, now)
            # What the brief named gets no "need a hand?" later the same day.
            self._asked.mark(plan.today + plan.next_day, now)
            logger.info("it support: morning brief to %s (%d today, %d early tomorrow, %d "
                        "late)%s", chat.email, len(plan.today), len(plan.next_day),
                        len(plan.late), f", sent to {target.email}" if target is not chat else "")

    async def _due_check_ins(self, chats: list[Chat]) -> None:
        """Message each person whose task has its BCD later today, once.

        Off unless IT_SUPPORT_DUE_ENABLED; office hours and working days only;
        with IT_SUPPORT_DUE_TEST_TO set, every check-in goes into that one
        person's chat instead, marked as a test. See ``due``.
        """
        s = self._settings
        if not s.it_support_due_enabled or self._sharepoint is None or self.me_id is None:
            return
        if time.monotonic() - self._due_checked < s.it_support_due_check_minutes * 60:
            return
        now = datetime.now(UTC)
        if not due.in_office_hours(s, now) or brief_mod.before_brief(s, now):
            return  # the morning brief speaks first
        self._due_checked = time.monotonic()
        test_to = s.it_support_due_test_to.strip().lower()
        by_email = {c.email: c for c in chats}
        for chat in chats:
            known = self._work.get(chat.email)
            if known is None or known.read_at is None:
                continue
            tasks = self._asked.new(due.to_ask(known, now=now, settings=s))
            if not tasks:
                continue
            target = by_email.get(test_to) if test_to else chat
            if target is None:
                logger.warning("it support: IT_SUPPORT_DUE_TEST_TO %s is not a chat it answers",
                               test_to)
                return
            spoken = await due.write(
                self._model, first_name=chat.first_name, tasks=tasks, now=now,
                order=s.it_support_chat_providers, context=work.describe(known, now),
            )
            text = spoken.text
            if target is not chat:
                text = f"(test: this would go to {chat.first_name}) {text}"
            sent = await self._graph.send(target.chat_id, talk.to_html(text))
            target.seen.add(str(sent.get("id")))
            target.active_at, target.notes_behind = time.monotonic(), True
            self._asked.mark(tasks, now)
            logger.info("it support: asked %s about %d task(s) closing soon%s", chat.email,
                        len(tasks), f" (sent to {target.email})" if target is not chat else "")

    # ── files in the chat: read, understood, answered ─────────────────

    def _spawn(self, job, name: str) -> None:
        task = asyncio.create_task(job, name=name)
        self._searches.add(task)
        task.add_done_callback(self._searches.discard)

    def _documents_turn(
        self, chat: Chat, asked: list[dict], newest_first: list[dict]
    ) -> bool:
        """Whether this turn is about documents: files just sent, the answer
        to a question about files sent before, or a yes to an offer. The work
        runs on its own, like a distributor search."""
        now = time.monotonic()
        oldest_first = list(reversed(asked))
        sent = [f for m in oldest_first for f in files_mod.in_message(m)]
        words = " ".join(
            t for t in (talk.plain(m) for m in oldest_first) if t != "(sent a file or a picture)"
        ).strip()
        history = self._history_lines(newest_first)
        offer, chat.offer = chat.offer, None
        if sent:
            chat.waiting_files = None
            self._spawn(self._documents(chat, words, history, sent=sent), "itsupport-docs")
            return True
        if chat.waiting_files and now - chat.waiting_files[1] < WAITING_SECONDS and (
            distributors.read_ask(words) is None
        ):
            opened, chat.waiting_files = chat.waiting_files[0], None
            self._spawn(self._documents(chat, words, history, opened=opened), "itsupport-docs")
            return True
        if offer and now - offer[1] < WAITING_SECONDS and _YES.match(words):
            self._spawn(self._find_for(chat, offer[0]), "itsupport-find")
            return True
        return False

    def _history_lines(self, newest_first: list[dict]) -> str:
        assert self.me_id is not None
        turns = talk.turns(list(reversed(newest_first)), self.me_id)[-8:]
        return "\n".join(
            f"{'IT Support' if t['role'] == 'assistant' else 'Them'}: {t['content'][:300]}"
            for t in turns
        )

    async def _messages(self, chat: Chat, top: int) -> list[dict[str, Any]]:
        """The chat's newest messages, newest first, without any from before
        it was cleared for this person (``memory.forget_before``)."""
        found = await self._graph.messages(chat.chat_id, top=top)
        cut = self._memory.forget_before(chat.email)
        if cut is None:
            return found
        return [m for m in found if (when := _when(m.get("createdDateTime"))) and when > cut]

    async def _say(self, chat: Chat, html_body: str, dots: Dots | None = None) -> None:
        """Send a message, or turn this turn's "…" into it."""
        if dots is None or not await dots.say(html_body):
            sent = await self._graph.send(chat.chat_id, html_body)
            chat.seen.add(str(sent.get("id")))
        chat.active_at, chat.notes_behind = time.monotonic(), True

    async def _find_for(self, chat: Chat, ask: distributors.Ask) -> None:
        what = ask.label.removeprefix("these ")
        place = ask.place.removeprefix("the ")
        progress = Progress(self._graph, chat.chat_id, f"{what[:40]} · {place}")
        sent_id = await progress.begin("Supplier library", "Checking our supplier library")
        chat.seen.add(str(sent_id))
        await self._find(chat, ask, progress)

    async def _documents(
        self, chat: Chat, words: str, history: str, *,
        sent: list[files_mod.ChatFile] | None = None,
        opened: list[files_mod.Opened] | None = None,
    ) -> None:
        count = len(sent or opened or [])
        progress = Progress(
            self._graph, chat.chat_id, f"{count} file{'s' if count != 1 else ''}",
            total_steps=3, heading="Reading your files", below="Here's what I found 👇",
        )
        started = time.monotonic()
        try:
            sent_id = await progress.begin("Files", "Opening the files")
            chat.seen.add(str(sent_id))
            if opened is None:
                try:
                    opened = [await files_mod.fetch_and_open(self._graph, f) for f in sent or []]
                except FilesNotAllowedError as exc:
                    progress.done("not allowed yet")
                    await progress.finish()
                    await self._say(chat, talk.to_html(
                        "I can't open files sent in chat yet, I'm still waiting on the "
                        "permission for that. Could you paste the text in for now?"
                    ))
                    logger.error("it support: chat files need Files.Read.All; run "
                                 "python -m app.itsupport.connect again (%s)", exc)
                    return
            ok = [o for o in opened if o.readable is not None]
            progress.done(f"{len(ok)} of {len(opened)} opened")
            if not ok:
                await progress.finish()
                why = "; ".join(f"{o.name}: {o.problem}" for o in opened)
                them = "them" if len(opened) > 1 else "it"
                await self._say(chat, talk.to_html(f"I couldn't open {them}: {why}"))
                return
            known = self._work.get(chat.email)
            for o in opened:
                files_mod.first_look(o, known.tasks if known else [])
            progress.start("Understanding", "Working out what you need")
            spent = Spent()
            now = datetime.now(UTC)
            goal = await goal_mod.decide(
                self._finder, text=words, opened=opened, history=history, work=known, now=now,
                user_key=chat.email, spent=spent,
            )
            label = {"requirements": "customer's requirements", "costing": "supplier quotes",
                     "chat": "a question", "ask": "need to check with you"}[goal.goal]
            progress.done(label)
            task = next((t for t in known.tasks if t.id == goal.task_id), None) if (
                known and goal.task_id) else None
            logger.info("it support: %s sent %d file(s): %s%s (%s, %s)", chat.email, len(opened),
                        goal.goal, f" for task {goal.task_id}" if goal.task_id else "",
                        "model" if goal.by_model else "rules", goal.why)
            if goal.goal == "ask":
                await progress.finish()
                chat.waiting_files = (opened, time.monotonic())
                await self._say(chat, talk.to_html(goal.question))
                return
            if goal.goal == "requirements":
                progress.start("Requirements", "Reading the requirements")
                result, left_out = await docs.read_requirements(self._claude, ok, task, chat.email)
                progress.done(f"{len(result.items)} item(s)")
                await progress.finish()
                await self._say(chat, docs.requirements_html(
                    result, left_out, offer_find=not goal.find_suppliers))
                if result.items:
                    ask = docs.as_find_ask(result)
                    if goal.find_suppliers:
                        await self._find_for(chat, ask)
                    else:
                        chat.offer = (ask, time.monotonic())
                return
            if goal.goal == "costing":
                progress.start("Quotes", "Reading the quotes")
                read = await docs.read_quotes(self._extractor, ok)
                progress.done(f"{len(read.quotes)} of {len(ok)} read")
                await progress.finish()
                await self._say(chat, docs.quotes_html(read, task))
                return
            # Not for either: the ordinary chat answers, knowing what was sent.
            await progress.finish()
            newest_first = await self._messages(chat, top=self._settings.it_support_history_messages
            )
            spoken = await talk.reply(
                self._model, first_name=chat.first_name, history=list(reversed(newest_first)),
                me_id=self.me_id or "", now=datetime.now(DUBAI),
                order=self._settings.it_support_chat_providers,
                notes=self._memory.notes(chat.email),
                context=work.describe(known, datetime.now(DUBAI)) + "\n\nFiles they just sent:\n"
                + "\n".join(files_mod.describe(o) for o in opened),
            )
            await self._say(chat, talk.to_html(spoken.text.strip()[:4000]))
        except Exception:  # noqa: BLE001 - say so rather than go quiet
            logger.exception("it support: reading %s's files failed", chat.email)
            with contextlib.suppress(Exception):
                await progress.finish()
            with contextlib.suppress(Exception):
                await self._say(chat, talk.to_html(
                    "Sorry, something went wrong while I was reading that. Can you send it again?"
                ))
        finally:
            logger.info("it support: files for %s took %.0fs", chat.email,
                        time.monotonic() - started)
