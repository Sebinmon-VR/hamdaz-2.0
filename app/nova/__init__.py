"""Nova in Teams: the presales assistant people chat with.

A Teams bot (an Azure Bot, single tenant — the app is uploaded to Hamdaz's
Teams only, never the public store). The pieces:

* ``bot`` — the line to Teams: checks each incoming message is the bot
  service writing to our bot, and sends replies and the typing dots.
* ``chat`` — one message start to finish: who asks, may they, the reply, the
  memory.
* ``brain`` — what they may see, what the data says, and the words: the data
  by plain queries first, a fast model (free ones first) to word the reply.
* ``router`` — ``POST /api/v1/nova/messages``, the endpoint the Azure Bot
  points at.

Read only: Nova changes nothing anywhere. Off unless NOVA_CHAT_ENABLED and the
bot's id and secret are set; open to NOVA_ALLOWED_EMAILS (the trial: Sebin).
Not yet: being added to new tasks, asking for requirement files, finding
distributors — the steps the user planned after the chat (2026-11-03).
"""
