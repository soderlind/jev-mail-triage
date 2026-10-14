from __future__ import annotations

from email.utils import parseaddr

from .config import PolicyConfig
from .models import Assessment, Decision, Outcome, PreparedMessage


def is_allowlisted(sender: str, allowlist: tuple[str, ...]) -> bool:
    _, address = parseaddr(sender)
    address = address.casefold()
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].rstrip(".")
    for entry in allowlist:
        candidate = entry.casefold().strip().lstrip("@").rstrip(".")
        if "@" in candidate:
            if address == candidate:
                return True
        elif domain == candidate or domain.endswith(f".{candidate}"):
            return True
    return False


def decide(prepared: PreparedMessage, assessment: Assessment, config: PolicyConfig) -> Decision:
    if prepared.truncated:
        return Decision(Outcome.LEAVE, "truncated_input")
    if is_allowlisted(prepared.sender, config.allowlist) and prepared.trusted_dmarc_pass:
        return Decision(Outcome.LEAVE, "authenticated_allowlist")
    if assessment.spam_score >= config.spam_threshold:
        return Decision(Outcome.MOVE, assessment.reason or "spam_threshold")
    if assessment.scam_score >= config.scam_threshold:
        return Decision(Outcome.MOVE, assessment.reason or "scam_threshold")
    return Decision(Outcome.LEAVE, assessment.reason or "below_threshold")
