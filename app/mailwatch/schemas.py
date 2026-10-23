from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MailWatchSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    mailbox: str
    #: The settings' own list. Empty means the CEO role.
    senders: list[str]
    #: Who is actually watched now: ``senders``, or the CEO role's holders.
    watching: list[str] = []
    use_model: bool
    watch_from: datetime | None
    #: Whether a bookmark is held — the next read is "since then".
    has_bookmark: bool = False
    subscription_id: str | None
    subscription_mailbox: str | None
    subscription_expires_at: datetime | None
    #: Where Graph posts, or why there is no such place here.
    notification_url: str | None = None
    last_notified_at: datetime | None
    last_read_at: datetime | None
    last_error: str | None
    mails_kept: int = 0


class MailWatchSettingsIn(BaseModel):
    enabled: bool | None = None
    #: Changing it starts the new mailbox from now; its history is not read.
    mailbox: str | None = Field(default=None, max_length=320)
    #: Empty list: whoever holds the CEO role.
    senders: list[str] | None = None
    use_model: bool | None = None


class ReadReportOut(BaseModel):
    mailbox: str
    seen: int
    kept: int
    already: int
    senders: list[str]
    error: str | None


class WatchedMailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mailbox: str
    received_at: datetime | None
    sender_email: str | None
    sender_name: str | None
    subject: str | None
    has_attachments: bool
    attachment_names: list[str]
    conversation_id: str | None
    web_link: str | None
    created_at: datetime
    #: new_tender, negotiation, general or confidential.
    category: str | None
    kind: str | None
    stage: str | None
    #: rules, model, or none — "none" with a low confidence is "unsure".
    decided_by: str | None
    reason: str | None
    confidence: float | None
    title: str | None
    customer: str | None
    reference_numbers: list[str]
    ariba_doc: str | None
    closing_at: datetime | None
    original_sender: str | None
    model_cost_usd: Decimal
    classify_error: str | None
    #: new, existing, stage_change, duplicate_mail, not_found or unsure.
    verdict: str | None
    matched_item_id: str | None
    match_method: str | None
    match_score: float | None
    match_reason: str | None
    match_candidates: list[dict[str, Any]]
    #: The matched task as SharePoint had it: assignee, status, stage, link…
    task: dict[str, Any] | None
    duplicate_of_id: uuid.UUID | None
    match_error: str | None
    teams_posted_at: datetime | None
    teams_error: str | None


class WatchedMailDetailOut(WatchedMailOut):
    body: str | None
    internet_message_id: str | None
    graph_message_id: str


class ReadingOut(BaseModel):
    category: str
    kind: str
    stage: str | None
    decided_by: str
    reason: str
    confidence: float
    title: str | None
    customer: str | None
    references: list[str]
    ariba_doc: str | None
    closing_at: datetime | None
    original_sender: str | None
    model_cost_usd: Decimal
    error: str | None


class TrialIn(BaseModel):
    days: int = Field(default=7, ge=1, le=60)
    limit: int = Field(default=100, ge=1, le=500)
    #: Unset follows the settings.
    use_model: bool | None = None


class CandidateOut(BaseModel):
    item_id: str
    title: str
    customer: str | None
    assignee: str | None
    status: str | None
    created_at: str | None
    score: float
    why: str


class MatchOut(BaseModel):
    verdict: str
    method: str | None
    item_id: str | None
    score: float
    reason: str
    candidates: list[CandidateOut]
    task: dict[str, Any] | None
    duplicate_of_id: uuid.UUID | None
    model_cost_usd: float
    error: str | None


class SearchIn(BaseModel):
    """Try the Proposals search with any title, as if a mail had said it."""

    text: str = Field(min_length=2, max_length=500)
    customer: str | None = None
    #: Tender, RFQ or PR numbers, e.g. "6000153446" or "PR 10205333".
    references: list[str] = []
    #: eoi, rfq, rfp, rft…: what stage the mail would be at.
    stage: str | None = None
    negotiation: bool = False


class TrialRowOut(BaseModel):
    received_at: datetime | None
    #: Hidden for a confidential mail; open it in Outlook with the link.
    subject: str | None
    web_link: str | None
    attachments: int
    reading: ReadingOut
    #: Only for new tenders and negotiations.
    match: MatchOut | None = None


class TrialOut(BaseModel):
    mailbox: str
    senders: list[str]
    since: datetime
    mails: int
    by_category: dict[str, int]
    by_decided: dict[str, int]
    by_verdict: dict[str, int]
    #: Rules not sure and no model to ask (or it failed).
    unsure: int
    model_cost_usd: Decimal
    rows: list[TrialRowOut]


class TeamsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connected: bool = False
    account_email: str | None
    account_name: str | None
    connected_at: datetime | None
    #: The test channel — the default — and whether posts go there now.
    test_team: str = ""
    test_channel: str = ""
    is_test_channel: bool = True
    #: Where cards go: "channel" or "chat"; said in words.
    target_kind: str = "channel"
    chat_id: str | None = None
    chat_name: str | None = None
    destination: str = ""
    #: The connection was granted group chats (else: connect again).
    can_chat: bool = False
    team_name: str | None
    channel_name: str | None
    post_negotiations: bool
    posting_from: datetime | None
    assign_new_tenders: bool
    assign_from: datetime | None
    #: Where new tenders' tasks are created: test / real / none.
    task_list: str = "test"
    #: The lists that choice can name, with their links: [{key, name, url}].
    task_lists: list[dict[str, str]] = []
    last_used_at: datetime | None
    last_error: str | None
    posted: int = 0
    test_tasks: int = 0


class TeamsIn(BaseModel):
    """Either switch, or both; one left out stays as it is."""

    post_negotiations: bool | None = None
    assign_new_tenders: bool | None = None
    task_list: Literal["test", "real", "none"] | None = None
    #: Where cards are posted: a team and channel, or a group chat.
    team_id: str | None = None
    channel_id: str | None = None
    chat_id: str | None = None


class TestTaskOut(BaseModel):
    """A task the mail watch would have created on the Proposals list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mail_id: uuid.UUID
    action: str
    title: str
    assigned_to_name: str | None
    assigned_to_email: str | None
    assigned_to_lookup_id: str | None
    status: str | None
    current_type: str | None
    end_user: str | None
    start_date: datetime | None
    bcd: datetime | None
    remarks: str | None
    previous_owner: str | None
    fields: dict[str, Any]
    assign_reason: str
    standing: list[dict[str, Any]]
    previous_item_id: str | None
    previous_title: str | None
    previous_stage: str | None
    #: The item made for it — in the list ``list_id`` — or why there is none.
    list_id: str | None = None
    list_item_id: str | None = None
    list_item_url: str | None = None
    list_error: str | None = None
    created_at: datetime
    #: From the mail.
    subject: str | None = None
    received_at: datetime | None = None
    teams_posted_at: datetime | None = None
    teams_error: str | None = None
