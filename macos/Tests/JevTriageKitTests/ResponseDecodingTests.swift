import Foundation
import Testing

@testable import JevTriageKit

func fixture(_ name: String) throws -> Data {
    let url = try #require(Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json"))
    return try Data(contentsOf: url)
}

@Suite struct ResponseDecodingTests {
    @Test func decodesRealHelperResponses() throws {
        let hello = try ResponseDecoder.decode(try fixture("hello"), expectingID: "h", as: HelloResult.self)
        #expect(hello.protocol == helperProtocolVersion)
        #expect(hello.commands.contains("worker.control"))

        let config = try ResponseDecoder.decode(try fixture("config"), expectingID: "s", as: ConfigResult.self)
        let effective = try #require(config.config)
        #expect(effective.bridge.username == "user@example.com")
        #expect(effective.policy.moveEnabled == false)
        #expect(effective.gate.mode == "shadow")

        let stats = try ResponseDecoder.decode(try fixture("stats"), expectingID: "st", as: StatsResult.self)
        #expect(stats.days == 7)
        #expect(stats.stats.firstSeen == nil)

        let status = try ResponseDecoder.decode(try fixture("status"), expectingID: "w", as: StatusResult.self)
        #expect(status.moveEnabled == false)
        #expect(status.worker.control == WorkerControl(paused: true, generation: 1))
        #expect(status.worker.status?.state == "bridge_unavailable")
        #expect(status.worker.alive == false)

        let control = try ResponseDecoder.decode(try fixture("control"), expectingID: "ctl", as: ControlResult.self)
        #expect(control.control.paused)
    }

    @Test func decodesCandidateHeadersHeldInMemory() throws {
        let result = try ResponseDecoder.decode(
            try fixture("candidate-headers"), expectingID: "ch", as: CandidateHeadersResult.self)
        #expect(result.candidates.count == 2)
        #expect(result.candidates[0].headers?["subject"]?.stringValue == "Win now")
        #expect(result.candidates[1].headers == nil)
        #expect(result.candidates[1].headerStatus == "source_not_pending")
        #expect(result.notCandidates == [CandidateKey(mailbox: "INBOX", uidValidity: 7, uid: 99)])
    }

    @Test func helperErrorsBecomeTypedErrors() throws {
        #expect(throws: HelperError.self) {
            try ResponseDecoder.decode(try fixture("not-configured"), expectingID: "n", as: StatusResult.self)
        }
        do {
            _ = try ResponseDecoder.decode(
                try fixture("confirmation-required"), expectingID: "e", as: ConfigResult.self)
            Issue.record("expected an error")
        } catch let error as HelperError {
            #expect(error.code == "confirmation_required")
            #expect(error.retryable == false)
            #expect(error.recoverySuggestion != nil)
        }
    }

    @Test(arguments: [
        ("", "empty output"),
        ("not json", "not a protocol response"),
        ("{\"protocol\":1,\"id\":\"x\",\"ok\":true,\"result\":{}}\n{}", "more than one line of output"),
        ("{\"protocol\":1,\"id\":\"other\",\"ok\":true,\"result\":{\"stored\":\"x\"}}", "response id does not match request"),
        ("{\"protocol\":1,\"id\":\"x\",\"ok\":true}", "ok response without result"),
        ("{\"protocol\":1,\"id\":\"x\",\"ok\":false}", "error response without error"),
    ])
    func rejectsMalformedResponses(output: String, reason: String) {
        #expect(throws: HelperClientError.malformedResponse(reason)) {
            try ResponseDecoder.decode(Data(output.utf8), expectingID: "x", as: StoredSecret.self)
        }
    }

    @Test func rejectsOtherProtocolVersions() {
        let data = Data(#"{"protocol":2,"id":"x","ok":true,"result":{"stored":"x"}}"#.utf8)
        #expect(throws: HelperClientError.protocolMismatch(helper: 2, app: 1)) {
            try ResponseDecoder.decode(data, expectingID: "x", as: StoredSecret.self)
        }
    }

    @Test func nullValuesSurviveEncodingForKeyRemoval() throws {
        let patch = BridgeSettings(username: "u").patch
        let json = String(decoding: try JSONEncoder().encode(patch), as: UTF8.self)
        #expect(json.contains(#""tls_ca_file":null"#))
        #expect(json.contains(#""port":1143"#))
    }

    @Test func policyPatchNeverChangesMoveSetting() throws {
        let config = try ResponseDecoder.decode(try fixture("config"), expectingID: "s", as: ConfigResult.self)
        guard case .object(let fields) = try #require(config.config).policy.patch else {
            Issue.record("policy patch must be an object")
            return
        }
        #expect(fields["move_enabled"] == nil)
    }
}

@Suite struct FolderChoiceTests {
    @Test func keepsSavedFolderMissingFromServerSelectable() {
        #expect(folderChoices(["INBOX", "Spam"], current: "Folders/Jev Review") == ["Folders/Jev Review", "INBOX", "Spam"])
    }

    @Test func doesNotDuplicateKnownOrEmptyValues() {
        #expect(folderChoices(["INBOX", "Spam", "INBOX"], current: "INBOX") == ["INBOX", "Spam"])
        #expect(folderChoices(["INBOX"], current: "") == ["INBOX"])
    }
}

@Suite struct StatisticsChartTests {
    private func stats(_ overrides: String) throws -> Statistics {
        var json = try JSONSerialization.jsonObject(with: try fixture("stats")) as! [String: Any]
        var result = json["result"] as! [String: Any]
        var values = result["stats"] as! [String: Any]
        let extra = try JSONSerialization.jsonObject(with: Data(overrides.utf8)) as! [String: Any]
        values.merge(extra) { $1 }
        result["stats"] = values
        json["result"] = result
        let data = try JSONSerialization.data(withJSONObject: json)
        return try ResponseDecoder.decode(data, expectingID: "st", as: StatsResult.self).stats
    }

    @Test func outcomeSlicesPartitionAssessedMail() throws {
        let s = try stats(#"{"assessed": 100, "flagged": 6, "spam": 4, "scam": 2, "left_in_place": 94, "allowlisted": 3, "too_large": 1, "restored_by_user": 0}"#)
        #expect(s.outcomeSlices.map(\.id) == ["spam", "scam", "clean", "allowlisted", "too_large"])
        #expect(s.outcomeSlices.reduce(0) { $0 + $1.value } == 100)
        #expect(s.flaggedRate == 0.06)
    }

    @Test func emptyPeriodHasNoSlicesOrRate() throws {
        let s = try stats("{}")
        #expect(s.outcomeSlices.isEmpty)
        #expect(s.flaggedRate == nil)
    }

    @Test func reasonsAreSortedAndReadable() throws {
        let s = try stats(#"{"local_reasons": {"near_duplicate_junk": 1, "local_bayes_clean": 128}}"#)
        #expect(s.reasonSlices.map(\.label) == ["Bayes clean", "Near duplicate junk"])
    }
}
