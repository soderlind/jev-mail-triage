# Conservative mailbox recovery and movement

Routine monitoring establishes a baseline and does not process messages already present; after UIDVALIDITY changes it keeps the old identity epoch and re-baselines the current mailbox, accepting that mail arriving during recovery may need explicit backfill. Moves require negotiated UID MOVE or UIDPLUS-scoped expunge, and ambiguous crash recovery requires manual review. This favors avoiding duplicate or unrelated mailbox changes over automatically catching up every message.
