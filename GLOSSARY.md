# Jev Mail Triage

This context describes how incoming email is assessed and, when explicitly enabled, routed for human review.

## Mail triage

**Assessment**:
The separate spam and scam/phishing judgments made about a message.
_Avoid_: Verdict

**Baseline**:
The set of messages already present when monitoring begins or must be safely re-established; baseline messages are not treated as new arrivals.
_Avoid_: Backfill

**Known correspondent**:
A sender address the user has written to, learned from the Sent folder. Trusted only together with a trusted DMARC pass.

**Local gate**:
The local filter that runs before Jev and gives a local verdict only when it is confident; everything else goes to Jev.
_Avoid_: RAG, local model

**Local verdict**:
The local gate's spam or clean answer for a message, with a reason. It feeds the same triage decision as a Jev assessment.

**Shadow mode**:
A local gate mode in which local verdicts are recorded for comparison, but Jev still assesses and decides.

**Review folder**:
A mailbox folder where qualifying messages are moved for a person to inspect.
_Avoid_: Quarantine, spam folder

**Scam or phishing**:
A deceptive message intended to trick its recipient into disclosing information, transferring money, or taking another harmful action. A link by itself does not make a message a scam or phishing.
_Avoid_: Suspicious link

**Spam**:
Unsolicited bulk or promotional mail. Subscribed newsletters, receipts, genuine correspondence, and other expected legitimate messages are not spam.
_Avoid_: Marketing email

**Triage decision**:
The policy outcome derived from an assessment: leave a message untouched or move it to the review folder.
_Avoid_: Classification
