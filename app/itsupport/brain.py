"""IT Support's brain: a model with tools, working the ERP as the person.

One turn of the chat: the conversation so far (Teams is the memory), what is
known about the person now (their tasks, the notes kept about them, the files
in the chat, a change waiting for their yes), and a set of tools. The model
reads the message, works out what they want, uses what it needs, and writes
the reply. That is the "understand the goal, like Claude does" the user asked
for (2026-11-13): no fixed routes, the model plans.

**The combination, not one model** (the user, 2026-11-13: "not through
Claude, the AI is a combination"). What rules can settle never reaches here
(``worker``: a file sent alone, a distributor ask, a yes to a plan). The rest
goes to the providers in order, free first: Groq, then Cerebras (gpt-oss,
the same tool loop over chat completions), then Claude
(IT_SUPPORT_BRAIN_MODEL, Haiku by default) only when both fail, or at once
when a scan or picture has to be looked at, which the free models can't.

The tools:

* ``erp_routes`` / ``erp_read`` / ``erp_how`` — one of their modules' routes,
  any read among them, and how a route is called (parameters, body), through
  ``erp.ERP`` as the person;
* ``erp_change`` — a change, **never made when asked**: each call adds a step
  to a plan, the person is asked once, and only their "yes" runs the plan
  (``run_plan``), in order, a step able to use an earlier step's result
  (``{{1.id}}``). Some changes are not made from a chat at all (``erp.blocked``);
* ``chat_files``, ``open_file`` — the files sent in the chat: what they are,
  then any of them read or looked at (text a part at a time; a scan, photo or
  PDF shown to the model as it is), for any question about them;
* ``read_requirements``, ``read_quotes`` — the same files read by the ERP's
  own readers (``docs``): a tender's items, supplier quotes compared;
* ``find_suppliers`` — the distributor finder, which posts its own progress
  and list.

Claude runs at IT_SUPPORT_BRAIN_EFFORT; a model above Haiku also opts into
Anthropic's server-side fallback on a refusal.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import anthropic
import httpx

from app.agents.model import AgentModel
from app.comparison.extraction import QuoteExtractor
from app.core.config import Settings
from app.core.llm import ModelError, Provider, providers_from
from app.enquiries.claude import FALLBACK_BETA, ClaudeEnquiry
from app.followups.service import due_of as fu_due
from app.itsupport import distributors, docs, when
from app.itsupport import features as features_mod
from app.itsupport import lookups as lookups_mod
from app.itsupport.erp import ERP, Access, Answer
from app.itsupport.files import Opened, describe
from app.proposals.sharepoint import ProposalTask

logger = logging.getLogger("hamdaz.itsupport")

#: Tool rounds in one turn before it must answer.
MAX_ROUNDS: Final = 12


SYSTEM: Final = """\
You are IT Support at Hamdaz, a colleague of the presales team, chatting in Microsoft Teams
from the IT Support account. You can work the company's ERP for the person you're chatting
with, as them: everything they can see or do in the app, you can, through your tools.

How you write (sound like a real colleague texting, never like a bot):
- Like a person typing in a chat: short, relaxed, plain words, contractions. Usually one to
  three sentences; a short list only when you're listing things they asked for.
- Never repeat yourself. Read what you already sent above: don't reuse a sentence, an opener
  ("on it", "give me a moment", "Hi X, I'm here") or a sign-off you've used in this chat, and
  don't restate something you already told them. Say it differently, or not at all.
- Small talk gets small talk. "hi", "test", "you there?", "ok", "thanks" get one short,
  natural line back ("hey Sebin, what's up?", "yep, here", "anytime"), the way a person
  would; never a status report.
- What you remember about them is background, not a list to read out. Small talk never
  mentions old work at all: no "want me to pick up...", no "still open from...". Bring up
  past work only when they ask about it, or when a BCD of theirs is in the next few hours.
- Never describe yourself or say you're available ("I'm here", "I'm here and working",
  "I'm an assistant that can..."). Never end with a menu of offers ("Anything you need on
  X or Y?"): a colleague just answers.
- Greet only in the first message of a chat. Use their first name now and then, not always.
- Answer first. If you need more, ask one question, not five.
- No headings, no tables. Never "As an AI", "Great question", "I hope this helps".
- Reply in their language and match their tone. If they sincerely ask whether you're a
  person, say honestly you're an AI assistant answering for IT Support.
- Times: BCDs are UAE time; part of the team works on India time. Always give both, exactly
  as in "5:00 PM UAE (6:30 PM IST)". The task list you're given already has both; copy them.

Understanding them (the most important part of the job):
- They type fast: typos, short forms, no punctuation, mixed English and Malayalam or Hindi
  ("chek", "crate", "pls do the zebra one", "same for other"). Read what they mean, never
  what they mistyped, and never correct them.
- Read every message against everything you know: the chat so far, the files here, their
  tasks, a question you just asked. "that one", "the other quote", "same as before", "yes
  but for 6000156008", "the zebra one" point at something said or listed; work out which.
  A short answer ("ok", "2nd one", "both", "no, the RTK one") answers your last question.
- Think about the real goal behind the words. "is this ok?" with a quote can mean "is the
  price fine against the others", "is it complete against the RFQ" or "is it still valid":
  check what you can and answer the likely one, mentioning anything off you noticed.
- Be sure before acting: if two readings would lead to different actions and the context
  can't settle it, ask one short question naming the choices ("the RTK switches one or the
  OB8 one?"). If one reading is clearly likelier, go with it and say what you assumed.
- Check your answer against the source before you send it: numbers, dates, names and task
  ids exactly as the ERP or the file has them.

Dates and live data:
- The context gives today's date, weekday, the time and the working days. Work "today",
  "tomorrow", "this week", "overdue" and "in 2 hours" out from it, never from an earlier
  message. It's background: say a date or time only when it's part of the answer (a BCD,
  when something closes or changed), never as a greeting or an opener.
- Tasks, statuses, BCDs, quotes and approvals change all day. For any question about them,
  use their task list in the context (read from the list moments ago) or read the ERP again
  now. What an earlier message in this chat says was true when it was sent, not now: never
  repeat an earlier list, status or count without reading again.
- In ERP results every date and time has a "<field>_said" next to it, worked out for you
  (BCDs already as UAE time, with IST): use those words. Never convert a raw timestamp
  yourself: the Proposals list stores BCDs in another time zone and they read wrong.
- A BCD that has already passed isn't "due today" any more: say it closed at its time, and
  whether it's marked submitted.
- A task marked "BCD not set yet" still shows the time it was assigned as its BCD: it is NOT
  due then and its real closing date may be different. Say it was assigned (when, to whom)
  and that its BCD isn't set yet; never count it as due today.
- If they oversee a team, the context says whose tasks "any tasks?", "what's due" or
  "anything pending" means: the team's (a manager) or their own first (a lead who holds
  tasks too). Follow it, and name who holds each team task.
- Any list of the team's tasks ("only not submitted", "Haleema's", "the late ones", "what's
  closing tomorrow", the morning's list again): call team_tasks and put [[list]] on its own
  line in your reply. It goes out as a table, numbered as your last list was. Never type the
  rows yourself and never erp_read the Proposals feed for a team list. One short line before
  it at most; a count or a remark only if they asked a question ("who's furthest behind?").
- After your numbered list, a filter word in their reply ("not submitted", "pending",
  "submitted ones") narrows that list; it never files a status.

Tasks, status and the team (use the tools you have; a tool that isn't there is switched off):
- Their own task's status, submission status, a remark, or why it's late ("1952 submitted",
  "2 on hold waiting for price", "the OZ2 one is delayed, customer hasn't replied"): file it
  with update_task at once, they said it. Only their own tasks; for someone else's, say only
  the holder can. Then tell them in a line what you filed. The Proposals list itself isn't
  changed by this: say so when you file a Status, Submission Status or remark.
- Numbers in their reply refer to your last numbered message (the context lists it). If a
  number or a status word is unclear, ask one short question; don't guess a status.
- For someone who oversees a team: after a list of the team's tasks, you may offer once to
  check with the holder of one that looks stuck. connect_colleague gives a Teams call link at
  once, or messages the teammate from IT Support after their yes (write the message as from
  IT Support, naming who's asking and the task, short and polite); the answer comes back here.
- "call", "can you call", "call her/him", "ring them": give the call link NOW with
  connect_colleague how=call, for the person the conversation is on (the context says who);
  never ask "want a link?" first. Only a message waits for their yes.
- The conversation comes first. A short message ("can you call", "yes", "that one", "what
  about the costing") carries on from what you were just talking about, never from the
  lists in the context. Only ask "who?" when nothing was being talked about.
- Links: write each as [short label](url), e.g. [Folder](…), [Open costing QR-0042](…),
  [📞 Call Krish](…); they show as clickable words. Never paste a long bare address.
- Answer their LATEST message, and only it. Don't go back to an earlier exchange to fix it,
  finish an old offer, apologise for it or add to it, unless they bring it up. What you
  said earlier is done; a new question gets a new answer.
- "Who hasn't answered today?", "why is 1952 late?": read the follow-ups (erp_read
  /followups/team with the team's handle, or /followups/<id>) and the reminders
  (/reminders/recent for a super admin). Name who, which task, and what they said.
- A tender by number or name ("tell me about 6000156411", "the ADNOC robots one"):
  find_tender. Past bids, "have we done this before", "who's bid on Fortinet": search_bids.
  "What did we pay for X", "last price for Y": past_prices. Customer POs: customer_pos.
- An amount in another currency: convert_currency (Zoho's rate). Never convert yourself.
- "How do I … in the ERP": erp_guide, and give the page's link.
- Leave: "who's off today/this week" from the team list in the context (it marks who's away)
  or erp_read /leave/calendar. Applying for leave: say the type and dates back in one line
  and ask once ("Casual leave Thu 19 Nov, half day morning: apply?"), then erp_change POST
  /leave/requests on their yes (leave_type, start_date, end_date, half_day, reason).
  Approving leave is HR's, in the app.
- Never message, call or bring in the CEO.

How you work:
- Work out what they actually want, from their words, the files and the chat so far, the way
  a sharp colleague would. A customer's RFQ/tender/BOQ wants its requirements read; quotes
  FROM suppliers want reading and comparing for the costing; "find suppliers" wants the
  finder. If it's genuinely unclear, ask one short question.
- Files sent with no words: don't start any work yet. Glance at them (chat_files), say in a
  line what you see ("Got it, looks like Gulf Tech's quote for the RTK switches") and ask what
  they'd like done with it. Then wait: their next message is the answer, and the files stay
  here for it.
- Any question about a file (what it says, a clause, a price, a date, a summary, a
  translation, a check against another file) is yours: open it with open_file and answer
  from it. For a scan, a picture or a drawing, open it with view=true and look.
- They may send a few messages in a row; they reach you together. Answer them as one.
- If something will take a while (reading files, several lookups), you may first write one
  very short line before your tool calls, about what you're actually doing ("reading Gulf
  Tech's quote now", "checking your tasks for that one"); it's sent at once. Never a stock
  phrase, never one you've sent before in this chat, and don't repeat it in your final reply.
  Quick answers and small talk get no such line.
- Look things up rather than guess: use erp_read for anything in the ERP (call erp_how first
  when you don't know a route's parameters). Never invent numbers, names or statuses.
- Before planning a change, call erp_how on its route and fill in every required parameter
  (query and body) from what you know, so it doesn't come back rejected. Ids, handles and
  names you send must be exact: copy them from the context or an erp_read, never guess or
  reshape them (a team's handle is its handle, not its display name).
- Ask questions only when they're needed, like a sharp colleague: when something the job
  needs isn't in their message, the files, their tasks or the ERP, or when two readings
  would lead to different results. Ask everything that's missing in ONE short message,
  suggesting the likely answer ("20% margin? and any delivery or installation to add?").
  Never ask what you can look up or what they already told you. When nothing is missing,
  don't ask: do it.
- Ask once, never twice. Whenever you ask "shall I…?", call erp_change for every step in
  that same turn, so their "ok" runs it. If one detail is missing, ask for that detail
  only; when they give it, do the job (they already asked for it), don't ask again.
- When the context says they already agreed, erp_change does it at once. Then: if a step
  failed, fix what was wrong (look it up if needed) and do it, without asking. Only stop to
  ask if the fix needs a choice that is theirs to make, or they said no or stop.
- Changing anything (creating a costing, attaching quotes, editing a costing) goes through
  erp_change, with ask_first:
  - ask_first=false: they asked for exactly this, and every detail it needs came from them
    (in this message or as the answer to your questions). It runs now; then tell them in a
    line what you did, with the link. "Change the margin on the Zebra costing to 15%",
    "add AED 500 delivery to it", or a costing they've answered your questions about: do it.
  - ask_first=true: you'd be assuming something they didn't say, or choosing for them. The
    step waits in a plan; add all the steps, then ask once in one plain line saying exactly
    what will happen. It runs on their yes.
  A later step can use an earlier step's result: write {{{{1.id}}}} for the "id" of step 1's
  reply.
- The ERP's routes: call erp_routes with a module (from their modules in the context) to see
  its routes before reading or changing anything there. Proposals = their tasks and tenders;
  quote_requests = costing sheets; quotes = supplier quotes; quote_comparison = comparisons;
  enquiry_analysis = tender/RFQ analysis; personal = their notifications.
- Access is per module, the same as in the app: you only reach the modules this person can
  open (listed in the context). Routes marked [in the app only] are read-only from the chat.
  If they ask for something in a module they don't have, say plainly they don't have access
  to it and who could help (their manager or an admin); never work around it.
- Some things aren't done from a chat: deleting, sending mail to suppliers or anyone, sending
  a quote for approval. Say they're done in the app, and give the link. Finance, accounts
  and payroll aren't yours at all: say that's not something you handle.
- After doing something, say what you did in a line, with the link to it in the app.
- Sending for approval is theirs to do in the app; you get it ready and send them the link.

Making a costing sheet (quote_requests): check you have what it needs, ask only for what's
missing, then build it in one go.
- What it needs:
  1. which task it's for (from the file, their words or their tasks); for one without a
     task, the customer, or that it's a test;
  2. the supplier quote(s): files in this chat, or quotes already on the task's costing;
  3. with several supplier quotes, which supplier (or the cheapest per line): you may
     attach them first and compare, then ask;
  4. the margin;
  5. extra charges on top of the supplier's price: delivery, installation, customs or duty,
     freight, bank charges, anything else, or none. Charges the supplier's own quote
     states come onto the sheet by themselves: say so ("their quote has AED 150 delivery,
     that's on") and don't ask about those;
  6. anything to change on the lines (quantities, an item to drop or add).
- Read a supplier quote once, with read_quotes (its lines, totals and stated charges);
  don't also open_file it, and never look at it as a picture unless read_quotes couldn't
  read it. Building the costing doesn't need it read at all: attaching it (step 2) has the
  ERP read it. Don't call erp_how for the routes written out here.
- Take all you can from their message, the files and their tasks. Ask for what's still
  missing in one short message, e.g. "20% margin? and any delivery or installation to
  add?". If they said there are no charges, or gave everything up front, don't ask.
- With what it needs, build it at once (ask_first=false): no "shall I?" after they've
  answered your questions or when they gave you everything.
- With a task: step 1 POST /quote-requests/from-task {{"task_id": "<task id>"}}, unless
  erp_read shows that task already has a costing (then use that one).
- Without a task (none matches, they say there isn't one, or it's a test): step 1 POST
  /quote-requests with query team=<handle> and a body with "title" and "customer_name"
  (all three required). The team is theirs, from "Their teams" in the context: the presales
  or proposals one if they have several, else their only one. Don't ask them for it; ask
  only if they have no team at all. Title: the supplier and their quote number, e.g. "Gulf
  Tech GT-2026-1187"; if they say it's a test, start the title with "TEST - " and use
  "TEST" as the customer. Don't make them find or create a task.
- Step 2: POST /quote-requests/{{{{1.id}}}}/supplier-quotes with attach_files = the quote
  file(s) from the chat (form_json empty). The ERP reads and compares them. Attaching does
  NOT put anything on the sheet: until step 3 the costing has no lines, and its PDF is empty.
- Step 3, pricing the sheet: POST /quote-requests/{{{{1.id}}}}/select-supplier with body
  {{"supplier_quote_id": "{{{{2.supplier_quotes.0.id}}}}", "markup_percent": <margin>}}. It
  copies that supplier's lines, quantities and costs onto the sheet, each sold at that margin
  (selling = landed cost ÷ (1 − margin)), and the supplier goes on the sheet too. "Markup",
  "margin" and "profit" from them all mean this one number. With several suppliers, price
  it with the one they chose (or select-lines for line by line; erp_how first).
- Step 4, when they named extra charges or line changes: POST
  /quote-requests/{{{{1.id}}}}/changes with them (below). The lines reprice at the margin.
- An existing costing whose quotes are attached but whose sheet is empty (no lines, totals
  0): price it the same way, with that costing's id and the supplier quote's id from erp_read.
- Never send it for approval: that is done in the app only.

Editing a costing (whenever they ask: margin, charges, quantities, prices, details): POST
/quote-requests/<id>/changes. Only what you name changes; everything else stays. Read the
costing first (erp_read /quote-requests/<id>) to see its lines (line 1 is the first) and
charges. The body, every part optional:
  {{"margin_percent": 15,
   "charges": [{{"action": "add", "label": "Delivery", "amount": 500, "currency": "AED",
                "stage": "destination"}},
               {{"action": "add", "label": "Customs", "percent": 5, "percent_of": "cif",
                "stage": "destination"}},
               {{"action": "update", "label": "Delivery", "amount": 650}},
               {{"action": "off", "label": "bank"}}, {{"action": "remove", "label": "Freight"}}],
   "lines": [{{"action": "update", "line": 2, "quantity": 20}},
             {{"action": "update", "line": 1, "rate": 1500}},
             {{"action": "add", "name": "Installation", "cost_rate": 400}},
             {{"action": "remove", "line": 3}}],
   "details": {{"customer_name": "...", "payment_terms": "...", "delivery_terms": "...",
               "expiry_date": "2026-12-31", "notes": "..."}}}}
- stage: "origin" for freight and insurance (up to arrival), "destination" for delivery,
  installation, clearance, customs, bank charges. A charge for one line: "line": n, and
  "per_unit": true when it's per piece. Charges are found by their label (or part of it).
- Adding or changing charges, costs or quantities reprices the lines at the margin; a
  selling "rate" they give for a line is kept as they said. Amounts are in the costing's
  currency unless they say otherwise.
- The whole-costing edit (PATCH /quote-requests/<id>) is refused from the chat.
- A costing with the approvers (sent for approval, approved) can't be changed: say it has
  to come back for rework in the app first, with the link.

- After it runs, send them the result from the replies, short: the costing's title, the
  supplier with currency, the lines on the sheet with cost and selling price, the totals
  and margin, anything the ERP couldn't read, and the link {app}/quote-requests/<id>. Also
  send them the costing sheet as a PDF with send_costing_pdf, in the same turn, once the
  sheet is priced (never while it still has no lines).
- Whenever they ask for the costing sheet, the PDF, the report or "the sheet" of a costing,
  send it with send_costing_pdf (look up the costing's id with erp_read if you don't have
  it). It's done at once; don't ask first. If it can't be attached, give them the link the
  tool returns.

App links (the web app): {app}/quote-requests/<quote id> for a quote's costing,
{app}/enquiries/<task id> for an enquiry analysis, {app}/proposals/my-tasks for their own
tasks, {app}/teams/<team handle>/proposals for a team's tasks (the handle from "Their teams").
Only these, the erp_guide's pages and links a tool gives you: never make up a page address
(there is no {app}/tasks).
"""

_TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "erp_routes",
        "description": "The ERP's routes in one module this person has: method, path under "
                       "/api/v1, what it does; [in the app only] where the chat may only read.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"module": {"type": "string", "description": "e.g. quote_requests"}},
            "required": ["module"], "additionalProperties": False,
        },
    },
    {
        "name": "erp_read",
        "description": "GET any route from the catalogue, as the person, live. Returns the "
                       "HTTP status and the JSON (cut if very long); every date or time in "
                       "it has a <field>_said next to it, in UAE and IST time.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Under /api/v1, e.g. /quotes"},
                "query": {"type": "string", "description": "a=1&b=2, or empty"},
            },
            "required": ["path", "query"], "additionalProperties": False,
        },
    },
    {
        "name": "erp_how",
        "description": "How a route is called: its parameters and its request body's schema.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH"]},
                "path": {"type": "string"},
            },
            "required": ["method", "path"], "additionalProperties": False,
        },
    },
    {
        "name": "erp_change",
        "description": "Make one change in the ERP, as the person. ask_first=false runs it "
                       "now (they asked for exactly this with every detail); ask_first=true "
                       "adds it to a plan that runs only on their yes. For a file upload "
                       "route, name the chat files in attach_files; form_json holds the other "
                       "form fields.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                "path": {"type": "string"},
                "query": {"type": "string", "description": "a=1&b=2, or empty"},
                "body_json": {"type": "string", "description": "The JSON body, or empty"},
                "attach_files": {"type": "array", "items": {"type": "string"}},
                "form_json": {"type": "string", "description": "Form fields as JSON, or empty"},
                "what": {"type": "string", "description": "This step in plain words"},
                "ask_first": {"type": "boolean",
                              "description": "true: wait for their yes; false: do it now"},
            },
            "required": ["method", "path", "query", "body_json", "attach_files", "form_json",
                         "what", "ask_first"],
            "additionalProperties": False,
        },
    },
    {
        "name": "chat_files",
        "description": "The files sent in this chat recently: name, what they look like, the "
                       "task they name, how they start.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [],
                         "additionalProperties": False},
    },
    {
        "name": "open_file",
        "description": "Open one file sent in the chat to read or look at it: its text, a part "
                       "at a time (part 1 first; the reply says how many parts there are), or, "
                       "with view=true, the PDF or picture itself to look at (scans, photos, "
                       "drawings, layouts).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "As chat_files names it"},
                "part": {"type": "integer", "description": "Which part of the text, from 1"},
                "view": {"type": "boolean", "description": "true to see the original instead"},
            },
            "required": ["name", "part", "view"], "additionalProperties": False,
        },
    },
    {
        "name": "read_requirements",
        "description": "Read customer documents (RFQ, tender, BOQ, specs) sent in the chat: "
                       "items with quantities and makes, conditions, deadline, what's missing.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "file_names": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string", "description": "Their task it's for, or empty"},
            },
            "required": ["file_names", "task_id"], "additionalProperties": False,
        },
    },
    {
        "name": "read_quotes",
        "description": "Read supplier quotations sent in the chat with the ERP's quote reader, "
                       "and compare them: totals, lines, cheapest, gaps.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"file_names": {"type": "array", "items": {"type": "string"}},
                           "task_id": {"type": "string",
                                       "description": "Their task these quotes are for, so "
                                                      "validity is checked against its BCD; "
                                                      "\"\" if none"}},
            "required": ["file_names", "task_id"], "additionalProperties": False,
        },
    },
    {
        "name": "send_costing_pdf",
        "description": "Send the costing sheet (selling & costing report) of one costing as a "
                       "PDF file in this chat, as it stands now. Files it in the task's folder "
                       "first, as the app does. Done at once, no need to ask.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "quote_request_id": {"type": "string",
                                     "description": "The costing's id (a UUID), from the ERP"},
            },
            "required": ["quote_request_id"], "additionalProperties": False,
        },
    },
    {
        "name": "team_tasks",
        "description": "For someone who oversees a team: the team's tasks, read now, narrowed "
                       "as they ask, shown in the chat as a table (put [[list]] in your reply "
                       "where it goes). who: a teammate's name or \"\" for everyone; section: "
                       "\"\" for the morning's list (closing today, past BCD, BCD not set), or "
                       "one of today/tomorrow/week/late/unset; state: open (not submitted or "
                       "completed), done, or all.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "who": {"type": "string"},
                "section": {"type": "string",
                            "enum": ["", "today", "tomorrow", "week", "late", "unset"]},
                "state": {"type": "string", "enum": ["all", "open", "done"]},
            },
            "required": ["who", "section", "state"], "additionalProperties": False,
        },
    },
    {
        "name": "find_suppliers",
        "description": "Start the supplier and distributor search (our past quotes, the web, "
                       "UAE directories). It posts its own progress and the list in the chat; "
                       "just tell them it's running.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"}, "brand": {"type": "string"},
                        "part": {"type": "string"}, "qty": {"type": "string"},
                    },
                    "required": ["description", "brand", "part", "qty"],
                    "additionalProperties": False,
                }},
                "place": {"type": "string", "description": "the UAE unless they said otherwise"},
            },
            "required": ["items", "place"], "additionalProperties": False,
        },
    },
]


@dataclass(slots=True)
class Step:
    method: str
    path: str
    query: str
    body: Any
    files: list[str]
    form: dict[str, str]
    what: str


@dataclass(slots=True)
class Plan:
    steps: list[Step] = field(default_factory=list)
    made_at: float = field(default_factory=time.monotonic)
    #: A step was rejected when it ran; the job they agreed to isn't done.
    failed: bool = False

    def said(self) -> str:
        return "; ".join(f"{n}. {s.what}" for n, s in enumerate(self.steps, 1))


@dataclass(slots=True)
class Person:
    user_id: uuid.UUID
    email: str
    first_name: str
    tasks: list[ProposalTask]
    #: Their modules in the ERP, as its access screen gives them.
    access: Access = field(default_factory=lambda: Access(frozenset()))
    #: The teammates of the teams they oversee (for connect_colleague).
    team_emails: frozenset[str] = frozenset()
    #: The same teammates by name ("Krishnendu" → krishnendu@…), lower case.
    team_names: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Turn:
    """What one turn knows and may change."""

    person: Person
    files: dict[str, Opened]
    #: The plan the tools build this turn; the worker keeps it for the yes.
    plan: Plan = field(default_factory=Plan)
    #: Starts the distributor finder in the chat.
    on_find: Callable[[distributors.Ask], Awaitable[None]] | None = None
    #: Sends a short line into the chat at once ("on it, reading the quotes").
    on_say: Callable[[str], Awaitable[None]] | None = None
    #: Posts a filed document in the chat as a file: (name, url, attachment id).
    on_file: Callable[[str, str, str], Awaitable[None]] | None = None
    #: Whether anything was said through ``on_say`` this turn.
    said_meanwhile: bool = False
    #: Claude's effort for this turn; None: IT_SUPPORT_BRAIN_EFFORT. Small
    #: talk runs low (the user, 2026-11-17: a "hi" took 20 s at high).
    effort: str | None = None
    #: Whether a tool ran this turn (real work, not just talk).
    working: bool = False
    #: Who answered: "groq:openai/gpt-oss-120b", "anthropic:claude-haiku-5-5".
    who: str = ""
    #: They already said yes to this job (it ran and a step failed, or this is
    #: the turn of their yes): erp_change does it at once instead of asking.
    agreed: bool = False
    #: What was done at once this turn, for the log.
    done: list[str] = field(default_factory=list)
    #: The replies of the steps done at once, in order, for {{n.id}}.
    results: list[Any] = field(default_factory=list)
    #: Status updates filed this turn (``status.Filed``).
    filed: list[Any] = field(default_factory=list)
    #: The task this turn was about (looked up or filed): (id, title, holder,
    #: holder's email); the worker keeps it as the conversation's subject.
    focus: tuple[str, str, str, str] | None = None
    #: The team's list, for someone who oversees a team (the worker's):
    #: (who, section, state) → the rows in words; the table goes to ``block``.
    on_team_list: Callable[[str, str, str], Awaitable[str]] | None = None
    #: The table the reply's [[list]] stands for.
    block: str | None = None
    #: Claude's tokens this turn, for the spend.
    input_tokens: int = 0
    output_tokens: int = 0


#: Text given to the model a part at a time, in characters.
PART_CHARS: Final = 20_000
#: What a picture or PDF may be to be shown to the model, in bytes.
VIEW_BYTES: Final = 20 * 1024 * 1024
_IMAGE_TYPES: Final = ("image/png", "image/jpeg", "image/gif", "image/webp")


_REF = re.compile(r"\{\{(\d+)\.([\w.]+)\}\}")


def _pick(value: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        value = value[int(key)] if isinstance(value, list) else (value or {}).get(key)
    return value


def _fill(text: str, results: list[Any]) -> str:
    def one(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        found = _pick(results[index], match.group(2)) if 0 <= index < len(results) else None
        return "" if found is None else str(found)
    return _REF.sub(one, text)


def _alternate(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chat as the API takes it: starting with them, same-side lines joined."""
    out: list[dict[str, Any]] = []
    for t in turns:
        if out and out[-1]["role"] == t["role"]:
            out[-1]["content"] += "\n" + t["content"]
        else:
            out.append(dict(t))
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "(earlier in the chat)"})
    return out


def _open(files: dict[str, Opened], name: str, part: int, view: bool) -> str | list[Any]:
    """One chat file for the model: a part of its text, or the original to
    look at (a PDF as a document, a picture as an image)."""
    opened = files.get(name) or next(
        (o for n, o in files.items() if n.lower() == name.strip().lower()), None)
    if opened is None:
        return f"No file called {name} in the chat. See chat_files."
    if opened.problem:
        return f"{opened.name} couldn't be opened: {opened.problem}"
    readable = opened.readable
    data = (readable.data if readable else None) or opened.data
    media = (readable.media_type if readable else "") or opened.media_type
    no_text = not opened.text.strip()
    if view or no_text:
        if data and len(data) <= VIEW_BYTES and (media == "application/pdf"
                                                 or media in _IMAGE_TYPES):
            encoded = base64.standard_b64encode(data).decode("ascii")
            kind = "document" if media == "application/pdf" else "image"
            return [{"type": kind, "source": {"type": "base64", "media_type": media,
                                              "data": encoded}},
                    {"type": "text", "text": f"{opened.name}, as sent."}]
        if no_text:
            return f"{opened.name} has no text I can read, and can't be shown as a picture."
    text = opened.text
    parts = max(1, -(-len(text) // PART_CHARS))
    part = min(max(part, 1), parts)
    chunk = text[(part - 1) * PART_CHARS: part * PART_CHARS]
    return f"{opened.name}, part {part} of {parts}:\n{chunk}"


class NeedsEyesError(Exception):
    """A free model was about to be shown a scan or a picture it can't see;
    the turn goes to Claude, which can."""


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same tools in the chat-completions shape the free providers take."""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
    }} for t in tools]


class Brain:
    """One turn, by the first of the providers that answers: the free ones
    first (Groq, Cerebras: the same tool loop over chat completions), Claude
    last (IT_SUPPORT_BRAIN_MODEL), or straight to Claude when a scan or
    picture must be looked at."""

    def __init__(self, settings: Settings, erp: ERP, finder: AgentModel,
                 claude: ClaudeEnquiry, extractor: QuoteExtractor,
                 http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._erp = erp
        self._finder = finder
        self._claude = claude
        self._extractor = extractor
        self._http = http
        self._client: anthropic.AsyncAnthropic | None = None
        self._system: str | None = None
        #: Set by the worker: the deterministic tools (``lookups``), where a
        #: change made from the chat is logged (``activity``), and how a
        #: planned Teams message is sent.
        self.lookups: lookups_mod.Lookups | None = None
        self.on_change: Callable[[Person, Step, Answer], Awaitable[None]] | None = None
        self.on_teams: Callable[[Person, Step], Awaitable[Answer]] | None = None

    def tools(self) -> list[dict[str, Any]]:
        """The tools this turn: the ERP's, and those of the features on."""
        if self.lookups is None:
            return _TOOLS
        return _TOOLS + lookups_mod.offered(self._settings)

    def providers(self) -> list[Provider]:
        """IT_SUPPORT_BRAIN_PROVIDERS' order (blank: LLM_PROVIDERS'), of those
        configured; Claude stands for IT_SUPPORT_BRAIN_MODEL."""
        known = {p.name: p for p in providers_from(self._settings)}
        order = self._settings.it_support_brain_providers.strip() or \
            self._settings.llm_providers
        out: list[Provider] = []
        for name in (n.strip().lower() for n in order.split(",")):
            if name in known and known[name] not in out:
                if name != "anthropic" and self._http is None:
                    continue
                out.append(known[name])
        return out

    @property
    def configured(self) -> bool:
        return self._settings.it_support_brain_enabled and bool(self.providers())

    @property
    def model(self) -> str:
        return self._settings.it_support_brain_model.strip() or "claude-haiku-5-5"

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key, timeout=300.0, max_retries=2
            )
        return self._client

    def system(self) -> str:
        """The instructions: the same for everyone (each person's modules are
        in the context, their routes behind erp_routes), so it stays cached."""
        if self._system is None:
            app = (self._settings.it_support_link_url or self._settings.frontend_url).rstrip("/")
            self._system = SYSTEM.format(app=app)
        return self._system

    # ── one turn ──────────────────────────────────────────────────────

    async def answer(self, turn: Turn, *, chat: list[dict[str, Any]], context: str) -> str:
        """The reply to the newest message; the plan, if any, left on ``turn``.
        A provider that fails hands the whole turn to the next, from the
        start (its reads are run again; its plan is dropped)."""
        providers = self.providers()
        started = time.monotonic()
        for index, provider in enumerate(providers):
            turn.plan = Plan()
            try:
                if provider.name == "anthropic":
                    text = await self._answer_claude(turn, chat, context)
                else:
                    text = await self._answer_free(provider, turn, chat, context)
            except NeedsEyesError:
                logger.info("it support: a picture to look at; the turn goes to Claude")
                if not any(p.name == "anthropic" for p in providers[index + 1:]):
                    return ("I can't see pictures or scans right now. Could you send it as a "
                            "PDF with text, or paste the words?")
                continue
            except (ModelError, httpx.HTTPError, anthropic.APIError, KeyError,
                    ValueError) as exc:
                logger.warning("it support: brain on %s failed: %s", provider.name, exc)
                if turn.done:
                    # Changes were already made; another model starting over
                    # could make them twice.
                    return ("I got part of it done (" + "; ".join(turn.done) + ") and then got "
                            "stuck. Can you check it in the app, or tell me to carry on?")
                continue
            model = self.model if provider.name == "anthropic" else provider.model
            turn.who = f"{provider.name}:{model}"
            logger.info("it support: %s answered %s in %.1fs", turn.who, turn.person.email,
                        time.monotonic() - started)
            return text
        return "Sorry, I'm a bit stuck right now. Give me a minute and try again?"

    async def _tool_or_error(self, turn: Turn, name: str,
                             args: dict[str, Any]) -> str | list[Any]:
        turn.working = True
        try:
            return await self._tool(turn, name, args)
        except NeedsEyesError:
            raise
        except KeyError as exc:
            return f"Failed: the call is missing {exc}. See the tool's parameters."
        except Exception as exc:  # noqa: BLE001 - the model hears it and carries on
            logger.exception("it support: tool %s failed", name)
            return f"Failed: {type(exc).__name__}: {exc}"

    async def _meanwhile(self, turn: Turn, text: str) -> None:
        if text.strip() and turn.on_say is not None and not turn.said_meanwhile:
            turn.said_meanwhile = True
            await turn.on_say(text.strip())

    async def _answer_free(self, provider: Provider, turn: Turn, chat: list[dict[str, Any]],
                           context: str) -> str:
        """The tool loop over chat completions (Groq, Cerebras, OpenRouter)."""
        assert self._http is not None
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system()}]
        messages += [{"role": m["role"], "content": m["content"][:1500]}
                     for m in _alternate(chat)]
        if len(messages) == 1 or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": "(no new words)"})
        messages[-1]["content"] = (f"[For you, not written by them]\n{context}\n\n"
                                   f"[Their message]\n{messages[-1]['content']}")
        headers = {"Authorization": f"Bearer {provider.api_key}",
                   "content-type": "application/json"}
        for _ in range(MAX_ROUNDS):
            body: dict[str, Any] = {
                "model": provider.model, "messages": messages, "tools": _openai_tools(self.tools()),
                "tool_choice": "auto", "max_tokens": 4000, "temperature": 0.3,
            }
            if "gpt-oss" in provider.model:
                body["reasoning_effort"] = self._settings.it_support_free_reasoning
            response = await self._http.post(
                f"{provider.base_url}/chat/completions", headers=headers, json=body,
                timeout=httpx.Timeout(self._settings.llm_timeout_seconds, connect=10.0))
            if response.status_code != 200:
                raise ModelError(f"{provider.name} answered {response.status_code}: "
                                 f"{response.text[:200]}")
            message = response.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            text = (message.get("content") or "").strip()
            logger.info("it support: brain round on %s for %s: %d tool call(s)",
                        provider.name, turn.person.email, len(calls))
            if not calls:
                if not text:
                    raise ModelError(f"{provider.name} answered with nothing")
                return text
            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": calls})
            await self._meanwhile(turn, text)
            for call in calls:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    out: str | list[Any] = "Failed: the arguments weren't valid JSON."
                else:
                    out = await self._tool_or_error(turn, function.get("name", ""), args)
                if not isinstance(out, str):
                    raise NeedsEyesError
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": out})
        return "That took me longer than it should. Can you give me a bit more to go on?"

    async def _answer_claude(self, turn: Turn, chat: list[dict[str, Any]], context: str) -> str:
        """The tool loop on Claude: last in the order, or for what must be seen."""
        messages = _alternate(chat)
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": "(no new words)"})
        last = messages[-1]["content"]
        messages[-1] = {"role": "user", "content": [
            {"type": "text", "text": f"[For you, not written by them]\n{context}"},
            {"type": "text", "text": last},
        ]}
        system = [{"type": "text", "text": self.system(), "cache_control": {"type": "ephemeral"}}]
        # Haiku has no server-side fallback; the larger models opt in.
        extra: dict[str, Any] = {} if "haiku" in self.model else {
            "betas": [FALLBACK_BETA], "fallbacks": "default"}
        for _ in range(MAX_ROUNDS):
            message = await self.client().beta.messages.create(
                model=self.model, max_tokens=8000, system=system, messages=messages,
                tools=self.tools(),
                output_config={"effort": turn.effort or self._settings.it_support_brain_effort},
                metadata={"user_id": str(turn.person.user_id)}, **extra,
            )
            usage = message.usage
            turn.input_tokens += usage.input_tokens or 0
            turn.output_tokens += usage.output_tokens or 0
            logger.info("it support: brain round on Claude for %s: %s in (%s cached), %s out, "
                        "%s", turn.person.email, usage.input_tokens,
                        getattr(usage, "cache_read_input_tokens", 0), usage.output_tokens,
                        message.stop_reason)
            messages.append({"role": "assistant", "content": message.content})
            uses = [b for b in message.content if b.type == "tool_use"]
            if message.stop_reason == "pause_turn":
                continue
            if message.stop_reason == "refusal":
                return "Sorry, I can't help with that one."
            words = "\n".join(b.text for b in message.content if b.type == "text").strip()
            if not uses:
                return words or "Sorry, I lost my train of thought there. Can you say that again?"
            await self._meanwhile(turn, words)
            results = []
            for use in uses:
                out = await self._tool_or_error(turn, use.name, dict(use.input))
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": out})
            messages.append({"role": "user", "content": results})
        return "That took me longer than it should. Can you give me a bit more to go on?"

    async def _tool(self, turn: Turn, name: str, args: dict[str, Any]) -> str | list[Any]:
        person = turn.person
        if name in lookups_mod.NAMES:
            if self.lookups is None:
                return "Not available."
            return await self.lookups.run(turn, name, args)
        if name == "erp_read":
            answer = await self._erp.call(person.user_id, "GET", args["path"],
                                          access=person.access, query=args["query"])
            # Every date and time said for the model, BCDs corrected (``when``).
            return Answer(answer.status, when.stamp(answer.body, datetime.now(UTC))).for_model()
        if name == "erp_routes":
            return self._erp.catalogue(person.access, args["module"].strip()) or (
                f"No routes in {args['module']} for them: it's not one of their modules, or "
                "not one you handle from the chat.")
        if name == "erp_how":
            return self._erp.how(args["method"], args["path"], person.access)
        if name == "open_file":
            return _open(turn.files, args["name"], args["part"], args["view"])
        if name == "erp_change":
            path = args["path"] if args["path"].startswith("/api/") else "/api/v1" + args["path"]
            if (why := self._erp.blocked(args["method"], _REF.sub("x", path),
                                         person.access)) is not None:
                return f"Not added: {why}. Tell them it's done in the app."
            missing = [f for f in args["attach_files"] if f not in turn.files]
            if missing:
                return f"Not added: no chat file named {', '.join(missing)}. See chat_files."
            try:
                body = json.loads(args["body_json"]) if args["body_json"].strip() else None
                form = json.loads(args["form_json"]) if args["form_json"].strip() else {}
            except ValueError as exc:
                return f"Not added: the JSON doesn't parse ({exc})."
            step = Step(args["method"], path, args["query"], body,
                        list(args["attach_files"]), form, args["what"])
            if turn.agreed or not args.get("ask_first", True):
                # They asked for exactly this, or already said yes to the job: do it.
                filled = _fill(step.path, turn.results)
                if "//" in filled.removeprefix("http://") or filled.endswith("/"):
                    return ("Not done: the path has an empty id. Use the real id from the "
                            "earlier reply (e.g. the costing's \"id\").")
                answer = await self._run_step(person, step, turn.files, turn.results)
                turn.results.append(answer.body)
                turn.done.append(f"{step.what}: {'done' if answer.ok else 'failed'}")
                logger.info("it support: agreed step for %s: %s %s -> %s", person.email,
                            step.method, step.path, answer.status)
                return (f"Done now as step {len(turn.results)}; a later step can use "
                        f"the real id below. {answer.for_model()[:6000]}")
            turn.plan.steps.append(step)
            return (f"Added as step {len(turn.plan.steps)}, not done yet. When the plan is "
                    "complete, ask them once in one line; it runs on their yes.")
        if name == "chat_files":
            if not turn.files:
                return "No files in this chat recently."
            return "\n".join(describe(o) for o in turn.files.values())
        if name == "read_requirements":
            opened = [turn.files[f] for f in args["file_names"] if f in turn.files]
            if not opened:
                return "None of those files are in the chat. See chat_files."
            task = next((t for t in person.tasks if t.id == args["task_id"]), None)
            result, left_out = await docs.read_requirements(
                self._claude, opened, task, str(person.user_id))
            return json.dumps({
                "summary": result.summary, "customer": result.customer,
                "deadline": result.deadline, "conditions": result.conditions,
                "not_stated": result.missing, "left_out": left_out,
                "items": [{"description": i.description, "brand": i.brand,
                           "part": i.part_number, "qty": str(i.quantity or ""),
                           "unit": i.unit, "spec": i.specification} for i in result.items],
            }, ensure_ascii=False, default=str)[:14_000]
        if name == "read_quotes":
            opened = [turn.files[f] for f in args["file_names"] if f in turn.files]
            if not opened:
                return "None of those files are in the chat. See chat_files."
            read = await docs.read_quotes(self._extractor, opened)
            task = next((t for t in turn.person.tasks if t.id == args.get("task_id", "")), None)
            due = fu_due(task) if task is not None else None
            summary = docs.quotes_summary(
                read, bcd=due.astimezone(when.UAE).date() if due else None,
                checks=features_mod.on(self._settings, "quote_checks"))
            return json.dumps(summary, ensure_ascii=False, default=str)[:14_000]
        if name == "send_costing_pdf":
            return await self._send_costing_pdf(turn, _fill(args["quote_request_id"],
                                                            turn.results).strip())
        if name == "team_tasks":
            if turn.on_team_list is None:
                return "Only for someone who oversees a team; this person doesn't."
            return await turn.on_team_list(args["who"], args["section"], args["state"])
        if name == "find_suppliers":
            if turn.on_find is None:
                return "The finder isn't available right now."
            items = tuple(distributors.Item(description=i["description"][:200],
                                            brand=i["brand"][:80], part=i["part"][:80],
                                            qty=i["qty"][:20]) for i in args["items"][:10])
            await turn.on_find(distributors.Ask(items, args["place"] or "the UAE"))
            return "Started. Its progress and the list post in the chat by themselves."
        return f"No tool called {name}."

    async def _send_costing_pdf(self, turn: Turn, quote_id: str) -> str:
        """The costing sheet PDF filed in the task's folder (the ERP's own
        route, as the person: their access decides) and posted in the chat."""
        try:
            uuid.UUID(quote_id)
        except ValueError:
            return "Not sent: that's not a costing id. Look the costing up with erp_read first."
        person = turn.person
        lines = await self._erp.call(person.user_id, "GET", f"/quote-requests/{quote_id}/items",
                                     access=person.access)
        if not lines.ok:
            return f"Not sent, the ERP said: {lines.for_model()[:1500]}"
        if isinstance(lines.body, list) and not lines.body:
            # An empty sheet prints a report of zeros with no items or supplier.
            return ("Not sent: the sheet has no lines yet, so the PDF would be empty. Price it "
                    "first: select-supplier with the attached supplier quote and their margin "
                    "(ask for the margin if they haven't said), then send it.")
        answer = await self._erp.call(person.user_id, "POST",
                                      f"/quote-requests/{quote_id}/report/file",
                                      access=person.access)
        if not answer.ok or not isinstance(answer.body, dict):
            return f"Not sent, the ERP said: {answer.for_model()[:1500]}"
        filed = answer.body
        name, url, attachment = filed.get("file_name"), filed.get("web_url"), \
            filed.get("attachment_id")
        if turn.on_file is None or not (name and url and attachment):
            return (f"Filed as {name} but I can't attach it in the chat here; give them this "
                    f"link to it instead: {url}")
        try:
            await turn.on_file(name, url, attachment)
        except Exception as exc:  # noqa: BLE001 - the link still gets it to them
            logger.warning("it support: posting %s in the chat failed: %s", name, exc)
            return (f"Filed as {name}, but Teams refused the file card ({exc}). Give them this "
                    f"link to it instead: {url}")
        turn.done.append(f"sent costing PDF {name}")
        return (f"Sent: {name} is in the chat now as a file (also in the task's folder). "
                "Don't paste its link; just mention it in a few words.")

    # ── the plan, on their yes ────────────────────────────────────────

    async def run_plan(self, person: Person, plan: Plan, files: dict[str, Opened]) -> str:
        """Every step in order, stopping at the first that fails; what came of
        it, for the reply."""
        results: list[Any] = []
        said = []
        for n, step in enumerate(plan.steps, 1):
            answer = await self._run_step(person, step, files, results)
            results.append(answer.body)
            said.append(f"Step {n} ({step.what}): {answer.for_model()[:3000]}")
            if not answer.ok:
                plan.failed = True
                said.append("Stopped there; the later steps were not run.")
                break
        return "\n".join(said)

    async def _run_step(self, person: Person, step: Step, files: dict[str, Opened],
                        results: list[Any]) -> Answer:
        """One change, as the person, with earlier results filled in; logged."""
        if step.method == "TEAMS":
            if self.on_teams is None:
                return Answer(503, "Messages to colleagues aren't available here.")
            return await self.on_teams(person, step)
        path = _fill(step.path, results)
        query = _fill(step.query, results)
        body = json.loads(_fill(json.dumps(step.body), results)) if step.body is not None \
            else None
        attached = [(f, files[f].data or b"", files[f].media_type) for f in step.files
                    if f in files]
        answer = await self._erp.call(person.user_id, step.method, path,
                                      access=person.access, query=query, body=body,
                                      files=attached or None,
                                      form={k: str(v) for k, v in step.form.items()})
        if self.on_change is not None:
            await self.on_change(person, step, answer)
        return answer
