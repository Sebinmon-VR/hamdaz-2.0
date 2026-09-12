"""Request and response shapes for the assistant."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.catalogue import VOICE_MAX_CHARS

# ── what a person sees ─────────────────────────────────────────────────


class ToolCapabilityOut(BaseModel):
    key: str
    label: str
    kind: Literal["read", "write"]
    requires_confirmation: bool


class ModuleCapabilityOut(BaseModel):
    key: str
    name: str
    tools: list[ToolCapabilityOut]


class StatusOut(BaseModel):
    """What the frontend needs before showing the chat: may I, and what can it do."""

    enabled: bool
    admitted: bool
    code: str | None
    reason: str | None
    model: str | None
    voice_enabled: bool
    voice: str | None
    realtime_enabled: bool
    modules: list[ModuleCapabilityOut]


class PlaceOut(BaseModel):
    """Where a page lives, once the assistant has worked out which one is meant.

    The screen navigates; this only says where to. Keeping those apart is what
    lets the same answer be rendered as "I have opened Quotes for you" by an
    app that moved and as a link by one that did not.
    """

    #: ``module.page`` — what was actually resolved, which may not be what was
    #: asked for. "quotes" resolves to "quotes.list", and the model should say
    #: what it opened rather than what it guessed.
    page: str
    module: str
    name: str
    #: "Quotes · All quotes". For a person, in a sentence.
    label: str
    #: The frontend route, parameters filled in.
    path: str


class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime
    last_message_at: datetime | None
    #: What the chat is about, when it was opened from somewhere specific — a
    #: report page rather than the assistant's own screen. Null for the rest.
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    subject_label: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID | None
    seq: int
    role: str
    content: str
    tool_calls: list[Any] | None
    created_at: datetime


class PendingActionOut(BaseModel):
    call_id: str
    tool_key: str
    label: str
    arguments: dict[str, Any]
    warning: str | None


class PendingOut(BaseModel):
    run_id: uuid.UUID
    actions: list[PendingActionOut]


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut]
    #: Set when the last turn stopped to ask before a write.
    pending: PendingOut | None


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    #: The route the person is looking at, so "open this one" and "who filed
    #: it" mean something. A path, not a page key — the browser knows where it
    #: is, and the API turns that back into a screen name and a record id.
    #: Ignored if it is not one of this app's own routes.
    page: str | None = Field(default=None, max_length=512)


class RealtimeStartIn(BaseModel):
    """Where the person is when a spoken conversation opens.

    A realtime session's instructions are fixed when its token is minted — the
    client is never allowed to change them — so this is the one chance to tell
    it where the conversation is starting from.
    """

    page: str | None = Field(default=None, max_length=512)


class ConfirmIn(BaseModel):
    run_id: uuid.UUID
    approved: bool


class SpeakIn(BaseModel):
    """Text to read aloud. Usually one assistant answer, sometimes a sentence
    of it — the frontend may speak each sentence as it streams in."""

    text: str = Field(min_length=1, max_length=VOICE_MAX_CHARS)
    #: Overrides the configured voice, for an admin sampling them. Everyone
    #: else gets the one the super admin chose.
    voice: str | None = None


class VoiceOut(BaseModel):
    key: str
    #: True for the one currently configured.
    active: bool


class VoiceOptionsOut(BaseModel):
    enabled: bool
    model: str
    voice: str
    instructions: str
    max_chars: int
    voices: list[VoiceOut]
    speech_models: list[str]
    #: Served rather than hardcoded in the admin screen, so adding one here
    #: is the only edit needed.
    realtime_models: list[str]
    realtime_model: str


class RealtimeToolOut(BaseModel):
    """One tool as the browser needs to know it.

    The browser never builds the request itself — it posts the arguments back
    and this API executes them. What it needs is the name the model will use,
    the key to post to, and whether saying yes is required first.
    """

    name: str
    tool_key: str
    label: str
    kind: Literal["read", "write"]
    requires_confirmation: bool
    warning: str | None


class RealtimeSessionOut(BaseModel):
    """Everything a browser needs to open one spoken conversation."""

    #: The ephemeral client secret. Short-lived and single-purpose.
    client_secret: str
    #: Epoch seconds. Past this the secret opens nothing.
    expires_at: int
    model: str
    voice: str
    #: The run this conversation is recorded against.
    run_id: uuid.UUID
    #: What the session was minted with, so the client can render the calls.
    tools: list[RealtimeToolOut]
    writes_enabled: bool


class RealtimeCallIn(BaseModel):
    run_id: uuid.UUID
    #: The function name the model used, or the tool key. Either is accepted.
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Set only after the person has been asked and said yes.
    confirmed: bool = False


class RealtimeCallOut(BaseModel):
    ok: bool
    status: int
    #: The tool result, as JSON text, to hand back to the model.
    output: str
    #: True when nothing ran because the person has not been asked yet.
    requires_confirmation: bool = False
    label: str | None = None
    warning: str | None = None


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    tool_key: str | None
    payload: dict[str, Any] | None
    created_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    user_name: str
    status: str
    model_key: str
    reasoning_effort: str
    started_at: datetime
    finished_at: datetime | None
    user_text: str
    answer_text: str | None
    error: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: Decimal
    tool_calls: int
    rounds: int
    cancel_requested: bool


class RunDetailOut(RunOut):
    events: list[RunEventOut]
    pending: list[PendingActionOut] | None


class RunPage(BaseModel):
    runs: list[RunOut]
    total: int


# ── administration ─────────────────────────────────────────────────────


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    model_key: str
    reasoning_effort: str
    max_tool_rounds: int
    max_output_tokens: int
    history_window: int
    turns_per_user_per_hour: int
    daily_cost_cap_user_usd: Decimal | None
    daily_cost_cap_total_usd: Decimal | None
    audience_mode: str
    confirm_writes_default: bool
    voice_enabled: bool
    voice_model: str
    voice: str
    voice_instructions: str | None
    realtime_enabled: bool
    realtime_model: str
    realtime_writes_enabled: bool
    extra_instructions: str | None
    updated_by_id: uuid.UUID | None
    updated_at: datetime
    #: Whether an OpenAI key is configured on the server. Not editable here.
    openai_configured: bool = False


class SettingsIn(BaseModel):
    """Only the fields given change. Null on a cap removes it."""

    enabled: bool | None = None
    model_key: str | None = Field(default=None, max_length=64)
    reasoning_effort: str | None = Field(default=None, max_length=16)
    max_tool_rounds: int | None = Field(default=None, ge=1, le=30)
    max_output_tokens: int | None = Field(default=None, ge=256, le=64000)
    history_window: int | None = Field(default=None, ge=0, le=200)
    turns_per_user_per_hour: int | None = Field(default=None, ge=1, le=10000)
    daily_cost_cap_user_usd: Decimal | None = Field(default=None, ge=0)
    daily_cost_cap_total_usd: Decimal | None = Field(default=None, ge=0)
    audience_mode: Literal["everyone", "allow_list"] | None = None
    confirm_writes_default: bool | None = None
    voice_enabled: bool | None = None
    voice_model: str | None = Field(default=None, max_length=40)
    voice: str | None = Field(default=None, max_length=24)
    #: Null or blank restores the shipped wording rather than removing steering.
    voice_instructions: str | None = Field(default=None, max_length=2000)
    realtime_enabled: bool | None = None
    realtime_model: str | None = Field(default=None, max_length=40)
    realtime_writes_enabled: bool | None = None
    extra_instructions: str | None = Field(default=None, max_length=8000)


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    description: str
    input_price: Decimal
    cached_input_price: Decimal
    output_price: Decimal
    enabled: bool
    #: The one the settings currently point at.
    active: bool = False


class ModelIn(BaseModel):
    enabled: bool | None = None
    input_price: Decimal | None = Field(default=None, ge=0)
    cached_input_price: Decimal | None = Field(default=None, ge=0)
    output_price: Decimal | None = Field(default=None, ge=0)


class ToolPolicyOut(BaseModel):
    tool_key: str
    label: str
    kind: Literal["read", "write"]
    method: str
    path: str
    description: str
    warning: str | None
    #: "live" or "planned". A planned tool is a roadmap entry: it is listed
    #: here and offered to nobody.
    status: Literal["live", "planned"]
    #: Kept out of the prompt until the model searches for it.
    deferred: bool
    enabled: bool
    confirm_override: bool | None
    allowed_roles: list[str] | None
    #: Narrows or widens the module's write restriction for this one tool.
    write_roles: list[str] | None
    #: After the module policy and the global default are applied.
    effective_enabled: bool
    effective_confirm: bool
    effective_roles: list[str] | None
    #: Who may actually have the assistant run this write. Null on a read —
    #: a write restriction never holds a read back, and showing one against a
    #: read would suggest it did.
    effective_write_roles: list[str] | None


class ModulePolicyOut(BaseModel):
    module_key: str
    name: str
    gate: Literal["open", "access", "admin"]
    description: str
    read_enabled: bool
    write_enabled: bool
    confirm_writes: bool | None
    #: Who may use this module through the assistant at all.
    allowed_roles: list[str] | None
    #: Who may have it *write* here. Null means whoever the route already
    #: allows — the restriction is an extra gate, never a grant.
    write_roles: list[str] | None
    #: What the module ships with, so the screen can show when an edit has
    #: moved away from it and what it would go back to.
    default_write_roles: list[str] | None
    #: Whether this module has any live write at all. False means the
    #: restriction is set for the day one arrives, and changes nothing today.
    has_writes: bool
    effective_confirm: bool
    tools: list[ToolPolicyOut]


class ModulePolicyIn(BaseModel):
    read_enabled: bool | None = None
    write_enabled: bool | None = None
    confirm_writes: bool | None = None
    allowed_roles: list[str] | None = None
    #: An empty list clears the restriction; leaving the field out changes
    #: nothing, which is what lets a screen send one switch at a time.
    write_roles: list[str] | None = None


class ToolPolicyIn(BaseModel):
    enabled: bool | None = None
    confirm_override: bool | None = None
    allowed_roles: list[str] | None = None
    write_roles: list[str] | None = None


class PoliciesBulkIn(BaseModel):
    modules: dict[str, ModulePolicyIn] = Field(default_factory=dict)
    tools: dict[str, ToolPolicyIn] = Field(default_factory=dict)


class AccessRuleIn(BaseModel):
    subject_type: Literal["user", "team", "role"]
    #: A user id, Entra object id or email; a team handle or id; or a role key.
    subject: str = Field(min_length=1, max_length=320)
    effect: Literal["allow", "block"]
    enabled: bool = True
    note: str | None = Field(default=None, max_length=2000)


class AccessRulePatch(BaseModel):
    effect: Literal["allow", "block"] | None = None
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=2000)


class AccessRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_type: str
    subject_id: str
    subject_label: str
    effect: str
    enabled: bool
    note: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime


class AnalyticsBucket(BaseModel):
    key: str
    label: str
    runs: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class AnalyticsTotals(BaseModel):
    runs: int
    completed: int
    failed: int
    blocked: int
    cancelled: int
    open: int
    people: int
    tool_calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    #: What the runs cost. Unchanged in meaning, deliberately: a figure that
    #: quietly starts including something new is worse than one that is
    #: honestly missing a part.
    cost_usd: Decimal
    #: What reading answers aloud cost over the same window. A spoken
    #: conversation is not in here — its cost is already on its run — so read
    #: ``voice.realtime`` for that half.
    voice_cost_usd: Decimal
    #: The two above added up. The figure to quote when somebody asks what the
    #: assistant costs.
    total_cost_usd: Decimal
    confirmations_requested: int
    confirmations_approved: int
    confirmations_declined: int
    refused_by_policy: int


class VoiceBucket(BaseModel):
    key: str
    label: str
    #: Clips read aloud, or spoken conversations.
    uses: int
    #: Speech only: characters of text handed to the model, which is what it
    #: is billed on.
    characters: int
    audio_input_tokens: int
    audio_output_tokens: int
    text_input_tokens: int
    text_output_tokens: int
    #: Realtime only, and only when the browser reported it. Not what the
    #: session is billed on — that is the tokens — but the number a person
    #: recognises when they ask why the bill looks like that.
    seconds: int
    cost_usd: Decimal


class VoiceAnalyticsOut(BaseModel):
    """The voice bill, split by kind because the two halves are not alike.

    ``speech`` is exact: it is counted server-side from the text we were about
    to send. ``realtime`` is what the browser reported OpenAI charging at the
    end of a session, so a session that ended in a closed tab is missing from
    it — the figure is a floor rather than a bill.
    """

    speech: VoiceBucket
    realtime: VoiceBucket
    #: Both halves added up.
    cost_usd: Decimal
    by_day: list[VoiceBucket]
    by_user: list[VoiceBucket]
    by_model: list[VoiceBucket]


class AnalyticsOut(BaseModel):
    since: date
    until: date
    totals: AnalyticsTotals
    voice: VoiceAnalyticsOut
    by_day: list[AnalyticsBucket]
    by_user: list[AnalyticsBucket]
    by_team: list[AnalyticsBucket]
    by_model: list[AnalyticsBucket]
    by_tool: list[AnalyticsBucket]


class VoiceModelOut(BaseModel):
    """One priced voice model, for the settings screen."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    #: "speech" or "realtime". Decides which prices below mean anything.
    kind: Literal["speech", "realtime"]
    description: str
    #: speech: USD per one million characters of text.
    char_price: Decimal
    #: realtime: USD per one million tokens, by sort.
    text_input_price: Decimal
    cached_text_input_price: Decimal
    audio_input_price: Decimal
    cached_audio_input_price: Decimal
    text_output_price: Decimal
    audio_output_price: Decimal
    enabled: bool
    #: The one the settings currently point at, for its kind.
    active: bool = False


class VoiceModelIn(BaseModel):
    """Only the fields given change. Prices for the other kind are ignored."""

    enabled: bool | None = None
    char_price: Decimal | None = Field(default=None, ge=0)
    text_input_price: Decimal | None = Field(default=None, ge=0)
    cached_text_input_price: Decimal | None = Field(default=None, ge=0)
    audio_input_price: Decimal | None = Field(default=None, ge=0)
    cached_audio_input_price: Decimal | None = Field(default=None, ge=0)
    text_output_price: Decimal | None = Field(default=None, ge=0)
    audio_output_price: Decimal | None = Field(default=None, ge=0)


class RealtimeUsageIn(BaseModel):
    """What the browser heard OpenAI report over one spoken conversation.

    Sent when the conversation ends, because OpenAI bills the session directly
    and the tokens never reach this process — the ``response.done`` events the
    browser receives are the only place these figures exist on our side. The
    client is asked to add them up over the session and send the totals once.

    Untrusted, and it does not need to be trusted: nothing is authorised on the
    strength of it and it only ever adds to a cost figure. It is recorded as a
    ``client`` report so that whoever reads the number knows what it is.
    """

    text_input_tokens: int = Field(default=0, ge=0)
    cached_text_input_tokens: int = Field(default=0, ge=0)
    audio_input_tokens: int = Field(default=0, ge=0)
    cached_audio_input_tokens: int = Field(default=0, ge=0)
    text_output_tokens: int = Field(default=0, ge=0)
    audio_output_tokens: int = Field(default=0, ge=0)
    #: How long it lasted, for the screen. Capped at a day to keep a
    #: mistyped figure from making a chart unreadable.
    seconds: int = Field(default=0, ge=0, le=86_400)
