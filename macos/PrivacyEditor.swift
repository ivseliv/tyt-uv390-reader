import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct PrivacyFileError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { L(message) }
}

enum UniversalPrivacyType: UInt8, CaseIterable {
    case off, arc4, des, aes128, aes256, enhance, proprietary, maes512

    var name: String {
        switch self {
        case .off: "Выкл."
        case .arc4: "ARC4"
        case .des: "DES"
        case .aes128: "AES-128"
        case .aes256: "AES-256"
        case .enhance: "Enhance"
        case .proprietary: "Proprietary algorithm"
        case .maes512: "MAES-512"
        }
    }

    static func displayName(for code: UInt8) -> String {
        guard let type = Self(rawValue: code) else { return LF("Код %ld", Int(code)) }
        return L(type.name)
    }

    var byteCount: Int {
        switch self {
        case .off: 0
        case .arc4: 5
        case .des: 8
        case .aes128: 16
        case .aes256: 32
        case .enhance: 9
        case .proprietary, .maes512: 64
        }
    }

    func randomHex() throws -> String {
        guard byteCount > 0 else { throw PrivacyFileError("Сначала выберите тип ключа.") }
        return try LocalHexKey.generate(bytes: byteCount)
    }
}

/// The V2.42 Open/Save Privacy dialogs serialize this fixed-size buffer directly.
/// The file has no magic. Slot 1 starts at byte 0. CPS displays slots 1 through 255.
struct UniversalPrivacyFile {
    static let size = 0x5000
    static let slots = 1...255
    private(set) var data: Data

    init() { data = Data(repeating: 0, count: Self.size) }

    init(data: Data) throws {
        guard data.count == Self.size else {
            throw PrivacyFileError("Для файла Universal Privacy CPS 2.42 ожидается 20 480 байт.")
        }
        self.data = Data(data)
    }

    init(url: URL) throws {
        try self.init(data: LocalCopy.read(url, maximumSize: Self.size))
    }

    private func offset(_ slot: Int) -> Int {
        precondition(Self.slots.contains(slot))
        return slot <= 16 ? (slot - 1) * 100 : 1600 + (slot - 17) * 68
    }

    static func capacity(_ slot: Int) -> Int { slot <= 16 ? 64 : 32 }

    func type(_ slot: Int) -> UInt8 { data[offset(slot)] }

    func name(_ slot: Int) -> String {
        let start = offset(slot) + 4
        var characters: [UInt16] = []
        for i in stride(from: start, to: start + 32, by: 2) {
            let unit = UInt16(data[i]) | UInt16(data[i + 1]) << 8
            if unit == 0 || unit == 0xffff { break }
            characters.append(unit)
        }
        return String(decoding: characters, as: UTF16.self)
    }

    func storedKey(_ slot: Int) -> String? {
        guard let kind = UniversalPrivacyType(rawValue: type(slot)), kind != .off,
              kind.byteCount <= Self.capacity(slot) else { return nil }
        let start = offset(slot) + 36
        return Self.hex(data[start..<start + kind.byteCount].reversed())
    }

    static func hex(_ bytes: some Sequence<UInt8>) -> String {
        bytes.map { String(format: "%02X", $0) }.joined()
    }

    /// A nil replacement keeps the opaque key storage byte-for-byte for a name-only edit.
    mutating func set(slot: Int, type newType: UInt8, name newName: String, replacement: String?, clear: Bool = false) throws {
        guard Self.slots.contains(slot) else { throw PrivacyFileError("Укажите позицию от 1 до 255.") }
        let base = offset(slot)
        let previous = type(slot)
        let rename = name(slot) != newName || clear
        if rename {
            guard newName.utf16.count <= 16,
                  !newName.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) || $0.value == 0xffff }) else {
                throw PrivacyFileError("Имя должно занимать до 16 кодовых единиц UTF-16 без управляющих символов.")
            }
        }

        var material: [UInt8]?
        if clear || (newType == 0 && newType != previous) {
            material = []
        } else if newType != previous || replacement != nil {
            guard let kind = UniversalPrivacyType(rawValue: newType), kind != .off,
                  kind.byteCount <= Self.capacity(slot) else {
                throw PrivacyFileError("Этот тип ключа не помещается в выбранную позицию DEC.")
            }
            guard let replacement else {
                throw PrivacyFileError("Для нового типа введите ключ или нажмите «Сгенерировать».")
            }
            material = try LocalHexKey.decode(replacement, bytes: kind.byteCount)
        }

        data[base] = clear ? 0 : newType
        if rename {
            var field = Data()
            for unit in (clear ? "" : newName).utf16 {
                field.append(UInt8(truncatingIfNeeded: unit))
                field.append(UInt8(unit >> 8))
            }
            field.append(Data(repeating: 0, count: 32 - field.count))
            data.replaceSubrange(base + 4..<base + 36, with: field)
        }
        if let material {
            let count = Self.capacity(slot)
            let bytes = Array(material.reversed()) + Array(repeating: UInt8(0), count: count - material.count)
            data.replaceSubrange(base + 36..<base + 36 + count, with: bytes)
        }
    }
}

struct PrivacyEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var bank = UniversalPrivacyFile()
    @State private var saved = UniversalPrivacyFile()
    @State private var slot = 1
    @State private var name = ""
    @State private var type: UInt8 = 0
    @State private var replacement = ""
    @State private var reveal = false
    @State private var search = ""
    @State private var filename: String?
    @State private var error: String?
    @State private var status: String?
    @State private var pending: NextAction?
    @State private var clearRequested = false
    @State private var discardRequested = false
    private enum NextAction { case new, open(URL), close }

    private var kind: UniversalPrivacyType? { UniversalPrivacyType(rawValue: type) }
    private var modifiedRow: Bool { type != bank.type(slot) || name != bank.name(slot) || !replacement.isEmpty }
    private var dirty: Bool { modifiedRow || bank.data != saved.data }

    private func rowName(_ index: Int) -> String {
        let value = bank.name(index)
        return value.isEmpty ? LF("Позиция %ld", index) : value
    }

    private func typeName(_ code: UInt8) -> String {
        UniversalPrivacyType.displayName(for: code)
    }

    private var visibleSlots: [Int] {
        UniversalPrivacyFile.slots.filter { index in
            search.isEmpty || String(index) == search || bank.name(index).localizedCaseInsensitiveContains(search)
                || typeName(bank.type(index)).localizedCaseInsensitiveContains(search)
        }
    }

    private func loadRow(_ index: Int) {
        slot = index
        name = bank.name(index)
        type = bank.type(index)
        replacement = ""
        reveal = false
        error = nil
    }

    private func applyRow() throws {
        guard modifiedRow else { return }
        try bank.set(slot: slot, type: type, name: name, replacement: replacement.isEmpty ? nil : replacement)
        replacement = ""
        reveal = false
    }

    private func request(_ next: NextAction) {
        if dirty { pending = next; discardRequested = true }
        else { perform(next) }
    }

    private func perform(_ next: NextAction) {
        do {
            switch next {
            case .new: bank = UniversalPrivacyFile(); filename = nil
            case .open(let url): bank = try UniversalPrivacyFile(url: url); filename = url.lastPathComponent
            case .close: dismiss(); return
            }
            saved = bank
            search = ""
            status = nil
            loadRow(1)
        } catch { self.error = error.localizedDescription }
    }

    private func open() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "dec") ?? .data]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        request(.open(url))
    }

    private func save() {
        do {
            try applyRow()
            let panel = NSSavePanel()
            panel.allowedContentTypes = [UTType(filenameExtension: "dec") ?? .data]
            panel.nameFieldStringValue = filename.map {
                LF("%@ — копия.dec", URL(fileURLWithPath: $0).deletingPathExtension().lastPathComponent)
            } ?? "Privacy.dec"
            panel.message = L("Выберите новое имя. Файл содержит ключи в открытом виде.")
            guard panel.runModal() == .OK, let url = panel.url else { return }
            try LocalCopy.write(bank.data, to: url)
            saved = bank
            filename = url.lastPathComponent
            status = LF("Сохранено: %@", url.lastPathComponent)
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Ключи Universal Privacy").font(.title2.weight(.semibold))
                    Text(dirty ? LF("%@ · изменено", filename ?? L("Новая таблица")) : (filename ?? L("Новая таблица"))).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Новая таблица") { request(.new) }
                Button("Открыть DEC…") { open() }
            }
            Divider()
            HSplitView {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Номер, имя или тип", text: $search)
                    List(selection: Binding<Int?>(get: { slot }, set: { index in
                        guard let index, slot != index else { return }
                        do { try applyRow(); loadRow(index) } catch { self.error = error.localizedDescription }
                    })) {
                        ForEach(visibleSlots, id: \.self) { index in
                            HStack {
                                Text(String(index)).monospacedDigit().foregroundStyle(.secondary).frame(minWidth: 26, alignment: .trailing)
                                Text(rowName(index)).lineLimit(1)
                                Spacer()
                                if bank.type(index) != 0 { Text(typeName(bank.type(index))).font(.caption) }
                            }.tag(index)
                        }
                    }
                }.padding(.trailing, 8).frame(minWidth: 190, idealWidth: 235, maxWidth: 305)
                Form {
                    LabeledContent("Слот", value: String(slot))
                    TextField("Имя", text: $name)
                    Picker("Тип", selection: Binding(get: { type }, set: {
                        type = $0; replacement = ""; reveal = false; error = nil
                    })) {
                        let options = UniversalPrivacyType.allCases.filter { $0.byteCount <= UniversalPrivacyFile.capacity(slot) }
                        if !options.contains(where: { $0.rawValue == type }) { Text(typeName(type)).tag(type) }
                        ForEach(options, id: \.rawValue) { option in
                            Text(L(option.name)).tag(option.rawValue)
                        }
                    }
                    if let kind, kind != .off, kind.byteCount <= UniversalPrivacyFile.capacity(slot) {
                        Section("Значение ключа") {
                            if reveal { TextField("Новый ключ HEX", text: $replacement).font(.system(.body, design: .monospaced)) }
                            else { SecureField("Новый ключ HEX", text: $replacement) }
                            Text(LF("%ld шестнадцатеричных знаков", kind.byteCount * 2)).font(.caption).foregroundStyle(.secondary)
                            Toggle("Показать ключ", isOn: $reveal)
                            if reveal, replacement.isEmpty, type == bank.type(slot), let current = bank.storedKey(slot) {
                                Text(current).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                            }
                            Button("Сгенерировать") {
                                do { replacement = try kind.randomHex(); error = nil }
                                catch { self.error = error.localizedDescription }
                            }
                        }
                    }
                    Button("Очистить позицию") { clearRequested = true }
                        .disabled(bank.type(slot) == 0 && bank.name(slot).isEmpty && !modifiedRow)
                }.formStyle(.grouped).privacySensitive().frame(minWidth: 325)
            }
            if let error { Text(error).foregroundStyle(.red) }
            else if let status, !dirty { Text(status).font(.callout).foregroundStyle(.secondary) }
            HStack {
                Button("Закрыть") { request(.close) }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Сохранить копию DEC…") { save() }.buttonStyle(.borderedProminent)
            }
        }
        .padding(18).frame(minWidth: 640, idealWidth: 770, minHeight: 450, idealHeight: 540)
        .interactiveDismissDisabled(dirty)
        .confirmationDialog("Отбросить несохранённые изменения таблицы?", isPresented: $discardRequested) {
            Button("Отбросить изменения", role: .destructive) { if let pending { perform(pending) }; pending = nil }
            Button("Продолжить редактирование", role: .cancel) { pending = nil }
        }
        .confirmationDialog(LF("Очистить позицию %ld в локальной таблице?", slot), isPresented: $clearRequested) {
            Button("Очистить", role: .destructive) {
                do {
                    try bank.set(slot: slot, type: 0, name: "", replacement: nil, clear: true)
                    loadRow(slot)
                } catch { self.error = error.localizedDescription }
            }
            Button("Отмена", role: .cancel) {}
        }
    }
}
