import Foundation

/// Plain-language names for the helper's internal codes. Unknown codes fall back to the
/// code with underscores replaced, so new helper codes still display sensibly.
public enum MailLabels {
    public static func humanize(_ code: String) -> String {
        let words = code.replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    /// Why a message was (or would be) moved or left.
    public static func reason(_ code: String?) -> String {
        guard let code else { return "—" }
        switch code {
        case "spam_threshold": return "Spam score"
        case "scam_threshold": return "Scam score"
        case "below_threshold": return "Below thresholds"
        case "authenticated_allowlist": return "Allowlisted sender"
        case "truncated_input": return "Long message, not moved"
        case "local_bayes_spam": return "Local gate: learned spam"
        case "local_bayes_clean": return "Local gate: learned clean"
        case "near_duplicate_junk": return "Local gate: like known spam"
        default: return humanize(code)
        }
    }

    /// A message's processing state.
    public static func status(_ code: String) -> String {
        switch code {
        case "queued": return "Waiting"
        case "retry_wait": return "Retrying"
        case "assessed": return "Not moved"
        case "moving": return "Moving"
        case "moved": return "Moved"
        case "needs_attention": return "Needs attention"
        case "superseded": return "Skipped"
        default: return humanize(code)
        }
    }

    public static func localVerdict(_ code: String?) -> String {
        guard let code else { return "—" }
        switch code {
        case "spam": return "Spam"
        case "clean": return "Clean"
        default: return humanize(code)
        }
    }
}

/// One group of messages needing attention, from `attention` or `attention.retry`.
public struct AttentionReason: Decodable, Sendable, Equatable, Identifiable {
    public let reason: String
    /// No move is in progress, so the helper can safely re-queue these.
    public let retryable: Bool
    public let count: Int

    public init(reason: String, retryable: Bool, count: Int) {
        self.reason = reason
        self.retryable = retryable
        self.count = count
    }

    public var id: String { "\(reason)-\(retryable)" }

    public var explanation: String {
        switch reason {
        case "attempt_limit_reached" where retryable:
            return "Assessment failed several times in a row, usually because TypeSafe or Bridge was unavailable."
        case "source_message_missing", "source_missing_unreconciled", "source_missing_before_move":
            return "The message disappeared from the Inbox before triage finished."
        case "malformed_message", "malformed_message_during_recheck":
            return "The message could not be read as valid email."
        case "input_too_large":
            return "The message is too long for the model to assess, even shortened. It stays in the Inbox."
        case "message_processing_error":
            return "An unexpected error occurred while reading the message."
        case "uidvalidity_changed", "uidvalidity_changed_before_recheck", "uidvalidity_changed_before_move":
            return "Bridge renumbered the mailbox, so the message can no longer be matched safely."
        case "message_fingerprint_changed", "message_changed_before_move", "source_changed_after_copy":
            return "The message changed while it was being handled."
        case "safe_move_capability_unavailable":
            return "Bridge did not offer a safe way to move the message."
        default:
            return retryable
                ? "Triage stopped handling the message: \(MailLabels.humanize(reason).lowercased())."
                : "A move was interrupted (\(MailLabels.humanize(reason).lowercased())). The message may be in the Inbox, the review folder, or both."
        }
    }
}

public struct AttentionResult: Decodable, Sendable {
    public let reasons: [AttentionReason]
    public let retried: Int?

    public var total: Int { reasons.reduce(0) { $0 + $1.count } }
    public var retryable: Int { reasons.filter(\.retryable).reduce(0) { $0 + $1.count } }
}
