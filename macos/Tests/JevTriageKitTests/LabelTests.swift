import Foundation
import Testing

@testable import JevTriageKit

@Suite struct LabelTests {
    @Test func knownCodesReadAsPlainLanguageAndUnknownCodesStillRead() {
        #expect(MailLabels.reason("spam_threshold") == "Spam score")
        #expect(MailLabels.reason(nil) == "—")
        #expect(MailLabels.status("assessed") == "Not moved")
        #expect(MailLabels.status("some_new_state") == "Some new state")
    }

    @Test func attentionTotalsSeparateRetryableRows() throws {
        let json = #"{"reasons": [{"reason": "attempt_limit_reached", "retryable": true, "count": 23}, {"reason": "move_outcome_ambiguous", "retryable": false, "count": 2}]}"#
        let result = try JSONDecoder().decode(AttentionResult.self, from: Data(json.utf8))
        #expect(result.total == 25)
        #expect(result.retryable == 23)
        #expect(result.retried == nil)
        #expect(result.reasons[1].explanation.contains("review folder"))
    }
}
