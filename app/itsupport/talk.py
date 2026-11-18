"""How IT Support writes, and its reply to a chat.

What a reply is made of: the person's first name, the time in the UAE, the
chat itself as Graph gives it (short-term memory), the notes kept about them
(``memory``, long-term) and their open tasks on the Proposals list (``work``).
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.core.llm import Spoken, TextModel

DUBAI: Final = ZoneInfo("Asia/Dubai")
IST: Final = ZoneInfo("Asia/Kolkata")

#: When no model answered at all.
STUCK: Final = "Sorry, I'm a bit stuck right now. Give me a minute and try again?"

SYSTEM: Final = """\
You are IT Support at Hamdaz, chatting with a colleague in Microsoft Teams from the IT Support
account.

How you write:
- Like a person typing in a chat: short, relaxed, plain words, contractions. Usually one to
  three sentences.
- Only the first message of a chat starts with a greeting; after that, get straight to it.
  Use their first name now and then, not in every message.
- Answer first. If you need more to fix something, ask one question, not five.
- Troubleshooting goes one or two steps at a time: say what to try, then ask how it went.
  Never a long numbered list of everything that might work.
- No headings, no tables, no bold labels. Never say "As an AI", "Great question",
  "I hope this helps" or "Let me know if you have any other questions". Don't repeat their
  question back to them. An emoji only rarely.
- Reply in the language they write in, and match their tone.

What you can and can't do:
- You can explain, guide and troubleshoot: Windows, Microsoft 365 (Outlook, Teams, OneDrive,
  SharePoint, Excel), printers, Wi-Fi and VPN, phones, browsers, common business software,
  and general tech questions. Drafting a message or explaining a term is fine too.
- You can find suppliers and distributors for a product: when they ask ("find Zebra ZD421
  distributors in UAE"), a search of our past supplier quotes and the web runs and posts
  its list in the chat by itself.
- You can see their open tasks on the Proposals list (shown below), read only. Use them to
  understand what they mean ("the Zebra one"), but you can't change a task, its status or
  its BCD; they do that on the list.
- BCD times are UAE time. Part of the team works on India time (IST, 1h30 ahead of UAE),
  so whenever you mention a time, give both, as in "5:00 PM UAE (6:30 PM IST)". The task
  list below already has both; copy them, don't work them out.
- You can't see or change anything else: no accounts, devices, licences, passwords or
  tickets. When something needs an admin (a password reset, access, a licence, new
  hardware), say so plainly.
- You don't know Hamdaz's own setup (Wi-Fi names, printer addresses, policies, who is who).
  Never make it up; say you don't have that yet.
- Never offer to do something you can't (look someone up, open a ticket, check later).
- You can't open files or images yet; if they send one, ask them to describe it in words.
- If they sincerely ask whether you're a person or a bot, be honest: you're an AI assistant
  answering for IT Support.
"""

_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr)>|<li[^>]*>", re.IGNORECASE)
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def plain(message: dict[str, Any]) -> str:
    """A Teams message's words: no HTML, entities undone; a file or picture
    with no words said as such."""
    body = (message.get("body") or {}).get("content") or ""
    # Every block ends a line: paragraphs, list points, headings, rows.
    lines = _BREAK.sub("\n", body)
    text = html.unescape(_TAG.sub(" ", lines))
    text = re.sub(r"[ \t ]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    if not text and (message.get("attachments") or "<img" in body):
        return "(sent a file or a picture)"
    return text


#: A link in a reply: "[📞 Call Krishnendu](https://…)" or a bare https://….
_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+)\)|(https?://[^\s<>\"]+)")
#: What ends a sentence, not a bare link.
_TRAILING = ".,;:!?)]'\""


def _text_html(text: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", html.escape(text)).replace("\n", "<br>")


def to_html(text: str) -> str:
    """The reply as Teams shows it: escaped, **bold** kept, lines kept, and
    links clickable: "[label](url)" as the label, a bare address as itself
    (Teams doesn't make a link of text in a message sent through Graph; the
    user, 2026-11-18: "at least make the link clickable")."""
    out, last = [], 0
    for found in _LINK.finditer(text):
        if found.group(2):
            label, url, end = found.group(1), found.group(2), found.end()
        else:
            url = found.group(3).rstrip(_TRAILING)
            label, end = url, found.start(3) + len(url)
        out.append(_text_html(text[last:found.start()]))
        out.append(f'<a href="{html.escape(url, quote=True)}">{_text_html(label)}</a>')
        last = end
    out.append(_text_html(text[last:]))
    return "".join(out)


def first_name(settings, email: str, display_name: str | None) -> str:
    """What to call them: IT_SUPPORT_NAMES if it names them (Teams says
    "Sri Vishnu"; he is Vishnu), else the first word of their Teams name."""
    for pair in settings.it_support_names.split(","):
        who, _, name = pair.partition("=")
        if who.strip().casefold() == email.strip().casefold() and name.strip():
            return name.strip()
    return (display_name or email).split()[0]


def system_for(first_name: str, now: datetime, *, notes: str = "", context: str = "") -> str:
    """The instructions: how to write, who this is, the time, what you
    remember about them and their open tasks."""
    out = (f"{SYSTEM}\nYou're chatting with {first_name}. It's "
           f"{now.astimezone(DUBAI):%A %d %B %Y, %H:%M} UAE "
           f"({now.astimezone(IST):%H:%M} IST).")
    if notes:
        out += f"\n\nWhat you remember about {first_name} from earlier chats:\n{notes}"
    if context:
        out += f"\n\n{first_name}'s open tasks:\n{context}"
    return out


def turns(history: list[dict[str, Any]], me_id: str) -> list[dict[str, str]]:
    """The chat, oldest first, as the model takes it: the account's own
    messages are the assistant's, everyone else's the person's."""
    out = []
    for m in history:
        if m.get("messageType") != "message" or m.get("deletedDateTime"):
            continue
        sender = ((m.get("from") or {}).get("user") or {}).get("id")
        text = plain(m)
        if text:
            out.append({"role": "assistant" if sender == me_id else "user", "content": text})
    return out


async def reply(
    model: TextModel, *, first_name: str, history: list[dict[str, Any]], me_id: str,
    now: datetime, order: str, notes: str = "", context: str = "",
) -> Spoken:
    """The whole reply (Graph can't show it as it is written)."""
    spoken = Spoken()
    async for _ in model.chat(
        system=system_for(first_name, now, notes=notes, context=context),
        messages=turns(history, me_id), spoken=spoken,
        order=order, max_tokens=800, label="it support chat",
    ):
        pass
    if not spoken.text.strip():
        spoken.text = STUCK
    return spoken
