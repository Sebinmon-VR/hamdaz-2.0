"""Write one event into a person's Outlook calendar, move it, or take it out.

Application permission (``Calendars.ReadWrite``), so the event goes into the
person's own calendar without their signing in. Every event this writes is a
task's bid closing time, shown as *free* so it never blocks a meeting, with
Outlook's own reminder set ahead of it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Final

import httpx

from app.core.config import Settings

GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
_REFRESH_BUFFER: Final = 300

#: How long the event lasts. The BCD is a moment; half an hour reads as one.
EVENT_LENGTH: Final = timedelta(minutes=30)


class TaskCalendarError(Exception):
    pass


class EventGone(TaskCalendarError):
    """The event is no longer in the calendar — deleted by the person."""


class TaskCalendar:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        response = await self._http.post(
            f"{self._settings.authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "grant_type": "client_credentials",
                "scope": GRAPH_SCOPE,
            },
        )
        if response.status_code != 200:
            raise TaskCalendarError(f"token request failed ({response.status_code})")
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600)) - _REFRESH_BUFFER
        return self._token

    async def _call(self, method: str, url: str, json: dict | None = None) -> httpx.Response:
        token = await self._access_token()
        response = await self._http.request(
            method, url, json=json, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 404:
            raise EventGone("The event is not in the calendar any more.")
        if response.status_code >= 400:
            raise TaskCalendarError(
                f"Outlook refused the calendar change ({response.status_code}): {response.text[:300]}"
            )
        return response

    @staticmethod
    def event_body(
        *, subject: str, starts: datetime, html: str, reminder_minutes: int
    ) -> dict[str, Any]:
        ends = starts + EVENT_LENGTH
        return {
            "subject": subject[:255],
            "body": {"contentType": "HTML", "content": html},
            "start": {"dateTime": starts.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {"dateTime": ends.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            # A deadline, not a meeting: it must not make them look busy.
            "showAs": "free",
            "isReminderOn": True,
            "reminderMinutesBeforeStart": reminder_minutes,
            "responseRequested": False,
        }

    async def create(self, owner: str, body: dict[str, Any]) -> str:
        """The new event's id. ``owner`` is the person's Entra id or address."""
        response = await self._call("POST", f"{GRAPH_BASE}/users/{owner}/events", body)
        return response.json()["id"]

    async def update(self, owner: str, event_id: str, body: dict[str, Any]) -> None:
        await self._call("PATCH", f"{GRAPH_BASE}/users/{owner}/events/{event_id}", body)

    async def delete(self, owner: str, event_id: str) -> None:
        try:
            await self._call("DELETE", f"{GRAPH_BASE}/users/{owner}/events/{event_id}")
        except EventGone:
            pass  # already gone: the outcome wanted
