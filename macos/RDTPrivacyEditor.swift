import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum RDTKeyBank: String, CaseIterable {
    case basic = "Basic"
    case enhanced128 = "Enhanced · 128 бит"
    case enhanced256 = "Enhanced · 256 бит"

    var width: Int { self == .basic ? 2 : self == .enhanced128 ? 16 : 32 }
    var count: Int { self == .basic ? 16 : 128 / width }

    fileprivate func range(_ slot: Int, base: Int) throws -> Range<Int> {
        guard (1...count).contains(slot) else { throw LocalCopy.Failure("Нет такой позиции ключа.") }
        let start = base + (slot - 1) * width
        return start..<start + width
    }
}

private func isAssignedKey(_ bytes: Data) -> Bool {
    !bytes.allSatisfy { $0 == 0 } && !bytes.allSatisfy { $0 == 255 }
}

private func displayKey(_ bytes: Data) -> String {
    isAssignedKey(bytes) ? bytes.reversed().map { String(format: "%02X", $0) }.joined() : ""
}

struct RDTKeyTable {
    static let fileSize = 852533
    private(set) var data: Data

    init(_ data: Data) throws {
        guard data.count == Self.fileSize,
              ["MD-UV380", "MD-UV390"].contains(String(decoding: data[0x125..<0x12d], as: UTF8.self)) else {
            throw LocalCopy.Failure("Выберите штатный RDT для MD-UV380 или MD-UV390.")
        }
        self.data = data
    }

    private func range(_ bank: RDTKeyBank, _ slot: Int) throws -> Range<Int> {
        // The first RDT segment has a 0x225 header displacement. Basic starts 0x90 bytes after Enhanced.
        try bank.range(slot, base: 0x59c0 + 0x225 + (bank == .basic ? 0x90 : 0))
    }

    func value(_ bank: RDTKeyBank, _ slot: Int) throws -> String {
        displayKey(data[try range(bank, slot)])
    }

    mutating func set(_ text: String, bank: RDTKeyBank, slot: Int) throws {
        let bounds = try range(bank, slot)
        if text.isEmpty {
            if try value(bank, slot).isEmpty { return }
            data.replaceSubrange(bounds, with: repeatElement(UInt8(255), count: bank.width))
        } else {
            let bytes = try LocalHexKey.decode(text, bytes: bank.width)
            guard !(bytes.allSatisfy { $0 == 0 } || bytes.allSatisfy { $0 == 255 }) else {
                throw LocalCopy.Failure("В этой таблице TYT значения из одних 00 или FF обозначают свободную позицию.")
            }
            data.replaceSubrange(bounds, with: bytes.reversed())
        }
    }
}

/// In-memory banks supplied by the current USB read. Only the session response decodes this type.
struct USBPrivacyKeys: Decodable, Sendable {
    private let basic: Data
    private let enhanced: Data
    private enum CodingKeys: String, CodingKey { case basic, enhanced }

    init(from decoder: Decoder) throws {
        let banks = try decoder.container(keyedBy: CodingKeys.self)
        basic = try banks.decode(Data.self, forKey: .basic)
        enhanced = try banks.decode(Data.self, forKey: .enhanced)
        guard basic.count == 32, enhanced.count == 128 else {
            throw LocalCopy.Failure("Получена неполная таблица ключей Privacy.")
        }
    }

    private func bytes(in bank: RDTKeyBank, slot: Int) -> Data? {
        let data = bank == .basic ? basic : enhanced
        guard let range = try? bank.range(slot, base: data.startIndex) else { return nil }
        return data[range]
    }

    func isAssigned(_ bank: RDTKeyBank, slot: Int) -> Bool {
        bytes(in: bank, slot: slot).map(isAssignedKey) ?? false
    }

    func value(_ bank: RDTKeyBank, slot: Int) -> String {
        bytes(in: bank, slot: slot).map(displayKey) ?? ""
    }
}

struct USBPrivacyViewer: View {
    let keys: USBPrivacyKeys
    @Environment(\.dismiss) private var dismiss
    @State private var bank = RDTKeyBank.basic
    @State private var slot = 1
    @State private var reveal = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Ключи Privacy из рации").font(.title2.bold())
            Picker("Таблица", selection: Binding(get: { bank }, set: { next in
                bank = next
                slot = 1
                reveal = false
            })) {
                ForEach(RDTKeyBank.allCases, id: \.self) { Text(L($0.rawValue)).tag($0) }
            }.pickerStyle(.segmented)
            if bank != .basic {
                Text("Обе раскладки показывают одну таблицу Enhanced. В штатной CPS 2.42 используется 128 бит, при specialprivacy=1 — 256 бит.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 16) {
                List(selection: Binding<Int?>(get: { slot }, set: { next in
                    guard let next, next != slot else { return }
                    slot = next
                    reveal = false
                })) {
                    ForEach(1...bank.count, id: \.self) { number in
                        HStack {
                            Text(LF("№ %ld", number))
                            Spacer()
                            Text(L(keys.isAssigned(bank, slot: number) ? "Задан" : "Свободен"))
                                .foregroundStyle(.secondary)
                        }.tag(number)
                    }
                }.frame(width: 170)
                VStack(alignment: .leading, spacing: 12) {
                    Text(LF("Позиция %ld", slot)).font(.headline)
                    if keys.isAssigned(bank, slot: slot) {
                        Text(LF("Ключ HEX · %ld символов", bank.width * 2)).font(.subheadline)
                        if reveal {
                            Text(verbatim: keys.value(bank, slot: slot))
                                .font(.system(.body, design: .monospaced)).textSelection(.enabled)
                        } else {
                            Text("•••• ••••").accessibilityLabel("Значение скрыто")
                        }
                        Toggle("Показать значение", isOn: $reveal)
                    } else {
                        Text("Позиция свободна").foregroundStyle(.secondary)
                    }
                }.privacySensitive().frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }.padding(20).frame(minWidth: 600, idealWidth: 660, minHeight: 370, idealHeight: 460)
    }
}

struct RDTPrivacyEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var original: Data?
    @State private var working: RDTKeyTable?
    @State private var source: URL?
    @State private var bank = RDTKeyBank.basic
    @State private var slot = 1
    @State private var input = ""
    @State private var reveal = false
    @State private var status = ""
    @State private var problem = ""
    @State private var discardToOpen = false
    @State private var confirmDiscard = false

    private var savedValue: String { (try? working?.value(bank, slot)) ?? "" }
    private var fieldChanged: Bool { input.uppercased() != savedValue }
    private var dirty: Bool { fieldChanged || working?.data != original }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Ключи Basic и Enhanced").font(.title2.bold())
                Spacer()
                Button("Открыть RDT…") { requestOpen() }
            }
            Text(source?.lastPathComponent ?? L("Откройте локальный кодплаг MD-UV380 или MD-UV390."))
                .foregroundStyle(.secondary)
            if working != nil {
                Picker("Таблица", selection: Binding(get: { bank }, set: { newBank in
                    bank = newBank; slot = 1; reloadField()
                })) {
                    ForEach(RDTKeyBank.allCases, id: \.self) { Text(L($0.rawValue)).tag($0) }
                }.pickerStyle(.segmented).disabled(fieldChanged)
                if bank != .basic {
                    Text("Штатная CPS 2.42 использует 8 ключей по 128 бит. Настройке CPS specialprivacy=1 соответствуют 4 ключа по 256 бит в той же области RDT.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                HStack(alignment: .top, spacing: 16) {
                    List(selection: Binding<Int?>(get: { slot }, set: { if let n = $0 { slot = n; reloadField() } })) {
                        ForEach(1...bank.count, id: \.self) { n in
                            HStack {
                                Text(LF("№ %ld", n))
                                Spacer()
                                Text(L((try? working?.value(bank, n)) == "" ? "Свободен" : "Задан"))
                                    .foregroundStyle(.secondary)
                            }.tag(n)
                        }
                    }.disabled(fieldChanged).frame(width: 170)
                    VStack(alignment: .leading, spacing: 10) {
                        Text(LF("Позиция %ld", slot)).font(.headline)
                        Text(LF("Ключ HEX · %ld символов", bank.width * 2)).font(.subheadline)
                        if reveal {
                            TextField("Ключ HEX", text: $input).font(.system(.body, design: .monospaced))
                        } else {
                            SecureField("Ключ HEX", text: $input)
                        }
                        Toggle("Показать значение", isOn: $reveal)
                        HStack {
                            Button("Сгенерировать") {
                                do {
                                    var key: String
                                    repeat { key = try LocalHexKey.generate(bytes: bank.width) }
                                    while key.allSatisfy({ $0 == "0" }) || key.allSatisfy({ $0 == "F" })
                                    input = key
                                    problem = ""; status = ""
                                } catch { problem = error.localizedDescription }
                            }
                            Button("Очистить") { input = "" }.disabled(input.isEmpty)
                        }
                        if fieldChanged {
                            HStack {
                                Button("Применить к копии") { applyField() }
                                Button("Отменить правку") { reloadField() }
                            }
                        }
                        Spacer()
                    }.privacySensitive().frame(maxWidth: .infinity, alignment: .leading)
                }
            } else { Spacer() }
            if !problem.isEmpty { Text(problem).foregroundStyle(.red) }
            if !status.isEmpty { Text(status).foregroundStyle(.secondary) }
            HStack {
                Button("Закрыть") {
                    if dirty { discardToOpen = false; confirmDiscard = true } else { dismiss() }
                }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Сохранить копию RDT…") { save() }.disabled(working == nil)
            }
        }
        .padding(20).frame(minWidth: 630, idealWidth: 700, minHeight: 370, idealHeight: 480)
        .interactiveDismissDisabled(dirty)
        .confirmationDialog("Отбросить локальные правки?", isPresented: $confirmDiscard) {
            Button("Отбросить изменения", role: .destructive) { if discardToOpen { open() } else { dismiss() } }
            Button("Продолжить редактирование", role: .cancel) {}
        }
    }

    private func requestOpen() {
        if dirty { discardToOpen = true; confirmDiscard = true } else { open() }
    }

    private func open() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "rdt") ?? .data]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let table = try RDTKeyTable(LocalCopy.read(url, maximumSize: RDTKeyTable.fileSize))
            original = table.data; working = table; source = url
            bank = .basic; slot = 1; reloadField()
        } catch { problem = error.localizedDescription }
    }

    private func reloadField() {
        input = savedValue; reveal = false; problem = ""; status = ""
    }

    @discardableResult private func applyField() -> Bool {
        do {
            if fieldChanged { try working?.set(input, bank: bank, slot: slot) }
            reloadField()
            return true
        } catch { problem = error.localizedDescription; return false }
    }

    private func save() {
        guard applyField(), let working else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "rdt") ?? .data]
        panel.nameFieldStringValue = (source?.deletingPathExtension().lastPathComponent ?? "TYT") + " — Privacy.rdt"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try LocalCopy.write(working.data, to: url)
            original = working.data; source = url; status = LF("Сохранено: %@", url.lastPathComponent)
        } catch { problem = error.localizedDescription }
    }
}
