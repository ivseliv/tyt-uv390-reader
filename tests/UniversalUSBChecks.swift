import Foundation

@main struct UniversalUSBChecks {
    static func main() throws {
        var usb = Data("MD-UV390".utf8) + Data(repeating: 0, count: 255 * 34)
        func setSlot(_ slot: Int, _ type: UInt8, _ name: String, _ status: UInt8) {
            let offset = 8 + (slot - 1) * 34
            usb[offset] = type
            let encoded = Data(name.utf16.flatMap { [UInt8(truncatingIfNeeded: $0), UInt8($0 >> 8)] })
            precondition(encoded.count <= 32)
            usb.replaceSubrange(offset + 1..<offset + 1 + encoded.count, with: encoded)
            usb[offset + 33] = status
        }
        setSlot(1, 1, "🎙", 1)
        setSlot(3, 0xff, "", 0)
        setSlot(16, 7, "Тест", 2)
        setSlot(17, 99, "Неизвестный", 0)
        setSlot(255, 4, "1234567890123456", 3)
        let report = try UniversalUSBReport(data: usb)
        precondition(report.model == "MD-UV390" && report.slots.count == 255)
        precondition(report.slots(in: .active).map(\.id) == [1, 16, 17, 255])
        precondition(report.slots(in: .firstFour).map(\.id) == [1, 2, 3, 4])
        precondition(report.slots(in: .all).map(\.id) == Array(1...255))
        precondition(report.slots.first?.name == "🎙" && report.slots.first?.status == .zeros)
        precondition(report.slots[15].status == .ones && report.slots[15].name == "Тест")
        precondition(report.slots[16].typeName == "Код 99" && report.slots[16].status == .notApplicable)
        precondition(report.slots[254].name == "1234567890123456" && report.slots[254].status == .dataPresent)

        func statusRows(_ data: Data) throws -> [[String: Any]] {
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let rows = json["slots"] as? [[String: Any]] else {
                preconditionFailure("Invalid USB status report")
            }
            precondition(Set(json.keys) == ["model", "slots"])
            precondition(json["model"] as? String == "MD-UV390")
            for row in rows {
                precondition(Set(row.keys) == ["slot", "name", "type", "typeName", "usbStatus"])
            }
            return rows
        }
        let firstFour = try statusRows(report.statusJSON(in: .firstFour))
        precondition(firstFour.map { $0["slot"] as? Int } == [1, 2, 3, 4])
        precondition(firstFour.map { $0["usbStatus"] as? String } == ["zeros", "notApplicable", "notApplicable", "notApplicable"])
        precondition(firstFour[0]["name"] as? String == "🎙" && firstFour[0]["typeName"] as? String == "ARC4")
        precondition(firstFour.map { $0["type"] as? Int } == [1, 0, 255, 0])

        let activeRows = try statusRows(report.statusJSON(in: .active))
        precondition(activeRows.map { $0["slot"] as? Int } == [1, 16, 17, 255])
        let allRows = try statusRows(report.statusJSON(in: .all))
        precondition(allRows.compactMap { $0["slot"] as? Int } == Array(1...255) && allRows.count == 255)
        let expectedStatuses = [1: "zeros", 16: "ones", 255: "dataPresent"]
        for (index, row) in allRows.enumerated() {
            precondition(row["usbStatus"] as? String == (expectedStatuses[index + 1] ?? "notApplicable"))
            precondition(row["name"] as? String == report.slots[index].name)
            precondition(row["typeName"] as? String == report.slots[index].typeName)
            precondition(row["type"] as? Int == Int(report.slots[index].type))
        }

        var local = UniversalPrivacyFile()
        let s = report.slots[254]
        try local.set(slot: 255, type: 4, name: "Другое имя", replacement: String(repeating: "EF", count: 32))
        precondition(s.localKey(in: local) == .mismatch)
        try local.set(slot: 255, type: 3, name: s.name, replacement: String(repeating: "AB", count: 16))
        precondition(s.localKey(in: local) == .mismatch)
        for (pattern, expected) in [("00", UniversalUSBReport.LocalValue.allZero), ("FF", .allFF),
                                    ("C4", .hex(String(repeating: "C4", count: 32)))] {
            try local.set(slot: 255, type: 4, name: s.name, replacement: String(repeating: pattern, count: 32))
            precondition(s.localKey(in: local) == expected)
        }
        precondition(report.slots[1].localKey(in: local) == .unsupported)
        precondition((try? UniversalUSBReport(data: usb.dropLast())) == nil)
        precondition((try? UniversalUSBReport(data: usb + Data([0]))) == nil)
        var wrong = usb
        wrong[7] = 0
        precondition((try? UniversalUSBReport(data: wrong)) == nil)
        wrong = usb
        wrong[8 + 33] = 255
        precondition((try? UniversalUSBReport(data: wrong)) == nil)
    }
}
