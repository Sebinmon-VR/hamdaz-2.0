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
from typing import Any, Final
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
from app.itsupport import (
    activity,
    costcheck,
    distributors,
    docs,
    due,
    features,
    jobs,
    memory,
    smalltalk,
    talk,
    tasklist,
    when,
    work,
)
from app.itsupport import brief as brief_mod
from app.itsupport import files as files_mod
from app.itsupport import goal as goal_mod
from app.itsupport import links as app_links
from app.itsupport import lookups as lookups_mod
from app.itsupport import sent as sent_mod
from app.itsupport import settings as settings_mod
from app.itsupport import status as status_mod
from app.itsupport.brain import Brain, Person, Plan, Step, Turn
from app.itsupport.erp import ERP, Access, Answer, access_for
from app.itsupport.graph import FilesNotAllowedError, GraphError, ITSupportGraph, NotConnectedError
from app.itsupport.progress import Progress
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.proposals.oversight import TeamTasksCache
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.itsupport")

DUBAI = ZoneInfo("Asia/Dubai")
#: After a failed check, wait this long before the next.
BACKOFF_SECONDS = 10.0
#: How often the background jobs (tasks, notes, check-ins) look for work.
BACKGROUND_SECONDS = 60.0
#: The live read of a person's tasks before a reply waits at most this long.
LIVE_READ_SECONDS = 6.0
#: A super admin may belong to several teams; this many are put in the context.
MAX_TEAMS_SHOWN = 3
#: Reading one team's tasks before a reply waits at most this long.
TEAM_READ_SECONDS = 15.0
#: Who is in a team and who of them is away are read again after this long.
TEAM_PEOPLE_SECONDS = 600.0
#: A numbered ask is what a reply refers to for this long.
ASKED_SECONDS = 16 * 3600
#: An answer is passed back to whoever asked for this long after the message.
RELAY_SECONDS = 8 * 3600
#: How long files waiting on an answer ("is this the RFQ or a quote?") and an
#: offer waiting on a yes ("want me to find suppliers?") are kept.
WAITING_SECONDS = 30 * 60
#: How long files sent in the chat stay at hand for later messages.
FILES_KEPT_SECONDS = 3 * 60 * 60
#: However long the messages keep coming, a turn starts after this.
GATHER_AT_MOST = 30.0
#: How long a person's modules are trusted before being read again.
ACCESS_SECONDS = 5 * 60
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
    #: The teams whose tasks they oversee, (id, name), by the app's own rule
    #: (``proposals.oversight``): super admins, the CEO and managers every
    #: team, a team's lead or manager theirs. For them "any tasks?" means the
    #: team's (the user, 2026-11-18).
    oversees: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    #: Whether "any tasks?" means the team's: an organisation role (super
    #: admin, CEO, manager). A team's lead is a member who holds tasks too
    #: (Jasna): theirs first, the team's when they ask about it.
    team_first: bool = False
    #: Their name in Teams, for what to call them when the page names none.
    display_name: str | None = None
    #: The last numbered list IT Support sent here (a daily ask, the team's
    #: day, a BCD question), so "1 submitted" knows what 1 is.
    asked: status_mod.Asked | None = None
    #: Someone asked IT Support to message this person (connect_colleague):
    #: (their email, the task, when, monotonic). This person's next answer
    #: goes back to them.
    relay: tuple[str, str, float] | None = None
    #: The task the conversation is on (the last one looked up or filed):
    #: (task id, title, holder, holder's email, monotonic), so "can you call"
    #: or "message her" knows who.
    focus: tuple[str, str, str, str, float] | None = None


def _words(message: dict[str, Any]) -> str:
    """What they typed in a message, without the stand-in for a file."""
    text = talk.plain(message)
    return "" if text == "(sent a file or a picture)" else text.strip()


#: What small talk sounds like. Only this runs at low effort with no notes:
#: a short message that isn't social ("can you call", "yes", "the 2nd one")
#: carries on the conversation and needs it all (2026-11-18: "can you call"
#: after talking about 855 got "call who? 1981?" at low effort).
#: Words of small talk, and those that make a message social on their own
#: ("ok" or "yes" alone answers whatever was just asked, so it isn't).
_SOCIAL_WORDS: Final = frozenset({
    "hi", "hii", "hiii", "hey", "heyy", "hello", "hai", "good", "morning", "afternoon",
    "evening", "night", "gm", "gn", "how", "are", "r", "you", "u", "doing", "whats", "what's",
    "up", "sup", "thanks", "thank", "thx", "ty", "bye", "see", "cya", "nice", "great",
    "cool", "lol", "haha", "test", "testing", "ok", "okay", "there", "again", "all", "much",
    "so", "very", "the", "team", "dear", "bro", "man", "it", "support", "itsupport"})
_SOCIAL_STRONG: Final = frozenset({
    "hi", "hii", "hiii", "hey", "heyy", "hello", "hai", "morning", "afternoon", "evening",
    "gm", "gn", "sup", "thanks", "thank", "thx", "ty", "bye", "cya", "lol", "haha", "test",
    "testing", "there", "how"})


def _social(words: str) -> bool:
    tokens = [t for t in re.findall(r"[a-z']+", words.casefold())]
    return (bool(tokens) and all(t in _SOCIAL_WORDS for t in tokens)
            and any(t in _SOCIAL_STRONG for t in tokens))


def _small_talk(words: str, batch: list[dict[str, Any]], files: dict[str, Any],
                plan_note: str) -> bool:
    """A greeting, a thanks: social, a few words, nothing sent with it, no
    question of ours waiting on the answer."""
    return (len(words.split()) <= SMALL_TALK_WORDS and not plan_note
            and _social(words)
            and not any(files_mod.in_message(m) for m in batch)
            and not re.search(r"\d{3,}", words))


def whose_tasks(chat: Chat) -> str:
    """For someone who oversees a team: whose tasks a question about tasks
    means. A manager or super admin: the team's. A team's lead, who holds
    tasks too: their own, the team's when they ask about it."""
    teams = ", ".join(n for _, n in chat.oversees[:MAX_TEAMS_SHOWN])
    if chat.team_first:
        return (f"They oversee {teams}: a question about tasks (any due today, pending, late, "
                "this week) is about the team, not their own list, unless they say my/mine/I. "
                "Answer from the team's list straight away, naming who holds each; don't say "
                "what their own list has.")
    return (f"They lead {teams} and hold tasks themselves: a question about tasks (any due "
            "today, pending, late) is about their own list first. Use the team's list when "
            "they ask about the team, everyone, or a colleague by name, or when they have no "
            "open tasks of their own; then name who holds each.")


def focus_note(chat: Chat) -> str:
    """What the conversation is on, if it was on a task in the last half hour."""
    if chat.focus is None or time.monotonic() - chat.focus[4] > 30 * 60:
        return ""
    task_id, title, holder, email, _ = chat.focus
    who = f", held by {holder}" + (f" ({email})" if email else "") if holder else ""
    return (f"THE CONVERSATION IS ON task {task_id} ({title[:70]}){who}. A short message "
            f"(\"can you call\", \"message her\", \"yes\", \"what about the costing\") "
            "carries on from this, not from the lists below.")


def answering_last(newest_first: list[dict[str, Any]],
                   batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chat oldest first, with the messages this turn answers moved to the
    end. A message sent while the last reply was being written sits before
    that reply in Teams' order, so the model took it as answered and replied
    to nothing (2026-11-18: "4500 USD in AED", sent while it answered about
    Fortinet, got a remark about task 1981). One not in the read yet is added."""
    ids = {m.get("id") for m in batch}
    oldest_first = list(reversed(newest_first))
    earlier = [m for m in oldest_first if m.get("id") not in ids]
    seen = {m.get("id") for m in oldest_first}
    now_asked = [m for m in oldest_first if m.get("id") in ids] + \
        [m for m in batch if m.get("id") not in seen]
    return earlier + now_asked


def asked_note(asked: status_mod.Asked) -> str:
    """The last numbered list sent here, for the brain."""
    what = {"status": "where these stand", "reason": "why these are late",
            "team": "the team's tasks", "bcd": "a placeholder BCD",
            "costing": "submitted tasks with no costing sheet"}.get(asked.kind, asked.kind)
    listed = "; ".join(f"{i.number} = {i.task_id} {i.title[:50]}" for i in asked.items[:30])
    out = (f"Your last numbered message ({what}, {when.said(asked.at, datetime.now(UTC))}): "
           f"{listed}. Numbers in their reply refer to these.")
    if asked.kind in ("status", "reason"):
        out += " File their answers with update_task."
    if asked.kind == "team":
        out += (" \"check with 2\" / \"ask Haleema about it\": connect_colleague (a call "
                "link, or a message after their yes).")
    if asked.kind == "costing":
        out += (" A supplier quote they send for one: make that task's costing (as in Making "
                "a costing sheet). \"not needed\", \"it was an EOI\": fine, nothing to "
                "file; say so in a line. \"done\"/\"added\": thank them.")
    return out


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


def mentioned(task_id: str, title: str, said: str) -> bool:
    """Whether a message names the task: its id, or its tender number (the
    check-in calls a task by its RFQ or tender number)."""
    numbers = {task_id} | set(re.findall(r"\d{6,}", title or ""))
    return any(re.search(rf"(?<!\d){re.escape(n)}(?!\d)", said) for n in numbers if n)


def due_key(task: due.DueTask) -> str:
    """A due-today check-in's flag in the sent ledger: the task on its UAE day."""
    return f"due-ask:{task.task_id}@{task.due_at.astimezone(DUBAI).date().isoformat()}"


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
        settings_mod.trial_overrides(settings)
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
        #: Each overseen team's members and who of them is away, with when read.
        self._team_people: dict[uuid.UUID, tuple[list, dict, float]] = {}
        self._team_age: dict[uuid.UUID, int] = {}
        #: Team tasks: the app's own cache (a minute), shared with its screens.
        self._team_cache: TeamTasksCache = getattr(
            getattr(app, "state", None), "team_tasks_cache", None) or TeamTasksCache()
        self._brain = Brain(settings, self._erp, self._finder, self._claude, self._extractor,
                            http=http) if self._erp is not None else None
        #: What it has sent once (asks, alerts), in the database.
        self._sent = sent_mod.Ledger(factory)
        #: A task's files (its task folder, its list attachments) and whether
        #: it has a costing; read only.
        from app.quoting.storage import QuoteDrive

        self._files = (costcheck.Files(factory, sharepoint, QuoteDrive(settings, http))
                       if sharepoint is not None else None)
        #: Where the polling jobs got to ("reviews": the last review seen).
        self._watermarks: dict[str, datetime] = {}
        if self._brain is not None and self._erp is not None:
            self._brain.lookups = lookups_mod.Lookups(settings, self._erp, factory)
            self._brain.lookups.on_teams = self._teams_message
            self._brain.lookups.sharepoint = sharepoint
            self._brain.lookups.files = self._files
            self._brain.lookups.on_submitted = self._submitted_note
            self._brain.lookups.sent = self._sent
            self._brain.on_teams = self._teams_message
            self._brain.on_change = self._log_change
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
    def graph(self) -> ITSupportGraph:
        """Graph as IT Support (the settings page's chat viewer reads with it)."""
        return self._graph

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
            if features.trial(self._settings):
                # A trial answers only its own people and leaves the lease to
                # the process answering everyone else.
                held = True
                if not was:
                    logger.warning("it support: TRIAL on this machine: answering only %s, "
                                   "without the lease", self._settings.it_support_allowed_emails)
            else:
                held = await settings_mod.claim(self._factory, signed_in_as=self._me_email,
                                                error=error)
            if not held:
                if was:
                    logger.warning("it support: another process (%s) is answering; this one "
                                   "stays quiet so nobody gets two replies", row.running_on)
                self.answering = False
                return
            # A trial answers its own people even with the page's switch off:
            # production is switched off there so the trial can run alone
            # (the user, 2026-11-18). Stopping the laptop stops the trial.
            self.answering = row.enabled or features.trial(self._settings)
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
        try:
            await jobs.feedback(self, chat, newest_first)
        except Exception:  # noqa: BLE001 - feedback is a record, not the chat
            logger.exception("it support: feedback check failed")
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
            chat.oversees, chat.team_first = await self._oversees(session, user_id)
        self._people[chat.email] = (user_id, access, time.monotonic())
        logger.info("it support: %s reaches %s%s", chat.email,
                    ", ".join(sorted(access.modules)) or "no modules",
                    " (super admin)" if access.is_admin else "")
        return user_id, access

    @staticmethod
    async def _oversees(session: AsyncSession, user_id: uuid.UUID
                        ) -> tuple[list[tuple[uuid.UUID, str]], bool]:
        """The teams whose tasks they may see, as the team task board decides
        (``oversight.GLOBAL_OVERSIGHT`` / ``TEAM_OVERSIGHT``), and whether
        their questions about tasks mean the team's (an organisation role
        only). An organisation role covers the follow-up's watched team
        (Presale) and their own."""
        from app.followups import service as fu
        from app.proposals.oversight import GLOBAL_OVERSIGHT, TEAM_OVERSIGHT
        from app.roles.service import global_role_keys
        from app.teams.service import team_role_keys

        rows = (await session.execute(
            select(Team.id, Team.name).join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(TeamMembership.user_id == user_id).distinct().order_by(Team.name))).all()
        if await global_role_keys(session, user_id) & GLOBAL_OVERSIGHT:
            out: list[tuple[uuid.UUID, str]] = []
            watched = (await fu.get_settings(session)).team_id
            if watched is not None and (team := await session.get(Team, watched)) is not None:
                out.append((team.id, team.name))
            out += [(tid, name) for tid, name in rows if tid != watched]
            return out, True
        return [(tid, name) for tid, name in rows
                if await team_role_keys(session, team_id=tid, user_id=user_id) & TEAM_OVERSIGHT
                ], False

    async def _team_view(self, chat: Chat) -> str:
        """The tasks of the teams they oversee, as the list has them now
        (the app's shared team cache, a minute at most), with who holds each
        and who's away. Empty for everyone else."""
        if not chat.oversees or self._sharepoint is None or self._factory is None:
            return ""
        parts: list[tuple[str, int]] = []
        for team_id, name in chat.oversees[:MAX_TEAMS_SHOWN]:
            try:
                parts.append(await asyncio.wait_for(self._team_tasks(team_id, name),
                                                    TEAM_READ_SECONDS))
            except Exception as exc:  # noqa: BLE001 - say so; the ERP can still be read
                logger.warning("it support: reading the %s team's tasks failed: %s", name,
                               exc or type(exc).__name__)
                parts.append((f"(The {name} team's tasks couldn't be read just now; read "
                              "them from the ERP.)", 1))
        # A team with nothing on the Proposals list (HR, say) is left out,
        # unless nothing else has anything either.
        busy = [text for text, count in parts if count]
        return "\n\n".join(busy or [text for text, _ in parts[:1]])

    async def _team_tasks(self, team_id: uuid.UUID, name: str) -> tuple[str, int]:
        """The team's list as text, and how many open tasks are on it."""
        now = datetime.now(UTC)
        data = await self._team_data(team_id)
        if data is None:
            return "", 0
        holders, held = data
        age = self._team_age.get(team_id, 0)
        return work.describe_team(name, holders, held, read_at=now - timedelta(seconds=age),
                                  now=now)

    async def _team_data(self, team_id: uuid.UUID
                         ) -> tuple[list[tasklist.Member], set[str]] | None:
        """Each member (what the team calls them, why they're away or None,
        their tasks), and the tasks still on a placeholder BCD. The tasks are a minute old at most;
        who's in the team and who's away are kept TEAM_PEOPLE_SECONDS (a
        9-person team's leave check took 8 s from the laptop, one at a time)."""
        from app.bcd import service as bcd
        from app.mailwatch.availability import away_reasons
        from app.proposals.oversight import resolve_members

        if self._factory is None or self._sharepoint is None:
            return None
        async with self._factory() as session:
            kept = self._team_people.get(team_id)
            if kept is None or time.monotonic() - kept[2] > TEAM_PEOPLE_SECONDS:
                team = await session.get(Team, team_id)
                if team is None:
                    return None
                members = await resolve_members(session, team, self._sharepoint)
                away = await away_reasons(session, [m.user_id for m in members])
                kept = (members, away, time.monotonic())
                self._team_people[team_id] = kept
            members, away, _ = kept
            rows, _, age = await self._team_cache.rows(
                self._sharepoint, team_id=team_id, members=members, limit=500, refresh=False)
            everyone = [t for m in members if m.lookup_id for t in rows.get(m.lookup_id, [])]
            held = await bcd.held_ids(session, everyone)
        self._team_age[team_id] = age
        return [tasklist.Member(
            name=talk.first_name(self._settings, m.email or "", m.name or m.email),
            away=away.get(m.user_id), tasks=rows.get(m.lookup_id or "", []),
            email=(m.email or "").lower(), full_name=m.name or "")
            for m in members], held

    def _list_names(self, chat: Chat) -> dict[str, str]:
        """Every word of the overseen teammates' names, lower case → the name
        the team's list shows ("sri" and "vishnu" → "Vishnu")."""
        out: dict[str, str] = {}
        for team_id, _ in chat.oversees:
            kept = self._team_people.get(team_id)
            for m in kept[0] if kept is not None else []:
                short = talk.first_name(self._settings, m.email or "", m.name or m.email)
                for word in f"{m.name or ''} {short} {(m.email or '').split('@')[0]}".split():
                    if len(word) > 2:
                        out.setdefault(word.casefold().strip(".,"), short)
        return out

    async def _team_rows(self, chat: Chat, ahead: int = 0
                         ) -> list[tuple[str, list[tasklist.Row]]] | None:
        """The rows of each team they oversee, read now (the shared cache, a
        minute at most); None when no team could be read."""
        now = datetime.now(UTC)
        out: list[tuple[str, list[tasklist.Row]]] = []
        for team_id, name in chat.oversees[:MAX_TEAMS_SHOWN]:
            try:
                data = await asyncio.wait_for(self._team_data(team_id), TEAM_READ_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the others may still read
                logger.warning("it support: reading the %s team's tasks failed: %s", name,
                               exc or type(exc).__name__)
                continue
            if data is not None:
                members, held = data
                out.append((name, tasklist.collect(members, held, now, ahead=ahead)))
        return out or None

    async def _team_list(self, chat: Chat, flt: tasklist.Filter
                         ) -> tuple[tasklist.Shown, list[tasklist.Row]] | None:
        """The team's list narrowed as asked, numbered as the last list was;
        the chat's numbered list becomes this one. None: unreadable now."""
        teams = await self._team_rows(chat, ahead=flt.ahead)
        if teams is None:
            return None
        # A team with nothing at all (HR) says nothing.
        if len(teams) > 1:
            teams = [(n, rows) for n, rows in teams if rows] or teams[:1]
        now = datetime.now(UTC)
        earlier = self._last_ask(chat)
        shown = tasklist.filtered(teams, flt, now, earlier)
        if shown.items:
            chat.asked = status_mod.Asked("team", shown.items, now)
        return shown, [r for _, rows in teams for r in flt.apply(rows)]

    async def _list_rules(self, chat: Chat, words: str) -> bool:
        """"show only not submitted", "Haleema's", "the late ones": the team's
        list again, narrowed by rules (no model), after the team's list was
        sent here, or any time for a manager (for whom tasks are the team's)."""
        if not chat.oversees:
            return False
        asked = self._last_ask(chat) or await self._asked_from_history(chat)
        if not chat.team_first and (asked is None or asked.kind != "team"):
            return False
        if any(t not in self._team_people for t, _ in chat.oversees[:MAX_TEAMS_SHOWN]):
            await self._team_rows(chat)  # their names, to read "Haleema's"
        flt = tasklist.read_filter(words, self._list_names(chat))
        if flt is None:
            return False
        found = await self._team_list(chat, flt)
        if found is None:
            await self._say(chat, talk.to_html(
                "I couldn't read the team's list just now. Try me again in a minute?"))
            return True
        shown, _ = found
        await self._say(chat, shown.html)
        logger.info("it support: %s asked for the team's list narrowed (%s): %d row(s), rules",
                    chat.email, flt.said(), shown.count)
        return True

    def _team_emails(self, chat: Chat) -> frozenset[str]:
        """The teammates of the teams they oversee, as last read."""
        return frozenset(self._team_names(chat).values())

    def _team_names(self, chat: Chat) -> dict[str, str]:
        """Those teammates by name, lower case: name → email."""
        out: dict[str, str] = {}
        for team_id, _ in chat.oversees:
            kept = self._team_people.get(team_id)
            if kept is not None:
                out.update({(m.name or m.email).casefold(): m.email.lower()
                            for m in kept[0] if m.email and m.email.lower() != chat.email})
        return out

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
            # Only a turn the model answers waits for the live read: "hi" doesn't.
            known = await self._fresh_work(chat)
            tasks = known.tasks if known else []
            await self._relay(chat, words)
            if words and not waiting and await self._status_rules(
                    chat, user_id, access, words, tasks):
                return
            # "show only not submitted" after the team's list: the list again,
            # narrowed by rules (2026-11-19: the model gave up on it).
            if words and not waiting and set(chat.files) == before and \
                    await self._list_rules(chat, words):
                return
            team = await self._team_view(chat)
            person = Person(user_id, chat.email, chat.first_name, tasks, access,
                            team_emails=self._team_emails(chat),
                            team_names=self._team_names(chat))
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
            history = talk.turns(answering_last(newest_first, batch), self.me_id)
            small = _small_talk(words, batch, files, plan_note)
            context = self._context(chat, person, known, files, batch, problems, plan_note,
                                    small_talk=small, earlier=self._earlier(newest_first),
                                    team=team)

            async def say(text: str) -> None:
                text = app_links.check(text, lookups_mod.link_base(self._settings))
                await self._say(chat, talk.to_html(text.strip()[:1000]))

            async def find(ask: distributors.Ask) -> None:
                self._spawn(self._find_for(chat, ask), "itsupport-find")

            async def send_file(name: str, url: str, attachment_id: str) -> None:
                sent = await self._graph.send_file(
                    chat.chat_id, talk.to_html("Here's the costing sheet 👇"), name=name,
                    url=url, attachment_id=attachment_id)
                chat.seen.add(str(sent.get("id")))
                logger.info("it support: sent %s to %s", name, chat.email)

            async def team_list(who: str, section: str, state: str) -> str:
                names = self._list_names(chat)
                picked = {names[w] for w in re.findall(r"[a-z]+", who.casefold())
                          if w in names}
                if who.strip() and not picked:
                    return f"{who} isn't in a team they oversee."
                flt = tasklist.Filter("" if state == "all" else state, frozenset(picked),
                                      frozenset({section}) if section else frozenset())
                found = await self._team_list(chat, flt)
                if found is None:
                    return "The team's list couldn't be read just now; say so."
                shown, rows = found
                turn.block = shown.html
                return (f"Shown as a table where you put {tasklist.MARK} on its own line in "
                        "your reply (once). Don't type the rows yourself; a short line before "
                        f"it at most. {shown.line}\n"
                        + tasklist.describe_rows(rows, datetime.now(UTC)))

            async def send_files(cards: list[tuple[str, str, str]], line: str) -> None:
                sent = await self._graph.send_files(chat.chat_id, talk.to_html(line), cards)
                chat.seen.add(str(sent.get("id")))
                logger.info("it support: sent %d file card(s) to %s", len(cards), chat.email)

            turn = Turn(person, files, on_find=find, on_say=say, on_file=send_file,
                        agreed=agreed,
                        effort="low" if small else None)
            turn.on_files = send_files
            if chat.oversees:
                turn.on_team_list = team_list
            if focus_note(chat) and chat.focus is not None:
                turn.focus = chat.focus[:4]  # what the conversation is on, carried over

            # No holding line or typing dots: the reply comes when it's ready
            # (the user's choice, 2026-11-17, after trying both).
            text = await self._brain.answer(turn, chat=history, context=context)
            await self._log_turn(chat, turn)
            if turn.focus is not None:
                chat.focus = (*turn.focus, time.monotonic())
            if agreed and (not turn.done or turn.done[-1].endswith("failed")):
                # Still not finished: their next message may carry what's missing.
                chat.agreed_until = time.monotonic() + WAITING_SECONDS
            if turn.done:
                logger.info("it support: done at once for %s: %s", chat.email,
                            "; ".join(turn.done))
            if turn.plan.steps:
                chat.plan = (turn.plan, time.monotonic())
                logger.info("it support: asked %s to confirm: %s", chat.email, turn.plan.said())
            # Links into the app only to pages it has (2026-11-19: some were wrong).
            text = app_links.check(text, lookups_mod.link_base(self._settings))
            body = tasklist.fill(talk.to_html(text.strip()[:4000]), turn.block)
            if self._settings.it_support_show_timing:
                body += (f"<br><br><i>({time.monotonic() - started:.1f}s · "
                         f"{turn.who or 'no model'})</i>")
            await self._say(chat, body)
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
                    + detail))
        finally:
            chat.busy = False

    def _last_ask(self, chat: Chat) -> status_mod.Asked | None:
        asked = chat.asked
        if asked is None or (datetime.now(UTC) - asked.at).total_seconds() > ASKED_SECONDS:
            return None
        return asked

    async def _status_rules(self, chat: Chat, user_id: uuid.UUID, access: Access, words: str,
                            tasks: list) -> bool:
        """A reply the rules read fully ("1 submitted, 2 on hold waiting for
        price", "1952 submitted", "BCD correct") filed at once, no model.
        False: the brain takes the message (with the ask in its context)."""
        s = self._settings
        if self._erp is None or self._factory is None:
            return False
        asked = self._last_ask(chat)
        if asked is None:
            asked = await self._asked_from_history(chat)
        if asked is not None and asked.kind == "bcd" and features.on(s, "bcd_asks"):
            if re.search(r"\b(correct|right|fine|ok(ay)?|yes|confirm(ed)?)\b", words, re.I) \
                    and not status_mod.looks_like_question(words):
                item = asked.items[0]
                answer = await self._erp.call(user_id, "POST",
                                              f"/bcd-checks/{item.ref}/confirm", access=access,
                                              body={})
                said = (f"Thanks, I've marked {item.task_id}'s BCD as right." if answer.ok
                        else f"Couldn't mark it: {status_mod._why(answer)}")
                await self._say(chat, talk.to_html(said))
                await activity.record(self._factory, email=chat.email, kind=activity.ANSWER,
                                      summary=f"BCD confirmed for {item.task_id}",
                                      ok=answer.ok, detail={"check": item.ref})
                chat.asked = None
                return True
            return False
        if not features.on(s, "status_updates"):
            return False
        if asked is not None and asked.kind in ("team", "costing"):
            return False  # "check with 2", "here's the quote for 1": the brain's
        reading = status_mod.read(words, asked=asked, tasks=tasks)
        if reading.unsure or not reading.updates:
            return False
        mine = {t.id: t for t in tasks}
        if any(u.task_id not in mine for u in reading.updates):
            return False
        if features.trial(s) and any("test" not in (mine[u.task_id].title or "").casefold()
                                     for u in reading.updates):
            await self._say(chat, talk.to_html(
                "Trial: I only file updates on tasks with \"test\" in the title."))
            return True
        from app.models.followup import FollowupStatus, TaskFollowup
        from app.reminders import service as reminders

        async with self._factory() as session:
            pending = {r.task_id: str(r.id) for r in (await session.scalars(
                select(TaskFollowup).where(TaskFollowup.assignee_id == user_id,
                                           TaskFollowup.status == FollowupStatus.PENDING))
            ).all()}
            written = (await reminders.get_settings(session)).write_sharepoint
        now = datetime.now(UTC)
        filed = []
        for update in reading.updates:
            done = await status_mod.file(self._erp, user_id=user_id, access=access,
                                         update=update, task=mine.get(update.task_id),
                                         pending_reason=pending, now=now)
            filed.append(done)
            await activity.record(self._factory, email=chat.email, kind=activity.ANSWER,
                                  summary=f"{update.task_id}: {done.said}", ok=done.ok,
                                  detail={"said": words[:500], "by": "rules"})
        said = status_mod.reply(filed, list_written=written)
        said += await self._costing_after(chat, reading.updates, filed, mine)
        await self._say(chat, talk.to_html(said))
        logger.info("it support: %s's update filed by rules: %s", chat.email,
                    "; ".join(f"{f.task_id} {f.said}" for f in filed))
        return True

    async def _costing_after(self, chat: Chat, updates: list, filed: list,
                             mine: dict) -> str:
        """After "1958 submitted": a line asking for its costing sheet when
        it has none anywhere (feature costing_ask), and that task isn't
        asked about again."""
        if not features.on(self._settings, "costing_ask") or self._files is None:
            return ""
        to = costcheck.test_to(self._settings)
        if to and chat.email != to:
            return ""  # being tried: only the tester's own chat
        ok = {f.task_id for f in filed if f.ok}
        tasks = [mine[u.task_id] for u in updates if u.task_id in ok and u.task_id in mine
                 and u.submission and not u.submission.casefold().startswith("not")
                 and costcheck.may_try(self._settings, chat.email, mine[u.task_id])
                 and not await self._sent.seen(costcheck.key(u.task_id, test=bool(to)))]
        if not tasks:
            return ""
        try:
            missing = await self._files.without_costing(tasks)
        except Exception:  # noqa: BLE001 - the update stands; the job asks later
            logger.exception("it support: costing check after %s's update failed", chat.email)
            return ""
        if missing is None:
            return ""  # something couldn't be read: the job asks later
        for task in tasks:
            await self._sent.mark(costcheck.key(task.id, test=bool(to)), email=chat.email,
                                  kind="costing-ask",
                                  detail={"after": "status answer",
                                          "asked": bool(missing and task in missing)})
        return (" " + costcheck.after_filing(missing, lookups_mod.link_base(self._settings))
                if missing else "")

    async def _submitted_note(self, person: Person, task) -> str:
        """For the model, after it filed "submitted": whether to ask for the
        costing sheet in its reply (none anywhere; feature costing_ask)."""
        if not features.on(self._settings, "costing_ask") or self._files is None:
            return ""
        to = costcheck.test_to(self._settings)
        if not costcheck.may_try(self._settings, person.email, task):
            return ""  # being tried: only the tester's own test tasks
        if await self._sent.seen(costcheck.key(task.id, test=bool(to))):
            return ""
        try:
            missing = await self._files.without_costing([task])
        except Exception:  # noqa: BLE001 - the job asks later
            logger.exception("it support: costing check for %s failed", task.id)
            return ""
        if missing is None:
            return ""
        await self._sent.mark(costcheck.key(task.id, test=bool(to)), email=person.email,
                              kind="costing-ask",
                              detail={"after": "status answer", "asked": bool(missing)})
        if not missing:
            return ""
        link = costcheck.make_link(lookups_mod.link_base(self._settings), task.id)
        return (" It has no costing sheet anywhere (not in the ERP, not on the task). In the "
                "same reply, ask for it: if they made it elsewhere, they send it to you here "
                "(you file it with file_to_task) or attach it on the Proposals list; only if "
                f"there's none, they make it in the ERP under this task: [Make the costing for "
                f"{task.id}]({link}).")

    async def _asked_from_history(self, chat: Chat) -> status_mod.Asked | None:
        """The last numbered ask, read back from the chat (after a restart)."""
        try:
            for message in await self._messages(chat, top=15):
                sender = ((message.get("from") or {}).get("user") or {}).get("id")
                at = _when(message.get("createdDateTime"))
                if sender != self.me_id or at is None:
                    continue
                if (datetime.now(UTC) - at).total_seconds() > ASKED_SECONDS:
                    return None
                if (found := status_mod.asked_in(talk.plain(message), at)) is not None:
                    chat.asked = found
                    return found
        except Exception:  # noqa: BLE001 - without it, the brain reads the chat
            logger.exception("it support: reading the last ask back failed")
        return None

    async def _relay(self, chat: Chat, words: str) -> None:
        """Their answer passed back to whoever asked IT Support to message them."""
        if chat.relay is None or not words:
            return
        asker, task_id, sent_at = chat.relay
        if time.monotonic() - sent_at > RELAY_SECONDS:
            chat.relay = None
            return
        chat.relay = None
        target = next((c for c in self._chats if c.email == asker), None)
        if target is None:
            return
        about = f" about {task_id}" if task_id else ""
        said = f"{chat.first_name} replied{about}: \"{words[:800]}\""
        await self._say(target, talk.to_html(said))
        await activity.record(self._factory, email=asker, kind=activity.SENT,
                              summary=f"passed on {chat.first_name}'s answer{about}",
                              detail={"from": chat.email, "task": task_id})

    async def _teams_message(self, person: Person, step: Step) -> Answer:
        """A message to a teammate from IT Support, after the asker's yes;
        their answer comes back to the asker when the agent chats with them."""
        email = step.path.removeprefix("message:").strip().lower()
        text = str((step.body or {}).get("text") or "").strip()
        if not email or not text:
            return Answer(400, "Nothing to send.")
        if self._factory is not None:
            from app.followups.service import ceo_emails

            async with self._factory() as session:
                if email in {e.casefold() for e in await ceo_emails(session)}:
                    return Answer(403, "Not the CEO, for now.")
        testers = settings_mod.trial_people(self._settings)
        if features.trial(self._settings) and email not in testers:
            trial_chat = next((c for c in self._chats if c.email == person.email), None)
            if trial_chat is None:
                return Answer(503, "Trial: no chat to send the test message to.")
            await self._say(trial_chat, talk.to_html(
                f"(trial: this would go to {email})\n{text}"))
            await activity.record(self._factory, email=person.email, kind=activity.SENT,
                                  summary=f"trial message (not sent) to {email}: {text[:200]}",
                                  detail={"to": email, "task": step.query, "trial": True})
            return Answer(200, f"Trial: not sent to {email}; shown here instead. In real use "
                               "their answer comes back to you.")
        target = next((c for c in self._chats if c.email == email), None)
        try:
            chat = target or await self._open_chat(email)
        except Exception as exc:  # noqa: BLE001
            return Answer(502, f"Couldn't open a chat with {email}: {exc}")
        await self._say(chat, talk.to_html(text))
        if target is not None:
            target.relay = (person.email, step.query, time.monotonic())
            back = "their answer will come back to you here"
        else:
            back = "I don't chat with them, so their answer stays in their Teams"
        await activity.record(self._factory, email=person.email, kind=activity.SENT,
                              summary=f"message to {email}: {text[:200]}",
                              detail={"to": email, "task": step.query})
        return Answer(200, f"Sent to {email}; {back}.")

    async def _log_change(self, person: Person, step: Step, answer: Answer) -> None:
        await activity.record(self._factory, email=person.email, kind=activity.CHANGE,
                              summary=step.what[:300], ok=answer.ok,
                              detail={"method": step.method, "path": step.path,
                                      "status": answer.status})

    async def _log_turn(self, chat: Chat, turn: Turn) -> None:
        """The turn's model spend, for the page's daily total."""
        if not (turn.input_tokens or turn.output_tokens):
            return
        from app.enquiries.claude import cost_of

        model = self._settings.it_support_brain_model
        cost = float(cost_of(model, turn.input_tokens, turn.output_tokens))
        await activity.record(self._factory, email=chat.email, kind=activity.TURN,
                              summary=f"{model}: {turn.input_tokens} in, "
                                      f"{turn.output_tokens} out",
                              cost_usd=cost, detail={"done": turn.done[:10]})

    def _context(self, chat: Chat, person: Person, known: work.Work | None,
                 files: dict[str, files_mod.Opened], batch: list[dict[str, Any]],
                 problems: list[str], plan_note: str, *, small_talk: bool = False,
                 earlier: str = "", team: str = "") -> str:
        """What the brain knows this turn, beyond the chat itself."""
        assert self._erp is not None
        now = datetime.now(DUBAI)
        new_files = {f.name for m in batch for f in files_mod.in_message(m)}
        lines = [
            f"{when.today_line(now, self._settings)} "
            f"You're chatting with {chat.first_name} ({chat.email}).",
            earlier,
            focus_note(chat),
            f"Their ERP modules (what you may do in each from the chat): "
            f"{self._erp.modules_said(person.access) or 'none'}"
            + (". They're a super admin." if person.access.is_admin else "."),
            "Their teams (handle: name): " + (
                "; ".join(f"{slug}: {name}" for slug, name in chat.teams) or "none"),
            work.describe(known, now) if self._sharepoint is not None else "",
        ]
        if team:
            lines.append(whose_tasks(chat))
            lines.append(team)
        if (asked := self._last_ask(chat)) is not None:
            lines.append(asked_note(asked))
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
            for name, job in self._jobs() if self.answering else ():
                try:
                    await job(list(chats))
                except NotConnectedError as exc:
                    logger.error("it support: %s", exc)
                    return
                except Exception:  # noqa: BLE001 - one job failing leaves the others
                    logger.exception("it support: %s failed", name)
            await asyncio.sleep(BACKGROUND_SECONDS)

    def _jobs(self) -> list[tuple[str, Any]]:
        """The background jobs, in order: reads first, then what is sent
        (each behind its feature switch; see ``jobs``)."""
        def bound(fn):
            return lambda chats: fn(self, chats)
        return [("refresh work", self._refresh_work), ("notes", self._update_notes),
                ("morning brief", self._morning_briefs), ("due check-ins", self._due_check_ins),
                ("daily asks", bound(jobs.daily_asks)), ("team briefs", bound(jobs.team_briefs)),
                ("bcd asks", bound(jobs.bcd_asks)),
                ("costing asks", bound(jobs.costing_asks)),
                ("approval pings", bound(jobs.approval_pings)),
                ("website enquiries", bound(jobs.website_enquiries)),
                ("leave handovers", bound(jobs.leave_handovers)),
                ("delivery watch", bound(jobs.delivery_watch))]

    async def _keep_work(self, chat: Chat, fresh: work.Work) -> None:
        """A new read of the person's tasks kept, and what changed since the
        last one told (a new task, a BCD moved), when those are on."""
        old = self._work.get(chat.email)
        self._work[chat.email] = fresh
        if old is not None:
            try:
                await jobs.work_changes(self, chat, list(self._chats), old, fresh)
            except Exception:  # noqa: BLE001 - the read is kept regardless
                logger.exception("it support: work changes for %s failed", chat.email)

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
                fresh = await work.read(self._sharepoint, self._factory, chat.email)
            except Exception as exc:  # noqa: BLE001 - the last read stands
                logger.warning("it support: reading %s's tasks failed: %s", chat.email, exc)
                continue
            await self._keep_work(chat, fresh)

    async def _fresh_work(self, chat: Chat) -> work.Work | None:
        """The person's tasks as the list has them now: read again before a
        reply when the last read is older than IT_SUPPORT_LIVE_SECONDS. A slow
        or failing list leaves the last read standing (the context says when
        it was read)."""
        known = self._work.get(chat.email)
        if self._sharepoint is None:
            return known
        age = (datetime.now(UTC) - known.read_at).total_seconds() \
            if known is not None and known.read_at is not None else None
        if age is not None and age < self._settings.it_support_live_seconds:
            return known
        try:
            fresh = await asyncio.wait_for(
                work.read(self._sharepoint, self._factory, chat.email), LIVE_READ_SECONDS)
        except Exception as exc:  # noqa: BLE001 - the last read stands
            logger.warning("it support: live read of %s's tasks failed (%s); using the one "
                           "from %s", chat.email, exc or type(exc).__name__,
                           f"{age:.0f}s ago" if age is not None else "never")
            return known
        await self._keep_work(chat, fresh)
        return fresh

    def _earlier(self, newest_first: list[dict[str, Any]]) -> str:
        """When IT Support last spoke in this chat: what it said then about
        tasks or times was true then, not now."""
        now = datetime.now(UTC)
        for message in newest_first:
            sender = ((message.get("from") or {}).get("user") or {}).get("id")
            if sender != self.me_id or message.get("messageType") != "message":
                continue
            sent = _when(message.get("createdDateTime"))
            if sent is None or (now - sent).total_seconds() < 120:
                return ""
            return (f"Your last message above was sent {when.said(sent, now)}: what it says "
                    "about tasks, statuses or times was true then. Read again for anything "
                    "about now.")
        return ""

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
        if features.on(s, "daily_asks"):
            return  # the morning ask says it, numbered; one message, not two
        now = datetime.now(UTC)
        day = brief_mod.due_now(s, now)
        if day is None:
            return
        test_to = s.it_support_due_test_to.strip().lower()
        by_email = {c.email: c for c in chats}
        for chat in chats:
            key = brief_mod.sent_key(chat.email, day)
            # The database's flag, not only the local file: a deploy wipes the
            # file, and the brief went out again (the user, 2026-11-19: "each
            # msg need its own flag ... if sent it dont sent again").
            if self._asked.has(key) or await self._sent.seen(key):
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
                    await self._sent.mark(key, email=chat.email, kind="morning-brief",
                                          detail={"skipped": away})
                    logger.info("it support: no brief for %s, away (%s)", chat.email, away)
                    continue
            plan = brief_mod.plan(known, now=now, settings=s)
            if plan.empty:
                self._asked.mark_key(key, now)
                await self._sent.mark(key, email=chat.email, kind="morning-brief",
                                      detail={"skipped": "nothing due"})
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
            await self._sent.mark(key, email=chat.email, kind="morning-brief")
            # What the brief named gets no "need a hand?" later the same day.
            self._asked.mark(plan.today + plan.next_day, now)
            for task in plan.today + plan.next_day:
                await self._sent.mark(due_key(task), email=chat.email, kind="due-ask",
                                      detail={"by": "morning brief"})
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
            # Each task's own flag in the database too: the file is gone after
            # a deploy.
            tasks = [t for t in tasks if not await self._sent.seen(due_key(t))]
            if not tasks:
                continue
            target = by_email.get(test_to) if test_to else chat
            if target is None:
                logger.warning("it support: IT_SUPPORT_DUE_TEST_TO %s is not a chat it answers",
                               test_to)
                return
            # And the chat itself: anything IT Support already said about the
            # task today (an ask sent before its flag was kept anywhere but
            # the local file) isn't asked again.
            try:
                said = await self._said_today(target, now)
            except Exception:  # noqa: BLE001 - unsure: ask nothing this round
                logger.exception("it support: reading %s's chat before a check-in failed",
                                 target.email)
                continue
            for task in [t for t in tasks if mentioned(t.task_id, t.title, said)]:
                await self._sent.mark(due_key(task), email=chat.email, kind="due-ask",
                                      detail={"skipped": "already said in the chat"})
            tasks = [t for t in tasks if not mentioned(t.task_id, t.title, said)]
            if not tasks:
                continue
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
            for task in tasks:
                await self._sent.mark(due_key(task), email=chat.email, kind="due-ask",
                                      detail={"task": task.task_id})
            logger.info("it support: asked %s about %d task(s) closing soon%s", chat.email,
                        len(tasks), f" (sent to {target.email})" if target is not chat else "")

    async def _said_today(self, chat: Chat, now: datetime) -> str:
        """Everything IT Support wrote in this chat on today's UAE date."""
        today = now.astimezone(DUBAI).date()
        out = []
        for message in await self._messages(chat, top=30):
            at = _when(message.get("createdDateTime"))
            sender = ((message.get("from") or {}).get("user") or {}).get("id")
            if at is None or at.astimezone(DUBAI).date() != today:
                continue
            if sender == self.me_id:
                out.append(talk.plain(message))
        return "\n".join(out)

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

    async def _say(self, chat: Chat, html_body: str) -> None:
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
