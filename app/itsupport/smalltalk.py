"""Small talk, answered the way a colleague would: by rules, no model.

The user (2026-11-17), after "hi" got "Hey Sebin, I'm here. Anything you need
on the Hamdaztech costing or the Neewer searches?": "in the company who asks
like this, make it like a colleague". A colleague answers "hi" with "hi, tell
me", not a status report, and a model shown the chat keeps reaching back into
it. So a message that is only a greeting, a test, a thanks, an ok or a bye
never reaches a model: it gets one short line from here, varied, never the
line used last.

Only when nothing is waiting on their answer: an "ok" to a plan is a yes, and
the worker checks that first.
"""

from __future__ import annotations

import random
import re
from typing import Final

_GREETING: Final = frozenset({
    "hi", "hii", "hiii", "hai", "hello", "helo", "hlo", "hey", "heyy", "hola", "yo",
    "gm", "morning", "good", "afternoon", "evening", "there", "you", "u", "r", "are",
    "test", "testing", "tst", "ping", "salam", "assalamualaikum", "namaste",
})
_THANKS: Final = frozenset({"thanks", "thank", "thankyou", "thx", "tq", "ty", "tnx", "thnx",
                            "you", "u", "so", "much", "a", "lot", "bro", "da"})
_OK: Final = frozenset({"ok", "okay", "okk", "okey", "k", "kk", "fine", "noted", "sure", "cool",
                        "great", "good", "nice", "alright", "done", "got", "it", "👍", "bro", "da"})
_BYE: Final = frozenset({"bye", "byee", "see", "you", "u", "later", "good", "night", "gn", "tc",
                         "take", "care", "cya"})

#: {name} is their first name; a line without it is fine too.
GREET: Final = ("Hi {name}, tell me", "Hey {name}, tell me", "Hi {name} 👋", "Hey, yes?",
                "Hi {name}, what's up?", "Yes {name}, tell me", "Hey {name}!")
MORNING: Final = ("Good morning {name}!", "Morning {name} 👋", "Good morning {name}, tell me")
AFTERNOON: Final = ("Good afternoon {name}!", "Afternoon {name}, tell me")
EVENING: Final = ("Good evening {name}!", "Evening {name}, tell me")
THANKS: Final = ("Anytime!", "No problem 👍", "Sure, anytime", "👍", "Happy to help")
OK: Final = ("👍", "Okay 👍", "Cool", "Sure")
BYE: Final = ("Bye {name}!", "See you {name} 👋", "Take care!", "Bye, have a good one")


def _words(text: str) -> list[str]:
    text = text.casefold().replace("👋", " ").replace("🙂", " ").replace("😊", " ")
    return [w for w in re.findall(r"[\w👍]+", text) if not w.isdigit()]


_GREET_CORE: Final = frozenset({
    "hi", "hii", "hiii", "hai", "hello", "helo", "hlo", "hey", "heyy", "hola", "yo", "gm",
    "morning", "afternoon", "evening", "there", "test", "testing", "tst", "ping", "salam",
    "assalamualaikum", "namaste",
})
_THANKS_CORE: Final = frozenset({"thanks", "thank", "thankyou", "thx", "tq", "ty", "tnx", "thnx"})
_BYE_CORE: Final = frozenset({"bye", "byee", "night", "gn", "later", "care", "cya"})


def kind(text: str) -> str | None:
    """greeting / morning / afternoon / evening / thanks / ok / bye, or None
    when the message says anything more than that."""
    words = _words(text)
    if not words or len(words) > 6:
        return None
    have = set(words)
    if have & _THANKS_CORE and have <= _THANKS | _OK:
        return "thanks"
    if have & _BYE_CORE and have <= _BYE:
        return "bye"
    if have & _GREET_CORE and have <= _GREETING:
        for part in ("morning", "afternoon", "evening"):
            if part in have or (part == "morning" and "gm" in have):
                return part
        return "greeting"
    if have <= _OK:
        return "ok"
    return None


_LINES: Final = {"greeting": GREET, "morning": MORNING, "afternoon": AFTERNOON,
                 "evening": EVENING, "thanks": THANKS, "ok": OK, "bye": BYE}


def reply(text: str, first_name: str, recent: list[str]) -> str | None:
    """The line to send, or None for the model. ``recent``: what IT Support
    said lately, so the same line isn't used twice running."""
    found = kind(text)
    if found is None:
        return None
    lines = [line.format(name=first_name).replace("  ", " ") for line in _LINES[found]]
    said = {r.strip().casefold() for r in recent}
    fresh = [line for line in lines if line.casefold() not in said]
    return random.choice(fresh or lines)
