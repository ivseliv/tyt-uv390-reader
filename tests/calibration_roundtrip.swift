import Foundation

@main
struct CalibrationRoundtrip {
    static func ensure(_ condition: Bool, _ message: String) throws {
        if !condition { throw CalibrationIssue(message) }
    }

    static func main() throws {
        let dir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        var fixture = Data((0..<8192).map { UInt8(truncatingIfNeeded: $0 * 31 + 7) })
        fixture.replaceSubrange(0x1b0..<0x1b4, with: [0x00, 0x25, 0x31, 0x43])
        fixture.replaceSubrange(0x124..<0x128, with: [0x00, 0x00, 0x55, 0x14])
        let originalURL = dir.appendingPathComponent("source.test")
        try fixture.write(to: originalURL)
        let source = try CalibrationProfile.open(originalURL)
        try ensure(try source.nativeData() == fixture, "No-op must retain every byte")
        let byID = Dictionary(uniqueKeysWithValues: CalibrationField.all.map { ($0.id, $0) })
        try ensure((1...48).allSatisfy { byID[$0] != nil }, "Missing field")
        try ensure(source.sourceValue(byID[1]!, 1) == "433,12500", "Frequency A RX")
        try ensure(source.sourceValue(byID[4]!, 5) == "145,50000", "Frequency B TX")

        // Independent landmarks in the .test grid, including the band boundary of both power tables.
        let changes = [(5, 5, 0x00), (10, 5, 0x05), (11, 5, 0x09), (28, 5, 0x0c),
                       (12, 9, 0x18), (29, 1, 0x19), (13, 9, 0x198), (30, 1, 0x199),
                       (20, 1, 0x90), (33, 1, 0x9b), (44, 5, 0xf3), (46, 5, 0x1af)]
        var edited = source
        var expected = fixture
        for (row, point, offset) in changes {
            let current = fixture[offset] ^ 0x80
            expected[offset] = current
            edited.values[byID[row]!.key(point)] = String(current)
        }
        edited.values["2.9"] = "432.01230"
        expected.replaceSubrange(0x1f4..<0x1f8, with: [0x30, 0x12, 0x20, 0x43])
        edited.values["47.5"] = "0,14"
        let jsonURL = dir.appendingPathComponent("profile.json")
        try LocalCopy.write(edited.json(), to: jsonURL)
        let fromJSON = try CalibrationProfile.open(jsonURL)
        try ensure(edited == fromJSON, "JSON profile must retain native and manual data")
        let nativeURL = dir.appendingPathComponent("copy.test")
        try LocalCopy.write(fromJSON.nativeData(), to: nativeURL)
        let reread = try CalibrationProfile.open(nativeURL)
        try ensure(try reread.nativeData() == expected, "Only the edited offsets should change")
        try ensure(try Data(contentsOf: originalURL) == fixture, "Original changed")
        try ensure(reread.sourceValue(byID[2]!, 9) == "432,01230", "BCD encoding")

        var visited = Set<Int>()
        for field in CalibrationField.all where !field.measurement {
            for point in field.points {
                let pos = field.offset(point)
                for byte in pos..<(pos + (field.frequency ? 4 : 1)) {
                    try ensure((0..<8192).contains(byte) && visited.insert(byte).inserted, "Overlapping cells")
                }
            }
        }
        for (key, bad) in [("5.5", "256"), ("29.6", "0"), ("1.1", "12.123456"), ("48.5", "100.1")] {
            var invalid = edited
            invalid.values[key] = bad
            do {
                try invalid.validate()
            } catch is CalibrationIssue { continue }
            throw CalibrationIssue("Accepted invalid field \(key)")
        }
        do { _ = try CalibrationProfile(native: fixture.dropLast()); throw CocoaError(.fileReadCorruptFile) }
        catch is CalibrationIssue {}
        do { try LocalCopy.write(Data(), to: originalURL); throw CocoaError(.fileWriteUnknown) }
        catch is LocalCopy.Failure {}
        try ensure(try Data(contentsOf: originalURL) == fixture, "Duplicate name overwrote the original")
    }
}
