from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class PreparedMessage:
    message_id: str | None
    sender: str
    subject: str
    prompt_text: str
    fingerprint: str
    truncated: bool
    input_bytes: int
    trusted_dmarc_pass: bool
    trusted_dmarc_fail: bool = False
    sender_address: str = ""
    body: str = ""
    links: tuple[str, ...] = ()
    raw_digest: str = ""


@dataclass(frozen=True)
class Assessment:
    spam_score: float
    scam_score: float
    model_version: str
    policy_version: str
    reason: str | None = None


class Outcome(StrEnum):
    LEAVE = "leave"
    MOVE = "move"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str


@dataclass(frozen=True)
class MailItem:
    account_id: str
    mailbox: str
    uid_validity: int
    uid: int
    message_id: str | None
    fingerprint: str | None
    spam_score: float | None
    scam_score: float | None
    decision_outcome: str | None
    decision_reason: str | None
    model_version: str | None
    policy_version: str | None
    truncated: bool
    status: str
    attempts: int
    next_attempt_at: float | None
    action_phase: str | None
    destination_uid: int | None
    destination_before: str | None
    last_error: str | None
    local_verdict: str | None = None
    local_reason: str | None = None


class LocalLabel(StrEnum):
    SPAM = "spam"
    CLEAN = "clean"


@dataclass(frozen=True)
class LocalVerdict:
    label: LocalLabel
    reason: str
