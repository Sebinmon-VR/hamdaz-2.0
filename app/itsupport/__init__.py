"""IT Support in Teams: an AI agent that chats as the IT Support account.

The user's plan (2026-11-11): not a bot. People message **IT Support**
(itsupport@hamdaz.com) in Teams like any colleague; this backend reads the
message through Graph, the model writes the answer, and it goes back through
Graph *as IT Support*. First a plain chat with no company data, tried with
Sebin only from a laptop, to see how fast and how human it is; data later.

* ``graph`` — the account's own delegated sign-in (kept encrypted in a local
  file, ``python -m app.itsupport.connect`` makes it) and the chat calls.
* ``talk`` — how it writes, and the reply.
* ``worker`` — the loop: check each allowed person's chat with the account
  every IT_SUPPORT_POLL_SECONDS, answer what is new.
* ``connect`` — the one-time sign-in, run on the laptop.

Only the people in IT_SUPPORT_ALLOWED_EMAILS are answered (the trial: Sebin);
anyone else's chat with IT Support is never read past its existence and never
answered. Off unless IT_SUPPORT_CHAT_ENABLED.

Graph has no typing dots and no streaming for a person's account, and a laptop
cannot be told of new messages, so the wait is: up to one poll, the model's
first-to-last words, and the send.
"""
