"""One message to Nova, start to finish.

The bot service gets its 200 at once (the router hands the message here as a
background task), so Teams never waits on us. Then: the typing dots — kept up
every few seconds until the reply is ready — who is asking (by their Entra
object id, against our users), whether they may use Nova yet, the data, the
reply, and the memory. A person's messages are answered one at a time, so a
burst of three reads the conversation in order.

Only 1:1 chats for now: what Nova may say depends on who asks, and a group
chat has more than one asker.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.llm import TextModel
from app.models.nova import NovaMessage, NovaPerson
from app.models.user import User
from app.nova import brain
from app.nova.bot import BotSendError, TeamsBot

logger = logging.getLogger("hamdaz.nova")

#: Teams drops the typing dots after a few seconds; they are sent again this
#: often while a reply is being worked out.
TYPING_EVERY_SECONDS = 3.0

GREETING = (
    "Hi {name}! I'm Nova, the presales team's assistant. Ask me about any tender or "
    "quote number, your tasks, or what's closing this week."
)


class NovaChat:
    def __init__(
        self,
        settings: Settings,
        bot: TeamsBot,
        model: TextModel,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.bot = bot
        self._model = model
        self._factory = factory
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    @property
    def ready(self) -> bool:
        return self.settings.nova_chat_enabled and self.bot.configured

    def allowed(self, email: str) -> bool:
        listed = {
            e.strip().lower() for e in self.settings.nova_allowed_emails.split(",") if e.strip()
        }
        return "*" in listed or email.lower() in listed

    def is_for_me(self, activity: dict[str, Any]) -> bool:
        """A conversation update saying Nova itself was just added — the app
        installed for someone."""
        me = (activity.get("recipient") or {}).get("id")
        return any(m.get("id") == me for m in activity.get("membersAdded") or [])

    # ── handlers ────────────────────────────────────────────────────────

    async def on_message(self, activity: dict[str, Any]) -> None:
        service_url = activity.get("serviceUrl") or ""
        conversation = activity.get("conversation") or {}
        conversation_id = conversation.get("id") or ""
        if not service_url or not conversation_id:
            return
        received = datetime.now(UTC)
        started = time.monotonic()
        typing = asyncio.create_task(self._keep_typing(service_url, conversation_id))
        try:
            if conversation.get("conversationType") not in (None, "personal"):
                await self._say(
                    service_url, conversation_id,
                    "I only chat one to one for now. Message me directly and I'll help.",
                )
                return
            async with self._factory() as session:
                user = await self._user(session, activity)
                refusal = self._refusal(user)
                if refusal:
                    await self._say(service_url, conversation_id, refusal)
                    return
                assert user is not None
                message = brain.clean(activity.get("text") or "")
                if not message:
                    await self._say(service_url, conversation_id,
                                    "I can't open files yet. Tell me in words what you're after.")
                    return
                async with self._locks.setdefault(user.id, asyncio.Lock()):
                    person = await self._person(session, user, service_url, conversation_id)
                    viewer = await brain.viewer_of(session, user)
                    past = await brain.history(
                        session, user.id, self.settings.nova_history_messages
                    )
                    now = brain.now_in_dubai()
                    found = await brain.find(session, viewer, message, now.date())
                    text, written_by = await brain.reply(
                        self._model, viewer, person.summary, past, found, message,
                        now=now, seconds=self.settings.nova_reply_seconds,
                    )
                    typing.cancel()
                    await self._say(service_url, conversation_id, text)
                    took = int((time.monotonic() - started) * 1000)
                    session.add_all([
                        NovaMessage(user_id=user.id, author="person", text=message[:4000],
                                    created_at=received),
                        NovaMessage(user_id=user.id, author="nova", text=text,
                                    written_by=written_by, took_ms=took,
                                    created_at=datetime.now(UTC)),
                    ])
                    await session.commit()
                    logger.info("nova: answered %s in %d ms (%s)", user.email, took, written_by)
                    # After the reply is out: the summary of older messages.
                    await brain.fold_old(
                        session, self._model, person, self.settings.nova_history_messages
                    )
                    await session.commit()
        except Exception:  # noqa: BLE001 - a person must hear something
            logger.exception("nova: a message failed")
            try:
                await self._say(service_url, conversation_id,
                                "Sorry, something went wrong on my side. Try again in a moment.")
            except Exception:  # noqa: BLE001 - nothing more to do
                pass
        finally:
            typing.cancel()

    async def on_added(self, activity: dict[str, Any]) -> None:
        """Nova was installed for someone: say hello, and keep the address of
        their chat so Nova can write to them first, later."""
        service_url = activity.get("serviceUrl") or ""
        conversation_id = (activity.get("conversation") or {}).get("id") or ""
        if not service_url or not conversation_id:
            return
        try:
            async with self._factory() as session:
                user = await self._user(session, activity)
                refusal = self._refusal(user)
                if refusal:
                    await self._say(service_url, conversation_id, refusal)
                    return
                assert user is not None
                await self._person(session, user, service_url, conversation_id)
                await session.commit()
                await self._say(service_url, conversation_id,
                                GREETING.format(name=user.display_name.split()[0]))
        except Exception:  # noqa: BLE001 - a greeting is not worth a crash
            logger.exception("nova: greeting failed")

    # ── parts ───────────────────────────────────────────────────────────

    async def _user(self, session: AsyncSession, activity: dict[str, Any]) -> User | None:
        object_id = (activity.get("from") or {}).get("aadObjectId")
        if not object_id:
            return None
        return await session.scalar(select(User).where(User.entra_object_id == object_id))

    def _refusal(self, user: User | None) -> str | None:
        if user is None or not user.is_active:
            return ("Hi! I don't know you yet. Sign in to the Hamdaz app once, "
                    "then message me again.")
        if not self.allowed(user.email):
            return (f"Hi {user.display_name.split()[0]}! I'm still being tested, "
                    "so I'm not open to you yet. I'll be around soon.")
        return None

    async def _person(
        self, session: AsyncSession, user: User, service_url: str, conversation_id: str
    ) -> NovaPerson:
        person = await session.get(NovaPerson, user.id)
        if person is None:
            person = NovaPerson(user_id=user.id)
            session.add(person)
        person.service_url, person.conversation_id = service_url, conversation_id
        return person

    async def _say(self, service_url: str, conversation_id: str, text: str) -> None:
        await self.bot.say(service_url, conversation_id, text)

    async def _keep_typing(self, service_url: str, conversation_id: str) -> None:
        try:
            while True:
                try:
                    await self.bot.typing(service_url, conversation_id)
                except BotSendError as exc:
                    logger.info("nova: typing not shown: %s", exc)
                    return
                await asyncio.sleep(TYPING_EVERY_SECONDS)
        except asyncio.CancelledError:
            pass
