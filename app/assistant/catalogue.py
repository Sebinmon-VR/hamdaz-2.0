"""What the assistant can do: the models it may run on and the tools it may call.

Every tool is one existing endpoint. The assistant never has a code path of its
own into a module — a tool call becomes an HTTP request to the real route,
carrying the caller's own session, so whatever that route refuses the assistant
is refused too. That is the whole of the access model, and it is why this file
can be written without thinking about permissions at all: it describes the
surface, and the routes decide.

Tool descriptions are written for the model, not copied from the OpenAPI
summary. A tool the model misunderstands is a tool it will misuse, so the words
here say when to use it, what identifiers it accepts, and what it will not do.

Three kinds of tool, told apart by ``kind``:

* ``read`` — GET. Runs as soon as the model asks.
* ``write`` — anything else that hits a route. Available, but only to the
  roles the module's ``write_roles`` names, and paused for the person's
  confirmation unless policy says otherwise. A write that *deletes* is
  further held to ``DELETE_ROLES``, whatever the policy rows say.
* ``client`` — performed by the browser on the screen in front of the person:
  press this, fill that, scroll, read what is there. The turn waits for the
  browser to report back.

Adding a tool means adding one entry here and running the seed, which creates
its policy row. Nothing else needs to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal

#: ``client`` is a third kind: a tool the *browser* performs rather than a
#: route — clicking a button on the screen, scrolling, filling a field,
#: reading what is on the page. The turn parks until the browser reports
#: back, exactly as a write parks for a confirmation, so the model sees the
#: result before it carries on. See ``app.assistant.agent``.
Kind = Literal["read", "write", "client"]
Location = Literal["path", "query", "body"]
Gate = Literal["open", "access", "admin"]
#: ``planned`` is a tool that is written down but not built. It is never
#: offered to the model and never appears in anybody's tool list; it exists
#: so the administration screen can show what is coming as well as what is
#: here, and so the decision to build it is recorded next to the ones
#: already made rather than in somebody's head.
Status = Literal["live", "planned"]


# ── models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    name: str
    description: str
    #: USD per one million tokens, as published by OpenAI in September 2026.
    #: Seeded into ``assistant_models`` where a super admin keeps them current.
    input_price: Decimal
    cached_input_price: Decimal
    output_price: Decimal
    #: Whether the model thinks before answering. The GPT-4 family does not,
    #: and rejects the reasoning parameter rather than ignoring it, so the
    #: effort setting is simply not sent to them.
    supports_reasoning: bool = True
    #: Whether the model can fetch a deferred tool's schema for itself.
    #: Measured, not assumed: every current model does except the smallest,
    #: which rejects the search tool outright and fails the whole turn. A
    #: model without it is sent the entire catalogue instead, which costs
    #: tokens but works — see ``tool_payload``.
    supports_tool_search: bool = True


def _m(
    key: str,
    name: str,
    description: str,
    inp: str,
    cached: str,
    out: str,
    *,
    reasoning: bool = True,
    tool_search: bool = True,
) -> ModelSpec:
    return ModelSpec(
        key,
        name,
        description,
        Decimal(inp),
        Decimal(cached),
        Decimal(out),
        supports_reasoning=reasoning,
        supports_tool_search=tool_search,
    )


MODELS: Final[tuple[ModelSpec, ...]] = (
    _m(
        "gpt-5.6-sol",
        "GPT-5.6 Sol",
        "OpenAI's current flagship. The default: the most reliable at deciding "
        "which tool to call and when to ask rather than guess, which is what "
        "matters most when a wrong call costs somebody's time.",
        "4.00", "0.40", "20.00",
    ),
    _m(
        "gpt-5.6-terra",
        "GPT-5.6 Terra",
        "Mid-tier. About half the cost of Sol on the same tools; a sensible "
        "choice once the assistant's answers have been watched for a while.",
        "2.00", "0.20", "12.00",
    ),
    _m(
        "gpt-5.6-luna",
        "GPT-5.6 Luna",
        "Small and fast. Cheapest by far; more likely to pick the wrong tool "
        "or the wrong argument on an ambiguous request.",
        "0.20", "0.02", "1.20",
    ),
    _m(
        "gpt-6-astra",
        "GPT-6 Astra",
        "OpenAI's most capable model. Several times the price of Sol; worth it "
        "only if Sol is measurably getting things wrong.",
        "10.00", "1.00", "50.00",
    ),
    # ── previous generations ───────────────────────────────────────────
    _m("gpt-5.5", "GPT-5.5", "The generation before 5.6. Capable and dear.",
       "5.00", "0.50", "30.00"),
    _m("gpt-5.4", "GPT-5.4", "Previous generation flagship.", "2.50", "0.25", "15.00"),
    _m("gpt-5.4-mini", "GPT-5.4 Mini", "Previous generation mid-tier.",
       "0.75", "0.075", "4.50"),
    _m(
        "gpt-5.4-nano",
        "GPT-5.4 Nano",
        "Previous generation small model. Cannot search for tools, so it is "
        "sent the whole catalogue on every turn — which makes it slower and "
        "dearer than its price suggests, and worse at choosing.",
        "0.20",
        "0.02",
        "1.25",
        tool_search=False,
    ),
    _m("gpt-5.2", "GPT-5.2", "Older, still a reasoning model.", "1.75", "0.175", "14.00"),
    _m(
        "gpt-5.1", "GPT-5.1", "Older. Reasons, but cannot search for tools.",
        "1.25", "0.125", "10.00", tool_search=False,
    ),
    _m(
        "gpt-5", "GPT-5", "The original GPT-5. Reasons; no tool search.",
        "1.25", "0.125", "10.00", tool_search=False,
    ),
    _m(
        "gpt-5-mini", "GPT-5 Mini", "Small and quick, and it still reasons.",
        "0.25", "0.025", "2.00", tool_search=False,
    ),
    _m(
        "gpt-5-nano", "GPT-5 Nano",
        "The cheapest model here that still reasons. Sent the whole catalogue, "
        "which eats into the saving.",
        "0.05", "0.005", "0.40", tool_search=False,
    ),

    # ── the GPT-4 family: fast, cheap, and not reasoning models ────────
    #
    # These answer without thinking first, which makes them quick and makes
    # them worse at deciding which tool to call on an ambiguous request. They
    # reject the reasoning parameter outright, so the effort setting does
    # nothing for them; it is skipped rather than sent. They also cannot search
    # for tools, so each turn carries the whole catalogue.
    _m(
        "gpt-4.1", "GPT-4.1", "Fast, does not reason. Good at plain questions.",
        "2.00", "0.50", "8.00", reasoning=False, tool_search=False,
    ),
    _m(
        "gpt-4.1-mini", "GPT-4.1 Mini", "Fast and cheap. Does not reason.",
        "0.40", "0.10", "1.60", reasoning=False, tool_search=False,
    ),
    _m(
        "gpt-4.1-nano", "GPT-4.1 Nano", "The cheapest here. Does not reason.",
        "0.10", "0.025", "0.40", reasoning=False, tool_search=False,
    ),
    _m(
        "gpt-4o", "GPT-4o", "The older general-purpose model. Does not reason.",
        "2.50", "1.25", "10.00", reasoning=False, tool_search=False,
    ),
    _m(
        "gpt-4o-mini", "GPT-4o Mini", "Older, small and very cheap. Does not reason.",
        "0.15", "0.075", "0.60", reasoning=False, tool_search=False,
    ),
)


MODELS_BY_KEY: Final[dict[str, ModelSpec]] = {m.key: m for m in MODELS}

#: What a fresh install answers with. Named here so the model default, the
#: seeder and the tests cannot drift apart. Chosen by measurement against
#: this app's own tool payload — see AssistantSettings.model_key.
DEFAULT_MODEL: Final = "gpt-5.6-terra"

#: What the OpenAI Responses API accepts for ``reasoning.effort``. Which of
#: these a given model honours is the model's business; an invalid pairing comes
#: back from the API as a clear error rather than being second-guessed here.
REASONING_EFFORTS: Final[tuple[str, ...]] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
)


# ── the voice ──────────────────────────────────────────────────────────
#
# The assistant reads its answers aloud through a speech model rather than the
# browser's built-in ``speechSynthesis``. That is not a preference: the built-in
# voices are whatever the operating system ships, they differ on every machine,
# and on most of them the result is flat enough that people stop using the
# feature. A speech model sounds like a person and sounds the same for everyone.
#
# The cost of that is a round trip, so the endpoint streams: playback starts on
# the first chunk rather than after the whole clip is generated.

#: Speech models a super admin may choose. ``gpt-4o-mini-tts`` is the default
#: and the only one that takes ``instructions`` — see ``VOICE_INSTRUCTIONS``.
#: The ``tts-1`` pair are the older, cheaper, unsteerable engines.
SPEECH_MODELS: Final[tuple[str, ...]] = ("gpt-4o-mini-tts", "tts-1", "tts-1-hd")

#: The voices OpenAI offers. Deliberately unannotated: how one sounds is a
#: judgement nobody should make from a docstring, and the right way to choose is
#: to listen to the same sentence in each. ``GET /assistant/voices`` returns
#: this list so an admin screen can offer a sample of every one.
VOICES: Final[tuple[str, ...]] = (
    "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar",
)

DEFAULT_VOICE: Final = "cedar"
DEFAULT_SPEECH_MODEL: Final = "gpt-4o-mini-tts"

#: How the voice should sound, in words. This is the difference between a model
#: reading text out and somebody telling you something, and it is a setting
#: rather than a constant because the right answer is a matter of taste and the
#: person with the taste is not the one deploying.
VOICE_INSTRUCTIONS: Final = (
    "Speak like a calm, capable colleague giving a quick update in an office. "
    "Warm and natural, never announcer-like or salesy. Vary the pace: brisk "
    "through facts and figures, with a small pause before a recommendation. "
    "Read identifiers such as quote numbers clearly and a little more slowly."
)

# -- the realtime voice -------------------------------------------------
#
# A different arrangement from the read-aloud voice above, and the difference is
# worth stating. Read-aloud is our loop: the text chat answers, then a speech
# model reads the answer out. Realtime is *OpenAI's* loop - the browser streams
# microphone audio straight to them and hears speech back, with no turn of ours
# in between. That is what makes it feel like a conversation, and it is also
# what makes it the more delicate thing to secure.
#
# Two rules keep it inside the same access model as everything else:
#
# 1. The tool list and the instructions are fixed **server side** when the
#    ephemeral token is minted. A tampered client cannot add a tool, because the
#    session it connects to was already defined.
# 2. Tools do not execute in the browser. Every call comes back to this API,
#    where policy is checked again and the call travels the same route as the
#    text chat, carrying the caller's own session.
#
# What is genuinely weaker is the confirmation pause. In the text chat the
# server holds a parked run and nothing happens until a person answers. In voice
# mode the server refuses a confirmable write and tells the client to ask; it
# records the question and the answer, but it is the client that puts the
# question. That is a real difference and it is why writes stay off in voice
# mode unless a super admin turns them on.

#: Realtime models this integration will accept.
REALTIME_MODELS: Final[tuple[str, ...]] = (
    "gpt-realtime-2.1",
    "gpt-realtime-2.1-mini",
    "gpt-realtime-2",
    "gpt-realtime",
    "gpt-realtime-mini",
)

#: The current full-size realtime model. The ``mini`` variants cost less and
#: answer sooner; which is right is a judgement made by listening, not reading.
DEFAULT_REALTIME_MODEL: Final = "gpt-realtime-2.1"

#: How long a minted session token is good for. Short by design: it is handed to
#: a browser and its only job is to open one connection, immediately.
REALTIME_TOKEN_SECONDS: Final = 120

#: Added to the instructions in voice mode only. Spoken answers follow different
#: rules from written ones, and the model has to be told which it is giving.
REALTIME_INSTRUCTIONS: Final = (
    "You are speaking out loud, not writing. Keep answers to a sentence or two "
    "unless you are asked for more. Never read out markdown, bullet characters, "
    "URLs, or raw identifiers longer than a few characters - say 'the Kuwait "
    "bid' rather than a task id. Say numbers and dates the way a person would. "
    "Before anything that changes data, say what it is in one short sentence "
    "and wait to be told to go ahead."
)


#: One request's ceiling. A spoken answer is a couple of sentences; anything
#: near this is a document being read aloud, which nobody waits for and which
#: bills per character. Refused rather than truncated, so the caller knows.
VOICE_MAX_CHARS: Final = 4000


# -- what the voice costs -----------------------------------------------
#
# Speech and spoken conversation are billed by OpenAI on their own meters,
# separately from the chat model, and neither meter is tokens-in-tokens-out.
# Until this was written the voice was simply not costed: the analytics screen
# showed the chat bill and called it the bill, which understated it by however
# much the voice was used. These prices are what make the voice appear on it.
#
# Two units, because OpenAI charges in two:
#
# * **speech** is priced per million *characters* of the text handed to it.
#   Characters rather than tokens because that is the only quantity we can
#   count exactly, on our own side, before the request is even sent - the
#   audio is streamed straight through to the browser and never measured here.
# * **realtime** is priced per million *tokens*, and audio tokens are dearer
#   than text ones by an order of magnitude, so they are kept apart rather
#   than averaged into one number that would be wrong for every session.
#
# As with the chat models these are seeded, not hardcoded: a super admin edits
# them in the settings screen when OpenAI moves a price, because a stale price
# does not fail loudly, it quietly makes every figure on the cost screen wrong.

VoiceKind = Literal["speech", "realtime"]


@dataclass(frozen=True, slots=True)
class VoiceModelSpec:
    """One priced voice model. Unused prices for its kind are zero."""

    key: str
    name: str
    kind: VoiceKind
    description: str
    #: speech only: USD per one million characters of input text.
    char_price: Decimal = Decimal(0)
    #: realtime only: USD per one million tokens, by sort.
    text_input_price: Decimal = Decimal(0)
    cached_text_input_price: Decimal = Decimal(0)
    audio_input_price: Decimal = Decimal(0)
    cached_audio_input_price: Decimal = Decimal(0)
    text_output_price: Decimal = Decimal(0)
    audio_output_price: Decimal = Decimal(0)


def _speech(key: str, name: str, description: str, chars: str) -> VoiceModelSpec:
    return VoiceModelSpec(key, name, "speech", description, char_price=Decimal(chars))


def _realtime(
    key: str,
    name: str,
    description: str,
    text_in: str,
    cached_text_in: str,
    audio_in: str,
    cached_audio_in: str,
    text_out: str,
    audio_out: str,
) -> VoiceModelSpec:
    return VoiceModelSpec(
        key,
        name,
        "realtime",
        description,
        text_input_price=Decimal(text_in),
        cached_text_input_price=Decimal(cached_text_in),
        audio_input_price=Decimal(audio_in),
        cached_audio_input_price=Decimal(cached_audio_in),
        text_output_price=Decimal(text_out),
        audio_output_price=Decimal(audio_out),
    )


VOICE_MODELS: Final[tuple[VoiceModelSpec, ...]] = (
    _speech(
        "gpt-4o-mini-tts",
        "GPT-4o Mini TTS",
        "The steerable speech model and the default. Dearer per character than "
        "the tts-1 pair, and the only one that acts on the voice instructions, "
        "which is most of the difference between a reading and a person talking.",
        "16.00",
    ),
    _speech(
        "tts-1",
        "TTS-1",
        "The older, cheaper engine. Ignores the voice instructions rather than "
        "failing on them.",
        "15.00",
    ),
    _speech(
        "tts-1-hd",
        "TTS-1 HD",
        "The higher-fidelity older engine, at twice the price of tts-1 and with "
        "the same indifference to the instructions.",
        "30.00",
    ),
    _realtime(
        "gpt-realtime-2.1",
        "GPT Realtime 2.1",
        "The current full-size spoken model. Audio in and out is where the money "
        "goes; the text figures barely register beside it.",
        "4.00", "0.40", "32.00", "0.40", "16.00", "64.00",
    ),
    _realtime(
        "gpt-realtime-2.1-mini",
        "GPT Realtime 2.1 Mini",
        "About a third the price of the full model and quicker to answer. The "
        "sensible default once a spoken assistant is used by more than a few people.",
        "0.60", "0.06", "10.00", "0.30", "2.40", "20.00",
    ),
    _realtime(
        "gpt-realtime-2",
        "GPT Realtime 2",
        "The previous generation of the spoken model.",
        "4.00", "0.40", "32.00", "0.40", "16.00", "64.00",
    ),
    _realtime(
        "gpt-realtime",
        "GPT Realtime",
        "The original spoken model.",
        "4.00", "0.40", "40.00", "2.50", "16.00", "80.00",
    ),
    _realtime(
        "gpt-realtime-mini",
        "GPT Realtime Mini",
        "The original small spoken model.",
        "0.60", "0.06", "10.00", "0.30", "2.40", "20.00",
    ),
)

VOICE_MODELS_BY_KEY: Final[dict[str, VoiceModelSpec]] = {m.key: m for m in VOICE_MODELS}


# ── module groups ──────────────────────────────────────────────────────


#: Who may have the assistant *write* in a module that carries company-wide
#: consequences. Straight from the brief: managers, CEOs and super admins.
#:
#: This is a separate question from ``gate``, and keeping the two apart is the
#: point of it. ``gate`` decides who can see the module at all — an ordinary
#: person still reads the teams list, still sees a quote. This decides who can
#: have the assistant *change* it. Folding the two together would mean the only
#: way to stop somebody having the assistant delete a team was to stop them
#: looking at teams, which is not a trade anybody would accept.
#:
#: Note what it does *not* cover: the modules whose writes are everyday work
#: for the person doing them — requesting leave, raising a quote request,
#: updating one's own proposal task. Those stay open here and are decided where
#: they have always been decided, by the route, which knows about HR team
#: membership and record ownership in a way a list of global roles cannot.
SENSITIVE_WRITE_ROLES: Final[tuple[str, ...]] = ("super_admin", "ceo", "manager")

#: Who may have the assistant *delete* anything, anywhere. A floor rather
#: than a default: a super admin's ``write_roles`` can narrow it further and
#: cannot widen it. Members and team leads are not on it, and that is the
#: decision — a colleague asking the assistant to "clear that out" should be
#: told it is a manager's call, not have it happen.
#:
#: What counts as a delete is decided per tool by ``ToolSpec.is_destructive``:
#: every DELETE route, and any write flagged ``destructive`` because it erases
#: data behind a POST — resetting a user, wiping somebody's HR file.
DELETE_ROLES: Final[tuple[str, ...]] = ("super_admin", "ceo", "manager")


@dataclass(frozen=True, slots=True)
class ModuleGroup:
    """A module as the assistant sees it. Keys match the access catalogue.

    ``gate`` decides whether the module's tools are *shown* to a person. It is
    a courtesy to the model and a saving of tokens, not the security boundary:

    * ``open`` — everyone signed in, like leave and quotes
    * ``access`` — only if the person's effective access includes the module
    * ``admin`` — only holders of a global admin role

    ``write_roles`` is the restriction this module *ships* with: the global
    roles that may have the assistant write here, or ``None`` for "whoever the
    route already allows". It is seeded into the module's policy row and is a
    starting point, not a rule — a super admin edits it in the settings screen
    and their edit is never overwritten by a later seed.
    """

    key: str
    name: str
    gate: Gate
    description: str
    write_roles: tuple[str, ...] | None = None


GROUPS: Final[tuple[ModuleGroup, ...]] = (
    ModuleGroup(
        "app",
        "The app itself",
        "open",
        "Moving around the ERP rather than reading from it. Open here because "
        "opening a screen somebody may already open is not a privilege — and "
        "the one tool in it resolves against that person's own access, so a "
        "page they cannot reach is a page it cannot send them to.",
    ),
    ModuleGroup(
        "quote_requests",
        "Quote Requests",
        "access",
        "Customer quotes being raised, and their approvals. Writes are left "
        "open: raising and submitting a quote is the everyday work of the "
        "people in this module, and who may approve one is a question the "
        "route answers from the approver list, not from a global role.",
    ),
    ModuleGroup(
        "quote_comparison",
        "Quote Comparison",
        "access",
        "Supplier quotes read and compared like with like.",
    ),
    ModuleGroup(
        "hr",
        "HR",
        "open",
        "Hiring, employee documents and performance reviews. Open here because "
        "everyone has their own HR record; the endpoints themselves refuse "
        "anybody who is not on the HR team the rest.",
    ),
    ModuleGroup(
        "finance",
        "Finance",
        "admin",
        "Profit and loss from the Zoho Books ledger, and the Zoho endpoints "
        "behind it. Read by super admin, CEO, manager or accountant, and by "
        "nobody else. Nothing here writes today; the restriction is set anyway "
        "so that the day something does, it arrives already restricted rather "
        "than open until somebody notices.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup(
        "assignment",
        "Work Assignment",
        "access",
        "How work is shared out: labels, capacity, the policy behind it, and "
        "the ranking that decides who gets the next job. Changing that policy "
        "changes who gets given work across the company, so it is held to the "
        "same roles as the other company-wide settings.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup(
        "templates",
        "Form Templates",
        "admin",
        "What the forms ask for, as data rather than code. A template edit "
        "changes what every future form collects.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup(
        "projects",
        "Projects",
        "access",
        "Projects, their milestones, the tasks on them and what is blocking "
        "them. Writes are left open: moving your own task along is the "
        "everyday work of everybody on a project, and who may change what is a "
        "question the route answers from membership rather than a global role.",
    ),
    ModuleGroup(
        "reports",
        "Reports",
        "access",
        "Daily, weekly and monthly team reports. Writes are left open: filing "
        "your own report is the everyday work of everybody who has one to file. "
        "What stops somebody reading a colleague's is not this — it is that the "
        "routes only ever return what the caller may see, so the same tool "
        "answers a manager and an ordinary person differently.",
    ),
    ModuleGroup("me", "About me", "open", "The signed-in person's own roles, teams and access."),
    ModuleGroup(
        "dashboard",
        "Dashboard",
        "access",
        "Team dashboards and their arrangement. Arranging one is team work "
        "rather than administration, and the route already asks whether the "
        "person is on the team.",
    ),
    ModuleGroup("directory", "People Directory", "access", "Everyone in the organisation."),
    ModuleGroup(
        "teams",
        "Teams",
        "access",
        "Teams, their members and their roles. Everyone reads this; creating, "
        "archiving and deleting teams, and moving people between them, is "
        "administration.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup("leave", "Leave", "open", "Requesting and deciding time off."),
    ModuleGroup("proposals", "Proposals", "access", "Proposal tasks from SharePoint."),
    ModuleGroup(
        "quotes",
        "Quotes",
        "open",
        "Customer quotes read from Zoho Books. Read by anyone signed in; "
        "anything that would write back to Zoho is administration, and is "
        "restricted here before such a tool exists rather than after.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup("meetings", "Meetings", "open", "The person's own Outlook calendar."),
    ModuleGroup(
        "workflows",
        "Workflows",
        "access",
        "A team's process run one task at a time — the presales flow from task "
        "to Zoho quote. Starting one, answering its questions and verifying "
        "what it found is the everyday work of the person whose task it is; "
        "the run does everything as them, through the same routes.",
    ),
    ModuleGroup(
        "notifications",
        "Notifications",
        "open",
        "What the system has told this person. Open because the routes only "
        "ever return the caller's own; marking one read is theirs to do.",
    ),
    ModuleGroup(
        "intake",
        "Mail Intake",
        "admin",
        "The watched mailbox that turns enquiries into proposal tasks: what is "
        "watched, every mail seen, and the mirror of the SharePoint list. Super "
        "admin territory on every route, which the routes enforce themselves.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup(
        "system",
        "System",
        "admin",
        "The administrator's console: what every background surface currently "
        "says, and who may do what across the parts recently built. Reads only.",
    ),
    ModuleGroup(
        "roles",
        "Roles & Permissions",
        "admin",
        "Global roles and who holds them. The sharpest thing the assistant can "
        "touch: a granted role changes what somebody may do everywhere. "
        "Granting super admin needs super admin, which the route enforces "
        "whatever is set here.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
    ModuleGroup(
        "user_admin",
        "User Administration",
        "admin",
        "User profiles and team module access. Granting a team a module is "
        "granting everyone on it a part of the system.",
        write_roles=SENSITIVE_WRITE_ROLES,
    ),
)

GROUPS_BY_KEY: Final[dict[str, ModuleGroup]] = {g.key: g for g in GROUPS}


# ── tools ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    location: Location
    #: JSON schema for the value, without nullability — that is added from ``required``.
    schema: dict[str, Any]
    description: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    #: ``module.action``. Also the function name the model sees.
    key: str
    module_key: str
    kind: Kind
    method: str
    #: Relative to the API prefix. ``{name}`` segments are filled from path params.
    path: str
    #: Short, for the UI: "Request leave".
    label: str
    #: For the model: when to use it and what it accepts.
    description: str
    params: tuple[Param, ...] = field(default_factory=tuple)
    #: Shown on the confirmation card for a write, as a plain warning.
    warning: str | None = None
    #: ``planned`` tools are listed for an administrator and given to nobody.
    status: Status = "live"
    #: Erases data irreversibly behind a method other than DELETE. A DELETE
    #: route is destructive without saying so; this is for the POST that
    #: wipes a file or resets an account. Read through ``is_destructive``.
    destructive: bool = False

    @property
    def is_live(self) -> bool:
        return self.status == "live"

    @property
    def is_client(self) -> bool:
        """Performed by the browser, not by a route."""
        return self.kind == "client"

    @property
    def is_destructive(self) -> bool:
        """Deletes something. Offered only to ``DELETE_ROLES``, whatever else says."""
        return self.method == "DELETE" or self.destructive

    @property
    def deferred(self) -> bool:
        """Kept out of the prompt until the model searches for it.

        Derived from one list rather than set per tool, so the answer to
        "what does the model see up front" is readable in one place.
        """
        return self.key not in EVERYDAY_TOOLS

    @property
    def name(self) -> str:
        """The function name the model calls. Dots are not allowed there."""
        return self.key.replace(".", "__")

    @property
    def is_write(self) -> bool:
        return self.kind == "write"

    @property
    def is_read(self) -> bool:
        return self.kind == "read"

    def schema(self) -> dict[str, Any]:
        """The strict JSON schema for this tool's arguments.

        Strict mode needs every property listed in ``required`` and no extras,
        so an optional parameter is expressed as nullable rather than omitted.
        That is what lets the API guarantee the arguments parse, which in turn
        is what lets the executor trust them.
        """
        properties: dict[str, Any] = {}
        for p in self.params:
            prop = dict(p.schema)
            if p.description:
                prop["description"] = p.description
            if not p.required:
                kind = prop.get("type")
                if isinstance(kind, str):
                    prop["type"] = [kind, "null"]
                elif isinstance(kind, list) and "null" not in kind:
                    prop["type"] = [*kind, "null"]
                else:
                    prop = {"anyOf": [prop, {"type": "null"}]}
            properties[p.name] = prop
        return {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in self.params],
            "additionalProperties": False,
        }

    def definition(self) -> dict[str, Any]:
        """The tool as the OpenAI Responses API wants it.

        ``strict`` is what makes the API guarantee the arguments validate, which
        is what lets the executor trust them without re-checking every field.
        """
        tool: dict[str, Any] = {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.schema(),
            "strict": True,
        }
        if self.deferred:
            tool["defer_loading"] = True
        return tool

    #: Fields the Responses API takes and the Realtime API rejects outright, as
    #: an unknown parameter rather than by ignoring them — so one stray key
    #: fails the whole spoken session with a 400. Both of these have shipped
    #: broken once; keep this list rather than popping keys at the call site.
    _RESPONSES_ONLY: ClassVar[tuple[str, ...]] = ("strict", "defer_loading")

    def realtime_definition(self) -> dict[str, Any]:
        """The tool as the Realtime API wants it.

        The Responses shape minus the fields Realtime will not accept. Two
        consequences worth naming. Arguments from a spoken session are not
        schema-guaranteed the way ``strict`` makes them, so the proxy that runs
        them treats them as untrusted input — which it would anyway, arriving
        over HTTP from a browser. And there is no deferral, so a spoken session
        carries every tool it may use; that is the other reason its tool list is
        kept narrower than the typed chat's.
        """
        tool = self.definition()
        for field_name in self._RESPONSES_ONLY:
            tool.pop(field_name, None)
        return tool

    def split(self, arguments: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], dict]:
        """Arguments -> (path values, query values, body). Nulls are dropped."""
        path: dict[str, str] = {}
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for p in self.params:
            value = arguments.get(p.name)
            if value is None:
                if p.location == "path":
                    raise ValueError(f"{self.key}: {p.name!r} is required")
                continue
            if p.location == "path":
                path[p.name] = str(value)
            elif p.location == "query":
                query[p.name] = value
            else:
                body[p.name] = _without_nulls(value)
        return path, query, body


#: The tools that stay in the prompt on every turn.
#:
#: Everything else is deferred: the model is told it exists only when it
#: searches for it. That is not a micro-optimisation. The full catalogue is
#: about 45KB of JSON schema, and sending it all costs tokens on every "hi" and,
#: worse, costs attention — a long list is a list the model reads less
#: carefully, and tool choice gets worse as it grows.
#:
#: The rule for being on this list is "somebody asks this most weeks". Anything
#: rarer is a search away and none the worse for it.
EVERYDAY_TOOLS: Final[frozenset[str]] = frozenset({
    # taking somebody to a screen, which is often the whole answer — and
    # pressing what is on it, which is the rest of the answer
    "app.open", "app.click", "app.fill", "app.scroll", "app.screen",
    # who am I and what can I see
    "me.roles", "me.teams", "me.access",
    "dashboard.mine",
    # the questions people actually ask
    "leave.mine", "leave.summary", "leave.calendar", "leave.request",
    "proposals.my_tasks", "proposals.team_tasks",
    "meetings.list",
    "quotes.search", "quotes.get",
    # finding a person or a team, which most other calls need first
    "directory.search",
    "teams.list", "teams.get", "teams.members",
    # the work in flight
    "quote_requests.mine", "quote_requests.list",
    # projects: finding one by name is what every other project question needs
    "projects.list", "projects.get", "projects.board", "projects.my_tasks",
    "hr.my_documents", "hr.my_reviews",
    "finance.profit_and_loss",
})


def _s(t: str, **extra: Any) -> dict[str, Any]:
    return {"type": t, **extra}


STR: Final = _s("string")
INT: Final = _s("integer")
BOOL: Final = _s("boolean")
DATE: Final = _s("string", description="YYYY-MM-DD")
STR_LIST: Final = _s("array", items={"type": "string"})


def _without_nulls(value: Any) -> Any:
    """Drop nulls from a body value, at every level.

    Strict mode forces the model to send a key for every property, so an
    argument it has nothing to say about arrives as ``null``. A top-level null
    has always been read as "not supplied" and dropped — see ``split``. This
    does the same inside a nested object, and it has to: a line item's
    ``quantity`` is a plain number with a default on the route, so an explicit
    ``null`` is not "use the default", it is a validation error and a failed
    call. Nothing in the catalogue uses null to mean "clear this", so reading it
    as "not supplied" everywhere is both consistent and safe.
    """
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def _object(properties: dict[str, Any], *, required: tuple[str, ...]) -> dict[str, Any]:
    """A nested object in strict form.

    Strict mode's rules apply at *every* level, not only the top one: an object
    inside an array must list every property in ``required`` and forbid extras,
    or the API rejects the whole tool and the turn fails. ``ToolSpec.schema``
    does this for a tool's own arguments; anything nested is built here so the
    two cannot drift, and so nobody has to remember the rule twice.

    An optional property is expressed as nullable rather than omitted, exactly
    as it is at the top level.
    """
    return {
        "type": "object",
        "properties": {
            name: schema if name in required else {**schema, "type": [schema["type"], "null"]}
            for name, schema in properties.items()
        },
        "required": list(properties),
        "additionalProperties": False,
    }


def _path(name: str, description: str, schema: dict[str, Any] = STR) -> Param:
    return Param(name, "path", schema, description, required=True)


def _query(name: str, description: str, schema: dict[str, Any] = STR) -> Param:
    return Param(name, "query", schema, description)


def _body(
    name: str, description: str, schema: dict[str, Any] = STR, *, required: bool = False
) -> Param:
    return Param(name, "body", schema, description, required=required)


_TEAM_REF: Final = _path("ref", "The team's handle (slug) or its id.")
_USER_ID: Final = _path("user_id", "The person's ERP user id.")
_ANY_USER: Final = (
    "Accepts the ERP user id, the Entra object id, or the email address."
)

_READ: Final = "read"
_WRITE: Final = "write"
_CLIENT: Final = "client"

# ── shared shapes: projects, reports, proposals, notifications ──
_PROJECT_ID = _path("project_id", "The project id, from projects.list or projects.get.")
_PROJECT_STATUS = _s("string", enum=["planned", "active", "on_hold", "done", "cancelled"])
_RAG = _s("string", enum=["green", "amber", "red", "grey"])
_TREND = _s("string", enum=["improving", "steady", "declining"])
_TASK_STATE = _s("string", enum=["not_started", "in_progress", "blocked", "done", "dropped"])
_PRIORITY = _s("string", enum=["low", "medium", "high", "critical"])
_PLAN = _s("string", enum=["on_plan", "off_plan_no_impact", "off_plan_impact"])
_ISSUE_STATE = _s("string", enum=["open", "in_progress", "resolved", "closed"])
_CADENCE = _s("string", enum=["daily", "weekly", "monthly", "quarterly", "yearly", "ad_hoc"])

# ── shared shapes: hr, templates, labels ──
# Shared pieces, each used by more than one tool below. The enum values are
# the StrEnum members in app.models.hr, app.models.templates and
# app.models.labels, spelled out so the model sees them.
_EMPLOYMENT_TYPE = _s(
    "string", enum=["full_time", "part_time", "contract", "internship", "temporary"]
)
_DOCUMENT_KIND = _s(
    "string",
    enum=[
        "offer_letter", "contract", "amendment", "id_document", "visa", "certificate",
        "payslip", "appraisal", "warning", "resignation", "other",
    ],
)
_LABEL_KIND = _s("string", enum=["category", "status", "skill"])
_DATETIME = _s("string", description="ISO 8601 date-time")

#: One question on a form, as ``app.forms.schemas.FieldIn`` takes it. Two of
#: its fields are left out because strict mode cannot express them: ``columns``
#: (a list of free-form field specs, for table fields) and ``scoring`` (a
#: free-form object). ``default`` is any scalar, so it is listed as required
#: with null already in its type rather than made nullable by ``_object``,
#: which would nest one type list inside another.
_FIELD = _object(
    {
        "key": STR,
        "label": STR,
        "type": _s(
            "string",
            enum=[
                "text", "textarea", "number", "currency", "percent", "date",
                "checkbox", "select", "table", "file",
            ],
        ),
        "section": _s("string", description="The section key it sits under."),
        "required": BOOL,
        "help": STR,
        "options": _s("array", items={"type": "string"}, description="For select."),
        "default": _s(["string", "number", "boolean", "null"]),
        "maps_to": _s("string", description="The Zoho estimate field it becomes, if any."),
    },
    required=("key", "label", "type", "default"),
)
_SECTION = _object({"key": STR, "name": STR, "help": STR}, required=("key", "name"))

# ── shared shapes: quote requests, comparisons, dashboards, teams, users ──
#: One supplier's offer, as ``app.comparison.schemas.QuoteIn`` has it. Used by
#: both the unsaved analysis and the save, so it is written once.
_SUPPLIER_QUOTE = _object(
    {
        "supplier_name": STR,
        "quote_number": STR,
        "quote_date": _s("string", description="As printed on the quote; free text."),
        "currency": _s("string", description="Three-letter code. Defaults to AED."),
        "fx_rate": _s(
            "number",
            description="One unit of this quote's currency in the comparison's "
                        "currency. Leave at 1 when they are the same.",
        ),
        "validity": STR,
        "delivery_time": STR,
        "payment_terms": STR,
        "warranty": STR,
        "incoterms": STR,
        "contact": STR,
        "notes": STR,
        "discount": _s("number"),
        "freight": _s("number"),
        "tax": _s("number"),
        "quoted_total": _s("number", description="The total the supplier printed."),
        "items": _s("array", items=_object(
            {
                "description": STR,
                "part_number": STR,
                "brand": STR,
                "unit": STR,
                "quantity": _s("number"),
                "unit_price": _s("number"),
                "line_total": _s(
                    "number",
                    description="As printed. Leave out and it is quantity x unit price.",
                ),
                "lead_time": STR,
            },
            required=("description",),
        )),
        "source": _s("string", enum=["upload", "manual"]),
        "file_name": STR,
        "extraction_note": STR,
    },
    required=("supplier_name",),
)

# ── shared shapes: assignment, analytics, intake, system, finance ──
_TEAM_ID: tuple = (_path("team_id", "The team's handle (slug) or its id."),)

#: The PolicyIn fields a strict schema can carry. ``capacity_by_label`` and
#: ``max_open_by_label`` are open dicts keyed by label, which strict mode has no
#: way to express (see the note on reports.fill), so they stay on the screen.
_POLICY_FIELDS: tuple = (
    _body("name", "What to call the policy."),
    _body("description", "What it is for, in a line."),
    _body("enabled", "Whether it is in force at all.", BOOL),
    _body(
        "default_capacity",
        "The share of work for anyone with no label saying otherwise. 1 is a "
        "full share, 0.5 is one task for every two, 0 is no work. 0-100.",
        _s("number"),
    ),
    _body("default_max_open", "The most open tasks anyone may hold. 0 or more.", INT),
    _body("excluded_labels", "Label keys that take somebody out of the pool.", STR_LIST),
    _body(
        "excluded_roles",
        "Role keys that are never given work — managers by default.",
        STR_LIST,
    ),
    _body("exclude_on_leave", "Skip anyone on approved leave today.", BOOL),
    _body("new_joiner_days", "How recent counts as a new joiner. 0-3650.", INT),
    _body(
        "new_joiner_from_first_seen",
        "Count new-joiner days from first sign-in rather than the join date.",
        BOOL,
    ),
    _body("weight_load", "How much effective load moves the ranking. 0-100.", _s("number")),
    _body("weight_open_count", "How much the open-task count moves it. 0-100.", _s("number")),
    _body(
        "weight_idle_days",
        "How much time since last assignment moves it. 0-100.",
        _s("number"),
    ),
)


TOOLS: Final[tuple[ToolSpec, ...]] = (
    # ── moving around the app ──────────────────────────────────────────
    ToolSpec(
        "app.open", "app", _READ, "GET", "/assistant/open",
        "Open a page",
        "Take the person to a screen in the ERP. Call it when what they want IS "
        "a page — 'show me the quotes', 'open my leave', 'take me to presales' "
        "— and alongside an answer when the screen is where they will carry on "
        "working. Pass `page` as a module name ('quotes', 'leave', "
        "'reports') for the obvious page in it, or 'module.page' for a "
        "particular one. Pass `team` as a team's handle for a page that "
        "belongs to one team. Pass `record` as the id of the ONE THING a "
        "detail page is about. It takes an id OR the name somebody used: "
        "record='Hamdaz ERP' opens that project, and the answer tells you "
        "which record it found. If the name matches more than one, the "
        "refusal lists them — ask which. Never invent an id. "
        "It moves the app and changes nothing else. Afterwards say what the "
        "answer's `label` says you opened, not what you hoped to open. If it "
        "comes back 404 the message tells you what to do instead — read it "
        "rather than guessing again.",
        (
            # Required, unlike most query parameters here. Strict mode makes the
            # model send every key, and an optional one it has nothing to say
            # about arrives as null and is dropped — which for this tool would
            # mean asking the route to open nowhere.
            Param(
                "page",
                "query",
                STR,
                "A module name like 'quotes', or 'module.page' like 'reports.overview'.",
                required=True,
            ),
            _query("team", "A team's handle, for a page that belongs to one team."),
            _query(
                "record",
                "The id of the record a detail page is about. Look it up first; "
                "never guess one.",
            ),
        ),
    ),
    # ── about me ───────────────────────────────────────────────────────
    # ── acting on the screen ───────────────────────────────────────────
    #
    # These four are not routes. The browser performs them and reports back,
    # and the turn waits for that report the way it waits for a confirmation.
    # They exist because "press Submit for me" and "scroll down" are things a
    # person says, and a tool that could only navigate had to answer them
    # with an apology. What the model may press is bounded twice: the screen
    # snapshot it is given lists only what is actually there, and the browser
    # refuses a destructive control — delete, remove, revoke — for anybody the
    # policy would not let delete through a tool either. See ``DELETE_ROLES``.
    ToolSpec(
        "app.screen", "app", _CLIENT, "CLIENT", "", "Read the screen",
        "What is on the screen in front of the person right now: its headings, "
        "and every button, link, tab and field with the label they see. Call "
        "this after app.open, app.click or app.fill to see what changed, or "
        "when the 'Controls on this screen' list you were given is stale. "
        "Returns the list; it changes nothing.",
    ),
    ToolSpec(
        "app.click", "app", _CLIENT, "CLIENT", "", "Press a button or link",
        "Press a button, link, tab or menu item on the screen the person is "
        "looking at, by the label they see on it — exactly as listed under "
        "'Controls on this screen' or by app.screen. Use it for things no "
        "other tool does: opening a dialog, switching a tab, pressing Save or "
        "Submit on a form the person has filled in. Prefer the module's own "
        "tool when one exists for the same action, because that one is "
        "checked and confirmed properly. It refuses a control whose label "
        "says delete, remove or similar unless the person may delete.",
        (
            _body("label", "The visible text of the control, as listed.", required=True),
            _body(
                "nth",
                "Which one, counting from 1, when several controls share the label.",
                INT,
            ),
        ),
    ),
    ToolSpec(
        "app.fill", "app", _CLIENT, "CLIENT", "", "Fill in a field",
        "Type a value into a field on the current screen, named by its label "
        "or placeholder as listed under 'Controls on this screen'. Works for "
        "text boxes, text areas, selects (pass the option's visible text or "
        "value) and checkboxes (pass true or false). It does not submit the "
        "form — press the button with app.click afterwards, and say what you "
        "filled in before you do.",
        (
            _body("label", "The field's label or placeholder, as listed.", required=True),
            _body("value", "What to put in it. For a checkbox, true or false.", required=True),
            _body("nth", "Which one, counting from 1, when several fields share the label.", INT),
        ),
    ),
    ToolSpec(
        "app.scroll", "app", _CLIENT, "CLIENT", "", "Scroll the page",
        "Scroll the screen the person is looking at: to the top or bottom, up "
        "or down by a screen, or to a heading or section by the text on it. "
        "Use it when they ask to see more, or to bring the part you are about "
        "to talk about into view.",
        (
            _body(
                "to",
                "top, bottom, up, down — or the text of a heading or section to "
                "bring into view.",
                required=True,
            ),
        ),
    ),
    ToolSpec(
        "me.roles", "me", _READ, "GET", "/roles/me", "My roles",
        "The global roles the signed-in person holds (super admin, CEO, manager, "
        "accountant). Use it to answer 'what am I allowed to do' before attempting "
        "an administrative action.",
    ),
    ToolSpec(
        "me.teams", "me", _READ, "GET", "/teams/me", "My teams",
        "The teams the signed-in person belongs to, with their role in each.",
        (_query("include_archived", "Also list archived teams.", BOOL),),
    ),
    ToolSpec(
        "me.access", "me", _READ, "GET", "/access/me", "My module access",
        "Which modules and pages the signed-in person can reach, and through which "
        "teams. Use it when someone asks why they cannot see a part of the system.",
    ),
    # ── dashboard ──────────────────────────────────────────────────────
    ToolSpec(
        "dashboard.mine", "dashboard", _READ, "GET", "/dashboards/me", "My dashboards",
        "A dashboard for each team the person belongs to: summary cards with that "
        "team's data. The quickest overview of what is going on for them.",
        (_query("local_only", "Skip cards that call out to Entra or SharePoint (faster).", BOOL),),
    ),
    ToolSpec(
        "dashboard.team", "dashboard", _READ, "GET", "/teams/{ref}/dashboard", "Team dashboard",
        "Render one team's dashboard with live data.",
        (_TEAM_REF, _query("local_only", "Skip cards that call out to Entra or SharePoint.", BOOL)),
    ),
    ToolSpec(
        "dashboard.layout", "dashboard", _READ, "GET", "/teams/{ref}/dashboard/layout",
        "Dashboard layout",
        "How one team's dashboard is arranged, and which other cards it could add, "
        "without loading any data.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "dashboard.arrange", "dashboard", _WRITE, "PUT", "/teams/{ref}/dashboard/layout",
        "Arrange a dashboard",
        "Replace one team's dashboard arrangement. Read dashboard.layout first to "
        "learn the available widget keys; a widget the team's modules do not "
        "support is refused.",
        (
            _TEAM_REF,
            _body(
                "widgets",
                "The cards in order: [{widget_key, enabled?, options?}].",
                _s(
                    "array",
                    items={
                        "type": "object",
                        "properties": {
                            "widget_key": {"type": "string"},
                            "enabled": {"type": ["boolean", "null"]},
                            "options": {
                                "type": ["object", "null"],
                                "properties": {"limit": {"type": ["integer", "null"]}},
                                "required": ["limit"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["widget_key", "enabled", "options"],
                        "additionalProperties": False,
                    },
                ),
                required=True,
            ),
        ),
        warning="Replaces the whole arrangement for everyone on the team.",
    ),
    # ── directory ──────────────────────────────────────────────────────
    ToolSpec(
        "directory.search", "directory", _READ, "GET", "/directory/users", "Search people",
        "Find people in the organisation directory by name, email, job title or "
        "department. Returns Entra object ids, which other tools accept as a user "
        "reference. Use this whenever someone names a colleague you need an id for.",
        (
            _query("search", "Text to match against name, email, title or department."),
            _query("include_guests", "Include external guest accounts.", BOOL),
            _query("include_disabled", "Include disabled accounts.", BOOL),
            _query("limit", "Maximum people to return (default 200).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "directory.person", "directory", _READ, "GET", "/directory/users/{object_id}",
        "One person",
        "One person's directory record — title, department, manager, phone — by "
        "Entra object id.",
        (_path("object_id", "The Entra object id from directory.search."),),
    ),
    # ── teams ──────────────────────────────────────────────────────────
    ToolSpec(
        "teams.list", "teams", _READ, "GET", "/teams", "List teams",
        "Every team, with member counts. Filter by name or handle.",
        (
            _query("search", "Match against team name or handle."),
            _query("include_archived", "Also list archived teams.", BOOL),
        ),
    ),
    ToolSpec(
        "teams.get", "teams", _READ, "GET", "/teams/{ref}", "One team",
        "One team in full, including its members and their roles.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "teams.members", "teams", _READ, "GET", "/teams/{ref}/members", "Team members",
        "The members of one team and the roles each holds in it.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "teams.for_user", "teams", _READ, "GET", "/teams/by-user/{user_id}", "Someone's teams",
        "Which teams a given person belongs to.",
        (_USER_ID, _query("include_archived", "Also list archived teams.", BOOL)),
    ),
    ToolSpec(
        "teams.create", "teams", _WRITE, "POST", "/teams", "Create a team",
        "Create a new team. Admins only. The handle is derived from the name "
        "unless given.",
        (
            _body("name", "The team's name.", required=True),
            _body("description", "What the team is for."),
            _body(
                "slug",
                "A URL handle, lowercase with hyphens. Derived from the name if omitted.",
            ),
        ),
    ),
    ToolSpec(
        "teams.update", "teams", _WRITE, "PATCH", "/teams/{ref}", "Rename a team",
        "Rename or redescribe a team, or change its handle. Admins only.",
        (
            _TEAM_REF,
            _body("name", "New name."),
            _body("description", "New description."),
            _body("slug", "New handle."),
        ),
    ),
    ToolSpec(
        "teams.add_member", "teams", _WRITE, "POST", "/teams/{ref}/members", "Add a member",
        "Add someone to a team, or replace the roles they hold in it. Admins only. "
        "Someone who has never signed in is provisioned from the directory.",
        (
            _TEAM_REF,
            _body("user_id", "The person. " + _ANY_USER, required=True),
            _body(
                "role_keys",
                "Team roles to hold: member, team_lead, team_manager, approver. "
                "Defaults to member.",
                STR_LIST,
            ),
        ),
        warning="Replaces whatever roles they already hold in this team.",
    ),
    ToolSpec(
        "teams.set_member_roles", "teams", _WRITE, "PATCH", "/teams/{ref}/members/{user_id}",
        "Change member roles",
        "Change the roles someone holds within a team. Admins only.",
        (
            _TEAM_REF,
            _USER_ID,
            _body(
                "role_keys",
                "The full set of team roles they should now hold.",
                STR_LIST,
                required=True,
            ),
        ),
    ),
    ToolSpec(
        "teams.remove_member", "teams", _WRITE, "DELETE", "/teams/{ref}/members/{user_id}",
        "Remove a member",
        "Remove someone from a team. Admins only.",
        (_TEAM_REF, _USER_ID),
        warning="They lose whatever this team's module grants gave them.",
    ),
    ToolSpec(
        "teams.archive", "teams", _WRITE, "POST", "/teams/{ref}/archive", "Archive a team",
        "Archive a team so it stops appearing. Admins only. Reversible with teams.restore.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "teams.restore", "teams", _WRITE, "POST", "/teams/{ref}/restore", "Restore a team",
        "Bring an archived team back. Admins only.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "teams.delete", "teams", _WRITE, "DELETE", "/teams/{ref}", "Delete a team",
        "Permanently delete a team. Only an archived team can be deleted, so "
        "archive it first. Admins only.",
        (_TEAM_REF,),
        warning="Permanent. Memberships and module grants go with it.",
    ),
    # ── leave ──────────────────────────────────────────────────────────
    ToolSpec(
        "leave.mine", "leave", _READ, "GET", "/leave/requests/me", "My leave requests",
        "The signed-in person's own leave requests, past and pending, with status "
        "and any decision note.",
    ),
    ToolSpec(
        "leave.summary", "leave", _READ, "GET", "/leave/summary/me", "My leave at a glance",
        "Days taken and pending for the signed-in person, and what is coming up.",
    ),
    ToolSpec(
        "leave.calendar", "leave", _READ, "GET", "/leave/calendar", "Who is off",
        "Who is on approved leave, day by day, over a window. Use it to answer "
        "'is anyone off next week' or to check before requesting dates.",
        (
            _query("start", "First day of the window. Defaults to today.", DATE),
            _query("days", "How many days to show (default 30).", INT),
        ),
    ),
    ToolSpec(
        "leave.rules", "leave", _READ, "GET", "/leave/settings", "Leave rules",
        "The rules leave is decided by: how many may be off at once, whether "
        "requests are decided automatically, and which team acts as HR.",
    ),
    ToolSpec(
        "leave.queue", "leave", _READ, "GET", "/leave/requests", "All leave requests (HR)",
        "Every leave request in the organisation. HR team members only; anyone "
        "else is refused.",
        (
            _query(
                "status",
                "Filter: pending, approved, rejected or cancelled.",
                _s("string", enum=["pending", "approved", "rejected", "cancelled"]),
            ),
            _query("upcoming_only", "Only requests that have not yet ended.", BOOL),
        ),
    ),
    ToolSpec(
        "leave.request", "leave", _WRITE, "POST", "/leave/requests", "Request leave",
        "Submit a leave request for the signed-in person. Both dates are inclusive; "
        "a single day has the same start and end. It may be decided automatically "
        "on submission — report the returned status and any decision note.",
        (
            _body(
                "leave_type",
                "annual, sick, emergency or unpaid. Defaults to annual.",
                _s("string", enum=["annual", "sick", "emergency", "unpaid"]),
            ),
            _body("start_date", "First day off.", DATE, required=True),
            _body("end_date", "Last day off, inclusive.", DATE, required=True),
            _body("reason", "Why, if they gave one."),
        ),
    ),
    ToolSpec(
        "leave.cancel", "leave", _WRITE, "POST", "/leave/requests/{request_id}/cancel",
        "Withdraw my leave request",
        "Withdraw one of the signed-in person's own leave requests.",
        (_path("request_id", "The leave request id, from leave.mine."),),
    ),
    ToolSpec(
        "leave.approve", "leave", _WRITE, "POST", "/leave/requests/{request_id}/approve",
        "Approve leave (HR)",
        "Approve a pending leave request. HR team members only. Set emergency to "
        "approve past the concurrent limit.",
        (
            _path("request_id", "The leave request id."),
            _body("note", "A note for the requester."),
            _body("emergency", "Override the concurrent-leave limit.", BOOL),
        ),
    ),
    ToolSpec(
        "leave.reject", "leave", _WRITE, "POST", "/leave/requests/{request_id}/reject",
        "Reject leave (HR)",
        "Reject a pending leave request with a reason. HR team members only. The "
        "note is mandatory.",
        (
            _path("request_id", "The leave request id."),
            _body("note", "Why it is refused. Required.", required=True),
        ),
    ),
    ToolSpec(
        "leave.update_rules", "leave", _WRITE, "PUT", "/leave/settings", "Change leave rules (HR)",
        "Change the leave rules. HR team members only. Only the fields given change.",
        (
            _body("max_concurrent", "How many may be off on the same day.", INT),
            _body("auto_decide", "Decide requests automatically on submission.", BOOL),
            _body(
                "limit_scope",
                "organisation counts everyone; team counts only the requester's teams.",
                _s("string", enum=["organisation", "team"]),
            ),
            _body("hr_team_slug", "Handle of the team that acts as HR."),
            _body("notify_hr_by_email", "Email HR about new requests.", BOOL),
            _body("max_days_per_request", "Longest single request allowed.", INT),
        ),
        warning="Changes how every future leave request is decided.",
    ),
    # ── proposals ──────────────────────────────────────────────────────
    ToolSpec(
        "proposals.my_tasks", "proposals", _READ, "GET", "/proposals/my-tasks",
        "My proposal tasks",
        "The proposal tasks assigned to the signed-in person in the SharePoint "
        "Proposals list.\n\n"
        "Each task carries title, status, priority, start_date, due_date and "
        "bid_closing_date. Questions about today, this week or what is overdue "
        "are answered by reading those dates — there is no date filter here.",
        (
            _query("open_only", "Hide completed tasks (default true).", BOOL),
            _query("limit", "Maximum tasks (default 200).", INT),
        ),
    ),
    ToolSpec(
        "proposals.team_tasks", "proposals", _READ, "GET", "/proposals/team-tasks",
        "A team's proposal tasks",
        "Every proposal task belonging to one team's members, grouped by person. "
        "For that team's leadership and admins; others are refused.\n\n"
        "Pass the team name the person used straight through — 'presales' or "
        "'Presales' both work, and there is no need to look the team up first. "
        "Each task carries title, status, priority, start_date, due_date and "
        "bid_closing_date, so questions about what is due today, this week or "
        "overdue are answered by reading those dates rather than by a different "
        "tool: there is no date filter on this endpoint.",
        (
            Param(
                "team",
                "query",
                STR,
                "The team's handle, e.g. 'presales'. Its name works too.",
                required=True,
            ),
            _query("open_only", "Hide completed tasks.", BOOL),
            _query("limit", "Rows per member (default 200).", INT),
            _query("refresh", "Re-read SharePoint instead of the cache.", BOOL),
        ),
    ),
    ToolSpec(
        "proposals.workload", "proposals", _READ, "GET", "/proposals/workload",
        "Proposal workload",
        "How many open proposals each person carries. Admins only.",
        (
            _query("team", "Restrict to one team's members, by handle or id."),
            _query("refresh", "Re-sweep SharePoint instead of the cache.", BOOL),
        ),
    ),
    ToolSpec(
        "proposals.attachments", "proposals", _READ, "GET",
        "/proposals/tasks/{task_id}/attachments", "Task attachments",
        "The files attached to one of the signed-in person's own proposal tasks.",
        (_path("task_id", "The SharePoint task id, from proposals.my_tasks."),),
    ),
    # ── quotes ─────────────────────────────────────────────────────────
    ToolSpec(
        "quotes.search", "quotes", _READ, "GET", "/quotes", "Search quotes",
        "Customer quotes from Zoho Books, filtered by status, customer, date range "
        "or free text. Read-only: nothing here can change a quote.",
        (
            _query(
                "status",
                "draft, sent, invoiced, accepted, declined or expired.",
                _s("string", enum=["draft", "sent", "invoiced", "accepted", "declined", "expired"]),
            ),
            _query("customer_name", "Match the customer's name."),
            _query("date_start", "Earliest quote date, inclusive.", DATE),
            _query("date_end", "Latest quote date, inclusive.", DATE),
            _query("search", "Free text across the quote."),
            _query("limit", "Maximum quotes. Omit for all of them.", INT),
            _query("refresh", "Re-read Zoho instead of the cache.", BOOL),
        ),
    ),
    ToolSpec(
        "quotes.by_number", "quotes", _READ, "GET", "/quotes/by-number/{number}",
        "Quote by number",
        "Find one quote by its quote number, e.g. QT-000123.",
        (_path("number", "The quote number as printed on the quote."),),
    ),
    ToolSpec(
        "quotes.get", "quotes", _READ, "GET", "/quotes/{quote_id}", "One quote",
        "One quote in full: lines, totals, customer, dates and a link to open it in Zoho.",
        (_path("quote_id", "The Zoho quote id, from quotes.search or quotes.by_number."),),
    ),
    ToolSpec(
        "quotes.related", "quotes", _READ, "GET", "/quotes/{quote_id}/related", "Related records",
        "The records connected to a quote: customer, items, sales orders, invoices, comments.",
        (
            _path("quote_id", "The Zoho quote id."),
            _query(
                "include",
                "Comma-separated subset: customer, items, salesorders, invoices, comments.",
            ),
        ),
    ),
    # ── meetings ───────────────────────────────────────────────────────
    ToolSpec(
        "meetings.list", "meetings", _READ, "GET", "/meetings", "My meetings",
        "The signed-in person's own Outlook calendar. Defaults to today plus the "
        "week ahead. Only ever their own calendar — there is no way to read "
        "somebody else's.",
        (
            _query("start", "First day to read. Defaults to today.", DATE),
            _query("end", "Last day to read, inclusive. Defaults to a week after start.", DATE),
            _query("search", "Match subject, location, organiser or attendee."),
            _query("meetings_only", "Only events with other people on them.", BOOL),
            _query("include_cancelled", "Include meetings that were called off.", BOOL),
            _query("limit", "Maximum events (default 100).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "meetings.get", "meetings", _READ, "GET", "/meetings/{event_id}", "One meeting",
        "One meeting in full, with everyone invited and their responses.",
        (_path("event_id", "The event id from meetings.list."),),
    ),
    # ── roles ──────────────────────────────────────────────────────────
    ToolSpec(
        "roles.list", "roles", _READ, "GET", "/roles", "List roles",
        "Every role that exists, global and per-team, with descriptions.",
        (_query("scope", "global or team.", _s("string", enum=["global", "team"])),),
    ),
    ToolSpec(
        "roles.assignments", "roles", _READ, "GET", "/roles/assignments", "Who holds what",
        "Everyone holding a global role, grouped by person.",
    ),
    ToolSpec(
        "roles.for_user", "roles", _READ, "GET", "/roles/users/{user_id}", "Someone's roles",
        "The global roles one person holds.",
        (_USER_ID,),
    ),
    ToolSpec(
        "roles.grant", "roles", _WRITE, "POST", "/roles/users/{user_id}", "Grant a role",
        "Give someone a global role. Super admin, CEO or manager; only a super "
        "admin may grant super_admin.",
        (_USER_ID, _body("role_key", "The role's key, e.g. manager.", required=True)),
        warning="Global roles reach across every team.",
    ),
    ToolSpec(
        "roles.revoke", "roles", _WRITE, "DELETE", "/roles/users/{user_id}/{role_key}",
        "Revoke a role",
        "Take a global role away from someone. The last super admin cannot be demoted.",
        (_USER_ID, _path("role_key", "The role's key.")),
    ),
    ToolSpec(
        "roles.create", "roles", _WRITE, "POST", "/roles", "Create a role",
        "Create a custom role. Admins only.",
        (
            _body("key", "Short lowercase key, e.g. auditor.", required=True),
            _body("name", "Display name.", required=True),
            _body("scope", "global or team.", _s("string", enum=["global", "team"]), required=True),
            _body("description", "What it is for."),
        ),
    ),
    ToolSpec(
        "roles.update", "roles", _WRITE, "PATCH", "/roles/{key}", "Rename a role",
        "Rename or redescribe a role. Admins only.",
        (
            _path("key", "The role's key."),
            _body("name", "New display name."),
            _body("description", "New description."),
        ),
    ),
    ToolSpec(
        "roles.delete", "roles", _WRITE, "DELETE", "/roles/{key}", "Delete a role",
        "Delete a custom role. System roles cannot be deleted. Admins only.",
        (_path("key", "The role's key."),),
        warning="Everyone holding it loses it.",
    ),
    # ── user administration ────────────────────────────────────────────
    ToolSpec(
        "users.profile", "user_admin", _READ, "GET", "/users/{ref}", "Everything about a user",
        "One person's full profile across every module: identity, roles, teams, "
        "leave and more. " + _ANY_USER,
        (
            _path("ref", "The person. " + _ANY_USER),
            _query("include", "Comma-separated section keys. Omit for all."),
            _query("local_only", "Skip the Entra lookup (faster).", BOOL),
        ),
    ),
    ToolSpec(
        "access.modules", "user_admin", _READ, "GET", "/modules", "Module catalogue",
        "Every module in the ERP and its pages — the things a team can be given.",
    ),
    ToolSpec(
        "access.for_user", "user_admin", _READ, "GET", "/access/users/{user_id}",
        "Someone's module access",
        "Which modules and pages a given person can reach, and via which teams.",
        (_USER_ID,),
    ),
    ToolSpec(
        "access.team", "user_admin", _READ, "GET", "/teams/{ref}/access", "A team's modules",
        "Which modules one team has been granted.",
        (_TEAM_REF,),
    ),
    ToolSpec(
        "access.grant", "user_admin", _WRITE, "POST", "/teams/{ref}/access",
        "Grant a module to a team",
        "Give a team a module, optionally limited to some of its pages. Super "
        "admin only. Admin-only modules cannot be granted to a team.",
        (
            _TEAM_REF,
            _body("module_key", "The module's key, from access.modules.", required=True),
            _body("page_keys", "Page keys to limit it to. Omit for the whole module.", STR_LIST),
        ),
        warning="Everyone on the team gains the module.",
    ),
    ToolSpec(
        "access.revoke", "user_admin", _WRITE, "DELETE", "/teams/{ref}/access/{module_key}",
        "Take a module from a team",
        "Remove a module from a team. Super admin only.",
        (_TEAM_REF, _path("module_key", "The module's key.")),
        warning="Everyone on the team loses the module, unless another team gives it.",
    ),
    # ── quote requests ─────────────────────────────────────────────────
    ToolSpec(
        "quote_requests.list", "quote_requests", _READ, "GET", "/quote-requests",
        "Quote requests",
        "Customer quotes being raised, with their status and who is deciding "
        "them. Filter by team or status.",
        (
            _query("team", "The team's handle or id."),
            _query(
                "status",
                "draft, submitted, approved, rejected, changes_requested or created.",
            ),
            _query("limit", "Maximum to return.", INT),
        ),
    ),
    ToolSpec(
        "quote_requests.mine", "quote_requests", _READ, "GET", "/quote-requests/mine",
        "My quote requests",
        "The quotes the signed-in person raised, or owes a decision on. The "
        "quickest answer to 'what is waiting on me'.",
        (_query("limit", "Maximum to return.", INT),),
    ),
    ToolSpec(
        "quote_requests.get", "quote_requests", _READ, "GET", "/quote-requests/{request_id}",
        "One quote request",
        "One quote in full: its lines, the supplier quotes behind it, its status "
        "and its history.",
        (_path("request_id", "The quote request id."),),
    ),
    ToolSpec(
        "quote_requests.items", "quote_requests", _READ, "GET",
        "/quote-requests/{request_id}/items", "Quote lines",
        "Just the priced lines of one quote request.",
        (_path("request_id", "The quote request id."),),
    ),
    ToolSpec(
        "quote_requests.reviews", "quote_requests", _READ, "GET",
        "/quote-requests/{request_id}/reviews", "Approval history",
        "Who approved, rejected or sent back one quote, and what they said.",
        (_path("request_id", "The quote request id."),),
    ),
    ToolSpec(
        "quote_requests.approvers", "quote_requests", _READ, "GET",
        "/quote-requests/{request_id}/approvers", "Who can decide this",
        "The people who may approve one quote request.",
        (_path("request_id", "The quote request id."),),
    ),
    ToolSpec(
        "quote_requests.queue", "quote_requests", _READ, "GET", "/quote-requests/queue",
        "Approved and waiting",
        "Quotes approved and waiting to be created in Zoho.",
        (
            _query("team", "The team's handle or id."),
            _query("mine_only", "Only the ones assigned to me.", BOOL),
        ),
    ),
    # ── quote comparison ───────────────────────────────────────────────
    ToolSpec(
        "comparisons.list", "quote_comparison", _READ, "GET", "/comparisons",
        "Saved comparisons",
        "Supplier quote comparisons that have been saved, newest first.",
        (_query("limit", "Maximum to return.", INT),),
    ),
    ToolSpec(
        "comparisons.mine", "quote_comparison", _READ, "GET", "/comparisons/mine",
        "My comparisons",
        "The comparisons the signed-in person made.",
        (_query("limit", "Maximum to return.", INT),),
    ),
    ToolSpec(
        "comparisons.get", "quote_comparison", _READ, "GET", "/comparisons/{comparison_id}",
        "One comparison",
        "One comparison in full: every supplier, the matched line items, the "
        "totals and the findings — including which suppliers did not quote "
        "everything, which is the thing most worth saying out loud.",
        (_path("comparison_id", "The comparison id."),),
    ),
    # ── HR ─────────────────────────────────────────────────────────────
    ToolSpec(
        "hr.my_documents", "hr", _READ, "GET", "/hr/me/documents", "My HR documents",
        "The documents HR holds about the signed-in person — contract, offer "
        "letter, certificates — with their expiry dates.",
    ),
    ToolSpec(
        "hr.my_performance", "hr", _READ, "GET", "/hr/me/performance", "My performance",
        "The signed-in person's own performance record, as far as it has been "
        "shared with them.",
    ),
    ToolSpec(
        "hr.my_reviews", "hr", _READ, "GET", "/hr/reviews/mine", "Reviews I owe",
        "Performance reviews the signed-in person has been asked to write, and "
        "whether each is started, submitted or still waiting.",
    ),
    ToolSpec(
        "hr.reviews_about_me", "hr", _READ, "GET", "/hr/reviews/about-me", "Reviews about me",
        "Reviews written about the signed-in person, where the cycle has been "
        "shared with them.",
    ),
    ToolSpec(
        "hr.openings", "hr", _READ, "GET", "/hr/openings", "Job openings",
        "Job openings. HR team members and admins only.",
        (
            _query(
                "status",
                "draft, open or closed.",
                _s("string", enum=["draft", "open", "closed"]),
            ),
        ),
    ),
    ToolSpec(
        "hr.opening", "hr", _READ, "GET", "/hr/openings/{ref}", "One opening",
        "One job opening in full, including how many have applied.",
        (_path("ref", "The opening's id or handle."),),
    ),
    ToolSpec(
        "hr.applications", "hr", _READ, "GET", "/hr/applications", "Applications",
        "Candidates who applied, and what stage each has reached. HR only.",
        (
            _query("opening_id", "Restrict to one opening."),
            _query(
                "stage",
                "new, shortlisted, interviewed, offered, hired or rejected.",
                _s(
                    "string",
                    enum=["new", "shortlisted", "interviewed", "offered", "hired", "rejected"],
                ),
            ),
        ),
    ),
    ToolSpec(
        "hr.application", "hr", _READ, "GET", "/hr/applications/{application_id}",
        "One candidate",
        "One application in full: their answers, their files and HR's notes.",
        (_path("application_id", "The application id."),),
    ),
    ToolSpec(
        "hr.documents", "hr", _READ, "GET", "/hr/documents", "Staff documents",
        "Employee documents across the company. HR only. Use "
        "expiring_within_days to answer 'whose papers are about to run out', "
        "which is the question this is usually asked for.",
        (
            _query("user_id", "Only this person's documents."),
            _query("expiring_within_days", "Only those expiring within this many days.", INT),
        ),
    ),
    ToolSpec(
        "hr.review_cycles", "hr", _READ, "GET", "/hr/review-cycles", "Review cycles",
        "Performance review cycles and their state. HR only.",
    ),
    ToolSpec(
        "hr.performance_for", "hr", _READ, "GET", "/hr/people/{user_id}/performance",
        "Somebody's performance",
        "One person's performance record. HR and admins only — this is somebody "
        "else's review history, and the endpoint refuses anybody else.",
        (_path("user_id", "The person's ERP user id."), _query("cycle_id", "One cycle only.")),
    ),
    # ── finance ────────────────────────────────────────────────────────
    ToolSpec(
        "finance.profit_and_loss", "finance", _READ, "GET", "/finance/profit-and-loss",
        "Profit and loss",
        "The company profit and loss for a period, computed from the Zoho Books "
        "ledger. Give either a named period or a pair of dates, not both. Super "
        "admin, CEO, manager or accountant only.",
        (
            Param(
                "period", "query",
                _s(
                    "string",
                    enum=[
                        "this_month", "last_month", "this_quarter", "last_quarter",
                        "this_year", "last_year", "last_12_months",
                    ],
                ),
                "A named period. Defaults to this_month.",
            ),
            _query("start", "First day, if not using a named period.", DATE),
            _query("end", "Last day, inclusive.", DATE),
        ),
    ),
    ToolSpec(
        "finance.comparison", "finance", _READ, "GET", "/finance/profit-and-loss/comparison",
        "Period against the one before",
        "This period's profit and loss beside the previous one, with the "
        "movement on each line. The tool for 'are we doing better than last "
        "month'.",
        (
            Param(
                "period", "query",
                _s(
                    "string",
                    enum=[
                        "this_month", "last_month", "this_quarter", "last_quarter",
                        "this_year", "last_year", "last_12_months",
                    ],
                ),
                "A named period. Defaults to this_month.",
            ),
            _query("start", "First day, if not using a named period.", DATE),
            _query("end", "Last day, inclusive.", DATE),
        ),
    ),
    ToolSpec(
        "finance.trend", "finance", _READ, "GET", "/finance/profit-and-loss/trend",
        "Monthly trend",
        "The same period cut by month, for a direction of travel rather than a "
        "single figure.",
        (
            Param(
                "period", "query",
                _s(
                    "string",
                    enum=[
                        "this_month", "last_month", "this_quarter", "last_quarter",
                        "this_year", "last_year", "last_12_months",
                    ],
                ),
                "A named period. Defaults to last_12_months for a trend.",
            ),
            _query("start", "First day, if not using a named period.", DATE),
            _query("end", "Last day, inclusive.", DATE),
        ),
    ),
    ToolSpec(
        "finance.account_postings", "finance", _READ, "GET",
        "/finance/accounts/{account_id}/postings", "What is behind a figure",
        "The individual documents making up one account's total, for when "
        "somebody asks why a line is what it is.",
        (
            _path("account_id", "The Zoho account id, from a profit and loss line."),
            _query("period", "A named period."),
            _query("start", "First day.", DATE),
            _query("end", "Last day, inclusive.", DATE),
        ),
    ),
    ToolSpec(
        "finance.diagnostics", "finance", _READ, "GET", "/finance/diagnostics",
        "Zoho data health",
        "What the Zoho token can actually read. Use it when the accounts look "
        "wrong or empty — it usually explains why before anybody guesses.",
    ),
    # ── work assignment ────────────────────────────────────────────────
    ToolSpec(
        "assignment.preview", "assignment", _READ, "GET", "/assignment/preview",
        "Who gets the next work",
        "How the current policy would share out the next piece of work, and the "
        "capacity behind each person's place in the order.",
        (_query("team", "The team's handle or id. Omit for the organisation default."),),
    ),
    ToolSpec(
        "assignment.policy", "assignment", _READ, "GET", "/assignment/policies/default",
        "The default policy",
        "The organisation-wide assignment policy: capacity ratios, limits and "
        "who is in the pool.",
    ),
    ToolSpec(
        "labels.people", "assignment", _READ, "GET", "/labels/people", "Everyone's labels",
        "The labels on each person — seniority, on leave, new joiner — including "
        "the ones applied automatically.",
        (
            _query("team", "Judge team-scoped labels against this team."),
            _query("new_joiner_days", "How recent counts as a new joiner.", INT),
        ),
    ),
    ToolSpec(
        "labels.list", "assignment", _READ, "GET", "/labels", "Label catalogue",
        "Every label that exists and what it means.",
        (_query("team", "Include this team's own labels."),),
    ),
    # ── who gets the next job ──────────────────────────────────────────
    ToolSpec(
        "analytics.preview", "assignment", _READ, "GET", "/analytics/preview",
        "Rank people for the next job",
        "Ranks a team's members for the next assignment and shows why each "
        "scored what they did. Keeps nothing.",
        (
            Param("team", "query", STR, "The team's handle. Required.", required=True),
            _query("refresh", "Re-read SharePoint instead of the cache.", BOOL),
        ),
    ),
    ToolSpec(
        "analytics.runs", "assignment", _READ, "GET", "/analytics/runs", "Kept rankings",
        "Rankings that were computed and kept, for comparing against now.",
        (_query("team", "Only this team's runs."), _query("limit", "Maximum.", INT)),
    ),
    # ── form templates ─────────────────────────────────────────────────
    ToolSpec(
        "templates.list", "templates", _READ, "GET", "/templates", "Form templates",
        "The forms this system asks people to fill in, and their state. Super "
        "admin only.",
        (
            _query("kind", "Only this kind of form."),
            _query("include_archived", "Include retired ones.", BOOL),
        ),
    ),
    ToolSpec(
        "templates.usable", "templates", _READ, "GET", "/templates/usable",
        "Forms I can fill in",
        "The forms the signed-in person may actually use.",
        (_query("kind", "Only this kind of form."),),
    ),
    # ── projects ───────────────────────────────────────────────────────
    #
    # Reads first and by some distance the most used: "which project is that"
    # is the question in front of every other one, including opening it.
    ToolSpec(
        "projects.list", "projects", _READ, "GET", "/projects",
        "Projects I can see",
        "Every project the person may see, newest first. THE way to turn a "
        "project's name into its id — search this before opening one, or "
        "before any other tool that takes a project id.",
        (
            _query("team", "Team handle (slug) or id."),
            _query("status", "planned, active, on_hold, done or dropped.", STR_LIST),
            _query("mine_only", "Only projects this person is on.", BOOL),
            _query("include_archived", "Include archived projects.", BOOL),
            _query("limit", "How many (1-200, default 50).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "projects.get", "projects", _READ, "GET", "/projects/{project_id}",
        "One project in full",
        "Everything about one project: its health dials, milestones with their "
        "dates and slippage, every task with who holds it, open issues, and the "
        "people on it. What to read before answering anything about a project "
        "somebody is looking at.",
        (_path("project_id", "The project id."),),
    ),
    ToolSpec(
        "projects.board", "projects", _READ, "GET", "/projects/board",
        "My work and my projects",
        "The signed-in person's own projects and the tasks they hold across "
        "all of them. The quickest answer to 'what am I meant to be doing'.",
    ),
    ToolSpec(
        "projects.my_tasks", "projects", _READ, "GET", "/projects/my-tasks",
        "My tasks across projects",
        "Just the tasks this person holds, across every project, with the "
        "project each belongs to.",
        (
            _query("open_only", "Only tasks that are not finished.", BOOL),
            _query("limit", "How many.", INT),
        ),
    ),
    ToolSpec(
        "projects.portfolio", "projects", _READ, "GET", "/projects/portfolio",
        "Every project at a glance",
        "One line per project across the whole portfolio: health, percentage, "
        "tasks done against open, and what is overdue. For 'how is everything "
        "going' rather than for one project.",
    ),
    ToolSpec(
        "projects.activity", "projects", _READ, "GET", "/projects/activity",
        "What moved over a period",
        "The progress log over a window: tasks that moved, milestones hit, "
        "issues raised, notes filed. Use it for 'what happened this week' — the "
        "project's own figures answer 'where is it now' and cannot answer this.",
        (
            _query("grain", "day, week, month, quarter, year or custom."),
            _query("on", "Any day inside the period.", DATE),
            _query("since", "custom only.", DATE),
            _query("until", "custom only.", DATE),
            _query("team", "Team handle or id."),
            _query("project_id", "One project, or leave out for all."),
            _query("kind", "task, health, milestone, issue, note.", STR_LIST),
            _query("mine_only", "Only this person's own updates.", BOOL),
        ),
    ),
    ToolSpec(
        "projects.updates", "projects", _READ, "GET", "/projects/{project_id}/updates",
        "One project's progress log",
        "Everything written down against one project, newest first.",
        (
            _path("project_id", "The project id."),
            _query("limit", "How many.", INT),
        ),
    ),
    # The two writes people say out loud. Everything else about a project —
    # creating one, moving a milestone, changing the health dials — is a
    # deliberate act done on the screen, where the consequences are visible.
    ToolSpec(
        "projects.move_task", "projects", _WRITE, "PATCH",
        "/projects/{project_id}/tasks/{task_id}",
        "Move a task along",
        "Change how far along a task is, or its status, and write a line in the "
        "progress log saying so. Only the fields given change. Read the task "
        "back first if you did not just look at it — 'mark it done' means the "
        "task they are talking about, not the first one in the list.",
        (
            _path("project_id", "The project id."),
            _path("task_id", "The task id."),
            _body("status", "not_started, in_progress, blocked, done or dropped."),
            _body("percent_complete", "0-100.", INT),
            _body("blocked_reason", "Why it is stuck. Say this when marking it blocked."),
            _body("note", "A line for the progress log alongside the change."),
            _body("hours", "Hours to add to the time already spent.", _s("number")),
        ),
        warning="Changes what the project says about this task, and tells "
                "whoever holds it.",
    ),
    ToolSpec(
        "projects.note", "projects", _WRITE, "POST", "/projects/{project_id}/updates",
        "Write a progress note",
        "A note on the project's log. No numbers move — this is for saying what "
        "happened, or why nothing did.",
        (
            _path("project_id", "The project id."),
            _body("body", "What to write down.", required=True),
        ),
    ),
    # ── reports ────────────────────────────────────────────────────────
    #
    # The same routes the page uses, so what comes back is already narrowed to
    # what the caller may see. That is the whole of the access model here: a
    # manager asking reports.list gets their team's, an ordinary person asking
    # the identical tool gets their own, and neither the model nor a tampered
    # argument can change which.
    ToolSpec(
        "reports.mine", "reports", _READ, "GET", "/reports",
        "My reports",
        "The signed-in person's own reports, newest period first. Pass mine=true "
        "for only theirs; leave it off to include anything they are entitled to "
        "read from teams they run.",
        (
            _query("mine", "Only the caller's own.", BOOL),
            _query("cadence", "daily, weekly, monthly or ad_hoc."),
            _query("status", "draft or submitted."),
            _query("since", "Reports whose period ends on or after this.", DATE),
            _query("until", "Reports whose period starts on or before this.", DATE),
            _query("limit", "How many. Up to 200.", INT),
        ),
    ),
    ToolSpec(
        "reports.list", "reports", _READ, "GET", "/reports",
        "Reports for a team",
        "Reports for one team, for somebody who may read them — a team manager "
        "or lead, or a CEO, manager or super admin. Anybody else gets an empty "
        "list rather than an error, because the honest answer to 'show me "
        "presales' reports' from somebody who cannot read them is that there "
        "are none they can see.",
        (
            _query("team", "Team handle (slug) or id."),
            _query("author_id", "One person's, by ERP user id."),
            _query("cadence", "daily, weekly, monthly or ad_hoc."),
            _query("since", "Reports whose period ends on or after this.", DATE),
            _query("until", "Reports whose period starts on or before this.", DATE),
            _query("limit", "How many. Up to 200.", INT),
        ),
    ),
    ToolSpec(
        "reports.get", "reports", _READ, "GET", "/reports/{report_id}",
        "One report in full",
        "The whole report: overview, every task with its status and links, the "
        "issues, the metrics, the remarks and the summary. Use it before "
        "summarising or analysing one — the list gives counts, this gives what "
        "was actually written.",
        (_path("report_id", "The report id."),),
    ),
    ToolSpec(
        "reports.form", "reports", _READ, "GET", "/reports/form",
        "What a report will ask",
        "The sections and this team's own questions, before one is started. Use "
        "it to tell somebody what they are about to be asked, and to learn which "
        "extra fields this particular team's report has.",
        (
            _query("team", "Team handle (slug) or id."),
            _query("cadence", "daily, weekly, monthly or ad_hoc."),
            _query("on", "A day inside the period. Defaults to today.", DATE),
        ),
    ),
    ToolSpec(
        "reports.overview", "reports", _READ, "GET", "/reports/overview",
        "What the reports say together",
        "Figures and open issues across every report the caller may read, over a "
        "period. This is the tool for 'how did presales do last month' and 'what "
        "is blocking us'. Narrowed to what they are entitled to see, so an "
        "ordinary person gets their own reports summarised and nobody else's.",
        (
            _query("team", "Team handle or id. Leave off for every team they can read."),
            _query("since", "From this day. Defaults to 30 days ago.", DATE),
            _query("until", "To this day. Defaults to today.", DATE),
        ),
    ),
    ToolSpec(
        "reports.start", "reports", _WRITE, "POST", "/reports",
        "Start a report",
        "Open a draft for a period, prefilled with the person's own Proposals "
        "tasks — which brings each bid's title, status, deadline, its link and "
        "its attachments. Nothing is filed yet. Follow it with reports.fill to "
        "put the words in and reports.submit to send it.",
        (
            _body("team_id", "The team's id. Use me.teams to find it.", required=True),
            _body(
                "cadence",
                "daily, weekly, monthly or ad_hoc.",
                _s("string", enum=["daily", "weekly", "monthly", "ad_hoc"]),
                required=True,
            ),
            _body("on", "A day inside the period. Defaults to today.", DATE),
            _body("period_start", "Ad-hoc only: when the period starts.", DATE),
            _body("period_end", "Ad-hoc only: when it ends.", DATE),
            _body("prefill_tasks", "Pull their Proposals tasks in. Default true.", BOOL),
            _body("include_closed", "Include finished tasks. Default false.", BOOL),
        ),
        warning="Opens a draft. Nothing is sent to anybody until it is submitted.",
    ),
    ToolSpec(
        "reports.fill", "reports", _WRITE, "PATCH", "/reports/{report_id}",
        "Fill in a draft report",
        "Write into a draft. Only the parts given change — but a list given at "
        "all replaces that whole section, so send every task row you want kept, "
        "not just the new one. Read the report back first if you did not just "
        "create it. Never invent a figure or a status the person did not give "
        "you: an overview you wrote for them is still filed under their name.",
        (
            _path("report_id", "The report id."),
            _body("overview", "What the period was about, in a few lines."),
            _body("remarks", "Anything worth saying that is not a task or a blocker."),
            _body("summary", "What the reader should take away, and what is next."),
            _body(
                "tasks",
                "The whole tasks section, replacing what is there.",
                _s("array", items=_object(
                    {
                        "title": STR,
                        "completion": _s(
                            "string",
                            enum=["not_started", "in_progress", "blocked", "done", "dropped"],
                        ),
                        "external_id": _s(
                            "string", description="The Proposals task id, for a pulled row."
                        ),
                        "status": STR,
                        "note": STR,
                        "link": STR,
                        "deadline": DATE,
                        "percent_complete": INT,
                    },
                    required=("title", "completion"),
                )),
            ),
            _body(
                "issues",
                "The whole issues section, replacing what is there.",
                _s("array", items=_object(
                    {
                        "title": STR,
                        "detail": STR,
                        "severity": _s(
                            "string", enum=["low", "medium", "high", "blocked"]
                        ),
                        "waiting_on": _s(
                            "string",
                            description="Who or what it waits on — often outside this system.",
                        ),
                        "resolved": BOOL,
                    },
                    required=("title", "severity"),
                )),
            ),
            # Pairs rather than an object keyed by metric, and not by choice:
            # strict mode has no way to express "an object whose keys I do not
            # know in advance" — an open object is rejected outright, and the
            # keys here are whatever this team's template asks for. The route
            # accepts either shape; see ReportEditIn.
            _body(
                "metrics",
                "Figures, as key/value pairs. The task counts are worked out "
                "already and only need sending to correct one; the team's own "
                "metrics — see reports.form for which — are typed in.",
                _s("array", items=_object(
                    {"key": STR, "value": _s("number")},
                    required=("key", "value"),
                )),
            ),
            _body(
                "answers",
                "This team's own questions, as key/value pairs. reports.form "
                "lists which keys this team's report has. Merged into what is "
                "already there, so you can send one answer at a time as you "
                "learn it; send a key with nothing in it to clear one.",
                _s("array", items=_object(
                    {
                        "key": STR,
                        "value": _s(["string", "number", "boolean"]),
                    },
                    required=("key", "value"),
                )),
            ),
        ),
        warning="Changes the draft. Anything sent as a list replaces that whole "
                "section.",
    ),
    ToolSpec(
        "reports.submit", "reports", _WRITE, "POST", "/reports/{report_id}/submit",
        "File a report",
        "Submit the draft. After this it cannot be changed by anybody, and it is "
        "emailed to the team's managers and leads, the CEO and the super admins. "
        "Read the report back to the person and get their agreement before "
        "calling this — it is not undoable and it goes to their management.",
        (_path("report_id", "The report id."),),
        warning="Files the report and emails it to the team's managers, the CEO "
                "and the super admins. It cannot be edited afterwards.",
    ),
    ToolSpec(
        "reports.comment", "reports", _WRITE, "POST", "/reports/{report_id}/comments",
        "Comment on a report",
        "A reader's remark on somebody else's submitted report. Decides nothing "
        "and changes nothing about the report. An author cannot comment on their "
        "own — what they have to add belongs in the next report.",
        (
            _path("report_id", "The report id."),
            _body("body", "What to say.", required=True),
        ),
        warning="The author of the report will see this.",
    ),
    # ── the writes that were held back ─────────────────────────────────
    #
    # These five were written down and left ``planned`` while the assistant was
    # only being read from. They are live now, with the arguments filled in that
    # a real call needs — a live tool whose body is half described is a tool
    # that fails on every use, which is worse than one that is honestly absent.
    #
    # Each of them still goes through its own route with the caller's session,
    # so "may this person do this" is answered where it always was: by the
    # Proposals list's ownership check, by the quote's approver list, by HR team
    # membership. What is decided here is only whether the assistant may ask.
    ToolSpec(
        "proposals.update_task", "proposals", _WRITE, "PATCH", "/proposals/tasks/{task_id}",
        "Update one of my proposal tasks",
        "Change the status, dates or notes on one of the signed-in person's own "
        "proposal tasks. Only the fields given change; everything else is left "
        "alone. Reassigning a task is not possible here. Dates are SharePoint's "
        "own format, e.g. 2026-09-30T00:00:00Z. Look the task up with "
        "proposals.my_tasks first and use the id it gives you.",
        (
            _path("task_id", "The SharePoint task id, from proposals.my_tasks."),
            _body("status", "New status, worded as the Proposals list words it."),
            _body("priority", "New priority, as the list words it."),
            _body("due_date", "New due date, ISO 8601 with a time."),
            _body("bid_closing_date", "New bid closing date, ISO 8601 with a time."),
            _body("submission_status", "New submission status."),
            _body("quote_no", "The quote number to record against the task."),
            _body("remarks", "Remarks, replacing what is there."),
            _body("working_notes", "Working notes, replacing what is there."),
        ),
        warning="This writes to the live Proposals list the team works in.",
    ),
    ToolSpec(
        "quote_requests.create", "quote_requests", _WRITE, "POST", "/quote-requests",
        "Raise a quote request",
        "Start a new customer quote for a team, with its priced lines. Read the "
        "customer, the currency and every line back to the person before calling "
        "this: a quote raised from a misheard figure is worse than no quote at "
        "all. Rates are per unit and the totals are worked out for you. Leave "
        "the dates out and the system uses its own.",
        (
            _query("team", "The team's handle (slug) or id. Required."),
            _body("title", "What the quote is for.", required=True),
            _body("customer_name", "The customer, as they should appear.", required=True),
            _body(
                "items",
                "The priced lines. Each needs a name; quantity defaults to 1 and "
                "rate to 0, so say both back before saving.",
                _s("array", items=_object(
                    {
                        "name": STR,
                        "description": STR,
                        "item_code": STR,
                        "brand": STR,
                        "unit": STR,
                        "quantity": _s("number"),
                        "rate": _s("number", description="Unit price."),
                    },
                    required=("name",),
                )),
                required=True,
            ),
            _body("currency", "Three-letter code. Defaults to AED."),
            _body("customer_id", "The customer's Zoho id, when it is known."),
            _body("contact_person", "Who at the customer asked."),
            _body("reference_number", "The customer's own PO or enquiry number."),
            _body("quote_date", "Quote date.", DATE),
            _body("expiry_date", "When the quote lapses.", DATE),
            _body("payment_terms", "Payment terms, in words."),
            _body("delivery_terms", "Delivery terms, in words."),
            _body("subject", "Subject line for the quote."),
            _body("notes", "Notes for the customer."),
        ),
        warning="This raises a real quote request that the approvers will see.",
    ),
    ToolSpec(
        "quote_requests.submit", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/submit", "Send a quote for approval",
        "Hand a drafted quote to its approvers. They are emailed a link to it "
        "immediately, so this is not undone by doing nothing. The route refuses "
        "a quote that is not ready, or not the caller's to send.",
        (_path("request_id", "The quote request id."),),
        warning="The approvers are emailed the moment this is sent.",
    ),
    ToolSpec(
        "quote_requests.review", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/reviews", "Approve, reject or send back a quote",
        "An approver's decision on a quote request. 'approve' releases it, "
        "'reject' refuses it, 'rework' sends it back to whoever raised it, and "
        "'comment' decides nothing and leaves it where it is. A rejection or a "
        "rework needs a note saying why. Approving a quote that carries several "
        "supplier offers means naming the one that won.",
        (
            _path("request_id", "The quote request id."),
            _body(
                "action",
                "One of approve, reject, rework, comment.",
                _s("string", enum=["approve", "reject", "rework", "comment"]),
                required=True,
            ),
            _body("note", "Why. Required for a rejection or a rework."),
            _body(
                "selected_supplier_quote_id",
                "Which supplier quote won. Required when approving one that has "
                "several.",
            ),
        ),
        warning="An approval releases the quote to be created in Zoho, and the "
                "decision is emailed to the requester.",
    ),
    ToolSpec(
        "hr.move_candidate", "hr", _WRITE, "POST", "/hr/applications/{application_id}/stage",
        "Move a candidate along",
        "Shortlist, interview, offer, hire or reject a candidate. 'withdrawn' is "
        "for somebody who pulled out themselves and is kept apart from "
        "'rejected' on purpose — the distinction matters if they apply again. "
        "Only the HR team may do this; the route refuses anybody else.",
        (
            _path("application_id", "The application id."),
            _body(
                "stage",
                "The stage to move them to.",
                _s(
                    "string",
                    enum=[
                        "new", "shortlisted", "interviewed", "offered",
                        "hired", "rejected", "withdrawn",
                    ],
                ),
                required=True,
            ),
            _body("note", "Why, for the record."),
        ),
        warning="Candidates may be told about stage changes.",
    ),
    # ── the last of the held-back reads ─────────────────────────────────
    #
    # ``zoho.read`` was planned until the decision was taken to give the
    # assistant the whole surface, bounded by the person's own access rather
    # than by a shortlist. The ``planned`` status is kept in the type for the
    # next tool that is written down before it is built.
    ToolSpec(
        "zoho.read", "finance", _READ, "GET", "/finance/zoho/{endpoint_key}",
        "Read a Zoho endpoint directly",
        "Read one Zoho Books endpoint straight through, by the key finance.zoho "
        "lists. The widest read in the system and the rawest: it answers with "
        "Zoho's own payload, so prefer the finance.* tools when one covers the "
        "question, and reach for this only for a figure they do not carry. "
        "Restricted to the finance roles by the route.",
        (
            _path("endpoint_key", "Which endpoint, from finance.zoho."),
            _query("page", "Page number, for endpoints that page.", INT),
            _query("per_page", "Rows per page, for endpoints that page.", INT),
        ),
    ),


    # ── projects, reports, proposals, notifications ───────────────────
    # ── projects ───────────────────────────────────────────────────────
    ToolSpec(
        "projects.create", "projects", _WRITE, "POST", "/projects",
        "Start a project",
        "Start a new project on a team. Only that team's lead or manager, or "
        "somebody running the company, may — anybody else is refused. Takes the "
        "team's id (use me.teams or teams.get to find it), not its handle. The "
        "person creating it is put on the project; the lead, if named, is put "
        "on it as lead. Read the name, team and dates back before calling this.",
        (
            _body("team_id", "The team's id, not its handle.", required=True),
            _body("name", "The project's name.", required=True),
            _body("code", "A short reference code, up to 32 characters."),
            _body("description", "What the project is, at length."),
            _body("objective", "What it is meant to achieve, in a few lines."),
            _body(
                "status",
                "planned, active, on_hold, done or cancelled. Defaults to planned.",
                _PROJECT_STATUS,
            ),
            _body("lead_id", "ERP user id of the person who will run it."),
            _body("start_on", "When it starts.", DATE),
            _body("target_end_on", "When it is meant to finish.", DATE),
            _body("budget_amount", "The budget, in the project's currency.", _s("number")),
            _body("currency", "Three-letter code. Defaults to AED."),
        ),
        warning="Creates a real project the team will see on its board.",
    ),
    ToolSpec(
        "projects.update", "projects", _WRITE, "PATCH", "/projects/{project_id}",
        "Change a project",
        "Change a project's name, code, description, objective, status, lead, "
        "dates, budget or spend. Only the fields given change. Project lead or "
        "somebody running the team only. This does not assess the health dials "
        "— that is projects.set_health, and it is kept apart so that saving a "
        "description never claims the health was reviewed.",
        (
            _PROJECT_ID,
            _body("name", "New name."),
            _body("code", "New reference code."),
            _body("description", "New description."),
            _body("objective", "New objective."),
            _body("status", "planned, active, on_hold, done or cancelled.", _PROJECT_STATUS),
            _body("lead_id", "ERP user id of the new lead."),
            _body("start_on", "New start date.", DATE),
            _body("target_end_on", "New target finish date.", DATE),
            _body("actual_end_on", "When it actually finished.", DATE),
            _body("budget_amount", "New budget.", _s("number")),
            _body("spend_amount", "Spend to date.", _s("number")),
            _body("currency", "Three-letter code."),
            _body(
                "percent_complete",
                "The lead's own completion figure, 0-100. Leave out to let the "
                "tasks decide it, which is usually right.",
                INT,
            ),
        ),
        warning="Changes what everyone on the project sees about it.",
    ),
    ToolSpec(
        "projects.set_health", "projects", _WRITE, "PUT", "/projects/{project_id}/health",
        "Assess the health dials",
        "Record the lead's judgement of the five dials — overall, scope, cost, "
        "schedule, benefits — each as a colour and a trend, with a note. Every "
        "dial is optional but sending this at all stamps the project as reviewed "
        "now, so call it only when the person is actually assessing health, not "
        "as a side effect of something else. Project lead or somebody running "
        "the team only. projects.get shows what the dates and budget suggest "
        "beside what is stored; read that first.",
        (
            _PROJECT_ID,
            _body("rag_overall", "green, amber, red or grey.", _RAG),
            _body("rag_scope", "green, amber, red or grey.", _RAG),
            _body("rag_cost", "green, amber, red or grey.", _RAG),
            _body("rag_schedule", "green, amber, red or grey.", _RAG),
            _body("rag_benefits", "green, amber, red or grey.", _RAG),
            _body("trend_overall", "improving, steady or declining.", _TREND),
            _body("trend_scope", "improving, steady or declining.", _TREND),
            _body("trend_cost", "improving, steady or declining.", _TREND),
            _body("trend_schedule", "improving, steady or declining.", _TREND),
            _body("trend_benefits", "improving, steady or declining.", _TREND),
            _body("note", "Why the dials say what they say."),
        ),
        warning="Stamps the project as reviewed now and writes the assessment to "
                "the progress log.",
    ),
    ToolSpec(
        "projects.archive", "projects", _WRITE, "POST", "/projects/{project_id}/archive",
        "Archive a project",
        "Archive a project so it drops off the board and the portfolio. Only "
        "somebody running the team may. Reversible with projects.restore; "
        "nothing is deleted.",
        (_PROJECT_ID,),
        warning="The project disappears from everyone's board until restored.",
    ),
    ToolSpec(
        "projects.restore", "projects", _WRITE, "POST", "/projects/{project_id}/restore",
        "Restore a project",
        "Bring an archived project back onto the board. Only somebody running "
        "the team may. Find archived ones with projects.list and "
        "include_archived=true.",
        (_PROJECT_ID,),
        warning="The project reappears on the team's board.",
    ),
    ToolSpec(
        "projects.delete", "projects", _WRITE, "DELETE", "/projects/{project_id}",
        "Delete a project",
        "Permanently delete a project and everything under it — milestones, "
        "tasks, issues and the progress log. Only somebody running the team "
        "may. Reports already filed against it survive with their own copy of "
        "its figures. Prefer projects.archive unless the person clearly wants "
        "it gone for good.",
        (_PROJECT_ID,),
        warning="Permanent. Milestones, tasks, issues and the whole progress log "
                "go with it.",
    ),
    ToolSpec(
        "projects.set_member", "projects", _WRITE, "PUT", "/projects/{project_id}/members",
        "Add or change a member",
        "Put somebody on a project, or change the role they hold on it: lead, "
        "member or viewer. Project lead or somebody running the team only. "
        "Takes the person's ERP user id — look them up with teams.members or "
        "users.profile first. Somebody already on the project has their role "
        "and responsibility replaced.",
        (
            _PROJECT_ID,
            _body("user_id", "The person's ERP user id.", required=True),
            _body(
                "role",
                "lead, member or viewer. Defaults to member.",
                _s("string", enum=["lead", "member", "viewer"]),
            ),
            _body("responsibility", "What they are on the project for, in a few words."),
        ),
        warning="Replaces whatever role they already hold on this project.",
    ),
    ToolSpec(
        "projects.remove_member", "projects", _WRITE, "DELETE",
        "/projects/{project_id}/members/{user_id}",
        "Take somebody off a project",
        "Remove a person from a project. Project lead or somebody running the "
        "team only. Refused while they still hold open tasks on it — reassign "
        "those with projects.update_task first.",
        (_PROJECT_ID, _path("user_id", "The person's ERP user id.")),
        warning="They lose sight of the project unless the team grant still gives it.",
    ),
    ToolSpec(
        "projects.add_milestone", "projects", _WRITE, "POST",
        "/projects/{project_id}/milestones",
        "Add a milestone",
        "Add a milestone to a project's plan. Project lead or somebody running "
        "the team only. The due date given becomes its baseline, which slippage "
        "is measured against from then on.",
        (
            _PROJECT_ID,
            _body("name", "The milestone's name.", required=True),
            _body("detail", "What reaching it means."),
            _body("owner_id", "ERP user id of whoever owns it."),
            _body("start_on", "When work towards it starts.", DATE),
            _body("due_on", "When it is due. Becomes the baseline.", DATE),
            _body("is_key", "A key milestone, shown prominently. Default false.", BOOL),
        ),
        warning="Adds to the project's plan and writes a line in the progress log.",
    ),
    ToolSpec(
        "projects.update_milestone", "projects", _WRITE, "PATCH",
        "/projects/{project_id}/milestones/{milestone_id}",
        "Change a milestone",
        "Change a milestone's name, owner, dates, plan status or order, or mark "
        "it done. Only the fields given change. Project lead or somebody "
        "running the team only. Moving the due date records a slip against the "
        "baseline; the percentage is only accepted for a milestone with no "
        "tasks, since otherwise the tasks decide it.",
        (
            _PROJECT_ID,
            _path("milestone_id", "The milestone id, from projects.get."),
            _body("name", "New name."),
            _body("detail", "New detail."),
            _body("owner_id", "ERP user id of the new owner."),
            _body("start_on", "New start date.", DATE),
            _body("due_on", "New due date. A move is recorded as slippage.", DATE),
            _body("done_on", "The day it was reached.", DATE),
            _body("percent_complete", "0-100. Only for a milestone with no tasks.", INT),
            _body("plan", "on_plan, off_plan_no_impact or off_plan_impact.", _PLAN),
            _body("is_key", "Whether it is a key milestone.", BOOL),
            _body("position", "Its place in the list, from 0.", INT),
        ),
        warning="Changes the plan everyone on the project works to.",
    ),
    ToolSpec(
        "projects.delete_milestone", "projects", _WRITE, "DELETE",
        "/projects/{project_id}/milestones/{milestone_id}",
        "Delete a milestone",
        "Remove a milestone from the plan. Project lead or somebody running the "
        "team only. The tasks under it stay on the project, just no longer "
        "grouped.",
        (_PROJECT_ID, _path("milestone_id", "The milestone id, from projects.get.")),
        warning="Permanent. Its tasks stay but lose their grouping.",
    ),
    ToolSpec(
        "projects.add_task", "projects", _WRITE, "POST", "/projects/{project_id}/tasks",
        "Add a task",
        "Add a task to a project, optionally under a milestone and assigned to "
        "somebody. Project lead or somebody running the team only. Whoever it "
        "is assigned to is put on the project if they are not already, and is "
        "told about it. Takes ERP user ids for the assignee and the ids from "
        "projects.get for the milestone.",
        (
            _PROJECT_ID,
            _body("title", "What the task is.", required=True),
            _body("detail", "More about it."),
            _body("milestone_id", "The milestone it belongs under."),
            _body("assignee_id", "ERP user id of who will do it."),
            _body(
                "status",
                "not_started, in_progress, blocked, done or dropped. Defaults to "
                "not_started.",
                _TASK_STATE,
            ),
            _body("priority", "low, medium, high or critical. Defaults to medium.", _PRIORITY),
            _body("start_on", "When it starts.", DATE),
            _body("due_on", "When it is due.", DATE),
            _body("estimate_hours", "How long it should take, in hours.", _s("number")),
        ),
        warning="Adds work to somebody's list and tells them about it.",
    ),
    ToolSpec(
        "projects.update_task", "projects", _WRITE, "PATCH",
        "/projects/{project_id}/tasks/{task_id}",
        "Change a task",
        "The full edit of a task: retitle it, move it under a milestone, hand it "
        "to somebody else, change its priority, dates, estimate or order — as "
        "well as the progress fields. Only the fields given change. Handing it "
        "to somebody, moving it, retitling it, or changing its due date or "
        "priority is the project lead's call; an ordinary assignee is refused "
        "on those and should use projects.move_task for progress. Read the task "
        "back first if you did not just look at it.",
        (
            _PROJECT_ID,
            _path("task_id", "The task id, from projects.get or projects.my_tasks."),
            _body("title", "New title."),
            _body("detail", "New detail."),
            _body("milestone_id", "Move it under this milestone."),
            _body("assignee_id", "ERP user id of who should now hold it."),
            _body("status", "not_started, in_progress, blocked, done or dropped.", _TASK_STATE),
            _body("priority", "low, medium, high or critical.", _PRIORITY),
            _body("percent_complete", "0-100.", INT),
            _body("start_on", "New start date.", DATE),
            _body("due_on", "New due date.", DATE),
            _body("estimate_hours", "New estimate, in hours.", _s("number")),
            _body("blocked_reason", "Why it is stuck. Say this when marking it blocked."),
            _body("position", "Its place in the list, from 0.", INT),
            _body("note", "A line for the progress log alongside the change."),
            _body("hours", "Hours to add to the time already spent.", _s("number")),
        ),
        warning="Changes the task for everyone on the project, and tells whoever "
                "holds it.",
    ),
    ToolSpec(
        "projects.delete_task", "projects", _WRITE, "DELETE",
        "/projects/{project_id}/tasks/{task_id}",
        "Delete a task",
        "Remove a task from a project entirely. Project lead or somebody running "
        "the team only. If the work was abandoned rather than mistaken, marking "
        "it dropped with projects.move_task keeps the record; this does not.",
        (_PROJECT_ID, _path("task_id", "The task id, from projects.get.")),
        warning="Permanent. The task and its history on the log's rows go.",
    ),
    ToolSpec(
        "projects.raise_issue", "projects", _WRITE, "POST", "/projects/{project_id}/issues",
        "Raise an issue",
        "Raise an issue or blocker on a project. Anybody on the project may — "
        "this is deliberately wider than changing the plan, because the person "
        "who trips over a problem is rarely the one running the project. Set "
        "needs_support with a note to put it in the 'support needed' box of the "
        "next status report.",
        (
            _PROJECT_ID,
            _body("title", "What the issue is.", required=True),
            _body("detail", "More about it."),
            _body("priority", "low, medium, high or critical. Defaults to medium.", _PRIORITY),
            _body("owner_id", "ERP user id of who should resolve it."),
            _body("due_on", "When it needs resolving by.", DATE),
            _body("needs_support", "Escalate it onto the next status report.", BOOL),
            _body("support_note", "What support is needed, if escalating."),
        ),
        warning="Everyone on the project sees the issue, and the owner is told.",
    ),
    ToolSpec(
        "projects.update_issue", "projects", _WRITE, "PATCH",
        "/projects/{project_id}/issues/{issue_id}",
        "Change an issue",
        "Work an issue: change its status, priority, owner, due date or support "
        "flag, or resolve it. Only the fields given change. Its owner, whoever "
        "raised it, or somebody running the project may; anybody else is "
        "refused. Marking it resolved or closed records when.",
        (
            _PROJECT_ID,
            _path("issue_id", "The issue id, from projects.get."),
            _body("title", "New title."),
            _body("detail", "New detail."),
            _body("status", "open, in_progress, resolved or closed.", _ISSUE_STATE),
            _body("priority", "low, medium, high or critical.", _PRIORITY),
            _body("owner_id", "ERP user id of the new owner."),
            _body("due_on", "New due date.", DATE),
            _body("needs_support", "Whether it goes on the next status report.", BOOL),
            _body("support_note", "What support is needed."),
            _body("position", "Its place in the list, from 0.", INT),
        ),
        warning="Changes the issue for everyone on the project.",
    ),
    ToolSpec(
        "projects.delete_issue", "projects", _WRITE, "DELETE",
        "/projects/{project_id}/issues/{issue_id}",
        "Delete an issue",
        "Remove an issue from a project entirely. Project lead or somebody "
        "running the team only. Resolving it with projects.update_issue keeps "
        "the record; this does not.",
        (_PROJECT_ID, _path("issue_id", "The issue id, from projects.get.")),
        warning="Permanent. The issue is gone rather than resolved.",
    ),
    # ── reports ────────────────────────────────────────────────────────
    ToolSpec(
        "reports.delete", "reports", _WRITE, "DELETE", "/reports/{report_id}",
        "Delete a report",
        "Remove a report. An author may remove their own draft; a submitted "
        "report can only be removed by a super admin. Anybody else is refused.",
        (_path("report_id", "The report id."),),
        warning="Permanent. A filed report is a record, and removing one is not "
                "undone.",
    ),
    ToolSpec(
        "reports.brief", "reports", _READ, "GET", "/reports/{report_id}/brief",
        "The short version of a report",
        "The AI-written paragraph summarising one submitted report, if the "
        "administrator has turned briefs on. Comes back with a state — "
        "disabled, not_applicable (a draft), absent, failed, stale or ready — "
        "and whether this reader may ask for it again. Shown to exactly the "
        "people who may read the report. Under some settings the first reader "
        "to ask is the one who waits for it to be written.",
        (_path("report_id", "The report id."),),
    ),
    ToolSpec(
        "reports.refresh_brief", "reports", _WRITE, "POST", "/reports/{report_id}/brief",
        "Write the brief again",
        "Ask for the brief on a submitted report to be written again, usually "
        "because the first one was too short or missed what the reader cares "
        "about. Costs a model call each time, and the administrator may have "
        "switched it off — read reports.brief first and check may_refresh.",
        (_path("report_id", "The report id."),),
        warning="Replaces the stored brief for everyone who reads this report.",
    ),
    ToolSpec(
        "reports.settings", "reports", _READ, "GET", "/reports/admin/settings",
        "Report delivery settings",
        "Who filed reports are emailed to, what the email carries, and how the "
        "AI brief is configured. Super admin only. These control delivery, not "
        "who may read a report.",
    ),
    ToolSpec(
        "reports.update_settings", "reports", _WRITE, "PATCH", "/reports/admin/settings",
        "Change report delivery settings",
        "Change who filed reports go to, what the email includes, and how the AI "
        "brief behaves. Only the fields given change. Super admin only. A "
        "common change is dropping daily from notify_cadences so managers are "
        "not mailed thirty times a week.",
        (
            _body("notify_on_submit", "Email anybody at all when a report is filed.", BOOL),
            _body("notify_team_oversight", "Email the team's managers and leads.", BOOL),
            _body("notify_company_wide", "Email holders of the company roles.", BOOL),
            _body(
                "company_roles",
                "Global role keys to email, e.g. ceo, super_admin. Every one must "
                "exist.",
                STR_LIST,
            ),
            _body("extra_recipients", "Extra email addresses, always copied.", STR_LIST),
            _body("copy_author", "Copy the author on their own report.", BOOL),
            _body(
                "notify_cadences",
                "Which cadences are emailed at all: daily, weekly, monthly, ad_hoc.",
                STR_LIST,
            ),
            _body("max_tasks_in_email", "How many task rows the email carries, 0-100.", INT),
            _body("include_task_list", "Put the task rows in the email.", BOOL),
            _body("include_issue_list", "Put the issues in the email.", BOOL),
            _body("log_retention_days", "How long delivery records are kept, 1-3650.", INT),
            _body("brief_enabled", "Turn the AI brief on or off.", BOOL),
            _body(
                "brief_mode",
                "on_submit writes it as the report is filed, on_first_open when "
                "the first manager opens it, on_request only when asked.",
                _s("string", enum=["on_submit", "on_first_open", "on_request"]),
            ),
            _body(
                "brief_followup",
                "off: read it only; refresh: may ask again; chat: may ask it questions.",
                _s("string", enum=["off", "refresh", "chat"]),
            ),
            _body(
                "brief_model_key",
                "An assistant model key, or empty to follow the assistant's own.",
            ),
            _body("brief_max_words", "How long a brief may be, 40-600 words.", INT),
        ),
        warning="Changes who is emailed about every report filed from now on.",
    ),
    ToolSpec(
        "reports.deliveries", "reports", _READ, "GET", "/reports/admin/deliveries",
        "Report delivery log",
        "What was emailed about which report, to whom, and what failed or was "
        "skipped and why. Super admin only. The answer to 'why did my manager "
        "not get it'. Counts by status come back alongside the rows.",
        (
            _query("since", "From this day. Defaults to the retention window.", DATE),
            _query(
                "status",
                "sent, failed or skipped.",
                _s("string", enum=["sent", "failed", "skipped"]),
            ),
            _query("limit", "How many (1-500, default 100).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "reports.templates", "reports", _READ, "GET", "/reports/admin/templates",
        "Report templates",
        "The report templates a team can be pointed at, with their ids — what "
        "reports.set_schedule needs. Super admin only.",
    ),
    ToolSpec(
        "reports.schedules", "reports", _READ, "GET", "/reports/admin/schedules",
        "Which template each team files",
        "Every team's schedule: for each cadence, which template it files, "
        "whether it is enabled, when it is due and who else is copied. Super "
        "admin only.",
    ),
    ToolSpec(
        "reports.set_schedule", "reports", _WRITE, "PUT", "/reports/admin/schedules",
        "Point a team's cadence at a template",
        "Set which template one team files for one cadence — this is what makes "
        "one team's report differ from the next. Replaces the schedule row for "
        "that team and cadence. Super admin only. Get template ids from "
        "reports.templates and the team id from teams.get.",
        (
            _body("team_id", "The team's id, not its handle.", required=True),
            _body(
                "cadence",
                "daily, weekly, monthly, quarterly, yearly or ad_hoc.",
                _CADENCE,
                required=True,
            ),
            _body("template_id", "The template's id, from reports.templates.", required=True),
            _body("enabled", "Whether the team files this cadence. Default true.", BOOL),
            _body("due_hour", "Hour of the day it is due, 0-23. Default 18.", INT),
            _body("due_weekday", "0 is Monday. Only meaningful for a weekly.", INT),
            _body("note", "A note shown to the team."),
            _body(
                "notify",
                "true or false to override the global emailing for this team's "
                "reports of this cadence; leave out to follow it.",
                BOOL,
            ),
            _body("extra_recipients", "Extra addresses copied on this team's reports.", STR_LIST),
        ),
        warning="Changes what everyone on that team is asked to report from now on.",
    ),
    ToolSpec(
        "reports.adopt_project_reporting", "reports", _WRITE, "POST",
        "/reports/admin/schedules/project-reporting",
        "Switch a team to project status reporting",
        "Point a team's cadences at the shipped project status templates, so "
        "people on it are asked which project and get health dials and a "
        "milestone timeline instead of the standard sections. Defaults to "
        "weekly, monthly and quarterly. Existing recipients, notes and due "
        "hours on those schedules are kept. Super admin only.",
        (
            _query("team", "The team's handle (slug) or id. Required."),
            _query(
                "cadences",
                "Which cadences to switch: any of weekly, monthly, quarterly. "
                "Leave out for all three.",
                STR_LIST,
            ),
        ),
        warning="Replaces what the team files for those cadences from now on.",
    ),
    # ── proposals ──────────────────────────────────────────────────────
    ToolSpec(
        "proposals.columns", "proposals", _READ, "GET", "/proposals/columns",
        "Proposals list columns",
        "The columns of the SharePoint Proposals list and, for choice columns, "
        "the values each accepts. Read it before proposals.update_task when you "
        "need the exact wording of a status or priority the list will take.",
    ),
    ToolSpec(
        "proposals.remove_attachment", "proposals", _WRITE, "DELETE",
        "/proposals/tasks/{task_id}/attachments/{file_name}",
        "Remove a task attachment",
        "Remove one file from one of the signed-in person's own proposal tasks "
        "in SharePoint. Admins may do it on any task. Get the exact file name "
        "from proposals.attachments first.",
        (
            _path("task_id", "The SharePoint task id, from proposals.my_tasks."),
            _path("file_name", "The file's name exactly as proposals.attachments gives it."),
        ),
        warning="Deletes the file from the live Proposals list the team works in.",
    ),
    # ── notifications ──────────────────────────────────────────────────
    ToolSpec(
        "notifications.list", "notifications", _READ, "GET", "/notifications",
        "My notifications",
        "What the signed-in person has been told — task assignments, reports "
        "filed, issues raised — newest first, with an unread count. Only ever "
        "their own; there is no way to read anybody else's.",
        (
            _query("unread_only", "Only the ones not yet read.", BOOL),
            _query("limit", "How many (1-200, default 50).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "notifications.unread_count", "notifications", _READ, "GET",
        "/notifications/unread-count",
        "How many unread",
        "Just the number of unread notifications for the signed-in person.",
    ),
    ToolSpec(
        "notifications.mark_read", "notifications", _WRITE, "POST", "/notifications/read",
        "Mark notifications read",
        "Mark some or all of the signed-in person's notifications as read. Pass "
        "ids for particular ones, or leave ids out to mark everything. An id "
        "that is not theirs simply matches nothing.",
        (
            _body(
                "ids",
                "Notification ids to mark. Leave out to mark all unread.",
                STR_LIST,
            ),
        ),
        warning="Clears the unread marks; there is no way to put them back.",
    ),

    # ── hr, templates, labels ─────────────────────────────────────────
    # ── hr ──
    ToolSpec(
        "hr.meta", "hr", _READ, "GET", "/hr/meta", "HR choices",
        "The vocabulary this module uses — document kinds, employment types, "
        "application stages, opening statuses, reviewer relations — and which "
        "posting, application and review forms exist, with the default of each. "
        "Read it before creating an opening or a cycle when a template has to be "
        "chosen, or when somebody asks what kinds of document HR can file.",
    ),
    ToolSpec(
        "hr.create_opening", "hr", _WRITE, "POST", "/hr/openings", "Create an opening",
        "Draft a new job opening. It is created as a draft and accepts nobody "
        "until hr.post_opening is called, so a half-described role does no "
        "harm. Leave the template ids out for the canonical forms; hr.meta "
        "lists the alternatives. HR team members only. The advert's own form "
        "answers cannot be given here — HR fills those in on screen.",
        (
            _body("title", "The job title.", required=True),
            _body("template_id", "The application form's template id. Omit for the default."),
            _body("posting_template_id", "The posting form's template id. Omit for the default."),
            _body("reference", "HR's own reference for the role."),
            _body("team_id", "The team's id, if the role belongs to one."),
            _body("department", "Department."),
            _body("location", "Where the job is."),
            _body(
                "employment_type",
                "full_time, part_time, contract, internship or temporary. Defaults to full_time.",
                _EMPLOYMENT_TYPE,
            ),
            _body("headcount", "How many to hire (default 1).", INT),
            _body("salary_range", "Salary range, in words."),
            _body("summary", "A short summary of the role."),
            _body("description", "The full description."),
            _body("requirements", "What a candidate needs."),
            _body("closes_on", "Last day applications are accepted.", DATE),
            _body("publicly_listed", "Show it on the public careers list. Default false.", BOOL),
            _body("hosted_form", "Use the hosted application form. Default true.", BOOL),
        ),
        warning="Creates a draft opening. Nothing is published until it is posted.",
    ),
    ToolSpec(
        "hr.update_opening", "hr", _WRITE, "PATCH", "/hr/openings/{ref}", "Edit an opening",
        "Change the details of a job opening. Only the fields given change. "
        "HR team members only. The status is not edited here: use "
        "hr.post_opening and hr.close_opening for that.",
        (
            _path("ref", "The opening's id or handle."),
            _body("title", "New title."),
            _body("template_id", "The application form's template id."),
            _body("posting_template_id", "The posting form's template id."),
            _body("reference", "HR's own reference."),
            _body("team_id", "The team's id."),
            _body("department", "Department."),
            _body("location", "Where the job is."),
            _body(
                "employment_type",
                "full_time, part_time, contract, internship or temporary.",
                _EMPLOYMENT_TYPE,
            ),
            _body("headcount", "How many to hire.", INT),
            _body("salary_range", "Salary range, in words."),
            _body("summary", "A short summary of the role."),
            _body("description", "The full description."),
            _body("requirements", "What a candidate needs."),
            _body("closes_on", "Last day applications are accepted.", DATE),
            _body("publicly_listed", "Show it on the public careers list.", BOOL),
            _body("hosted_form", "Use the hosted application form.", BOOL),
        ),
        warning="Changes what candidates see if the opening is already posted.",
    ),
    ToolSpec(
        "hr.post_opening", "hr", _WRITE, "POST", "/hr/openings/{ref}/post", "Post an opening",
        "Make a drafted opening live: the share link starts working and "
        "applications are accepted. Required fields on the posting form are "
        "checked now, so an incomplete draft is refused with a reason. HR team "
        "members only.",
        (_path("ref", "The opening's id or handle."),),
        warning="The opening goes live and candidates can apply from this moment.",
    ),
    ToolSpec(
        "hr.close_opening", "hr", _WRITE, "POST", "/hr/openings/{ref}/close", "Close an opening",
        "Stop an opening accepting applications. The record and every "
        "application to it are kept — this is what to use when somebody asks "
        "to 'remove' or 'take down' a job, not hr.delete_opening. Pass "
        "filled=true when it is closing because somebody was hired. HR team "
        "members only.",
        (
            _path("ref", "The opening's id or handle."),
            _query("filled", "Closed because somebody was hired.", BOOL),
        ),
        warning="The share link stops accepting applications.",
    ),
    ToolSpec(
        "hr.rotate_opening_link", "hr", _WRITE, "POST", "/hr/openings/{ref}/rotate-link",
        "Issue a new share link",
        "Replace an opening's public share link with a fresh one. The old link "
        "stops working immediately for everyone who has it, including "
        "candidates part way through the form. Use it when a link has been "
        "shared somewhere it should not have been. HR team members only.",
        (_path("ref", "The opening's id or handle."),),
        warning="Everybody holding the old link loses it at once, candidates included.",
    ),
    ToolSpec(
        "hr.delete_opening", "hr", _WRITE, "DELETE", "/hr/openings/{ref}", "Delete an opening",
        "Permanently destroy an opening, every application sent to it and "
        "their uploaded files. Super admin only. Almost always the wrong tool: "
        "hr.close_opening keeps the record and stops applications, which is "
        "what people usually mean. Offer letters already filed against an "
        "employee survive.",
        (_path("ref", "The opening's id or handle."),),
        warning="Permanent. Every application and candidate file on this opening goes with it.",
    ),
    ToolSpec(
        "hr.candidate_notes", "hr", _WRITE, "PATCH", "/hr/applications/{application_id}/notes",
        "Set HR's notes on a candidate",
        "Replace HR's private notes on one application. Candidates never see "
        "these. The text given replaces what is there, so read the application "
        "first and include anything worth keeping. HR team members only.",
        (
            _path("application_id", "The application id."),
            _body("internal_notes", "The notes, replacing what is there."),
        ),
        warning="Replaces the existing notes rather than adding to them.",
    ),
    ToolSpec(
        "hr.hire", "hr", _WRITE, "POST", "/hr/applications/{application_id}/hire",
        "Record who a candidate became",
        "Link a hired candidate to the employee record they became, once "
        "their Microsoft account has signed in for the first time. Find the "
        "ERP user id with directory.search or teams.members. Set close_opening "
        "to also mark the opening filled. HR team members only.",
        (
            _path("application_id", "The application id."),
            _body("user_id", "The ERP user id of the employee they became.", required=True),
            _body("close_opening", "Also close the opening as filled. Default false.", BOOL),
        ),
        warning="Marks the candidate hired and ties their application to an employee record.",
    ),
    ToolSpec(
        "hr.delete_application", "hr", _WRITE, "DELETE", "/hr/applications/{application_id}",
        "Delete an application",
        "Permanently remove one candidate's application and the files they "
        "uploaded. This is the endpoint behind a candidate asking to be "
        "forgotten. Super admin only. Rejecting a candidate is "
        "hr.move_candidate, not this.",
        (_path("application_id", "The application id."),),
        warning="Permanent. The candidate's CV and other uploads are destroyed with it.",
    ),
    ToolSpec(
        "hr.purge_person", "hr", _WRITE, "DELETE", "/hr/people/{user_id}/hr-data",
        "Erase somebody's HR file",
        "Destroy everything HR holds about one person: their documents and "
        "every review written about them. Reviews they wrote about colleagues "
        "are kept, and their user account is untouched. Super admin only. Do "
        "not call this for a leaver unless asked in those words — deactivating "
        "somebody is a teams matter, not this.",
        (_path("user_id", "The person's ERP user id."),),
        warning="Permanent. Their contracts, documents and every review about them are destroyed.",
    ),
    ToolSpec(
        "hr.document", "hr", _READ, "GET", "/hr/documents/{document_id}", "One HR document",
        "The details of one employee document — kind, title, dates, expiry — "
        "without the file itself. HR sees any; a colleague sees only their own "
        "and only those marked visible to them, and anything else answers 404.",
        (_path("document_id", "The document id, from hr.documents or hr.my_documents."),),
    ),
    ToolSpec(
        "hr.update_document", "hr", _WRITE, "PATCH", "/hr/documents/{document_id}",
        "Edit a document's details",
        "Change the kind, title, note, dates or employee visibility of a filed "
        "document. Only the fields given change; the file itself is never "
        "replaced here. HR team members only.",
        (
            _path("document_id", "The document id."),
            _body("kind", "What sort of document it is.", _DOCUMENT_KIND),
            _body("title", "New title."),
            _body("note", "A note about it."),
            _body("issued_on", "When it was issued.", DATE),
            _body("expires_on", "When it runs out.", DATE),
            _body("visible_to_employee", "Whether the person may see it themselves.", BOOL),
        ),
        warning="Making a document visible lets the employee read it at once.",
    ),
    ToolSpec(
        "hr.delete_document", "hr", _WRITE, "DELETE", "/hr/documents/{document_id}",
        "Delete a document",
        "Permanently delete one employee document and its file. Super admin "
        "only — HR files documents but does not destroy them, because the file "
        "is often the only copy anybody can produce later.",
        (_path("document_id", "The document id."),),
        warning="Permanent. The file may be the only copy that exists.",
    ),
    ToolSpec(
        "hr.create_cycle", "hr", _WRITE, "POST", "/hr/review-cycles", "Start a review cycle",
        "Create a performance review cycle. It starts as a draft with nobody "
        "nominated: follow it with hr.nominate to say who reviews whom and "
        "hr.open_cycle to let them begin. Leave template_id out for the newest "
        "active review form; hr.meta lists the others. HR team members only.",
        (
            _body("name", "The cycle's name, e.g. 'H2 2026 reviews'.", required=True),
            _body("template_id", "The review form's template id. Omit for the newest active one."),
            _body("description", "What the cycle is for."),
            _body("period_start", "First day of the period being reviewed.", DATE),
            _body("period_end", "Last day of the period being reviewed.", DATE),
            _body("due_on", "When reviews are due.", DATE),
            _body(
                "shared_with_subjects",
                "Let the people reviewed read what was written. Default false.",
                BOOL,
            ),
        ),
        warning="Creates a draft cycle. Nobody is asked to write anything until it is opened.",
    ),
    ToolSpec(
        "hr.review_cycle", "hr", _READ, "GET", "/hr/review-cycles/{cycle_id}", "One review cycle",
        "One cycle in full: its state, period, due date, whether the results "
        "are shared, and how many reviews are nominated and submitted. HR only.",
        (_path("cycle_id", "The cycle id, from hr.review_cycles."),),
    ),
    ToolSpec(
        "hr.open_cycle", "hr", _WRITE, "POST", "/hr/review-cycles/{cycle_id}/open",
        "Open a review cycle",
        "Let the nominated reviewers start filling their forms in. HR team "
        "members only.",
        (_path("cycle_id", "The cycle id."),),
        warning="Every nominated reviewer can start writing from this moment.",
    ),
    ToolSpec(
        "hr.close_cycle", "hr", _WRITE, "POST", "/hr/review-cycles/{cycle_id}/close",
        "Close a review cycle",
        "Stop a cycle: nobody can submit after this, and what was submitted "
        "stays readable. This is what to use when somebody asks to 'end' or "
        "'wrap up' reviews — not hr.delete_cycle. HR team members only.",
        (_path("cycle_id", "The cycle id."),),
        warning="Reviews not yet submitted cannot be submitted afterwards.",
    ),
    ToolSpec(
        "hr.share_cycle", "hr", _WRITE, "POST", "/hr/review-cycles/{cycle_id}/sharing",
        "Share or hide a cycle's results",
        "Decide whether the people reviewed in a cycle may read the reviews "
        "written about them. shared=true opens them up; shared=false hides "
        "them again. HR team members only.",
        (
            _path("cycle_id", "The cycle id."),
            # Required, unlike most query parameters: the route has no default,
            # and a null dropped by strict mode would be a 422 rather than "no".
            Param(
                "shared",
                "query",
                BOOL,
                "Whether the people reviewed may read their reviews. Required.",
                required=True,
            ),
        ),
        warning="Sharing lets everyone reviewed in the cycle read what colleagues wrote about them.",
    ),
    ToolSpec(
        "hr.delete_cycle", "hr", _WRITE, "DELETE", "/hr/review-cycles/{cycle_id}",
        "Delete a review cycle",
        "Permanently destroy a cycle and every review in it, submitted ones "
        "included. Super admin only. hr.close_cycle is what is wanted almost "
        "every time.",
        (_path("cycle_id", "The cycle id."),),
        warning="Permanent. Every assessment written in this cycle is destroyed.",
    ),
    ToolSpec(
        "hr.nominate", "hr", _WRITE, "POST", "/hr/review-cycles/{cycle_id}/nominations",
        "Nominate reviewers",
        "Ask people to review people in a cycle: each nomination names who is "
        "reviewed (subject) and who writes it (reviewer), by ERP user id. The "
        "same id for both is a self-review. All or nothing — one bad "
        "nomination fails the whole request. HR team members only.",
        (
            _path("cycle_id", "The cycle id."),
            _body(
                "nominations",
                "Who reviews whom. relation is how the reviewer knows them: self, "
                "manager, peer, report, hr or other (default other).",
                _s("array", items=_object(
                    {
                        "subject_id": _s(
                            "string", description="ERP user id of the person reviewed."
                        ),
                        "reviewer_id": _s("string", description="ERP user id of who writes it."),
                        "relation": _s(
                            "string", enum=["self", "manager", "peer", "report", "hr", "other"]
                        ),
                        "due_on": DATE,
                    },
                    required=("subject_id", "reviewer_id"),
                )),
                required=True,
            ),
        ),
        warning="Each reviewer is asked to write about a colleague.",
    ),
    ToolSpec(
        "hr.withdraw_nomination", "hr", _WRITE, "POST", "/hr/reviews/{review_id}/withdraw",
        "Withdraw a nomination",
        "Un-invite a reviewer who has not started writing — the ordinary fix "
        "for nominating the wrong person. Refused once they have written "
        "anything; at that point it is a deletion and a super admin's call "
        "through hr.delete_review. HR team members only.",
        (_path("review_id", "The review id."),),
        warning="The reviewer is no longer asked to write this review.",
    ),
    ToolSpec(
        "hr.delete_review", "hr", _WRITE, "DELETE", "/hr/reviews/{review_id}", "Delete a review",
        "Permanently delete one review whatever state it is in, submitted "
        "included. Super admin only. For a review nobody has started, "
        "hr.withdraw_nomination is enough.",
        (_path("review_id", "The review id."),),
        warning="Permanent. What was written is destroyed.",
    ),
    ToolSpec(
        "hr.reviews", "hr", _READ, "GET", "/hr/reviews", "Search reviews",
        "Reviews across cycles, filtered by cycle, subject, reviewer or status. "
        "HR sees everything. Anyone else is narrowed to reviews they wrote or "
        "that are about them, whatever filters they pass, and sees content "
        "only where the cycle has been shared.",
        (
            _query("cycle_id", "Only this cycle."),
            _query("subject_id", "Only reviews about this person, by ERP user id."),
            _query("reviewer_id", "Only reviews written by this person, by ERP user id."),
            _query(
                "status",
                "pending, draft, submitted or declined.",
                _s("string", enum=["pending", "draft", "submitted", "declined"]),
            ),
        ),
    ),
    ToolSpec(
        "hr.review", "hr", _READ, "GET", "/hr/reviews/{review_id}", "One review",
        "One review in full. The reviewer and HR get the form's questions with "
        "it; a subject sees it only once the cycle is shared. Anything the "
        "caller may not read answers 404.",
        (_path("review_id", "The review id, from hr.my_reviews or hr.reviews."),),
    ),
    ToolSpec(
        "hr.decline_review", "hr", _WRITE, "POST", "/hr/reviews/{review_id}/decline",
        "Decline to write a review",
        "Turn down a review the signed-in person was nominated to write, with "
        "a reason. Only the nominated reviewer can do this.",
        (
            _path("review_id", "The review id, from hr.my_reviews."),
            _body("reason", "Why they are declining."),
        ),
        warning="HR sees the refusal and the reason.",
    ),
    ToolSpec(
        "hr.reopen_review", "hr", _WRITE, "POST", "/hr/reviews/{review_id}/reopen",
        "Hand a review back",
        "Return a submitted review to its author for more work, clearing the "
        "frozen score. HR team members only.",
        (_path("review_id", "The review id."),),
        warning="The submitted score is cleared until the reviewer submits again.",
    ),
    # ── templates ──
    ToolSpec(
        "templates.get", "templates", _READ, "GET", "/templates/{ref}", "One form template",
        "One template in full: its fields in order, their sections, scoring "
        "and Zoho mappings, who may use it, and whether the caller may fill it "
        "in (may_use) and why not (use_reason).",
        (_path("ref", "The template's key or id."),),
    ),
    ToolSpec(
        "templates.create", "templates", _WRITE, "POST", "/templates", "Create a form template",
        "Create a new form as a draft, granted to nobody. Publish it with "
        "templates.publish and hand it out with grants before anyone can fill "
        "it in. Super admin only. kind says what the form is for ('job_posting', "
        "'performance_review') and defaults to the key. Table-field columns "
        "and scoring rules cannot be set here; they are edited on screen.",
        (
            _body("key", "A short unique key, lowercase with underscores.", required=True),
            _body("name", "The form's display name.", required=True),
            _body("kind", "What it is for, so a module can find its own. Defaults to the key."),
            _body("description", "What the form is for, in a sentence."),
            _body("fields", "The questions, in order.", _s("array", items=_FIELD)),
            _body(
                "sections",
                "The sections the fields are grouped under.",
                _s("array", items=_SECTION),
            ),
        ),
        warning="Creates a draft. Nobody can fill it in until it is published and granted.",
    ),
    ToolSpec(
        "templates.update", "templates", _WRITE, "PATCH", "/templates/{ref}",
        "Edit a form template",
        "Change a template's name, kind, description, fields or sections. Only "
        "what is given changes — but fields or sections given at all replace "
        "that whole list, so read the template first and send every field to "
        "keep. Super admin only. A field's table columns and scoring rules "
        "cannot be sent here and are lost from any field this call resends; "
        "edit those on screen.",
        (
            _path("ref", "The template's key or id."),
            _body("name", "New display name."),
            _body("kind", "What it is for."),
            _body("description", "New description."),
            _body(
                "fields",
                "The whole list of fields, replacing what is there.",
                _s("array", items=_FIELD),
            ),
            _body(
                "sections",
                "The whole list of sections, replacing what is there.",
                _s("array", items=_SECTION),
            ),
        ),
        warning="Changes what every future form of this kind collects; a list sent "
                "replaces the whole list.",
    ),
    ToolSpec(
        "templates.publish", "templates", _WRITE, "POST", "/templates/{ref}/publish",
        "Publish a form template",
        "Make a draft template usable by the teams it is granted to. Super admin only.",
        (_path("ref", "The template's key or id."),),
        warning="The form becomes fillable by everyone it is granted to.",
    ),
    ToolSpec(
        "templates.archive", "templates", _WRITE, "POST", "/templates/{ref}/archive",
        "Retire a form template",
        "Retire a template so nobody can fill it in, keeping it readable so "
        "forms already submitted from it still make sense. Reversible with "
        "templates.restore. Super admin only.",
        (_path("ref", "The template's key or id."),),
        warning="Nobody can fill this form in until it is restored.",
    ),
    ToolSpec(
        "templates.restore", "templates", _WRITE, "POST", "/templates/{ref}/restore",
        "Restore a form template",
        "Bring an archived template back into use. Super admin only.",
        (_path("ref", "The template's key or id."),),
        warning="The form becomes fillable again by everyone it is granted to.",
    ),
    ToolSpec(
        "templates.delete", "templates", _WRITE, "DELETE", "/templates/{ref}",
        "Delete a form template",
        "Permanently delete a template that nothing has ever been filled in "
        "from. One that has been used is refused — archive it instead. Super "
        "admin only.",
        (_path("ref", "The template's key or id."),),
        warning="Permanent. Only an unused template can be deleted.",
    ),
    ToolSpec(
        "templates.grants", "templates", _READ, "GET", "/templates/{ref}/grants",
        "Who may use a template",
        "The grants on one template: which teams may use it and, within each, "
        "which team roles. A grant with no team means every team; one with no "
        "roles means anyone on the team. No grants at all means super admins only.",
        (_path("ref", "The template's key or id."),),
    ),
    # ── labels (assignment) ──
    ToolSpec(
        "labels.create", "assignment", _WRITE, "POST", "/labels", "Add a label",
        "Create a label the assignment policy can speak in. kind is category, "
        "status or skill. Pass team to scope it to one team rather than the "
        "whole organisation. Super admin, CEO or manager only.",
        (
            _query("team", "The team's id, to scope the label to one team. Omit for everywhere."),
            _body("key", "A short unique key, lowercase with hyphens.", required=True),
            _body("name", "The display name.", required=True),
            _body("kind", "category, status or skill.", _LABEL_KIND, required=True),
            _body("description", "What holding it means."),
            _body("color", "A colour for the badge, e.g. a hex code."),
        ),
        warning="Adds a label the assignment policy can then refer to.",
    ),
    ToolSpec(
        "labels.update", "assignment", _WRITE, "PATCH", "/labels/{key}", "Rename a label",
        "Change a label's name, description, colour or kind. The key itself "
        "cannot change — the policy and every assignment refer to it. kind can "
        "only change on a label the product did not ship with. Pass team for "
        "a team-scoped label. Super admin, CEO or manager only.",
        (
            _path("key", "The label's key, from labels.list."),
            _query("team", "The team's id, for a team-scoped label."),
            _body("name", "New display name."),
            _body("description", "New description."),
            _body("color", "New colour."),
            _body("kind", "category, status or skill.", _LABEL_KIND),
        ),
        warning="Changes how the label reads everywhere it is shown.",
    ),
    ToolSpec(
        "labels.delete", "assignment", _WRITE, "DELETE", "/labels/{key}", "Remove a label",
        "Delete a label. System labels — the ones the policy is built on — are "
        "refused; rename those instead. Pass team for a team-scoped label. "
        "Super admin, CEO or manager only.",
        (
            _path("key", "The label's key."),
            _query("team", "The team's id, for a team-scoped label."),
        ),
        warning="Everyone holding the label loses it.",
    ),
    ToolSpec(
        "labels.assign", "assignment", _WRITE, "POST", "/labels/assign", "Give someone a label",
        "Put a label on a person, optionally only within one team and "
        "optionally until a date. on-leave and new-joiner cannot be given "
        "here: they are worked out from approved leave and joining dates, so "
        "change those instead. Super admin, CEO or manager only.",
        (
            _body("user_id", "The person's ERP user id.", required=True),
            _body("label_key", "The label's key, from labels.list.", required=True),
            _body("team_id", "Scope it to one team. Omit for everywhere."),
            _body(
                "expires_at",
                "When it should lapse on its own. Omit to keep it until removed.",
                _DATETIME,
            ),
            _body("note", "Why, for the record."),
        ),
        warning="Changes how work is shared out to this person.",
    ),
    ToolSpec(
        "labels.unassign", "assignment", _WRITE, "POST", "/labels/unassign", "Take a label away",
        "Remove a label from a person. Give the same team_id the label was "
        "assigned with, or omit it for an organisation-wide one. A derived "
        "label (on-leave, new-joiner) cannot be removed this way. Super admin, "
        "CEO or manager only.",
        (
            _body("user_id", "The person's ERP user id.", required=True),
            _body("label_key", "The label's key.", required=True),
            _body("team_id", "The team it was scoped to, if any."),
            _body("expires_at", "Ignored when removing.", _DATETIME),
            _body("note", "Ignored when removing."),
        ),
        warning="Changes how work is shared out to this person.",
    ),
    ToolSpec(
        "labels.set_joined_on", "assignment", _WRITE, "PUT", "/labels/people/{user_id}/joined-on",
        "Set someone's joining date",
        "Record the date the new-joiner rule counts from for one person. "
        "Entra gives no hire date, so without this the rule falls back to when "
        "they first appeared in the ERP, which can be very wrong for somebody "
        "who was here long before it. Super admin, CEO or manager only.",
        (
            _path("user_id", "The person's ERP user id."),
            _body("joined_on", "The date they joined.", DATE, required=True),
        ),
        warning="Changes whether the person counts as a new joiner.",
    ),
    ToolSpec(
        "labels.suggested", "assignment", _READ, "GET", "/labels/suggested", "Shipped labels",
        "The labels the product ships with and the capacity each suggests, "
        "which is what a new policy is seeded with. Use it to explain the "
        "defaults; labels.list is what actually exists.",
    ),

    # ── quote requests, comparisons, dashboards, teams, users ─────────
    # ── quote requests ─────────────────────────────────────────────────
    ToolSpec(
        "quote_requests.tasks", "quote_requests", _READ, "GET", "/quote-requests/tasks",
        "My tasks that could become quotes",
        "The signed-in person's own Proposals tasks, each marked with whether a "
        "quote request has already been raised for it. Use it before "
        "quote_requests.from_task to find the task id, and to answer 'which of "
        "my bids still has no quote'. Only ever their own tasks.",
        (
            _query(
                "scope",
                "live (default): not finished and the bid has not closed. open: "
                "closed bids too. all: completed ones as well.",
                _s("string", enum=["live", "open", "all"]),
            ),
        ),
    ),
    ToolSpec(
        "quote_requests.from_task", "quote_requests", _WRITE, "POST",
        "/quote-requests/from-task", "Raise a quote from a task",
        "Start a quote request from one of the signed-in person's own Proposals "
        "tasks instead of typing it in: the title, customer, bid closing date "
        "and remarks are copied from the task. It arrives with no priced lines "
        "— those come from the supplier quotes attached on the screen, or from "
        "quote_requests.update. Find the task id with quote_requests.tasks; a "
        "task that is not theirs is refused.",
        (
            Param(
                "team", "query", STR,
                "The team's handle (slug) or id the quote belongs to. Required.",
                required=True,
            ),
            _body("task_id", "The SharePoint task id, from quote_requests.tasks.", required=True),
        ),
        warning="This raises a real quote request against that task.",
    ),
    ToolSpec(
        "quote_requests.update", "quote_requests", _WRITE, "PATCH",
        "/quote-requests/{request_id}", "Edit a quote request",
        "Rewrite a quote request that is still the caller's — in draft, or sent "
        "back for rework. Despite the method this is a whole replacement, not a "
        "merge: every field and every line is set from what you send, and a "
        "line left out is gone. Read the quote with quote_requests.get first "
        "and send it back whole with the change made. Refused once it is with "
        "the approvers or approved.",
        (
            _path("request_id", "The quote request id."),
            _body("title", "What the quote is for.", required=True),
            _body("customer_name", "The customer, as they should appear.", required=True),
            _body("customer_id", "The customer's Zoho id, when it is known."),
            _body("contact_person", "Who at the customer asked."),
            _body("reference", "Our own reference for the quote."),
            _body("reference_number", "The customer's own PO or enquiry number."),
            _body("quote_date", "Quote date.", DATE),
            _body("expiry_date", "When the quote lapses.", DATE),
            _body("currency", "Three-letter code. Defaults to AED."),
            _body("salesperson_name", "The salesperson, as Zoho names them."),
            _body("place_of_supply", "Place of supply, for tax."),
            _body("payment_terms", "Payment terms, in words."),
            _body("delivery_terms", "Delivery terms, in words."),
            _body("cf_bcd", "Bid closing date.", DATE),
            _body("cf_portal", "The portal the enquiry came through."),
            _body("subject", "Subject line for the quote."),
            _body("notes", "Notes for the customer."),
            _body("terms", "Terms and conditions text."),
            _body("discount", "Quote-level discount. Defaults to 0.", _s("number")),
            _body("shipping_charge", "Shipping charge. Defaults to 0.", _s("number")),
            _body("adjustment", "A final adjustment to the total. Defaults to 0.", _s("number")),
            _body("tax_name", "The tax on the quote, by name: 'VAT'. Null for none."),
            _body(
                "tax_percentage",
                "The tax rate, applied once to the total before tax (lines less the "
                "discount, plus shipping and the adjustment). 0-100; null for none. "
                "Not per line.",
                _s("number"),
            ),
            _body(
                "multiple_supplier_quotes",
                "True when several suppliers quoted the same requirement and the "
                "approver must choose between them.",
                BOOL,
            ),
            _body(
                "items",
                "The whole set of priced lines, replacing what is there. Each "
                "needs a name; quantity defaults to 1 and rate to 0.",
                _s("array", items=_object(
                    {
                        "name": STR,
                        "description": STR,
                        "item_code": STR,
                        "brand": STR,
                        "unit": STR,
                        "quantity": _s("number"),
                        "rate": _s("number", description="Unit selling price."),
                        "discount": _s("number"),
                        "cost_rate": _s("number", description="What the line costs us, per unit."),
                        "source_supplier_quote_id": _s(
                            "string", description="The supplier quote the line was priced from."
                        ),
                    },
                    required=("name",),
                )),
            ),
        ),
        warning="Replaces every field and every line of the quote with what is sent.",
    ),
    ToolSpec(
        "quote_requests.select_supplier", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/select-supplier", "Price a quote from a supplier",
        "Take one supplier's offer as the quote's own lines: their items become "
        "the priced lines, each priced to keep the margin: selling price = the "
        "supplier's cost ÷ (1 − margin), so a 20% margin on 100 is 125. A "
        "margin of 0 prices the job at cost, which is allowed and visible. "
        "Every rate can still be edited line by line afterwards with "
        "quote_requests.update. The supplier quote ids are on the quote from "
        "quote_requests.get. Only while the quote is the caller's to edit.",
        (
            _path("request_id", "The quote request id."),
            _body(
                "supplier_quote_id",
                "Which attached supplier quote to price from.",
                required=True,
            ),
            _body(
                "markup_percent",
                "The margin each line keeps, as a share of its selling price: the "
                "rate is the supplier's cost ÷ (1 − this). 0 to under 100; defaults "
                "to 0. The field keeps its old name; the number is a margin.",
                _s("number"),
            ),
        ),
        warning="Replaces the quote's priced lines with that supplier's, priced at the margin.",
    ),
    ToolSpec(
        "quote_requests.negotiate", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/negotiate", "Reopen an approved quote",
        "Open another round on a quote that was already approved, because the "
        "customer came back. The approved round is kept whole so the next "
        "reviewer can see what changed; the quote goes back to being editable "
        "and must be submitted and approved again. Say what the customer asked "
        "for in the note. Refused on a quote that is not approved.",
        (
            _path("request_id", "The quote request id."),
            _body("note", "What the customer came back with. Required.", required=True),
        ),
        warning="Takes the quote out of the approved queue and emails the "
                "approvers that their approval no longer stands.",
    ),
    ToolSpec(
        "quote_requests.comment", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/comments", "Comment on a quote",
        "A remark on a quote request, anchored to what it is about: the whole "
        "quote, one field, one line, or one supplier quote. It decides nothing "
        "— an approver's decision is quote_requests.review. It is emailed to "
        "the other side: an owner's comment goes to the approvers, an "
        "approver's to the people who raised the quote.",
        (
            _path("request_id", "The quote request id."),
            _body("body", "What to say.", required=True),
            _body(
                "target_type",
                "What it is about: quote (the whole thing, the default), field, "
                "item or supplier_quote.",
                _s("string", enum=["quote", "field", "item", "supplier_quote"]),
            ),
            _body(
                "target_ref",
                "For field, the field name; for item, the line id; for "
                "supplier_quote, the supplier quote id. Omit for the whole quote.",
            ),
        ),
        warning="The people on the other side of the quote are emailed this.",
    ),
    ToolSpec(
        "quote_requests.resolve_comment", "quote_requests", _WRITE, "POST",
        "/quote-requests/{request_id}/comments/{comment_id}/resolve",
        "Mark a quote comment dealt with",
        "Mark one comment on a quote request as resolved. Nothing else about "
        "the quote changes. Comment ids are on the quote from quote_requests.get.",
        (
            _path("request_id", "The quote request id."),
            _path("comment_id", "The comment id."),
        ),
        warning="The comment is shown as dealt with to everyone on the quote.",
    ),
    # ── quote comparison ───────────────────────────────────────────────
    ToolSpec(
        "comparisons.analyse", "quote_comparison", _WRITE, "POST", "/comparisons/analyse",
        "Compare supplier quotes without saving",
        "Compare two or more supplier offers typed in as data — supplier, "
        "their line items, prices and terms — and get back the matched lines, "
        "totals, spread and savings. Nothing is stored; it is the tool for "
        "'which of these is cheapest' when the numbers are in the conversation. "
        "Uploading the supplier's documents is done on the screen, not here. "
        "To keep the result, use comparisons.save with the same quotes.",
        (
            _body(
                "currency",
                "Three-letter code everything is compared in. Defaults to AED.",
            ),
            _body(
                "quotes",
                "The supplier offers, at least one. Each needs a supplier_name; "
                "prices are in that quote's own currency, converted by fx_rate.",
                _s("array", items=_SUPPLIER_QUOTE),
                required=True,
            ),
        ),
        warning="Runs a model over the quotes to match their lines, which costs money.",
    ),
    ToolSpec(
        "comparisons.save", "quote_comparison", _WRITE, "POST", "/comparisons",
        "Save a comparison",
        "Store a supplier quote comparison for colleagues to refer to. The "
        "analysis is worked out again over what is saved, so the saved figures "
        "always follow from the saved lines. Read the suppliers and their "
        "totals back to the person before saving.",
        (
            _body("title", "What was being bought.", required=True),
            _body("reference", "An enquiry or project reference."),
            _body("notes", "Anything worth recording beside the numbers."),
            _body(
                "currency",
                "Three-letter code everything is compared in. Defaults to AED.",
            ),
            _body(
                "quotes",
                "The supplier offers. Each needs a supplier_name.",
                _s("array", items=_SUPPLIER_QUOTE),
            ),
        ),
        warning="Creates a comparison everyone in the module can read.",
    ),
    ToolSpec(
        "comparisons.delete", "quote_comparison", _WRITE, "DELETE",
        "/comparisons/{comparison_id}", "Delete a comparison",
        "Permanently delete a saved comparison and the supplier documents "
        "attached to it. Only the person who created it, or a super admin, may.",
        (_path("comparison_id", "The comparison id."),),
        warning="Permanent. The uploaded supplier documents go with it.",
    ),
    # ── dashboard ──────────────────────────────────────────────────────
    ToolSpec(
        "dashboard.widgets", "dashboard", _READ, "GET", "/widgets", "Dashboard widget catalogue",
        "Every dashboard card that exists, with its key, what it shows and "
        "which module it needs. Use it to name a card somebody wants added; "
        "dashboard.layout says which of these one particular team may use.",
    ),
    ToolSpec(
        "dashboard.reset_layout", "dashboard", _WRITE, "DELETE",
        "/teams/{ref}/dashboard/layout", "Reset a dashboard",
        "Drop one team's saved dashboard arrangement so it falls back to the "
        "defaults for its modules. Admins only. Returns the layout it now has.",
        (_TEAM_REF,),
        warning="Throws away the team's arrangement for everyone on it.",
    ),
    # ── teams ──────────────────────────────────────────────────────────
    ToolSpec(
        "teams.add_members", "teams", _WRITE, "POST", "/teams/{ref}/members/bulk",
        "Add several members",
        "Add several people to a team at once, all with the same roles. Admins "
        "only. Each person is attempted on their own, so one bad id does not "
        "stop the rest — the answer lists who was added and who failed, and "
        "why; report both. Someone who has never signed in is provisioned from "
        "the directory.",
        (
            _TEAM_REF,
            _body(
                "user_ids",
                "The people. Each accepts the ERP user id, the Entra object id, "
                "or the email address.",
                STR_LIST,
                required=True,
            ),
            _body(
                "role_keys",
                "Team roles each will hold: member, team_lead, team_manager, "
                "approver. Defaults to member.",
                STR_LIST,
            ),
        ),
        warning="Replaces whatever roles any of them already hold in this team.",
    ),
    # ── user administration ────────────────────────────────────────────
    ToolSpec(
        "users.sections", "user_admin", _READ, "GET", "/users/sections",
        "What a profile can contain",
        "The sections a user profile is made of — their keys, which call out "
        "to Entra, which are slow, and which users.reset will wipe. Use it to "
        "choose the include keys for users.profile.",
    ),
    ToolSpec(
        "users.reset", "user_admin", _WRITE, "POST", "/users/{ref}/reset",
        "Wipe a user's data",
        "Strip every module's data about one person — their leave, HR file, "
        "team memberships, roles and the rest — while keeping the account so "
        "they can still sign in. Admins only. The answer says what was removed "
        "and what it looked like before, which is the last time it can be "
        "seen. Confirm the exact person first. Accepts the ERP user id, the "
        "Entra object id, or the email address.",
        (_path("ref", "The person. Accepts the ERP user id, the Entra object id, or the email address."),),
        warning="Irreversible. Everything the ERP holds about this person is erased.",
        destructive=True,
    ),
    ToolSpec(
        "users.delete", "user_admin", _WRITE, "DELETE", "/users/{ref}",
        "Remove a user entirely",
        "Delete a person from the ERP altogether: their data in every module "
        "and the account itself. Admins only. They are not removed from the "
        "directory and can be provisioned again by signing in, but with a "
        "blank record. Confirm the exact person first. Accepts the ERP user "
        "id, the Entra object id, or the email address.",
        (_path("ref", "The person. Accepts the ERP user id, the Entra object id, or the email address."),),
        warning="Irreversible. The account and everything about this person go.",
    ),

    # ── assignment, analytics, intake, system, finance ────────────────
    # ── assignment ──
    ToolSpec(
        "assignment.policies", "assignment", _READ, "GET", "/assignment/policies",
        "Every assignment policy",
        "The organisation default and every team that has a policy of its own, "
        "each with whether the caller may edit it. Use it to find out which "
        "teams share work differently from the default before reading or "
        "changing one.",
    ),
    ToolSpec(
        "assignment.update_policy", "assignment", _WRITE, "PATCH",
        "/assignment/policies/default", "Change the default policy",
        "Change the organisation-wide assignment policy. Only the fields given "
        "change. Super admin or CEO; a manager cannot change the default, only "
        "their own teams'. Per-label ratios and caps are not editable here — "
        "they are set on the screen. Read assignment.policy first and say back "
        "what will change: this decides how much work everybody in the company "
        "is given.",
        _POLICY_FIELDS,
        warning="Changes how work is shared out for every team running on the default.",
    ),
    ToolSpec(
        "assignment.team_policy", "assignment", _READ, "GET",
        "/assignment/policies/team/{team_id}", "A team's policy",
        "The policy governing one team: its own if it has one, otherwise the "
        "organisation default. A null team_id in the answer means it is "
        "running on the default. Accepts the team's handle or id.",
        _TEAM_ID,
    ),
    ToolSpec(
        "assignment.create_team_policy", "assignment", _WRITE, "POST",
        "/assignment/policies/team/{team_id}", "Give a team its own policy",
        "Create a policy of the team's own, copied from the default, so it can "
        "be tuned without touching anyone else. Refused if the team already has "
        "one. Super admin or CEO anywhere; a manager only on a team they belong "
        "to. Takes no settings — follow it with assignment.update_team_policy.",
        _TEAM_ID,
        warning="From now on this team's work is shared out by its own policy, "
                "not the default.",
    ),
    ToolSpec(
        "assignment.update_team_policy", "assignment", _WRITE, "PATCH",
        "/assignment/policies/team/{team_id}", "Change a team's policy",
        "Change one team's own assignment policy. Only the fields given change. "
        "Refused with 404 if the team has no policy of its own — create one "
        "with assignment.create_team_policy first. Super admin or CEO anywhere; "
        "a manager only on a team they belong to. Per-label ratios and caps are "
        "set on the screen, not here. Read assignment.team_policy first and say "
        "back what will change.",
        _TEAM_ID + _POLICY_FIELDS,
        warning="Changes how work is shared out across this team.",
    ),
    ToolSpec(
        "assignment.drop_team_policy", "assignment", _WRITE, "DELETE",
        "/assignment/policies/team/{team_id}", "Drop a team's policy",
        "Delete a team's own policy so it falls back to the organisation "
        "default. Refused if the team has none. Super admin or CEO anywhere; a "
        "manager only on a team they belong to.",
        _TEAM_ID,
        warning="The team's own settings are gone for good; it runs on the "
                "default from now on.",
    ),
    # ── who gets the next job ──
    ToolSpec(
        "analytics.rank", "assignment", _WRITE, "POST", "/analytics/runs",
        "Rank people and keep it",
        "Compute the ranking for a team and store it as the record of a "
        "decision, with the policy frozen onto it. Use analytics.preview "
        "instead unless the person wants it kept. Re-reads SharePoint every "
        "time. The team must have a policy of its own, and the caller must be "
        "allowed to edit it. Writes nothing to SharePoint.",
        (
            _query("team", "The team's handle. Required.", STR),
            _body("notes", "Why it was kept — what was decided on the strength of it."),
        ),
        warning="Stores a ranking others will treat as the record of a decision.",
    ),
    ToolSpec(
        "analytics.run", "assignment", _READ, "GET", "/analytics/runs/{run_id}",
        "One kept ranking",
        "A kept ranking in full: every person, their place, the task counts "
        "behind it and the policy as it stood. Get the id from analytics.runs.",
        (_path("run_id", "The run id, from analytics.runs."),),
    ),
    ToolSpec(
        "analytics.explain", "assignment", _READ, "GET",
        "/analytics/runs/{run_id}/people/{user_id}", "Why someone ranked where they did",
        "The factor breakdown for one person in a kept run: the raw number, "
        "where it sat against everyone else, and how much each factor moved "
        "them. The tool for 'why is she third'.",
        (_path("run_id", "The run id, from analytics.runs."), _USER_ID),
    ),
    ToolSpec(
        "analytics.delete_run", "assignment", _WRITE, "DELETE", "/analytics/runs/{run_id}",
        "Delete a kept ranking",
        "Remove a kept ranking. Only somebody who may edit that team's policy. "
        "It was kept as the record of a decision, so ask before removing it.",
        (_path("run_id", "The run id, from analytics.runs."),),
        warning="Permanent. The record of what this decision was based on is gone.",
    ),
    # ── mail intake ──
    ToolSpec(
        "intake.settings", "intake", _READ, "GET", "/intake/settings",
        "Mail intake settings",
        "How the tender mailbox is watched: which inbox, who may send, the "
        "thresholds, where it notifies, and — the one to look at first — "
        "whether create_in_sharepoint is on, meaning it writes real rows into "
        "the Proposals list. Super admin only.",
    ),
    ToolSpec(
        "intake.update_settings", "intake", _WRITE, "PATCH", "/intake/settings",
        "Change mail intake settings",
        "Change how the tender mailbox is watched. Only the fields given change. "
        "Super admin only. Empty sender and domain lists admit nobody. "
        "Changing the mailbox watches from now, never replaying old mail. "
        "Turning create_in_sharepoint on is the moment this system starts "
        "writing rows into the live Proposals list — read that back and get an "
        "explicit yes before sending it.",
        (
            _body("enabled", "Watch the mailbox at all.", BOOL),
            _body("mailbox", "The inbox to watch, as an email address."),
            _body(
                "allowed_senders",
                "Email addresses allowed to raise tenders. Empty admits nobody.",
                STR_LIST,
            ),
            _body("allowed_domains", "Sender domains allowed. Empty admits nobody.", STR_LIST),
            _body(
                "create_in_sharepoint",
                "THE live-write switch: create real Proposals rows.",
                BOOL,
            ),
            _body("update_negotiation", "Mark a matched task's Negotiation column.", BOOL),
            _body("negotiation_value", "What to write into that column."),
            _body(
                "update_order_status",
                "Set a matched task's OrderStatus column when a purchase order arrives.",
                BOOL,
            ),
            _body("order_status_value", "What to write into OrderStatus. Usually Received."),
            _body("assign_team_id", "The team whose ranking picks the assignee, by id."),
            _body(
                "match_threshold",
                "How sure it must be to match an existing task. 0-1.",
                _s("number"),
            ),
            _body(
                "classify_threshold",
                "How sure it must be that a mail is a tender. 0-1.",
                _s("number"),
            ),
            _body("teams_webhook_url", "The Microsoft Teams webhook to post to."),
            _body("notify_in_app", "Raise in-app notifications.", BOOL),
            _body("notify_teams", "Post to the Teams channel.", BOOL),
            _body("poll_seconds", "How often to poll the mailbox. 15-3600.", INT),
        ),
        warning="Changes whose mail is read and what happens to it. Switching "
                "create_in_sharepoint on writes to the live Proposals list.",
    ),
    ToolSpec(
        "intake.messages", "intake", _READ, "GET", "/intake/messages",
        "Mail the intake has seen",
        "Every email the intake has looked at, including the ones it ignored — "
        "which are the useful ones when somebody asks why nothing happened to "
        "a mail they sent. Comes with a count per status. Super admin only.",
        (
            _query("status", "Only this status, e.g. received, ignored, failed, done."),
            _query("category", "Only this category as the classifier named it."),
            _query("limit", "How many (1-200, default 50).", INT),
            _query("offset", "Skip this many, for paging.", INT),
        ),
    ),
    ToolSpec(
        "intake.message", "intake", _READ, "GET", "/intake/messages/{message_id}",
        "One intake message",
        "One email and everything decided about it: the classifier's reasoning, "
        "what it extracted, the match it chose and the shortlist it chose from, "
        "who it assigned and why, and exactly what it would have posted to "
        "SharePoint. Read this before retrying one. Super admin only.",
        (_path("message_id", "The message id, from intake.messages."),),
    ),
    ToolSpec(
        "intake.retry", "intake", _WRITE, "POST", "/intake/messages/{message_id}/retry",
        "Put a message back through",
        "Run the intake pipeline over one email again, from where it is now, "
        "with the classification redone. For after a setting changed — a sender "
        "added, a threshold lowered. Super admin only. If create_in_sharepoint "
        "is on, a retry can create or update a real Proposals row; check "
        "intake.settings first and say so.",
        (_path("message_id", "The message id, from intake.messages."),),
        warning="Re-runs the decision. With writing switched on this can post "
                "to the live Proposals list and notify people.",
    ),
    ToolSpec(
        "intake.mirror", "intake", _READ, "GET", "/intake/mirror",
        "Is the local copy current",
        "Whether the local mirror of the Proposals list is up to date: row "
        "count, how many carry an embedding, when it last synced and any error. "
        "Fewer embedded than rows means matching is degraded. Super admin only.",
    ),
    ToolSpec(
        "intake.sync_mirror", "intake", _WRITE, "POST", "/intake/mirror/sync",
        "Refresh the mirror now",
        "Pull the Proposals list into the local mirror and rewrite the ranking "
        "without waiting for the timer. Reads SharePoint; writes nothing to it. "
        "Super admin only. Takes a while on a large list.",
        (_query("embed", "Also embed changed rows (default true).", BOOL),),
        warning="Re-reads SharePoint and recomputes who is next in line.",
    ),
    ToolSpec(
        "intake.standing", "intake", _READ, "GET", "/intake/standing",
        "Who the intake gives the next task to",
        "The stored ranking the intake assigns from — rank 1 is next — with "
        "the counts and factors behind each place and why anyone is out. A "
        "table kept current by the mirror sync, not a fresh computation. Super "
        "admin only.",
        (_query("team", "The team's handle or id. Omit for the organisation-wide queue."),),
    ),
    # ── system console ──
    ToolSpec(
        "system.console", "system", _READ, "GET", "/admin/console",
        "The administration console",
        "Every administration surface in one call: each section's endpoints, "
        "its caution, and live figures — failed intake messages, a mirror that "
        "has not synced, failed report deliveries — with needs_attention on "
        "each and summed across them. The first thing to read when a super "
        "admin asks whether anything needs looking at. Super admin only.",
        (_query("status_only", "Skip the endpoint lists and return just the figures.", BOOL),),
    ),
    ToolSpec(
        "system.permissions", "system", _READ, "GET", "/admin/permissions",
        "Who may do what",
        "The permission rules behind the administration screens, in words, "
        "with who currently holds each global role. Use it to answer 'why was "
        "I refused' or 'who can actually do this today'. Super admin only.",
    ),
    # ── finance ──
    ToolSpec(
        "finance.zoho", "finance", _READ, "GET", "/finance/zoho",
        "Zoho endpoints this module reads",
        "The catalogue of Zoho Books endpoints the finance module can read, "
        "with the key each is called by and the scope it needs. Describes the "
        "code without calling Zoho, so it is free to call. Super admin, CEO, "
        "manager or accountant only.",
    ),

    # ── workflows ──────────────────────────────────────────────────────
    ToolSpec(
        "workflows.list", "workflows", _READ, "GET", "/workflows", "Workflows I can start",
        "The workflows this person may start, with their steps. The presales one "
        "takes a Proposals task from documents to a Zoho quote.",
    ),
    ToolSpec(
        "workflows.get", "workflows", _READ, "GET", "/workflows/{key}", "One workflow",
        "One workflow by its key, with every step and what each does.",
        (_path("key", "The workflow key, e.g. presales_rfq."),),
    ),
    ToolSpec(
        "workflows.runs", "workflows", _READ, "GET", "/workflows/runs", "Workflow runs",
        "Runs this person owns or their team's: which task, which step, what it "
        "is waiting for. Use mine=true for only theirs, open=true for only the "
        "ones still going.",
        (
            _query("mine", "Only runs this person owns.", BOOL),
            _query("open", "Only runs still going.", BOOL),
            _query("workflow", "Only runs of this workflow key."),
            _query("limit", "At most this many.", INT),
        ),
    ),
    ToolSpec(
        "workflows.run", "workflows", _READ, "GET", "/workflows/runs/{run_id}", "One run in full",
        "Everything about one run: its steps and where it is, what it is waiting "
        "on (the pending question and its fields), what it has learnt (context), "
        "the files it holds, the mails it sent and received, and the event log. "
        "Read this before answering a question about a run.",
        (_path("run_id", "The run id."),),
    ),
    ToolSpec(
        "workflows.for_task", "workflows", _READ, "GET", "/workflows/for-task/{task_id}",
        "Runs on a task",
        "The runs already going on one Proposals task, and the workflows that "
        "could be started on it.",
        (_path("task_id", "The SharePoint task id, from proposals.my_tasks."),),
    ),
    ToolSpec(
        "workflows.start", "workflows", _WRITE, "POST", "/workflows/{key}/runs",
        "Start a workflow on a task",
        "Begin a workflow for one Proposals task. The run is this person's and "
        "acts as them. It runs as far as its first question and stops there; "
        "read it back with workflows.run to see what it asks. One open run per "
        "task per workflow.",
        (
            _path("key", "The workflow key, e.g. presales_rfq."),
            _body("subject_id", "The SharePoint task id.", required=True),
            _body("subject_label", "The task title, for the list."),
        ),
        warning="The workflow will read the task, ask suppliers for quotes and "
                "prepare a quote request — each step asking this person first.",
    ),
    ToolSpec(
        "workflows.answer", "workflows", _WRITE, "POST", "/workflows/runs/{run_id}/answer",
        "Answer what a run is waiting on",
        "Answer the question a run has stopped on. For a form, give the fields "
        "as key/value pairs using the keys the pending question lists. For a "
        "review, either send nothing to verify it as shown, or value_json with "
        "the edited value (as JSON text) — for instance the supplier list with "
        "one removed. Read the run first so the answer matches the question.",
        (
            _path("run_id", "The run id."),
            _body(
                "pairs",
                "Form answers, one {key, value} per field.",
                _s("array", items=_object({"key": STR, "value": STR}, required=("key", "value"))),
            ),
            _body("value_json", "For a review: the edited value as JSON text."),
        ),
        warning="The run carries on from this answer — a verified supplier list "
                "is what the request for quotation goes to.",
    ),
    ToolSpec(
        "workflows.wake", "workflows", _WRITE, "POST", "/workflows/runs/{run_id}/wake",
        "Check a waiting run now",
        "Make a run that is waiting on the world — supplier replies, an "
        "approval — look now instead of at its next poll.",
        (_path("run_id", "The run id."),),
        warning="Only looks; changes nothing unless what it was waiting for has arrived.",
    ),
    ToolSpec(
        "workflows.retry", "workflows", _WRITE, "POST", "/workflows/runs/{run_id}/retry",
        "Retry a failed run",
        "Try the step a run failed on again, from where it was.",
        (_path("run_id", "The run id."),),
        warning="The failed step runs again; whatever it had already done stays done.",
    ),
    ToolSpec(
        "workflows.cancel", "workflows", _WRITE, "POST", "/workflows/runs/{run_id}/cancel",
        "Stop a run",
        "Stop a run for good. Mails already sent stay sent; nothing further happens.",
        (_path("run_id", "The run id."),),
        warning="Cannot be undone; a new run has to be started from the beginning.",
    ),
    ToolSpec(
        "workflows.settings", "workflows", _READ, "GET", "/workflows/admin/settings",
        "The workflow switches",
        "Whether workflows may send mail, write to SharePoint and create in Zoho. "
        "Super admin only.",
    ),
    ToolSpec(
        "workflows.update_settings", "workflows", _WRITE, "PATCH", "/workflows/admin/settings",
        "Flip a workflow switch",
        "Turn sending mail, writing to SharePoint or creating in Zoho on or off "
        "for every workflow. Super admin only; the route refuses anybody else.",
        (
            _body("send_email", "Whether the email step may send.", BOOL),
            _body("write_sharepoint", "Whether files may be attached to tasks.", BOOL),
            _body("write_zoho", "Whether estimates may be created in Zoho Books.", BOOL),
            _body("from_mailbox", "The mailbox request-for-quote mails go out from."),
            _body("poll_seconds", "How often waiting runs are checked.", INT),
        ),
        warning="Turning a switch on lets every run past that step act on the world.",
    ),
    ToolSpec(
        "workflows.flows", "workflows", _READ, "GET", "/workflows/admin/flows",
        "Every workflow, archived included",
        "The full list for an administrator, with each one's steps.",
    ),
    ToolSpec(
        "workflows.archive_flow", "workflows", _WRITE, "POST", "/workflows/admin/flows/{key}/archive",
        "Retire a workflow",
        "Stop a workflow being started. Runs already going carry on.",
        (_path("key", "The workflow key."),),
        warning="Nobody can start this workflow until it is restored.",
    ),
    ToolSpec(
        "workflows.restore_flow", "workflows", _WRITE, "POST", "/workflows/admin/flows/{key}/restore",
        "Bring a workflow back",
        "Make an archived workflow startable again.",
        (_path("key", "The workflow key."),),
        warning="The workflow can be started again.",
    ),
)

#: Everything, including what is only planned. For the administration
#: screen, which is the one caller that should see the whole picture.
TOOLS_BY_KEY: Final[dict[str, ToolSpec]] = {t.key: t for t in TOOLS}
TOOLS_BY_NAME: Final[dict[str, ToolSpec]] = {t.name: t for t in TOOLS if t.is_live}

#: What may actually be offered. Everything downstream — policy, the tool
#: list a turn is given, the seeder — works from this, so a planned tool
#: cannot reach a model by being forgotten about.
LIVE_TOOLS: Final[tuple[ToolSpec, ...]] = tuple(t for t in TOOLS if t.is_live)

#: Lets the model fetch a deferred tool's schema when it needs one. Sent
#: alongside the loaded tools whenever anything is deferred.
TOOL_SEARCH: Final[dict[str, Any]] = {"type": "tool_search"}


def tools_for_module(module_key: str) -> list[ToolSpec]:
    return [t for t in TOOLS if t.module_key == module_key]
