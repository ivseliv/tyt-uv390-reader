import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct UniversalUSBReport: Sendable {
    enum ValueStatus: UInt8, Sendable {
        case notApplicable, zeros, ones, dataPresent

        var summary: String {
            switch self {
            case .notApplicable: L("Нет оценки")
            case .zeros: L("Нули · неизвестно")
            case .ones: L("FF · неизвестно")
            case .dataPresent: L("Есть данные")
            }
        }

        var reportCode: String {
            switch self {
            case .notApplicable: "notApplicable"
            case .zeros: "zeros"
            case .ones: "ones"
            case .dataPresent: "dataPresent"
            }
        }

        var label: String {
            switch self {
            case .notApplicable: L("Для этого типа нет доступного значения.")
            case .zeros: L("Рация вернула нули. Определить исходный ключ по этому ответу нельзя.")
            case .ones: L("Рация вернула только байты FF. Определить исходный ключ по этому ответу нельзя.")
            case .dataPresent: L("В поле ключа есть данные. В этом окне доступны сведения о позиции.")
            }
        }
    }

    enum LocalValue: Equatable {
        case mismatch, unsupported, allZero, allFF, hex(String)
    }

    struct Slot: Identifiable, Sendable {
        let id: Int
        let name: String
        let type: UInt8
        let status: ValueStatus

        var typeName: String { UniversalPrivacyType(rawValue: type)?.name ?? "Код \(type)" }
        var displayTypeName: String { UniversalPrivacyType.displayName(for: type) }
        var isEnabled: Bool { type != 0 && type != 0xff }

        func localKey(in file: UniversalPrivacyFile) -> LocalValue {
            guard file.type(id) == type, !name.contains("\u{fffd}"),
                  file.name(id).utf16.elementsEqual(name.utf16) else { return .mismatch }
            guard let hex = file.storedKey(id) else { return .unsupported }
            if hex.allSatisfy({ $0 == "0" }) { return .allZero }
            if hex.allSatisfy({ $0 == "F" }) { return .allFF }
            return .hex(hex)
        }
    }

    let model: String
    let slots: [Slot]

    enum SlotFilter { case active, firstFour, all }

    func slots(in filter: SlotFilter) -> [Slot] {
        switch filter {
        case .active: slots.filter(\.isEnabled)
        case .firstFour: slots.filter { (1...4).contains($0.id) }
        case .all: slots
        }
    }

    func statusJSON(in filter: SlotFilter) throws -> Data {
        let rows: [[String: Any]] = slots(in: filter).map { slot in
            ["slot": slot.id, "name": slot.name, "type": Int(slot.type),
             "typeName": slot.typeName, "usbStatus": slot.status.reportCode]
        }
        return try JSONSerialization.data(withJSONObject: ["model": model, "slots": rows],
                                          options: [.prettyPrinted, .sortedKeys])
    }

    init(data: Data) throws {
        let recordSize = 34
        guard data.count == 8 + UniversalPrivacyFile.slots.count * recordSize,
              let model = String(data: data.prefix(8), encoding: .ascii),
              ["MD-UV380", "MD-UV390"].contains(model) else {
            throw PrivacyFileError("Ответ USB не соответствует таблице Universal Privacy для MD-UV380/390.")
        }
        self.model = model
        slots = try UniversalPrivacyFile.slots.map { slot in
            let offset = data.startIndex + 8 + (slot - 1) * recordSize
            var units: [UInt16] = []
            for start in stride(from: offset + 1, to: offset + 33, by: 2) {
                let unit = UInt16(data[start]) | UInt16(data[start + 1]) << 8
                if unit == 0 || unit == 0xffff { break }
                units.append(unit)
            }
            guard let status = ValueStatus(rawValue: data[offset + 33]) else {
                throw PrivacyFileError(LF("Неизвестный статус значения USB в позиции %ld.", slot))
            }
            return Slot(id: slot, name: String(decoding: units, as: UTF16.self), type: data[offset], status: status)
        }
    }
}

struct UniversalUSBViewer: View {
    let report: UniversalUSBReport
    @Environment(\.dismiss) private var dismiss
    @State private var filter: UniversalUSBReport.SlotFilter
    @State private var selected: Int?
    @State private var localFile: UniversalPrivacyFile?
    @State private var filename = ""
    @State private var localValue: UniversalUSBReport.LocalValue?
    @State private var error: String?

    init(report: UniversalUSBReport) {
        self.report = report
        let firstActive = report.slots.first(where: \.isEnabled)
        _filter = State(initialValue: firstActive != nil ? .active : .all)
        _selected = State(initialValue: (firstActive ?? report.slots.first)?.id)
    }

    private var visible: [UniversalUSBReport.Slot] {
        report.slots(in: filter)
    }

    private var selection: Binding<Int?> {
        Binding(get: { selected }, set: { next in
            if next != selected {
                localValue = nil
                selected = next
            }
        })
    }

    private func openDEC() {
        localValue = nil
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "dec") ?? .data]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            localFile = try UniversalPrivacyFile(url: url)
            filename = url.lastPathComponent
            error = nil
        } catch {
            localFile = nil
            filename = ""
            self.error = error.localizedDescription
        }
    }

    private func saveStatusReport() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = "TYT-Universal-Privacy-status.json"
        panel.message = LF("Отчёт содержит имена, типы и статусы видимых слотов (%ld).", visible.count)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try LocalCopy.write(report.statusJSON(in: filter), to: url)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Universal Privacy").font(.title2.bold())
                Text(report.model + " · USB").foregroundStyle(.secondary)
                Spacer()
                Picker("Позиции", selection: Binding(get: { filter }, set: { next in
                    filter = next
                    if !visible.contains(where: { $0.id == selected }) {
                        selection.wrappedValue = visible.first?.id
                    }
                })) {
                    Text("Активные").tag(UniversalUSBReport.SlotFilter.active)
                    Text("Слоты 1–4").tag(UniversalUSBReport.SlotFilter.firstFour)
                    Text("Все").tag(UniversalUSBReport.SlotFilter.all)
                }.pickerStyle(.segmented).labelsHidden().fixedSize()
            }
            Table(visible, selection: selection) {
                TableColumn("Слот") { row in Text(String(row.id)).monospacedDigit() }.width(55)
                TableColumn("Имя") { row in Text(verbatim: row.name.isEmpty ? "—" : row.name) }
                TableColumn("Тип", value: \.displayTypeName)
                TableColumn("Ответ USB") { row in Text(row.status.summary).help(row.status.label) }
            }.accessibilityLabel("Позиции Universal Privacy")
            if let selected, let slot = visible.first(where: { $0.id == selected }) {
                Text(LF("Слот %ld · %@", slot.id, slot.displayTypeName)).font(.headline)
                Text(slot.status.label).foregroundStyle(.secondary)
                if let localFile {
                    HStack {
                        Text(verbatim: filename).lineLimit(1).truncationMode(.middle)
                        if localValue == nil {
                            Button("Посмотреть значение из DEC") { localValue = slot.localKey(in: localFile) }
                        } else {
                            Button("Скрыть") { localValue = nil }
                        }
                    }
                    if let localValue { localDetail(localValue) }
                }
            }
            if let error { Text(error).foregroundStyle(.red) }
            HStack {
                Button("Открыть свой DEC…", action: openDEC)
                Button("Сохранить отчёт…", action: saveStatusReport).disabled(visible.isEmpty)
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .privacySensitive()
        .padding(18).frame(minWidth: 620, idealWidth: 700, minHeight: 445, idealHeight: 530)
    }

    @ViewBuilder private func localDetail(_ value: UniversalUSBReport.LocalValue) -> some View {
        switch value {
        case .mismatch:
            Text("Имя или тип в этой позиции DEC отличается от данных рации.")
        case .unsupported:
            Text("Для этой позиции в DEC нет значения поддерживаемого типа.")
        case .allZero:
            Text("В файле DEC поле заполнено нулями. Такой файл мог быть сохранён после чтения со скрытыми ключами.")
        case .allFF:
            Text("В файле DEC поле заполнено байтами FF. Значение ключа в рации неизвестно.")
        case .hex(let key):
            Text("Значение из файла DEC").font(.subheadline.bold())
            Text(verbatim: key).font(.system(.body, design: .monospaced)).textSelection(.enabled)
            Text("Совпали слот, имя и тип. Совпадение самих ключей не проверено.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}
