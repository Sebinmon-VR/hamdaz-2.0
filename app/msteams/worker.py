"""The loop that answers Teams chats as each connected AI employee.

Every few seconds, for each AI employee that is switched on, set to answer in
Teams and connected to its own account: list that account's chats, most
recently active first; read the new messages from other people since the last
look; answer each through the assistant as its sender; and post the answer back
into the chat as the employee.

Polling, not Graph change notifications: notifications need a public address
for Microsoft to call, and this app runs where it has none. A poll is one
request per employee when nothing has happened.

In a group chat it answers only when @mentioned; in a one-to-one chat, always.
It starts from the moment the account was connected — nothing said before then
is answered — and remembers how far it has read in each chat.

One instance at a time, by advisory lock: two would answer every message twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.ai_employee import AIEmployee
from app.models.teams_chat import AIEmployeeAccount, TeamsChat
from app.models.user import User
from app.msteams import bridge
from app.msteams.graph import EmployeeGraph, GraphAccountError, decrypt, encrypt

logger = logging.getLogger("hamdaz.teams.worker")

#: Beside the others (812_401 – 812_407), used by nothing else.
TEAMS_LOCK = 812_408
BACKOFF_SECONDS = 60
#: Entries kept in an account's activity log.
ACTIVITY_KEPT = 100


def record(account: AIEmployeeAccount, outcome: str, *, sender: str | None, chat: str | None, detail: str) -> None:
    """One line in the employee's Teams activity, for its card on the admin page."""
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "from": sender,
        "chat": chat,
        "outcome": outcome,
        "detail": detail[:500],
    }
    account.activity = [*(account.activity or [])[-(ACTIVITY_KEPT - 1):], entry]


def when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def sender_id(message: dict[str, Any]) -> str | None:
    return ((message.get("from") or {}).get("user") or {}).get("id")


def mentions(message: dict[str, Any], account_id: str) -> bool:
    for mention in message.get("mentions") or []:
        user = ((mention.get("mentioned") or {}).get("user") or {})
        if user.get("id") == account_id:
            return True
    return False


def new_messages(
    messages: list[dict[str, Any]], *, since: datetime, account_id: str, group: bool
) -> list[dict[str, Any]]:
    """Messages to answer, oldest first: by someone else, after ``since``, mentioned in a group."""
    out = []
    for message in messages:
        created = when(message.get("createdDateTime"))
        if created is None or created <= since:
            continue
        if message.get("messageType") != "message" or message.get("deletedDateTime"):
            continue
        who = sender_id(message)
        if not who or who == account_id:
            continue
        if group and not mentions(message, account_id):
            continue
        out.append(message)
    return sorted(out, key=lambda m: m.get("createdDateTime") or "")


def allowed(settings: Settings, person: User | None, chat: dict[str, Any]) -> bool:
    """Whether the test lock lets this message be answered at all."""
    test = settings.ai_teams_test_list
    if not test:
        return True
    return (
        person is not None
        and chat.get("chatType") == "oneOnOne"
        and person.email.lower() in test
    )


def bursts(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Consecutive messages from the same person, together: one turn, one reply.

    People type in bursts — "hi", "good afternoon", then the question — and
    answering each on its own puts the replies behind the conversation.
    """
    out: list[list[dict[str, Any]]] = []
    for message in messages:
        if out and sender_id(out[-1][-1]) == sender_id(message):
            out[-1].append(message)
        else:
            out.append([message])
    return out


class TeamsWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        graph: EmployeeGraph,
        caller: bridge.AppCaller,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._graph = graph
        self._caller = caller
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("AI employees' Teams worker started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass
        return not self._stopping.is_set()

    async def _loop(self) -> None:
        if not await self._sleep(20):
            return
        while not self._stopping.is_set():
            wait = max(3, self._settings.ai_teams_poll_seconds)
            if self._settings.ai_teams_enabled:
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 - a loop must not die
                    logger.exception("Teams poll failed")
                    wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self) -> None:
        async with self._factory() as session:
            got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": TEAMS_LOCK})
            if not got:
                await session.commit()
                return
            rows = (
                await session.execute(
                    select(AIEmployee, AIEmployeeAccount)
                    .join(AIEmployeeAccount, AIEmployeeAccount.employee_id == AIEmployee.id)
                    .where(
                        AIEmployee.enabled.is_(True),
                        AIEmployee.teams_enabled.is_(True),
                        AIEmployeeAccount.status == "connected",
                    )
                )
            ).all()
            for employee, account in rows:
                try:
                    await self.poll(session, employee, account)
                except GraphAccountError as exc:
                    logger.warning("Teams poll for %s: %s", employee.name, exc)
                    account.error = str(exc)[:1000]
                    record(account, "error", sender=None, chat=None, detail=str(exc))
                    if exc.reconnect:
                        account.status = "needs_reconnect"
                        self._graph.forget(str(employee.id))
                await session.commit()

    async def _token(self, account: AIEmployeeAccount) -> str:
        token, rotated = await self._graph.access_token(
            str(account.employee_id), decrypt(self._settings, account.refresh_token_enc)
        )
        if rotated:
            account.refresh_token_enc = encrypt(self._settings, rotated)
        return token

    async def poll(self, session: AsyncSession, employee: AIEmployee, account: AIEmployeeAccount) -> None:
        token = await self._token(account)
        if self._settings.ai_teams_test_list:
            await self.poll_test(session, employee, account, token)
            return
        marks: dict[str, str] = dict(account.watermarks or {})
        start = account.connected_at
        for chat in await self._graph.recent_chats(token):
            chat_id = chat.get("id")
            preview = chat.get("lastMessagePreview") or {}
            latest = when(preview.get("createdDateTime"))
            if not chat_id or latest is None:
                continue
            since = when(marks.get(chat_id)) or start
            if latest <= since:
                # Chats come most recently active first: past this one, nothing is new.
                break
            group = chat.get("chatType") != "oneOnOne"
            if group and self._settings.ai_teams_test_list:
                # Test lock: a group reply is read by everyone in the group.
                marks[chat_id] = preview.get("createdDateTime")
                continue
            messages = await self._graph.messages(token, chat_id)
            waiting = False
            for burst in bursts(new_messages(messages, since=since, account_id=account.entra_object_id, group=group)):
                if not await self.answer(session, employee, account, token, chat, burst):
                    waiting = True
                    break
                marks[chat_id] = burst[-1]["createdDateTime"]
                account.watermarks = dict(marks)
                await session.commit()
            if not waiting:
                marks[chat_id] = preview.get("createdDateTime")
        account.watermarks = marks
        account.last_poll_at = datetime.now(UTC)
        account.error = None

    async def poll_test(
        self, session: AsyncSession, employee: AIEmployee, account: AIEmployeeAccount, token: str
    ) -> None:
        """Under the test lock: read ONLY the one-to-one chat with each test sender.

        No chat list, no previews, no other chat is touched — the account may be
        a real person's, and their other conversations are none of this app's
        business. A one-to-one chat's id is made of its two members' object
        ids, so it is addressed directly; the order of the two is found once
        and remembered.
        """
        marks: dict[str, str] = dict(account.watermarks or {})
        senders = (
            await session.scalars(
                select(User.entra_object_id).where(
                    func.lower(User.email).in_(sorted(self._settings.ai_teams_test_list)),
                    User.is_active.is_(True),
                )
            )
        ).all()
        me = account.entra_object_id
        for oid in senders:
            if not oid or oid == me:
                continue
            key = f"_chat:{oid}"
            chat_id = marks.get(key)
            if not chat_id:
                for candidate in (f"19:{me}_{oid}@unq.gbl.spaces", f"19:{oid}_{me}@unq.gbl.spaces"):
                    if await self._graph.chat_exists(token, candidate):
                        chat_id = candidate
                        marks[key] = candidate
                        break
            if not chat_id:
                continue  # they have never chatted one-to-one; nothing to read
            since = when(marks.get(chat_id)) or account.connected_at
            chat = {"id": chat_id, "chatType": "oneOnOne"}
            messages = await self._graph.messages(token, chat_id)
            waiting = False
            for burst in bursts(new_messages(messages, since=since, account_id=me, group=False)):
                if not await self.answer(session, employee, account, token, chat, burst):
                    waiting = True
                    break
                marks[chat_id] = burst[-1]["createdDateTime"]
                account.watermarks = dict(marks)
                await session.commit()
            newest = max((m.get("createdDateTime") or "" for m in messages), default="")
            if not waiting and newest and (when(newest) or since) > since:
                marks[chat_id] = newest
        account.watermarks = marks
        account.last_poll_at = datetime.now(UTC)
        account.error = None

    async def answer(
        self,
        session: AsyncSession,
        employee: AIEmployee,
        account: AIEmployeeAccount,
        token: str,
        chat: dict[str, Any],
        burst: list[dict[str, Any]],
    ) -> bool:
        """Answer one burst. False means "not yet": leave it for the next look."""
        chat_id = chat["id"]
        message = burst[-1]
        kind = "one-to-one" if chat.get("chatType") == "oneOnOne" else (chat.get("chatType") or "chat")
        who = ((message.get("from") or {}).get("user") or {}).get("displayName") or "someone"
        texts = (bridge.plain_text((m.get("body") or {}).get("content") or "") for m in burst)
        words = "\n".join(text for text in texts if text)
        if not words:
            return True
        person = await session.scalar(
            select(User).where(User.entra_object_id == sender_id(message), User.is_active.is_(True))
        )
        if not allowed(self._settings, person, chat):
            # Test lock: not one of the test senders, or not a one-to-one chat.
            # Nothing is sent — not even a refusal.
            logger.info("Teams: %s ignored a message outside the test list", employee.name)
            record(account, "ignored", sender=who, chat=kind, detail="Outside the test lock: not a listed sender, or not a one-to-one chat. Nothing was sent.")
            return True
        if person is None:
            await self._graph.send(
                token,
                chat_id,
                bridge.to_html(
                    "I can only help people who have signed in to the Hamdaz app at least once. "
                    f"Sign in at {self._settings.frontend_url} and message me again."
                ),
            )
            record(account, "answered", sender=who, chat=kind, detail="Not signed in to the app; told to sign in once.")
            return True

        row = await session.scalar(
            select(TeamsChat).where(
                TeamsChat.employee_id == employee.id,
                TeamsChat.teams_chat_id == chat_id,
                TeamsChat.user_id == person.id,
            )
        )
        if row is None:
            row = TeamsChat(
                employee_id=employee.id, teams_chat_id=chat_id, chat_type=chat.get("chatType"), user_id=person.id
            )
            session.add(row)
        row.last_message_at = datetime.now(UTC)

        if words.lower().strip(" .!") in ("new", "start over", "new chat", "/new"):
            row.assistant_conversation_id, row.pending_run_id = None, None
            await self._graph.send(token, chat_id, bridge.to_html("Starting a fresh conversation. What can I do for you?"))
            return True

        if row.assistant_conversation_id is None:
            created = await self._caller.new_conversation(person.id, employee.id)
            if isinstance(created, str):
                await self._graph.send(token, chat_id, bridge.to_html(f"Sorry — {created}"))
                record(account, "error", sender=who, chat=kind, detail=f"Could not start a conversation: {created}")
                return True
            row.assistant_conversation_id = created
            await session.flush()

        if row.pending_run_id is not None:
            decision = bridge.yes_or_no(words)
            if decision is None:
                await self._graph.send(
                    token,
                    chat_id,
                    bridge.to_html(
                        "I'm waiting on your **yes** or **no** for the change I asked about. "
                        "Say **new** to drop it and start over."
                    ),
                )
                return True
            turn = await self._caller.decide(
                person.id, row.assistant_conversation_id, str(row.pending_run_id), decision
            )
            row.pending_run_id = None
        else:
            turn = await self._caller.say(person.id, row.assistant_conversation_id, words)
            if turn.status == 409 and turn.error and "still working" in turn.error:
                # The previous turn is still going: this burst waits for the
                # next look rather than getting an error back.
                record(account, "waiting", sender=who, chat=kind, detail="Previous answer still in progress; will answer next.")
                return False

        if turn.confirm and turn.run_id:
            row.pending_run_id = turn.run_id
        await self._graph.send(token, chat_id, bridge.to_html(bridge.reply_for(turn)))
        outcome = "error" if turn.error else ("asked" if turn.confirm else "answered")
        detail = turn.error or (bridge.reply_for(turn)[:240])
        record(account, outcome, sender=who, chat=kind, detail=f'"{words[:120]}" → {detail}')
        return True
