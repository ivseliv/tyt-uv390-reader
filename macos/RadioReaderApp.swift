import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum LocalService: String, Identifiable {
    case usbPrivacy, universal, legacy, calibration
    var id: Self { self }
}

struct RadioSettings: Decodable, Sendable {
    let usbModel: String?
    let fileModel: String?
    let dmrId: Int?
    let channels: [Channel]
    let zones: [Zone]
    let contacts: [Contact]
    let rxGroups: [ReceiveGroup]?
    let scanLists: [ScanList]?
    let cpsSections: [CPSSection]?
    let readNotes: [String]?
    let unrecognizedRxGroupSlots: [Int]?

    struct Channel: Decodable, Identifiable, Sendable {
        let index: Int
        let name: String
        let mode: String
        let rxHz: Int?
        let txHz: Int?
        let bandwidthCode: Int?
        let colorCode: Int?
        let timeSlot: Int?
        let contactIndex: Int?
        let rxGroupIndex: Int?
        let scanListIndex: Int?
        let privacyTypeCode: Int?
        let privacyIndexCode: Int?
        let rxToneCode: Int?
        let txToneCode: Int?
        let details: [CPSField]?

        var id: Int { index }
        var modeLabel: String {
            switch mode {
            case "analog": L("Аналог")
            case "DMR": "DMR"
            case "AM": "AM"
            case "M17": "M17"
            default: L("Неизвестен")
            }
        }
        var bandwidth: String { bandwidthCode.flatMap { [0: (12.5).formatted(.number), 1: "20", 2: "25"][$0] } ?? "—" }
    }

    struct Zone: Decodable, Identifiable, Sendable {
        let index: Int
        let name: String
        let sideA: [Member]
        let sideB: [Member]
        var id: Int { index }

        enum CodingKeys: String, CodingKey {
            case index, name
            case sideA = "A"
            case sideB = "B"
        }

        struct Member: Decodable, Sendable {
            let index: Int
            let name: String?
        }
    }

    struct Contact: Decodable, Identifiable, Sendable {
        let index: Int
        let name: String
        let dmrId: Int?
        let dtmfNumber: String?
        let callsign: String?
        let ring: Bool?
        let type: String
        var id: Int { index }
        var address: String {
            switch type {
            case "dtmf": dtmfNumber ?? "—"
            case "m17": callsign ?? "—"
            default: dmrId.map(String.init) ?? "—"
            }
        }
        var typeLabel: String {
            switch type {
            case "group": L("Групповой")
            case "private": L("Индивидуальный")
            case "all": L("Общий вызов")
            case "dtmf": "DTMF"
            case "m17": "M17"
            default: L("Неизвестен")
            }
        }
    }

    struct ReceiveGroup: Decodable, Identifiable, Sendable {
        let index: Int
        let name: String
        let contacts: [Int]
        var id: Int { index }
    }

    struct ScanList: Decodable, Identifiable, Sendable {
        let index: Int
        let name: String
        let channels: [Int]
        let primary: Int?
        let secondary: Int?
        let revert: Int?
        var id: Int { index }
    }
}

struct SettingsFile: Sendable {
    let data: Data
    let settings: RadioSettings
    static let openTypes = [UTType.json] + ["rdt", "yaml", "yml", "csv"].compactMap { UTType(filenameExtension: $0) }

    var writeModel: String? {
        let models = Set([settings.usbModel, settings.fileModel].compactMap { $0 })
        guard models.count == 1, let model = models.first, ["MD-UV380", "MD-UV390"].contains(model),
              let id = settings.dmrId, (0...0xFFFFFF).contains(id),
              settings.rxGroups != nil, settings.scanLists != nil else { return nil }
        return model
    }

    init(data: Data) throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        settings = try decoder.decode(RadioSettings.self, from: data)
        self.data = data
    }

    static func open(_ url: URL) throws -> Self {
        let granted = url.startAccessingSecurityScopedResource()
        defer { if granted { url.stopAccessingSecurityScopedResource() } }
        switch url.pathExtension.lowercased() {
        case "json":
            let data = try Data(contentsOf: url)
            let file = try Self(data: data)
            guard ["MD-UV380", "MD-UV390"].contains(file.settings.usbModel ?? file.settings.fileModel ?? "") else { return file }
            let invalid = Set((file.settings.rxGroups ?? []).filter { group in
                group.contacts.contains { $0 < 1 || $0 > 10000 } || group.name.contains("\u{fffd}")
                || group.name.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
            }.map(\.index))
            guard !invalid.isEmpty, var object = try JSONSerialization.jsonObject(with: data) as? [String:Any],
                  let groups = object["rx_groups"] as? [[String:Any]] else { return file }
            object["rx_groups"] = groups.filter { !invalid.contains($0["index"] as? Int ?? 0) }
            let slots = invalid.sorted().map(String.init).joined(separator: ", ")
            object["unrecognized_rx_group_slots"] = invalid.union(file.settings.unrecognizedRxGroupSlots ?? []).sorted()
            object["read_notes"] = (file.settings.readNotes ?? []) + ["В позициях групп приёма \(slots) старого снимка данные не соответствуют формату TYT. Эти записи пропущены при открытии копии."]
            return try Self(data: JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]))
        case "rdt", "yaml", "yml", "csv":
            return try RadioReader.importFile(url)
        default:
            throw RadioReader.Failure("Выберите RDT, YAML qdmr, JSON TYT Reader или таблицу CSV.")
        }
    }

    func save(to url: URL) throws { try Self.saveData(data, to:url) }

    static func saveData(_ data: Data, to url: URL) throws {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let manager = FileManager.default
        let staging = try manager.url(for: .itemReplacementDirectory, in: .userDomainMask,
                                      appropriateFor: url.deletingLastPathComponent(), create: true)
        defer { try? manager.removeItem(at: staging) }
        let temporary = staging.appendingPathComponent(url.lastPathComponent)
        guard manager.createFile(atPath: temporary.path, contents: data,
                                 attributes: [.posixPermissions: 0o600]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        if manager.fileExists(atPath: url.path) {
            _ = try manager.replaceItemAt(url, withItemAt: temporary, options: .usingNewMetadataOnly)
        } else {
            try manager.moveItem(at: temporary, to: url)
        }
    }
}

enum RadioReader {
    struct Failure: LocalizedError {
        let errorDescription: String?

        init(_ message: String) { errorDescription = LD(message) }
    }

    struct USBReadResult: Sendable {
        let file: SettingsFile
        let keys: USBPrivacyKeys?

        init(data: Data) throws {
            guard let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let settings = envelope["settings"] as? [String: Any] else {
                throw Failure("Компонент чтения вернул неполный ответ USB.")
            }
            // Only the settings object can reach the document, exports, or print views.
            file = try SettingsFile(data: JSONSerialization.data(withJSONObject: settings, options: [.prettyPrinted, .sortedKeys]))
            if let bank = envelope["usb_privacy"], !(bank is NSNull) {
                guard let object = bank as? [String: Any] else {
                    throw Failure("Компонент чтения вернул неверный формат таблицы Privacy.")
                }
                keys = try JSONDecoder().decode(USBPrivacyKeys.self, from: JSONSerialization.data(withJSONObject: object))
            } else {
                keys = nil
            }
        }
    }

    private static func usbHelper() throws -> URL {
        guard let helper = Bundle.main.url(forAuxiliaryExecutable: "read_usb") else {
            throw Failure("В приложении отсутствует компонент чтения. Пересоберите приложение.")
        }
        return helper
    }

    private static func checkUSB(_ status: Int32, log: String) throws {
        if status == 0 { return }
        if log.contains("USB 0483:df11 not found") {
            throw Failure("Рация не обнаружена. Проверьте USB-кабель и питание рации.")
        }
        if log.contains("Found multiple USB") {
            throw Failure("Подключите только одну рацию и повторите чтение.")
        }
        let detail = log.split(whereSeparator: \.isNewline).first { !$0.contains("identified as") }
        throw Failure(detail.map { LF("Ошибка чтения USB. %@", String($0)) } ?? "Считать данные по USB не удалось.")
    }

    static func read() throws -> USBReadResult {
        let helper = try usbHelper()
        let (data, log, status) = try run(scriptName: "read_settings", arguments: ["--reader", helper.path, "--include-privacy", "-"])
        try checkUSB(status, log: log)
        return try USBReadResult(data: data)
    }

    static func readUniversalPrivacy() throws -> UniversalUSBReport {
        let (data, log, status) = try run(executable: usbHelper(), arguments: ["--universal-privacy"])
        try checkUSB(status, log: log)
        return try UniversalUSBReport(data: data)
    }

    private static func writeFailure(_ title: String, diagnostics: String) -> Failure {
        let lines = diagnostics.split(whereSeparator: \.isNewline).map { LD(String($0)) }
        return Failure(([L(title)] + lines).joined(separator: "\n"))
    }

    static func prepareWrite(file: SettingsFile) throws -> RadioWriteReview {
        guard let model = file.writeModel else { throw Failure("Для записи откройте штатный RDT или полный снимок TYT с моделью и DMR ID.") }
        let (data, log, status) = try run(scriptName: "write_settings", arguments: ["prepare", "--reader", try usbHelper().path], input: file.data)
        guard status == 0 else { throw writeFailure("Не удалось сравнить настройки с рацией.", diagnostics: log) }
        return try RadioWriteReview(data, expectedModel: model, expectedDMRID: file.settings.dmrId)
    }

    static func prepareRestore(backup: URL) throws -> RadioWriteReview {
        let access = backup.startAccessingSecurityScopedResource()
        defer { if access { backup.stopAccessingSecurityScopedResource() } }
        let (data, log, status) = try run(scriptName: "write_settings",
                                          arguments: ["restore-preview", "--backup", backup.path, "--reader", try usbHelper().path])
        guard status == 0 else { throw writeFailure("Не удалось сравнить резервную копию с рацией.", diagnostics: log) }
        return try RadioWriteReview(data)
    }

    static func writeVerified(_ plan: Data, backup: URL) throws {
        let access = backup.startAccessingSecurityScopedResource()
        defer { if access { backup.stopAccessingSecurityScopedResource() } }
        let (_, log, status) = try run(scriptName: "write_settings",
                                      arguments: ["apply", "--reader", try usbHelper().path, "--backup", backup.path],
                                      input: plan)
        guard status == 0 else {
            let detail = log.isEmpty ? "Запись не подтверждена. Заново считайте настройки рации." : log
            throw writeFailure("Операция записи завершилась с ошибкой.", diagnostics: detail)
        }
    }

    static func importFile(_ url: URL) throws -> SettingsFile {
        let csv = url.pathExtension.lowercased() == "csv"
        let (data, log, status) = try run(scriptName: csv ? "cps_csv" : "import_settings",
                                          arguments: csv ? ["import", url.path] : [url.path])
        guard status == 0 else {
            throw Failure(log.isEmpty ? "Файл не удалось импортировать." : log)
        }
        return try SettingsFile(data: data)
    }

    static func exportRDT(source: URL, destination: URL, baseline: SettingsFile, edited: SettingsFile) throws {
        let from = source.startAccessingSecurityScopedResource(), to = destination.startAccessingSecurityScopedResource()
        defer {
            if from { source.stopAccessingSecurityScopedResource() }
            if to { destination.stopAccessingSecurityScopedResource() }
        }
        let input = try editRequest(baseline: baseline, edited: edited)
        let (_, log, status) = try run(scriptName: "rdt_editor", arguments: [source.path, destination.path], input: input)
        guard status == 0 else { throw Failure(log.isEmpty ? "Не удалось сохранить копию RDT." : log) }
    }

    static func exportFromTemplate(source: URL, destination: URL, snapshot: SettingsFile) throws {
        let from = source.startAccessingSecurityScopedResource(), to = destination.startAccessingSecurityScopedResource()
        defer {
            if from { source.stopAccessingSecurityScopedResource() }
            if to { destination.stopAccessingSecurityScopedResource() }
        }
        let (_, log, status) = try run(scriptName:"rdt_template", arguments:[source.path,destination.path], input:snapshot.data)
        guard status == 0 else { throw Failure(log.isEmpty ? "Не удалось перенести таблицы в копию шаблона." : log) }
    }

    static func editRequest(baseline: SettingsFile, edited: SettingsFile) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["baseline": JSONSerialization.jsonObject(with: baseline.data),
                                                     "edited": JSONSerialization.jsonObject(with: edited.data)])
    }

    static func normalized(baseline: SettingsFile, edited: SettingsFile) throws -> SettingsFile {
        let input = try editRequest(baseline: baseline, edited: edited)
        let (data, log, status) = try run(scriptName: "normalize_snapshot", arguments: [], input: input)
        guard status == 0 else { throw Failure(log.isEmpty ? "Проверьте значения настроек." : log) }
        return try SettingsFile(data: data)
    }

    static func template(_ url: URL) throws -> SettingsFile {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let (data, log, status) = try run(scriptName: "rdt_editor", arguments:["--new-from-template",url.path])
        guard status == 0 else { throw Failure(log.isEmpty ? "Не удалось открыть шаблон." : log) }
        return try SettingsFile(data:data)
    }

    static func mergeCSV(_ url: URL, file: SettingsFile) throws -> SettingsFile {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let (data, log, status) = try run(scriptName:"cps_csv", arguments:["merge",url.path], input:file.data)
        guard status == 0 else { throw Failure(log.isEmpty ? "Не удалось добавить таблицу CSV." : log) }
        return try SettingsFile(data:data)
    }

    enum ExportFormat {
        case channels, contacts, yaml

        var suffix: String {
            switch self {
            case .channels: L(" — каналы.csv")
            case .contacts: L(" — контакты.csv")
            case .yaml: " — qdmr.yaml"
            }
        }
        var contentType: UTType {
            self == .yaml ? (UTType(filenameExtension: "yaml") ?? .text) : .commaSeparatedText
        }
    }

    static func export(_ format: ExportFormat, file: SettingsFile, to url: URL) throws {
        let (data, log, status) = try run(
            scriptName: format == .yaml ? "export_qdmr" : "cps_csv",
            arguments: format == .yaml ? [] : ["export", format == .channels ? "channels" : "contacts"],
            input: file.data)
        guard status == 0 else { throw Failure(log.isEmpty ? "Не удалось подготовить файл экспорта." : log) }
        try SettingsFile.saveData(data, to:url)
    }

    private static func run(scriptName: String, arguments: [String], input: Data? = nil) throws -> (Data, String, Int32) {
        guard let script = Bundle.main.url(forResource: scriptName, withExtension: "py") else {
            throw Failure(LF("Компонент %@ отсутствует. Пересоберите приложение.", scriptName))
        }
        let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
        guard let python else { throw Failure("Для чтения настроек требуется Python 3.") }
        return try run(executable: URL(fileURLWithPath: python),
                       arguments: ["-E", "-S", "-B", script.path] + arguments, input: input)
    }

    private static func run(executable: URL, arguments: [String], input: Data? = nil) throws -> (Data, String, Int32) {
        let process = Process()
        let output = Pipe()
        let diagnostics = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = diagnostics
        var inputDirectory: URL?
        var inputHandle: FileHandle?
        defer {
            try? inputHandle?.close()
            if let inputDirectory { try? FileManager.default.removeItem(at: inputDirectory) }
        }
        if let input {
            let manager = FileManager.default
            let dir = manager.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try manager.createDirectory(at: dir, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            inputDirectory = dir
            let url = dir.appendingPathComponent("document")
            guard manager.createFile(atPath: url.path, contents: input, attributes: [.posixPermissions: 0o600]) else {
                throw CocoaError(.fileWriteUnknown)
            }
            inputHandle = try FileHandle(forReadingFrom: url)
            process.standardInput = inputHandle
        }
        try process.run()

        // Helpers bound their diagnostics; standard output can exceed the pipe buffer.
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let log = String(decoding: diagnostics.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        process.waitUntilExit()
        return (data, log.trimmingCharacters(in: .whitespacesAndNewlines), process.terminationStatus)
    }
}

@MainActor
final class ReaderViewModel: ObservableObject {
    struct Snapshot {
        enum Source {
            case radio(Date, USBPrivacyKeys?)
            case file(URL)
        }

        let file: SettingsFile
        let source: Source

        var usbPrivacy: USBPrivacyKeys? {
            if case .radio(_, let keys) = source { return keys }
            return nil
        }

        var summary: String {
            var parts: [String] = []
            if let id = file.settings.dmrId { parts.append("DMR ID \(id)") }
            if let model = file.settings.usbModel {
                parts.append(LF("USB-модель %@", model))
            } else if let model = file.settings.fileModel {
                parts.append(model)
            }
            parts.append(provenance)
            return parts.joined(separator: " · ")
        }

        var provenance: String {
            switch source {
            case .radio(let date, _): LF("Считано в %@", date.formatted(date: .omitted, time: .shortened))
            case .file(let url): LF("Файл: %@", url.lastPathComponent)
            }
        }

        var suggestedFilename: String {
            switch source {
            case .radio: file.settings.dmrId.map { "tyt-settings-\($0)" } ?? "tyt-settings"
            case .file(let url): url.deletingPathExtension().lastPathComponent
            }
        }
    }

    enum Activity { case reading, readingUniversal, opening, saving, comparing, writing }

    @Published private(set) var snapshot: Snapshot? { didSet { writeNotice = nil } }
    @Published private(set) var activity: Activity? { didSet { if activity != nil { writeNotice = nil } } }
    @Published private(set) var error: String?
    @Published private(set) var savedFilename: String?
    @Published private(set) var writeNotice: String?
    @Published var presentsOpenPanel = false
    @Published var editor: CodeplugDraft?
    @Published var service: LocalService?
    @Published var universalUSB: UniversalUSBReport?

    func openDTMF() {
        guard canStart else { return }
        do {
            let file: SettingsFile
            var url: URL?
            if let snapshot {
                file = snapshot.file
                if case .file(let source) = snapshot.source { url = source }
            } else {
                file = try SettingsFile(data: Data(#"{"source_format":"dtmf","channels":[],"contacts":[],"zones":[],"rx_groups":[],"scan_lists":[]}"#.utf8))
            }
            let draft = try CodeplugDraft(file: file, source: url)
            try draft.showDTMF()
            editor = draft
        } catch { self.error = error.localizedDescription }
    }

    func startEditor(new: Bool = false) {
        guard canStart else { return }
        do {
            if new {
                let blank = Data(#"{"channels":[],"contacts":[],"zones":[],"rx_groups":[],"scan_lists":[]}"#.utf8)
                editor = try CodeplugDraft(file: SettingsFile(data: blank), source: nil)
            } else if let snapshot {
                var url: URL?
                if case .file(let source) = snapshot.source { url = source }
                editor = try CodeplugDraft(file:snapshot.file, source:url)
            }
        } catch { self.error = error.localizedDescription }
    }

    func newFromTemplate() {
        guard canStart else { return }
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension:"rdt") ?? .data]
        panel.message = L("Выберите штатный RDT. Редактор создаст черновик со всеми настройками шаблона.")
        guard panel.runModal() == .OK, let source = panel.url else { return }
        activity = .opening
        Task {
            defer { activity = nil }
            do {
                let file = try await Task.detached { try RadioReader.template(source) }.value
                editor = try CodeplugDraft(file:file,source:source)
            } catch { self.error = error.localizedDescription }
        }
    }

    func edited(_ file: SettingsFile, url: URL) {
        snapshot = Snapshot(file:file,source:.file(url))
        savedFilename = url.lastPathComponent
        error = nil
    }

    func checkDestination(_ url: URL, for snapshot: Snapshot) throws {
        if case .file(let source) = snapshot.source,
           source.standardizedFileURL.resolvingSymlinksInPath() == url.standardizedFileURL.resolvingSymlinksInPath() {
            throw RadioReader.Failure("Выберите новое имя для копии исходного файла.")
        }
    }

    func export(_ format: RadioReader.ExportFormat) {
        guard canSave, let snapshot else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [format.contentType]
        panel.nameFieldStringValue = snapshot.suggestedFilename + format.suffix
        if format == .yaml {
            panel.message = L("В YAML qdmr сохраняются DMR ID, каналы, контакты, зоны, группы приёма и списки сканирования. Для остальных настроек сохраните также JSON или копию RDT.")
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        activity = .saving
        Task {
            defer { activity = nil }
            do {
                try checkDestination(url, for: snapshot)
                let file = snapshot.file
                try await Task.detached { try RadioReader.export(format,file:file,to:url) }.value
                savedFilename = url.lastPathComponent; error = nil
            } catch { self.error = error.localizedDescription }
        }
    }

    var canStart: Bool { activity == nil && !presentsOpenPanel && editor == nil && service == nil && universalUSB == nil }
    var canSave: Bool { canStart && snapshot != nil }
    var canWrite: Bool { canStart && snapshot?.file.writeModel != nil }

    func requestWrite() {
        guard canWrite, let snapshot else { return }
        Task {
            guard canWrite else { return }
            do {
                writeNotice = try await programRadio(snapshot.file, source: snapshot.provenance)
            } catch { self.error = error.localizedDescription }
        }
    }

    func programRadio(_ file: SettingsFile, source: String, baseline: SettingsFile? = nil) async throws -> String? {
        try await reviewWrite(source: source) {
            let input = try baseline.map { try RadioReader.normalized(baseline: $0, edited: file) } ?? file
            return try RadioReader.prepareWrite(file: input)
        }
    }

    func requestRestore() {
        guard canStart else { return }
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "tytbackup") ?? .data]
        panel.title = L("Восстановить настройки из резервной копии…")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            guard canStart else { return }
            do {
                writeNotice = try await reviewWrite(source: url.lastPathComponent, restoring: true) {
                    try RadioReader.prepareRestore(backup: url)
                }
            } catch { self.error = error.localizedDescription }
        }
    }

    private func reviewWrite(source: String, restoring: Bool = false, prepare: @escaping @Sendable () throws -> RadioWriteReview) async throws -> String? {
        guard activity == nil else { throw RadioReader.Failure("Дождитесь завершения текущей операции.") }
        activity = .comparing
        error = nil
        writeNotice = nil
        savedFilename = nil
        defer { activity = nil }
        let review = try await Task.detached(priority: .userInitiated, operation: prepare).value
        return try await review.applyIfApproved {
            let panel = NSSavePanel()
            panel.title = L("Резервная копия перед записью")
            panel.message = L("Выберите новое имя. Перед обновлением рации здесь будут сохранены исходные секторы.")
            panel.allowedContentTypes = [UTType(filenameExtension: "tytbackup") ?? .data]
            panel.nameFieldStringValue = "\(review.summary.model)-\(Int(Date().timeIntervalSince1970)).tytbackup"
            guard panel.runModal() == .OK, let url = panel.url else { return nil }
            return review.confirm(source: source, backup: url, restoring: restoring) ? url : nil
        } write: { data, url in
            activity = .writing
            let process = ProcessInfo.processInfo
            let token = process.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled],
                                              reason: "TYT USB codeplug programming")
            defer { process.endActivity(token) }
            try await Task.detached(priority: .userInitiated) { try RadioReader.writeVerified(data, backup: url) }.value
        }
    }

    func showOpenPanel() {
        guard canStart else { return }
        presentsOpenPanel = true
    }

    func showSavePanel() {
        guard canStart, let snapshot else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.json]
        if snapshot.usbPrivacy != nil {
            panel.message = L("JSON сохраняет назначения Privacy каналам. Значения ключей доступны только при просмотре USB-снимка в приложении.")
        }
        panel.nameFieldStringValue = snapshot.suggestedFilename + ".json"
        panel.canCreateDirectories = true
        activity = .saving
        guard panel.runModal() == .OK, let destination = panel.url else {
            activity = nil
            return
        }
        savedFilename = nil
        error = nil
        Task {
            defer { activity = nil }
            do {
                try checkDestination(destination, for: snapshot)
                let file = snapshot.file
                try await Task.detached(priority: .userInitiated) { try file.save(to: destination) }.value
                savedFilename = destination.lastPathComponent
            } catch let failure {
                error = LF("Не удалось сохранить JSON. %@", failure.localizedDescription)
            }
        }
    }

    func open(_ result: Result<URL, any Error>) {
        savedFilename = nil
        error = nil
        switch result {
        case .failure(let failure):
            let nsError = failure as NSError
            if nsError.domain != NSCocoaErrorDomain || nsError.code != NSUserCancelledError {
                error = LF("Не удалось открыть файл. %@", failure.localizedDescription)
            }
        case .success(let url):
            activity = .opening
            Task {
                defer { activity = nil }
                do {
                    let file = try await Task.detached(priority: .userInitiated) { try SettingsFile.open(url) }.value
                    snapshot = Snapshot(file: file, source: .file(url))
                } catch is DecodingError {
                    error = url.pathExtension.lowercased() == "json"
                        ? L("Этот JSON не похож на файл настроек TYT Reader.")
                        : L("Импортированный файл содержит настройки в неожиданном формате.")
                } catch let failure {
                    error = LF("Не удалось открыть файл. %@", failure.localizedDescription)
                }
            }
        }
    }

    func readUniversalPrivacy() {
        guard canStart else { return }
        activity = .readingUniversal
        error = nil
        savedFilename = nil
        Task {
            defer { activity = nil }
            do {
                universalUSB = try await Task.detached(priority: .userInitiated) {
                    try RadioReader.readUniversalPrivacy()
                }.value
            } catch { self.error = error.localizedDescription }
        }
    }

    func read() {
        guard canStart else { return }
        activity = .reading
        error = nil
        savedFilename = nil
        Task {
            defer { activity = nil }
            do {
                let result = try await Task.detached(priority: .userInitiated) { try RadioReader.read() }.value
                snapshot = Snapshot(file: result.file, source: .radio(Date(), result.keys))
            } catch let failure as RadioReader.Failure {
                error = failure.localizedDescription
            } catch {
                self.error = LF("Ошибка чтения. %@", error.localizedDescription)
            }
        }
    }
}

@main
struct RadioReaderApp: App {
    @StateObject private var reader = ReaderViewModel()
    @NSApplicationDelegateAdaptor(RadioLifecycle.self) private var lifecycle
    @State private var language = AppLanguagePreference.choice

    private func openGuide(_ name: String) {
        if let url = Bundle.main.url(forResource:name,withExtension:"md") {
            NSWorkspace.shared.open(url)
        }
    }

    var body: some Scene {
        Window("TYT · Настройки рации", id: "reader") {
            ReaderView(reader: reader)
                .frame(minWidth: 760, minHeight: 460)
                .onAppear { lifecycle.reader = reader }
        }
        .defaultSize(width: 1120, height: 680)
        .commands {
            CommandMenu("Язык / Language / 语言") {
                Picker("Язык", selection: Binding(get: { language }, set: { next in
                    guard next != language else { return }
                    language = next
                    AppLanguagePreference.select(next)
                    let notice = NSAlert()
                    let translations = Bundle.main.url(forResource: next, withExtension: "lproj")
                        .flatMap(Bundle.init(url:)) ?? Bundle.main
                    notice.messageText = translations.localizedString(forKey: "Язык изменится при следующем запуске TYT Reader.", value: nil, table: nil)
                    notice.addButton(withTitle: translations.localizedString(forKey: "Готово", value: nil, table: nil))
                    notice.runModal()
                })) {
                    Text("Как в macOS").tag("system")
                    Text(verbatim: "Русский").tag("ru")
                    Text(verbatim: "English").tag("en")
                    Text(verbatim: "简体中文").tag("zh-Hans")
                }.pickerStyle(.inline)
            }
            CommandMenu("Сервис") { ServiceActions(reader: reader) }
            CommandGroup(replacing:.help) {
                Button("Возможности TYT Reader") { openGuide("CPS-COVERAGE") }
                Button("Экраны Windows CPS и источники") { openGuide("CPS-REFERENCES") }
            }
            CommandGroup(replacing: .newItem) {
                Button("Новый кодплаг…") { reader.startEditor(new:true) }.keyboardShortcut("n").disabled(!reader.canStart)
                Button("Новый из шаблона RDT…",action:reader.newFromTemplate).disabled(!reader.canStart)
                Button("Открыть файл…", action: reader.showOpenPanel)
                    .keyboardShortcut("o")
                    .disabled(!reader.canStart)
                Button("Сохранить JSON…", action: reader.showSavePanel)
                    .keyboardShortcut("s")
                    .disabled(!reader.canSave)
                Button("Редактировать копию…") { reader.startEditor() }.keyboardShortcut("e").disabled(!reader.canSave)
                Menu("Экспорт CSV") {
                    Button("Каналы…") { reader.export(.channels) }
                    Button("Контакты…") { reader.export(.contacts) }
                }.disabled(!reader.canSave)
                Button("Экспорт YAML для qdmr…") { reader.export(.yaml) }.disabled(!reader.canSave)
                Button("Печать…") { if let snapshot = reader.snapshot { printCodeplug(snapshot.file.settings, title:snapshot.suggestedFilename) } }
                    .keyboardShortcut("p").disabled(!reader.canSave)
            }
        }
    }
}

@MainActor
private final class RadioLifecycle: NSObject, NSApplicationDelegate {
    weak var reader: ReaderViewModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        reader?.activity == .writing ? .terminateCancel : .terminateNow
    }
}

private struct ServiceActions: View {
    @ObservedObject var reader: ReaderViewModel

    var body: some View {
        Group {
            Button("Записать настройки в рацию…", action: reader.requestWrite)
                .disabled(!reader.canWrite)
                .help("Перед записью программа сравнит настройки и покажет изменения.")
            Button("Восстановить настройки из резервной копии…", action: reader.requestRestore)
            Divider()
            Button("Последовательности DTMF…") { reader.openDTMF() }
            Divider()
            Button("Basic и Enhanced (USB)…") { reader.service = .usbPrivacy }
                .disabled(reader.snapshot?.usbPrivacy == nil)
            Button("Считать Universal Privacy по USB…", action: reader.readUniversalPrivacy)
                .help("По завершении штатного чтения рация перезапустится.")
            Button("Ключи Universal (.dec)…") { reader.service = .universal }
            Button("Ключи Basic и Enhanced (.rdt)…") { reader.service = .legacy }
            Divider()
            Button("Сервисная калибровка…") { reader.service = .calibration }
        }.disabled(!reader.canStart)
    }
}

struct ReaderView: View {
    @ObservedObject var reader: ReaderViewModel
    @State private var selectedTab = "channels"

    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: "antenna.radiowaves.left.and.right")
                    .font(.system(size: 25, weight: .medium))
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 46, height: 46)
                    .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text("TYT MD‑UV390").font(.title2.weight(.semibold))
                    if let snapshot = reader.snapshot {
                        Text(snapshot.summary)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(snapshot.provenance)
                    } else {
                        Text("Подключите рацию по USB или откройте RDT, YAML, JSON или CSV.")
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if let activity = reader.activity {
                    if activity == .readingUniversal { Text("Читаю Universal Privacy…").foregroundStyle(.secondary) }
                    if activity == .comparing || activity == .writing { Text(activityLabel(activity)).foregroundStyle(.secondary) }
                    ProgressView().controlSize(.small)
                        .accessibilityLabel(activityLabel(activity))
                }
                Button(action: reader.read) {
                    Label(reader.activity == .reading ? L("Считывание…") : L("Считать настройки"), systemImage: "arrow.down.to.line")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(!reader.canStart)
                Button("Записать в рацию…", action: reader.requestWrite)
                    .disabled(!reader.canWrite)
                    .help("Перед записью программа сравнит настройки и покажет изменения.")
            }

            if let error = reader.error {
                Label(error, systemImage: "exclamationmark.circle")
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }

            Divider()

            HStack {
                fileActions
                Menu("Сервис") { ServiceActions(reader: reader) }.fixedSize().disabled(!reader.canStart)
                Spacer()
                Button("Редактировать копию…") { reader.startEditor() }.disabled(!reader.canSave)
                Menu {
                    Button("Каналы в CSV…") { reader.export(.channels) }
                    Button("Контакты в CSV…") { reader.export(.contacts) }
                    Button("YAML для qdmr…") { reader.export(.yaml) }
                    Button("Печать…") {
                        if let snapshot = reader.snapshot { printCodeplug(snapshot.file.settings, title:snapshot.suggestedFilename) }
                    }
                } label: { Image(systemName:"ellipsis.circle") }.menuStyle(.borderlessButton).fixedSize().disabled(!reader.canSave)
                .accessibilityLabel("Экспорт и печать")
            }
            if let settings = reader.snapshot?.file.settings {
                HSplitView {
                    CPSNavigation(settings:settings,selection:$selectedTab).frame(minWidth:164,idealWidth:200,maxWidth:246)
                    Group {
                        switch selectedTab {
                        case "channels": ChannelPanel(settings:settings)
                        case "zones": ZonePanel(zones:settings.zones)
                        case "contacts": ContactTable(contacts:settings.contacts)
                        case "rx_groups":
                            VStack(alignment: .leading) {
                                if let slots = settings.unrecognizedRxGroupSlots, !slots.isEmpty {
                                    Text(LF("Не распознаны позиции: %@", slots.map(String.init).joined(separator: ", ")))
                                        .font(.callout).foregroundStyle(.secondary)
                                }
                                ReceiveGroupPanel(groups:settings.rxGroups,contacts:settings.contacts)
                            }
                        case "scan_lists": ScanListPanel(lists:settings.scanLists,channels:settings.channels)
                        case "dtmf":
                            VStack(alignment: .leading, spacing: 10) {
                                Button("Редактировать последовательности…", action: reader.openDTMF)
                                CPSSectionPanel(section:settings.section("dtmf"), title:settings.title(for:"dtmf"))
                            }
                        case "privacy":
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Button("Считать Universal Privacy по USB…", action: reader.readUniversalPrivacy)
                                        .help("По завершении штатного чтения рация перезапустится.")
                                    if reader.snapshot?.usbPrivacy != nil {
                                        Button("Basic и Enhanced (USB)…") { reader.service = .usbPrivacy }
                                    }
                                }
                                .disabled(!reader.canStart)
                                HStack {
                                    Button("Ключи Universal (.dec)…") { reader.service = .universal }
                                    Button("Basic и Enhanced (.rdt)…") { reader.service = .legacy }
                                }.disabled(!reader.canStart)
                                CPSSectionPanel(section:settings.section("privacy"), title:settings.title(for:"privacy"))
                            }
                        default: CPSSectionPanel(section:settings.section(selectedTab), title:settings.title(for:selectedTab)).id(selectedTab)
                        }
                    }.padding(.leading,8).frame(maxWidth:.infinity,maxHeight:.infinity)
                }
            } else {
                EmptyPanel(symbol:"tray",message:emptyMessage)
            }

            if let notice = reader.writeNotice {
                Label(notice, systemImage: "info.circle").font(.callout).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else if let filename = reader.savedFilename {
                Label(LF("Сохранено: %@", filename), systemImage: "checkmark.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(18)
        .sheet(isPresented: Binding(get:{ reader.editor != nil },set:{ if !$0 { reader.editor = nil } })) {
            if let draft = reader.editor {
                CodeplugEditor(draft: draft, program: { file in
                    try await reader.programRadio(file, source: draft.source?.lastPathComponent ?? L("Открытая копия в редакторе"),
                                                  baseline: draft.original)
                }) { file,url in reader.edited(file,url:url) }
            }
        }
        .sheet(item: $reader.service) { service in
            switch service {
            case .usbPrivacy:
                if let keys = reader.snapshot?.usbPrivacy { USBPrivacyViewer(keys: keys) }
            case .universal: PrivacyEditor()
            case .legacy: RDTPrivacyEditor()
            case .calibration: CalibrationEditor()
            }
        }
        .sheet(isPresented: Binding(get: { reader.universalUSB != nil }, set: { if !$0 { reader.universalUSB = nil } })) {
            if let report = reader.universalUSB { UniversalUSBViewer(report: report) }
        }
        .fileImporter(isPresented: $reader.presentsOpenPanel, allowedContentTypes: SettingsFile.openTypes, onCompletion: reader.open)
    }

    private var fileActions: some View {
        HStack {
            Button(action: reader.showOpenPanel) {
                Label("Открыть файл…", systemImage: "folder")
            }
            .disabled(!reader.canStart)
            Button(action: reader.showSavePanel) {
                Label("Сохранить JSON…", systemImage: "square.and.arrow.down")
            }
            .disabled(!reader.canSave)
        }
        .fixedSize()
    }

    private func activityLabel(_ activity: ReaderViewModel.Activity) -> String {
        switch activity {
        case .reading: L("Чтение настроек")
        case .readingUniversal: L("Чтение Universal Privacy")
        case .opening: L("Открытие файла")
        case .saving: L("Сохранение файла")
        case .comparing: L("Сравниваю настройки с рацией…")
        case .writing: L("Записываю и проверяю настройки…")
        }
    }

    private var emptyMessage: String {
        switch reader.activity {
        case .reading: L("Читаю настройки рации…")
        case .readingUniversal: L("Читаю слоты Universal Privacy…")
        case .opening: L("Открываю файл…")
        case .comparing: L("Сравниваю настройки с рацией…")
        case .writing: L("Записываю и проверяю настройки…")
        case .saving, nil: L("Здесь появятся настройки")
        }
    }
}

private struct EmptyPanel: View {
    let symbol: String
    let message: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 30)).accessibilityHidden(true)
            Text(L(message)).font(.callout)
        }
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct ChannelTable: View {
    let channels: [RadioSettings.Channel]
    @Binding var selected: Int?

    private func frequency(_ hertz: Int?) -> String {
        guard let hertz else { return "—" }
        return (Double(hertz) / 1_000_000).formatted(.number.precision(.fractionLength(5)))
    }

    var body: some View {
        Table(channels, selection:$selected) {
            TableColumn("№") { Text($0.index.formatted()).monospacedDigit() }.width(36)
            TableColumn("Канал", value: \.name).width(min: 100, ideal: 150)
            TableColumn("Режим", value: \.modeLabel).width(min: 64, ideal: 90)
            TableColumn("RX, МГц") { Text(frequency($0.rxHz)).monospacedDigit() }.width(min: 78, ideal: 106)
            TableColumn("TX, МГц") { Text(frequency($0.txHz)).monospacedDigit() }.width(min: 78, ideal: 106)
            TableColumn("Полоса, кГц", value: \.bandwidth).width(min: 80, ideal: 96)
            TableColumn("Color Code") { Text($0.colorCode.map(String.init) ?? "—") }.width(min: 72, ideal: 90)
            TableColumn("Time Slot") { Text($0.timeSlot.map(String.init) ?? "—") }.width(min: 58, ideal: 78)
        }
        .accessibilityLabel("Каналы рации")
    }
}

private struct ZonePanel: View {
    let zones: [RadioSettings.Zone]
    @State private var selectedIndex: Int?

    private var selectedZone: RadioSettings.Zone? {
        zones.first { $0.index == selectedIndex } ?? zones.first
    }

    private struct Row: Identifiable {
        let id: Int
        let index: Int
        let name: String
    }

    private func side(_ label: String, _ members: [RadioSettings.Zone.Member]) -> some View {
        let rows = members.enumerated().map {
            Row(id: $0.offset + 1, index: $0.element.index, name: $0.element.name ?? "—")
        }
        return VStack(alignment: .leading, spacing: 6) {
            Text(LF("Сторона %@", label)).font(.headline)
            if rows.isEmpty {
                EmptyPanel(symbol: "list.bullet", message: "Нет каналов")
            } else {
                Table(rows) {
                    TableColumn("Позиция") { Text($0.id.formatted()).monospacedDigit() }.width(68)
                    TableColumn("Канал", value: \.name)
                    TableColumn("№ канала") { Text($0.index.formatted()).monospacedDigit() }.width(78)
                }
                .accessibilityLabel(LF("Каналы стороны %@", label))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var body: some View {
        if let zone = selectedZone {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Зона", selection: Binding(get: { zone.index }, set: { selectedIndex = $0 })) {
                    ForEach(zones) { Text($0.name).tag($0.index) }
                }
                .fixedSize()
                HStack(spacing: 14) {
                    side("A", zone.sideA)
                    side("B", zone.sideB)
                }
            }
        } else {
            EmptyPanel(symbol: "rectangle.split.2x1", message: "Нет зон")
        }
    }
}

private struct ContactTable: View {
    let contacts: [RadioSettings.Contact]

    var body: some View {
        if contacts.isEmpty {
            EmptyPanel(symbol: "person.crop.rectangle", message: "Нет контактов")
        } else {
            Table(contacts) {
                TableColumn("№") { Text($0.index.formatted()).monospacedDigit() }.width(44)
                TableColumn("Имя", value: \.name)
                TableColumn(contacts.contains { $0.type == "dtmf" || $0.type == "m17" } ? L("Номер / позывной") : "DMR ID") {
                    Text($0.address).monospacedDigit()
                }
                    .width(min: 100, ideal: 130)
                TableColumn("Сигнал") { Text($0.ring.map { $0 ? L("Вкл.") : L("Выкл.") } ?? "—") }.width(64)
                TableColumn("Тип вызова", value: \.typeLabel).width(min: 120, ideal: 180)
            }
            .accessibilityLabel("Контакты")
        }
    }
}

private struct ListDetailPanel<Item: Identifiable, Detail: View>: View where Item.ID == Int {
    let items: [Item]?
    let title: String
    let name: KeyPath<Item, String>
    let emptyMessage: String
    @ViewBuilder let detail: (Item) -> Detail
    @State private var selectedIndex: Int?

    var body: some View {
        if let items, let selected = items.first(where: { $0.id == selectedIndex }) ?? items.first {
            VStack(alignment: .leading, spacing: 10) {
                Picker(L(title), selection: Binding(get: { selected.id }, set: { selectedIndex = $0 })) {
                    ForEach(items) { item in
                        Text("\(item.id) · \(item[keyPath: name])").tag(item.id)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                detail(selected)
            }
        } else {
            EmptyPanel(symbol: "list.bullet", message: items == nil ? "В файле нет этого раздела" : emptyMessage)
        }
    }
}

private struct MemberRow<Value>: Identifiable {
    let id: Int
    let index: Int
    let value: Value?

    static func resolve(_ indices: [Int], in values: [Int: Value]) -> [Self] {
        indices.enumerated().map { Self(id: $0.offset + 1, index: $0.element, value: values[$0.element]) }
    }
}

private struct ReceiveGroupPanel: View {
    let groups: [RadioSettings.ReceiveGroup]?
    let contacts: [RadioSettings.Contact]

    var body: some View {
        let byIndex = Dictionary(contacts.map { ($0.index, $0) }, uniquingKeysWith: { first, _ in first })
        ListDetailPanel(items: groups, title: "Группа приёма", name: \.name, emptyMessage: "Нет групп приёма") { group in
            if group.contacts.isEmpty {
                EmptyPanel(symbol: "person.2", message: "В группе нет контактов")
            } else {
                Table(MemberRow.resolve(group.contacts, in: byIndex)) {
                    TableColumn("Позиция") { Text(String($0.id)).monospacedDigit() }.width(66)
                    TableColumn("№ контакта") { Text(String($0.index)).monospacedDigit() }.width(84)
                    TableColumn("Контакт") { Text($0.value?.name ?? L("Контакт не найден")) }
                    TableColumn("DMR ID") { Text($0.value?.dmrId.map(String.init) ?? "—").monospacedDigit() }
                        .width(min: 94, ideal: 130)
                }
                .accessibilityLabel("Контакты группы приёма")
            }
        }
    }
}

private struct ScanListPanel: View {
    let lists: [RadioSettings.ScanList]?
    let channels: [RadioSettings.Channel]

    private func channelName(_ index: Int, in channels: [Int: RadioSettings.Channel]) -> String {
        if index == 0 { return L("Выбранный на рации") }
        return channels[index]?.name ?? LF("Канал №%ld не найден", index)
    }

    var body: some View {
        let byIndex = Dictionary(channels.map { ($0.index, $0) }, uniquingKeysWith: { first, _ in first })
        ListDetailPanel(items: lists, title: "Список сканирования", name: \.name, emptyMessage: "Нет списков сканирования") { list in
            VStack(alignment: .leading, spacing: 8) {
                if list.primary != nil || list.secondary != nil || list.revert != nil {
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 2) {
                        if let index = list.primary {
                            GridRow {
                                Text("Приоритет 1").foregroundStyle(.secondary)
                                Text(channelName(index, in: byIndex))
                            }
                        }
                        if let index = list.secondary {
                            GridRow {
                                Text("Приоритет 2").foregroundStyle(.secondary)
                                Text(channelName(index, in: byIndex))
                            }
                        }
                        if let index = list.revert {
                            GridRow {
                                Text("Канал передачи").foregroundStyle(.secondary)
                                Text(channelName(index, in: byIndex))
                            }
                        }
                    }
                    .font(.callout)
                }
                if list.channels.isEmpty {
                    EmptyPanel(symbol: "list.bullet", message: "В списке нет каналов")
                } else {
                    Table(MemberRow.resolve(list.channels, in: byIndex)) {
                        TableColumn("Позиция") { Text(String($0.id)).monospacedDigit() }.width(66)
                        TableColumn("№ канала") { Text($0.index == 0 ? "—" : String($0.index)).monospacedDigit() }.width(80)
                        TableColumn("Канал") { Text(channelName($0.index, in: byIndex)) }
                        TableColumn("Режим") { Text($0.value?.modeLabel ?? "—") }.width(min: 75, ideal: 100)
                    }
                    .accessibilityLabel("Каналы списка сканирования")
                }
            }
        }
    }
}
