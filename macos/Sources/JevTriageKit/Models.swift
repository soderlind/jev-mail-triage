import Foundation

// Result types for helper commands. Keys arrive in snake_case and are converted.

public struct HelloResult: Decodable, Sendable {
    public let `protocol`: Int
    public let helperVersion: String
    public let python: String
    public let frozen: Bool
    public let keychainBackend: String
    public let commands: [String]
}

public struct BridgeSettings: Codable, Equatable, Sendable {
    public var accountId: String
    public var host: String
    public var port: Int
    public var securityMode: String
    public var username: String
    public var mailbox: String
    public var tlsCaFile: String?

    public static let securityModes = ["starttls", "implicit_tls"]

    public init(
        accountId: String = "primary", host: String = "127.0.0.1", port: Int = 1143,
        securityMode: String = "starttls", username: String = "", mailbox: String = "INBOX",
        tlsCaFile: String? = nil
    ) {
        self.accountId = accountId
        self.host = host
        self.port = port
        self.securityMode = securityMode
        self.username = username
        self.mailbox = mailbox
        self.tlsCaFile = tlsCaFile
    }

    public var patch: JSONValue {
        [
            "account_id": .string(accountId), "host": .string(host), "port": .number(Double(port)),
            "security_mode": .string(securityMode), "username": .string(username),
            "mailbox": .string(mailbox), "tls_ca_file": .optional(tlsCaFile),
        ]
    }
}

public struct ServiceSettings: Codable, Equatable, Sendable {
    public var pollIntervalSeconds: Int
    public var batchSize: Int
    public var maxAttempts: Int
    public var backoffMaxSeconds: Int

    public var patch: JSONValue {
        [
            "poll_interval_seconds": .number(Double(pollIntervalSeconds)),
            "batch_size": .number(Double(batchSize)),
            "max_attempts": .number(Double(maxAttempts)),
            "backoff_max_seconds": .number(Double(backoffMaxSeconds)),
        ]
    }
}

public struct PolicySettings: Codable, Equatable, Sendable {
    public var moveEnabled: Bool
    public var spamThreshold: Double
    public var scamThreshold: Double
    public var maxInputBytes: Int
    public var reviewFolder: String
    public var trustedAuthservId: String?
    public var allowlist: [String]
    public var model: String

    /// Everything except `move_enabled`, which changes only through an explicit action.
    public var patch: JSONValue {
        [
            "spam_threshold": .number(spamThreshold), "scam_threshold": .number(scamThreshold),
            "max_input_bytes": .number(Double(maxInputBytes)), "review_folder": .string(reviewFolder),
            "trusted_authserv_id": .optional(trustedAuthservId), "allowlist": .strings(allowlist),
            "model": .string(model),
        ]
    }
}

public struct GateSettings: Codable, Equatable, Sendable {
    public var mode: String
    public var allowedLanguages: [String]
    public var spamFolders: [String]
    public var hamFolders: [String]
    public var sentFolders: [String]
    public var minExamples: Int
    public var bayesSpamThreshold: Double
    public var bayesCleanThreshold: Double
    public var nearDuplicateSimilarity: Double
    public var learnFromJev: Bool
    public var trainIntervalSeconds: Int

    public static let modes = ["off", "shadow", "enforce"]

    public var patch: JSONValue {
        [
            "mode": .string(mode), "allowed_languages": .strings(allowedLanguages),
            "spam_folders": .strings(spamFolders), "ham_folders": .strings(hamFolders),
            "sent_folders": .strings(sentFolders), "min_examples": .number(Double(minExamples)),
            "bayes_spam_threshold": .number(bayesSpamThreshold),
            "bayes_clean_threshold": .number(bayesCleanThreshold),
            "near_duplicate_similarity": .number(nearDuplicateSimilarity),
            "learn_from_jev": .bool(learnFromJev),
            "train_interval_seconds": .number(Double(trainIntervalSeconds)),
        ]
    }
}

public struct AppConfig: Codable, Equatable, Sendable {
    public var bridge: BridgeSettings
    public var service: ServiceSettings
    public var policy: PolicySettings
    public var gate: GateSettings
    public var databasePath: String
}

public struct ConfigResult: Decodable, Sendable {
    public let path: String
    public let exists: Bool?
    public let config: AppConfig?
}

public struct StoredSecret: Decodable, Sendable {
    public let stored: String
}

public struct AdoptResult: Decodable, Sendable {
    public let adopted: [String]
    public let missing: [String]
}

public struct BridgeCheck: Decodable, Sendable, Equatable {
    public let connected: Bool
    public let mailbox: String
    public let supportsMove: Bool
    public let supportsUidplus: Bool
    public let reviewFolder: String
    public let reviewFolderExists: Bool
    public let missingTrainingFolders: [String]
    public let folders: [String]
}

/// Result of `bridge.folders`.
public struct FolderList: Decodable, Sendable, Equatable {
    public let folders: [String]
}

/// Options for a folder picker. A saved value missing on the server stays selectable
/// (first), so opening Settings never silently rewrites the configuration.
public func folderChoices(_ folders: [String], current: String) -> [String] {
    var seen = Set<String>()
    let unique = folders.filter { seen.insert($0).inserted }
    return current.isEmpty || seen.contains(current) ? unique : [current] + unique
}

public struct TypeSafeCheck: Decodable, Sendable, Equatable {
    public let valid: Bool
    public let model: String
    public let modelAvailable: Bool
    public let models: [String]
}

public struct WorkerError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
    public let at: Double?
}

public struct LastCycle: Codable, Equatable, Sendable {
    public let finishedAt: Double
    /// `steady`, or `baseline`/`rebaseline` when existing mail was recorded without assessment.
    public let baseline: String
    public let discovered: Int
    public let assessed: Int
    public let moved: Int
    public let attention: Int

    public var recordedBaseline: Bool { baseline != "steady" }
}

/// Contents of `worker-status.json`, written by the background worker.
public struct WorkerStatus: Codable, Equatable, Sendable {
    public let `protocol`: Int
    public let version: String
    public let pid: Int32
    public let state: String
    public let since: Double
    public let heartbeat: Double
    public let error: WorkerError?
    public let lastCycle: LastCycle?
    public let nextCycleAt: Double?
    public let consecutiveFailures: Int
    public let lockHolder: Int32?
    public let settings: [String: JSONValue]?
}

public struct WorkerControl: Codable, Equatable, Sendable {
    public let paused: Bool
    public let generation: Int

    public init(paused: Bool, generation: Int) {
        self.paused = paused
        self.generation = generation
    }
}

public struct WorkerInfo: Decodable, Sendable {
    public let status: WorkerStatus?
    public let alive: Bool
    public let control: WorkerControl
}

public struct ControlResult: Decodable, Sendable {
    public let control: WorkerControl
}

public struct GateInfo: Decodable, Sendable {
    public let mode: String
    public let totals: [String: Double]
}

public struct StatusResult: Decodable, Sendable {
    public let counts: [String: [String: Int]]
    public let gate: GateInfo
    public let moveEnabled: Bool
    public let reviewFolder: String
    public let lockHeld: Bool
    public let lockHolder: Int32?
    public let worker: WorkerInfo
}

public struct Statistics: Decodable, Sendable {
    public let discovered: Int
    public let assessed: Int
    public let waiting: Int
    public let needsAttention: Int
    public let flagged: Int
    public let spam: Int
    public let scam: Int
    public let moved: Int
    public let moving: Int
    public let leftInPlace: Int
    public let tooLarge: Int
    public let allowlisted: Int
    public let restoredByUser: Int
    public let decidedByJev: Int
    public let decidedLocally: Int
    public let localSpam: Int
    public let localClean: Int
    public let localDisagree: Int
    public let firstSeen: Double?
    public let lastSeen: Double?
    public let localReasons: [String: Int]
}

public struct StatsResult: Decodable, Sendable {
    public let days: Int?
    public let stats: Statistics
}

/// One labelled share of a chart.
public struct StatSlice: Identifiable, Equatable, Sendable {
    public let id: String
    public let label: String
    public let value: Int

    public init(id: String, label: String, value: Int) {
        self.id = id
        self.label = label
        self.value = value
    }
}

extension Statistics {
    /// Assessed mail split into non-overlapping outcomes; empty slices are dropped.
    /// `left_in_place` includes allowlisted, truncated and restored mail, so those are
    /// subtracted to leave the "below threshold" remainder.
    public var outcomeSlices: [StatSlice] {
        let otherLeft = leftInPlace - allowlisted - tooLarge - restoredByUser
        let slices = [
            StatSlice(id: "spam", label: "Spam", value: spam),
            StatSlice(id: "scam", label: "Scam", value: scam),
            StatSlice(id: "clean", label: "Below threshold", value: max(otherLeft, 0)),
            StatSlice(id: "allowlisted", label: "Allowlisted", value: allowlisted),
            StatSlice(id: "too_large", label: "Too large", value: tooLarge),
            StatSlice(id: "restored", label: "Restored by you", value: restoredByUser),
        ]
        return slices.filter { $0.value > 0 }
    }

    /// Share of assessed mail that was flagged, or nil when nothing was assessed.
    public var flaggedRate: Double? {
        assessed > 0 ? Double(flagged) / Double(assessed) : nil
    }

    /// Local gate reasons, largest first, with readable labels.
    public var reasonSlices: [StatSlice] {
        localReasons
            .sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
            .map { key, value in
                let words = key.replacingOccurrences(of: "local_", with: "").replacingOccurrences(of: "_", with: " ")
                return StatSlice(id: key, label: words.prefix(1).uppercased() + words.dropFirst(), value: value)
            }
    }
}

public struct CandidateKey: Hashable, Codable, Sendable {
    public let mailbox: String
    public let uidValidity: Int
    public let uid: Int

    public init(mailbox: String, uidValidity: Int, uid: Int) {
        self.mailbox = mailbox
        self.uidValidity = uidValidity
        self.uid = uid
    }

    public var json: JSONValue {
        ["mailbox": .string(mailbox), "uid_validity": .number(Double(uidValidity)), "uid": .number(Double(uid))]
    }
}

public struct Candidate: Decodable, Identifiable, Sendable, Equatable {
    public let mailbox: String
    public let uidValidity: Int
    public let uid: Int
    public let messageId: String?
    public let spamScore: Double?
    public let scamScore: Double?
    public let decisionReason: String?
    public let modelVersion: String?
    public let localVerdict: String?
    public let localReason: String?
    public let status: String
    public let truncated: Bool
    /// Present only in `candidates.headers` responses; held in memory, never persisted.
    public let headerStatus: String?
    public let headers: [String: JSONValue]?

    public var key: CandidateKey { CandidateKey(mailbox: mailbox, uidValidity: uidValidity, uid: uid) }
    public var id: CandidateKey { key }
}

public struct CandidatesResult: Decodable, Sendable {
    public let candidates: [Candidate]
    public let spamThreshold: Double
    public let scamThreshold: Double
}

public struct CandidateHeadersResult: Decodable, Sendable {
    public let candidates: [Candidate]
    public let notCandidates: [CandidateKey]
}
