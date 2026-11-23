import Foundation

/// Only synthetic vectors. The test never opens a user-selected file or an attached radio.
@main
struct PrivacyFileChecks {
    static func rejected(_ action: () throws -> Void) -> Bool {
        do { try action(); return false } catch { return true }
    }

    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }

        // 16 large records followed by 239 small ones. The final 2628 bytes are opaque.
        let factory = root.appendingPathComponent("synthetic.dec")
        let saved = root.appendingPathComponent("saved.dec")
        var bytes = Data(repeating: 0xa9, count: 0x5000)
        let longSlot = 15 * 100, shortSlot = 1600, last = 1600 + 238 * 68
        bytes[longSlot] = 7
        bytes[shortSlot] = 4
        bytes[last] = 1
        let nameBytes = Array("Проба".utf16).flatMap { [UInt8(truncatingIfNeeded: $0), UInt8($0 >> 8)] }
        bytes.replaceSubrange(shortSlot + 4..<shortSlot + 36,
                              with: nameBytes + [UInt8](repeating: 0, count: 32 - nameBytes.count))
        for i in 0..<64 { bytes[longSlot + 36 + i] = UInt8(63 - i) }
        for i in 0..<32 { bytes[shortSlot + 36 + i] = UInt8(31 - i) }
        for i in 0..<5 { bytes[last + 36 + i] = UInt8(4 - i) }
        try bytes.write(to: factory)
        var file = try UniversalPrivacyFile(url: factory)
        precondition(file.data == bytes && file.type(1) == 0xa9 && file.name(17) == "Проба")
        precondition(file.storedKey(16) == UniversalPrivacyFile.hex(UInt8(0)...63))
        precondition(file.storedKey(17) == UniversalPrivacyFile.hex(UInt8(0)...31))
        precondition(file.storedKey(255) == UniversalPrivacyFile.hex(UInt8(0)...4))
        try file.set(slot: 17, type: 4, name: "Проба", replacement: nil)
        precondition(file.data == bytes)

        try file.set(slot: 17, type: 4, name: "Я", replacement: nil)
        var expected = bytes
        expected.replaceSubrange(shortSlot + 4..<shortSlot + 36, with: [0x2f, 0x04] + [UInt8](repeating: 0, count: 30))
        precondition(expected == file.data)
        try file.set(slot: 17, type: 1, name: "Я", replacement: UniversalPrivacyFile.hex(UInt8(0)...4))
        expected[shortSlot] = 1
        expected.replaceSubrange(shortSlot + 36..<shortSlot + 68, with: [4, 3, 2, 1, 0] + [UInt8](repeating: 0, count: 27))
        precondition(expected == file.data)
        let original = file.data
        precondition(rejected { try file.set(slot: 17, type: 7, name: "", replacement: String(repeating: "AB", count: 64)) })
        precondition(rejected { try file.set(slot: 17, type: 3, name: "", replacement: "XX") })
        precondition(rejected { try file.set(slot: 17, type: 4, name: "", replacement: nil) })
        precondition(rejected { try file.set(slot: 17, type: 1, name: String(repeating: "Ж", count: 17), replacement: nil) })
        precondition(rejected { _ = try UniversalPrivacyFile(data: Data(count: UniversalPrivacyFile.size - 1)) })
        precondition(file.data == original)

        // Changing one known row preserves unknown algorithms, auxiliary bytes and trailing data.
        try file.set(slot: 255, type: 0, name: "", replacement: nil, clear: true)
        precondition(file.type(255) == 0 && file.name(255).isEmpty && file.storedKey(255) == nil)
        precondition(file.data[last + 1..<last + 4] == bytes[last + 1..<last + 4])
        precondition(file.data[last + 68..<0x5000] == bytes[last + 68..<0x5000])
        try LocalCopy.write(file.data, to: saved)
        let reopened = try UniversalPrivacyFile(url: saved)
        let originalOnDisk = try Data(contentsOf: factory)
        let attrs = try FileManager.default.attributesOfItem(atPath: saved.path)
        precondition(reopened.data == file.data && originalOnDisk == bytes)
        precondition((attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        precondition(rejected { try LocalCopy.write(reopened.data, to: factory) })

        var blank = UniversalPrivacyFile()
        for kind in UniversalPrivacyType.allCases where kind != .off {
            let generated = try kind.randomHex()
            try blank.set(slot: 1, type: kind.rawValue, name: kind.name.prefix(12).description, replacement: generated)
            precondition(blank.storedKey(1) == generated && generated.count == 2 * kind.byteCount)
        }
        // Independently specified file offsets. No-op edits preserve both kinds of empty sentinel.
        var rdt = Data(repeating: 0xab, count: 852533)
        rdt.replaceSubrange(0x125..<0x12d, with: "MD-UV390".utf8)
        rdt.replaceSubrange(0x5c75..<0x5c77, with: [0, 0])
        var legacy = try RDTKeyTable(rdt)
        try legacy.set("", bank: .basic, slot: 1)
        precondition(legacy.data == rdt)
        let edge = 0x5c75 + 15 * 2
        var desired = rdt
        desired.replaceSubrange(edge..<edge + 2, with: [0x34, 0x12])
        try legacy.set("1234", bank: .basic, slot: 16)
        precondition(legacy.data == desired && rejected { try legacy.set("FFFF", bank: .basic, slot: 1) })
        for bank in [RDTKeyBank.enhanced128, .enhanced256] {
            let input = (0..<bank.width).map { UInt8($0) }
            let offset = 0x5be5 + (bank.count - 1) * bank.width
            desired.replaceSubrange(offset..<offset + bank.width, with: input.reversed())
            try legacy.set(UniversalPrivacyFile.hex(input), bank: bank, slot: bank.count)
            precondition(legacy.data == desired)
        }
        let rdtCopy = root.appendingPathComponent("edited.rdt")
        try LocalCopy.write(legacy.data, to: rdtCopy)
        let readback = try RDTKeyTable(LocalCopy.read(rdtCopy, maximumSize: RDTKeyTable.fileSize))
        let basic = try readback.value(.basic, 16)
        precondition(readback.data == desired && basic == "1234")

        // USB sends the same byte order as these two RDT banks, without the RDT header or padding.
        let usbBasic = Data(desired[0x5c75..<0x5c95])
        let usbEnhanced = Data(desired[0x5be5..<0x5c65])
        func usbTable(_ basic: Data, _ enhanced: Data) throws -> USBPrivacyKeys {
            let frame = try JSONEncoder().encode(["basic": basic, "enhanced": enhanced])
            return try JSONDecoder().decode(USBPrivacyKeys.self, from: frame)
        }
        let usb = try usbTable(usbBasic, usbEnhanced)
        for bank in RDTKeyBank.allCases {
            for slot in 1...bank.count {
                let expectedValue = try readback.value(bank, slot)
                precondition(usb.value(bank, slot: slot) == expectedValue)
                precondition(usb.isAssigned(bank, slot: slot) == !expectedValue.isEmpty)
            }
        }
        let erased = try usbTable(Data(repeating: 255, count: 32), Data(repeating: 0, count: 128))
        precondition(!erased.isAssigned(.basic, slot: 16) && !erased.isAssigned(.enhanced256, slot: 4))
        precondition(usb.value(.basic, slot: 0).isEmpty && usb.value(.enhanced256, slot: 5).isEmpty)
        precondition(rejected { _ = try usbTable(usbBasic.dropLast(), usbEnhanced) })
        precondition(rejected { _ = try usbTable(usbBasic, usbEnhanced + Data([0])) })
        precondition(rejected {
            _ = try JSONDecoder().decode(USBPrivacyKeys.self, from: Data(#"{"basic":"!","enhanced":"!"}"#.utf8))
        })
        print("DEC and RDT local file checks passed")
    }
}
