import Foundation

@main struct RadioWriteReviewChecks {
    @MainActor static func main() async throws {
        func proposal(blocks: Int = 1, sectors: Int = 1, changes: [String: Int] = ["channels": 1], dmrID: Int = 123) throws -> Data {
            try JSONSerialization.data(withJSONObject: [
                "model": "MD-UV390", "dmr_id": dmrID, "blocks": blocks, "sectors": sectors,
                "changes": changes, "plan": "VFlU"
            ])
        }
        let bytes = try proposal()
        let review = try RadioWriteReview(bytes, expectedModel: "MD-UV390")
        precondition((try? RadioWriteReview(bytes, expectedModel: "MD-UV380")) == nil)
        precondition((try? RadioWriteReview(bytes, expectedDMRID: 124)) == nil)
        for invalid in [
            try proposal(blocks: 0), try proposal(sectors: 0), try proposal(sectors: 3),
            try proposal(changes: ["dmr_id": 1]), try proposal(changes: ["channels": -1]),
            try proposal(changes: ["unexpected": 1]), try proposal(changes: [:]), try proposal(dmrID: -1)
        ] { precondition((try? RadioWriteReview(invalid)) == nil) }

        let backup = URL(fileURLWithPath: "/test-backup.tytbackup")
        var confirmations = 0, writes = 0
        let same = try RadioWriteReview(proposal(blocks: 0, sectors: 0, changes: [:]))
        let notNeeded = try await same.applyIfApproved(confirm: { confirmations += 1; return backup },
                                                       write: { _, _ in writes += 1 })
        precondition(notNeeded != nil && confirmations == 0 && writes == 0)
        let declined = try await review.applyIfApproved(confirm: { confirmations += 1; return nil },
                                                        write: { _, _ in writes += 1 })
        precondition(declined == nil && confirmations == 1 && writes == 0)
        let applied = try await review.applyIfApproved(confirm: { confirmations += 1; return backup }) { received, url in
            precondition(received == bytes && url == backup)
            writes += 1
        }
        precondition(applied?.contains(backup.path) == true && confirmations == 2 && writes == 1)
        do {
            _ = try await review.applyIfApproved(confirm: { backup }) { _, _ in throw CocoaError(.fileWriteUnknown) }
            preconditionFailure("A failed verification must not report a successful write")
        } catch { precondition((error as NSError).code == CocoaError.fileWriteUnknown.rawValue) }
    }
}
