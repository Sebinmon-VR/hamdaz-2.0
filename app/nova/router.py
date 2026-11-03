"""The one door Teams knocks on: ``POST /api/v1/nova/messages``.

Public, and holding no session dependency: the bot service has no cookie. What
admits a message is the bot service's signature on it (:meth:`TeamsBot.verify`),
checked before anything in it is read as true. The answer is worked out after
the 200 goes back, so Teams never waits on a model.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.nova.bot import BotAuthError
from app.nova.chat import NovaChat

logger = logging.getLogger("hamdaz.nova")

router = APIRouter(prefix="/nova", tags=["nova"])


@router.post("/messages", include_in_schema=False)
async def bot_messages(request: Request, background: BackgroundTasks) -> Response:
    nova: NovaChat | None = getattr(request.app.state, "nova", None)
    if nova is None or not nova.ready:
        # Off, or no bot registered here: nothing to say, nothing to retry.
        return Response(status_code=200)
    try:
        activity = await request.json()
    except ValueError:
        return Response(status_code=400)
    if not isinstance(activity, dict):
        return Response(status_code=400)
    try:
        await nova.bot.verify(
            request.headers.get("Authorization"),
            service_url=activity.get("serviceUrl"),
            channel_id=activity.get("channelId"),
        )
    except BotAuthError as exc:
        logger.warning("nova: refused a message: %s", exc)
        return Response(status_code=401)
    kind = activity.get("type")
    if kind == "message":
        background.add_task(nova.on_message, activity)
    elif kind == "conversationUpdate" and nova.is_for_me(activity):
        background.add_task(nova.on_added, activity)
    return Response(status_code=200)
