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

Two kinds of tool, told apart by ``kind``:

* ``read`` — GET. Runs as soon as the model asks.
* ``write`` — anything else. Available, but only to the roles the module's
  ``write_roles`` names, and paused for the person's confirmation unless policy
  says otherwise.

Adding a tool means adding one entry here and running the seed, which creates
its policy row. Nothing else needs to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal

Kind = Literal["read", "write"]
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

    @property
    def is_live(self) -> bool:
        return self.status == "live"

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

TOOLS: Final[tuple[ToolSpec, ...]] = (
    # ── about me ───────────────────────────────────────────────────────
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
    # ── written down, not built ────────────────────────────────────────
    #
    # Listed so the administration screen shows what is coming as well as what
    # is here. ``planned`` tools are filtered out of every list the model sees,
    # so nothing below can be called; they are a roadmap kept next to the code
    # rather than in somebody's head.
    ToolSpec(
        "zoho.read", "finance", _READ, "GET", "/finance/zoho/{endpoint_key}",
        "Read a Zoho endpoint directly",
        "Read one Zoho Books endpoint straight through. Planned deliberately: it "
        "is the widest read in the system and wants a narrower shape before an "
        "assistant is given it.",
        (_path("endpoint_key", "Which endpoint, from finance.zoho."),),
        status="planned",
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
