import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct EditField {
  enum Kind {
    case text, number, frequency, tone
    case choice([String])
    case ids, zoneMembers
  }
  let key: String
  let name: String
  let kind: Kind
  init(_ key: String, _ name: String, _ kind: Kind = .text) {
    self.key = key
    self.name = name
    self.kind = kind
  }
  func choiceLabel(_ value: String) -> String {
    switch (key, value) {
    case ("mode", "analog"): L("Аналог")
    case ("type", "group"): L("Групповой")
    case ("type", "private"): L("Индивидуальный")
    case ("type", "all"): L("Общий вызов")
    case ("type", "dtmf"): "DTMF"
    case ("type", "m17"): "M17"
    case ("bandwidth_code", "12,5"): (12.5).formatted(.number)
    default: LD(value)
    }
  }

}

private let tables = [
  ("channels", "Каналы"), ("contacts", "Контакты"), ("zones", "Зоны A/B"),
  ("rx_groups", "Группы приёма"), ("scan_lists", "Списки сканирования"),
]
private typealias Object = [String: Any]
private struct RecordKey: Hashable {
  let table: String
  let id: Int
}

@MainActor
final class CodeplugDraft: ObservableObject {
  private var document: Object
  private var pending: [RecordKey: [String: String]] = [:]
  private var removed: [String: Set<Int>] = [:]
  @Published var table = "channels"
  @Published var selection: Int?
  @Published private(set) var dirty = false
  @Published var error: String?
  @Published var saving = false
  let original: SettingsFile
  let source: URL?

  init(file: SettingsFile, source: URL?) throws {
    guard let json = try JSONSerialization.jsonObject(with: file.data) as? Object else {
      throw RadioReader.Failure("Не удалось открыть настройки в редакторе.")
    }
    document = json
    original = file
    self.source = source
    if json["source_format"] as? String == "dtmf" { table = "section/dtmf_sequences" }
  }

  private var sections: [Object] { document["cps_sections"] as? [Object] ?? [] }
  func showDTMF() throws {
    if sectionIndex("section/dtmf_sequences") == nil {
      guard let url = Bundle.main.url(forResource: "DTMFBlank", withExtension: "json"),
            let section = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? Object else {
        throw RadioReader.Failure("В приложении отсутствует описание последовательностей DTMF.")
      }
      document["cps_sections"] = sections + [section]
    }
    table = "section/dtmf_sequences"
    selection = nil
  }
  private func sectionIndex(_ table: String) -> Int? {
    sections.firstIndex { "section/" + ($0["id"] as? String ?? "") == table }
  }
  private var isTYTSnapshot: Bool {
    let model = document["file_model"] as? String ?? document["usb_model"] as? String
    return canExportRDT || model == "MD-UV380" || model == "MD-UV390"
  }
  private var buttonBlockWasRead: Bool {
    if canExportRDT { return true }
    let rows = sections.first { $0["id"] as? String == "buttons" }?["rows"] as? [Object] ?? []
    return rows.contains { ["Боковые кнопки", "Долгое нажатие"].contains($0["name"] as? String ?? "") }
  }
  private var partialUSB: Bool { document["usb_model"] != nil && document["read_notes"] != nil }
  var sequencesOnly: Bool { document["source_format"] as? String == "dtmf" }
  private var localEmptySlots: [String: [Int]] { document["local_empty_slots"] as? [String: [Int]] ?? [:] }
  private func assignmentKnown(_ kind: String, _ slot: Int) -> Bool {
    buttonBlockWasRead || localEmptySlots[kind]?.contains(slot) == true
  }
  var slotSourceNote: String? {
    if table == "section/dtmf_sequences" {
      return "Допустимы 0–9, A–D, * и #. «Не считано» означает, что исходное значение неизвестно. Пустое поле очищает позицию."
    }
    guard isTYTSnapshot else { return nil }
    if ["quick", "numberKeys"].contains(table), !buttonBlockWasRead {
      return "Для непрочитанной позиции исходное назначение неизвестно. Можно задать его в локальной копии."
    }
    if isSlotTable, !canExportRDT {
      if partialUSB && table == "section/emergency" {
        return "По USB считаны позиции 11–32. Исходные записи 1–10 неизвестны."
      }
      if partialUSB && table == "section/messages" {
        return "По USB считаны позиции 1–48. Исходные записи 49–50 неизвестны."
      }
      if !partialUSB && (document["usb_model"] != nil || !buttonBlockWasRead) {
        return "Полнота этого раздела в снимке неизвестна. Новая запись будет добавлена в локальную копию."
      }
    }
    return nil
  }
  var choices: [(String, String)] {
    if sequencesOnly { return [("section/dtmf_sequences", "Последовательности DTMF")] }
    return tables + [("radio", "DMR ID")] + (isTYTSnapshot ? [
      ("quick", "Быстрый доступ (1–6)"), ("numberKeys", "Цифровые клавиши (10 позиций)"),
    ] : [])
      + creatableSections.filter { sectionIndex($0.0) == nil }
      + sections.compactMap { section in
        guard let id = section["id"] as? String, let title = section["title"] as? String,
          id != "basic"
        else { return nil }
        return ("section/" + id, title)
      }
  }
  private var creatableSections: [(String, String)] {
    var kinds = [("section/messages", "Шаблоны сообщений")]
    if isTYTSnapshot {
      kinds += [
        ("section/gps", "Позиционирование"), ("section/emergency", "Аварийная сигнализация"),
      ]
    }
    return kinds
  }
  private func records(_ table: String) -> [Object] {
    if table == "numberKeys" {
      let assigned = sections.first { $0["id"] as? String == "buttons" }?["rows"] as? [Object] ?? []
      return (1...10).map { slot in
        let name = "Цифровая клавиша · позиция " + String(slot)
        let fields = assigned.first { $0["name"] as? String == name }?["fields"] as? [Object] ?? []
        let value = fields.first { $0["label"] as? String == "Контакт" }?["value"] as? String
        let index = value?.split(separator: " ").dropFirst().first.flatMap { Int($0) }
        var row: Object = ["index": slot, "name": name]
        if let index { row["contact_index"] = index }
        else if assignmentKnown(table, slot) { row["contact_index"] = 0 }
        return row
      }
    }
    if table == "quick" {
      let quickRows = sections.filter { ["dtmf", "buttons"].contains($0["id"] as? String ?? "") }
        .flatMap { $0["rows"] as? [Object] ?? [] }
      return (1...6).map { i in
        let name = "Быстрый доступ " + String(i)
        let fields =
          quickRows.first(where: { $0["name"] as? String == name })?["fields"] as? [Object] ?? []
        func value(_ label: String) -> String? {
          fields.first(where: { $0["label"] as? String == label })?["value"] as? String
        }
        let number = value("Контакт")?.split(separator: " ").dropFirst().first.flatMap { Int($0) }
        return [
          "index": i, "name": name,
          "quick_type": value("Действие") ?? (value("Профиль") != nil ? "DTMF" :
            (assignmentKnown(table, i) ? "Не назначено" : "Нет данных")),
          "contact_index": number ?? 0, "message_slot": Int(value("Шаблон") ?? "") ?? 1,
          "profile": value("Профиль") ?? "DTMF-1",
          "encode_slot": value("Номер Encode").flatMap(Int.init) ?? 0,
        ]
      }
    }
    if table == "radio" {
      return [["index": 1, "name": "Идентификатор DMR", "dmr_id": document["dmr_id"] ?? NSNull()]]
    }
    if let i = sectionIndex(table) {
      return (sections[i]["rows"] as? [Object] ?? []).enumerated().map { i, row in
        let words = (row["name"] as? String ?? "").split(separator: " ")
        var slot: Int?
        if table == "section/messages" || table == "section/gps" {
          slot = words.last.flatMap { Int($0) }
        } else if table == "section/emergency" {
          slot = words.first == "№" ? words.dropFirst().first.flatMap { Int($0) } : 0
        }
        return row.merging(["index": slot ?? (i + 1)]) { _, new in new }
      }
    }
    return document[table] as? [Object] ?? []
  }
  var rows: [(Int, String)] {
    records(table).compactMap { record in
      guard let index = record["index"] as? Int else { return nil }
      let title =
        pending[RecordKey(table: table, id: index)]?["name"] ?? record["name"] as? String ?? ""
      return (index, isRecordTable ? title : CPSRow.displayName(title))
    }
  }
  var active: Int? {
    selection.flatMap { i in rows.contains { $0.0 == i } ? i : nil } ?? rows.first?.0
  }
  var isRecordTable: Bool { tables.contains { $0.0 == table } }
  var isSlotTable: Bool { creatableSections.contains { $0.0 == table } }
  var canExportRDT: Bool { source?.pathExtension.lowercased() == "rdt" }
  private func record(_ key: RecordKey) -> Object {
    records(key.table).first { $0["index"] as? Int == key.id } ?? [:]
  }

  func candidates(for field: String) -> [(Int, String)]? {
    let target: String
    switch field {
    case "A", "B", "channels", "primary", "secondary", "revert": target = "channels"
    case "contacts", "contact_index": target = "contacts"
    case "scan_list_index": target = "scan_lists"
    case "rx_group_index": target = "rx_groups"
    default: return nil
    }
    var result: [(Int, String)] = records(target).compactMap { row in
      guard let id = row["index"] as? Int else { return nil }
      let name = pending[RecordKey(table: target, id: id)]?["name"] ?? row["name"] as? String ?? ""
      return (id, name)
    }
    if table == "scan_lists" { result.insert((0, L("Выбранный на рации канал")), at: 0) }
    if table == "numberKeys" { result.insert((0, L("Без назначения")), at: 0) }
    return result
  }

  private func fieldList(_ key: RecordKey, _ row: Object) -> [EditField] {
    let name = EditField("name", "Название")
    let mode = pending[key]?["mode"] ?? row["mode"] as? String
    var result: [EditField]
    switch key.table {
    case "numberKeys":
      return [.init("contact_index", "№ контакта (0 — без назначения)", .number)]
    case "quick":
      let action = pending[key]?["quick_type"] ?? row["quick_type"] as? String ?? "Не назначено"
      var fields = [
        EditField(
          "quick_type", "Действие", .choice(["Не назначено", "Вызов", "Сообщение", "DTMF"]))
      ]
      if action == "DTMF" {
        fields.append(
          .init("profile", "Профиль", .choice(["DTMF-1", "DTMF-2", "DTMF-3", "DTMF-4"])))
        fields.append(.init("encode_slot", "Номер Encode (0–32)", .number))
      }
      if ["Вызов", "Сообщение"].contains(action) {
        fields.append(.init("contact_index", "№ контакта", .number))
      }
      if action == "Сообщение" { fields.append(.init("message_slot", "№ шаблона", .number)) }
      return fields
    case "radio": return [.init("dmr_id", "DMR ID", .number)]
    case "channels":
      result = [
        name,
        .init(
          "mode", "Режим",
          .choice(canExportRDT ? ["analog", "DMR"] : ["analog", "DMR", "AM", "M17"])),
        .init("rx_hz", "Приём, МГц", .frequency), .init("tx_hz", "Передача, МГц", .frequency),
        .init("bandwidth_code", "Полоса, кГц", .choice(["12,5", "20", "25"])),
        .init("scan_list_index", "№ списка сканирования", .number),
      ]
      if mode == "DMR" {
        result += [
          .init("color_code", "Color Code (0–15)", .number),
          .init("time_slot", "Time Slot (1–2)", .number),
          .init("contact_index", "№ контакта передачи", .number),
          .init("rx_group_index", "№ группы приёма", .number),
        ]
      } else if mode == "analog" {
        result += [
          .init("rx_tone_code", "Субтон приёма", .tone),
          .init("tx_tone_code", "Субтон передачи", .tone),
        ]
      }
      if mode == "DMR" {
        result += [
          .init("privacy_type_code", "Тип Privacy (0–3)", .number),
          .init("privacy_index_code", "Индекс слота Privacy (с нуля)", .number),
        ]
      }
      result += extraFields(row, key: "details")
    case "contacts":
      let type = pending[key]?["type"] ?? row["type"] as? String
      result = [
        name,
        .init(
          "type", "Вид вызова",
          .choice(
            canExportRDT
              ? ["group", "private", "all"] : ["group", "private", "all", "dtmf", "m17"])),
      ]
      if type == "dtmf" {
        result.append(.init("dtmf_number", "Номер DTMF"))
      } else if type == "m17" {
        result.append(.init("callsign", "Позывной"))
      } else {
        result.append(.init("dmr_id", "DMR ID", .number))
        result.append(.init("ring", "Звуковое оповещение", .choice(["Вкл.", "Выкл."])))
      }
    case "zones":
      result = [
        name, .init("A", "Каналы стороны A", .zoneMembers),
        .init("B", "Каналы стороны B", .zoneMembers),
      ]
    case "rx_groups": result = [name, .init("contacts", "Номера контактов", .ids)]
    case "scan_lists":
      result = [
        name, .init("channels", "Номера каналов", .ids), .init("primary", "Приоритет 1", .number),
        .init("secondary", "Приоритет 2", .number), .init("revert", "Канал передачи", .number),
      ]
    case "section/emergency" where key.id > 0:
      result = [.init("emergency_name", "Название системы")] + extraFields(row, key: "fields")
    default: result = extraFields(row, key: "fields")
    }
    return result
  }

  private static let fieldOptions: [String: [String: [String]]] = {
    guard let url = Bundle.main.url(forResource: "CPSChoices", withExtension: "json"),
      let data = try? Data(contentsOf: url)
    else { return [:] }
    return (try? JSONDecoder().decode([String: [String: [String]]].self, from: data)) ?? [:]
  }()

  private func extraFields(_ row: Object, key: String) -> [EditField] {
    (row[key] as? [Object] ?? []).enumerated().compactMap { i, field in
      guard let label = field["label"] as? String else { return nil }
      let originalValue = field["value"] as? String ?? ""
      let section = table.hasPrefix("section/") ? String(table.dropFirst(8)) : table
      let kind: EditField.Kind
      if section == "vfo", ["Субтон приёма", "Субтон передачи"].contains(label) {
        kind = .text
      } else if let choices = Self.fieldOptions[section]?[label], !choices.isEmpty {
        kind = .choice(choices)
      } else if ["Вкл.", "Выкл."].contains(originalValue) {
        kind = .choice(["Вкл.", "Выкл."])
      } else {
        kind = .text
      }
      return .init("\(key)/\(i)", label, kind)
    }
  }

  var fields: [EditField] {
    guard let id = active else { return [] }
    let key = RecordKey(table: table, id: id)
    return fieldList(key, record(key))
  }

  private func stringValue(_ field: EditField, _ record: Object) -> String {
    if let slash = field.key.firstIndex(of: "/"),
      let i = Int(field.key[field.key.index(after: slash)...]),
      let list = record[String(field.key[..<slash])] as? [Object], list.indices.contains(i)
    {
      return list[i]["value"] as? String ?? ""
    }
    if field.key == "emergency_name" {
      return (record["name"] as? String ?? "").components(separatedBy: " · ").dropFirst().joined(
        separator: " · ")
    }
    let value = record[field.key]
    if field.key == "bandwidth_code", let code = value as? Int {
      return [0: "12,5", 1: "20", 2: "25"][code] ?? String(code)
    }
    if field.key == "ring" { return (value as? Bool).map { $0 ? "Вкл." : "Выкл." } ?? "" }
    switch field.kind {
    case .tone:
      guard let code = value as? Int else { return "" }
      if code == 65535 { return L("Выкл.") }
      if code & 0x8000 != 0 {
        return String(format: "D%03X", code & 0xfff) + (code & 0x4000 == 0 ? "N" : "I")
      }
      if let n = Int(String(code, radix: 16)) {
        return NSDecimalNumber(decimal: Decimal(n) / 10).stringValue
      }
      return String(format: "0x%04X", code)
    case .frequency:
      guard let hz = value as? Int else { return "" }
      return NSDecimalNumber(decimal: Decimal(hz) / 1_000_000).stringValue
    case .zoneMembers:
      return (value as? [Object] ?? []).compactMap { ($0["index"] as? Int).map(String.init) }
        .joined(separator: ", ")
    case .ids: return (value as? [Int] ?? []).map(String.init).joined(separator: ", ")
    default: return value as? String ?? (value as? Int).map(String.init) ?? ""
    }
  }

  // Editable free text, such as messages and record names, remains verbatim. For
  // typed CPS fields, show the translated unit beside the numeric editor while
  // retaining the canonical unit in the value written back to the draft.
  func textInput(_ field: EditField, id: Int, raw: Binding<String>) -> (value: Binding<String>, unit: String?) {
    guard case .text = field.kind, field.key.contains("/"), !CPSField.isUserText(label: field.name)
    else { return (raw, nil) }
    let initial = stringValue(field, record(RecordKey(table: table, id: id)))
    let markers = ["Не считано", "Выкл.", "Нет", "Выбранный канал", "Непрерывно", "Всегда", "Без ограничения"]
    let parts = initial.split(separator: " ")
    if let last = parts.last, ["Гц", "кГц", "мс", "с", "мин"].contains(String(last)),
       parts.count > 1, parts.dropLast().joined().allSatisfy({ $0.isNumber || $0 == "," || $0 == "." }) {
      let suffix = " " + String(last)
      let value = Binding<String>(get: {
        let text = raw.wrappedValue
        return text.hasSuffix(suffix) ? String(text.dropLast(suffix.count)) : (markers.contains(text) ? L(text) : text)
      }, set: { text in
        raw.wrappedValue = markers.first(where: { L($0) == text }) ?? (text.isEmpty ? "" : text + suffix)
      })
      return (value, L(String(last)))
    }
    if markers.contains(initial) {
      let value = Binding<String>(
        get: { markers.contains(raw.wrappedValue) ? L(raw.wrappedValue) : raw.wrappedValue },
        set: { text in raw.wrappedValue = markers.first(where: { L($0) == text }) ?? text })
      return (value, nil)
    }
    return (raw, nil)
  }

  func binding(_ field: EditField, id: Int) -> Binding<String> {
    let key = RecordKey(table: table, id: id)
    let initial = stringValue(field, record(key))
    return Binding(
      get: { self.pending[key]?[field.key] ?? initial },
      set: { value in
        self.objectWillChange.send()
        self.pending[key, default: [:]][field.key] = value
        if key.table == "section/vfo", field.name == "Режим",
          let index = self.sectionIndex(key.table),
          let changes = self.pending[key]
        {
          do {
            var current = self.sections
            var rows = current[index]["rows"] as? [Object] ?? []
            guard rows.indices.contains(id - 1) else { return }
            rows[id - 1] = try self.apply(changes, key: key, to: rows[id - 1])
            current[index]["rows"] = rows
            self.document["cps_sections"] = current
            self.pending.removeValue(forKey: key)
          } catch {
            self.error = error.localizedDescription
            return
          }
        }
        self.dirty = true
        self.error = nil
      })
  }

  func add(copy: Bool) {
    var list = records(table)
    if isSlotTable {
      var current = sections
      if sectionIndex(table) == nil,
        let title = creatableSections.first(where: { $0.0 == table })?.1
      {
        current.append(["id": String(table.dropFirst(8)), "title": title, "rows": [Object]()])
      }
      guard let section = current.firstIndex(where: { "section/" + ($0["id"] as? String ?? "") == table })
      else { return }
      let used = Set(records(table).compactMap { $0["index"] as? Int })
      let limit = table == "section/messages" ? 50 : (table == "section/gps" ? 16 : 32)
      let slots = partialUSB && table == "section/emergency" ? Array(11...32) + Array(1...10) : Array(1...limit)
      guard let slot = slots.first(where: { !used.contains($0) }) else {
        error = "Все позиции заняты."
        return
      }
      var rows = current[section]["rows"] as? [Object] ?? []
      let title: String
      let fields: [(String, String)]
      switch table {
      case "section/messages":
        title = "Шаблон " + String(slot)
        fields = [("Текст", L("Новое сообщение"))]
      case "section/gps":
        guard
          let c = records("contacts").first(where: { $0["type"] as? String == "private" })
            ?? records("contacts").first(where: {
              ["group", "private", "all"].contains($0["type"] as? String ?? "")
            }),
          let id = c["index"] as? Int
        else {
          error = "Сначала добавьте цифровой контакт."
          return
        }
        title = "GPS " + String(slot)
        fields = [
          ("Период", "30 с"), ("Контакт", "№ " + String(id) + " · " + (c["name"] as? String ?? "")),
          ("Канал", "Выбранный канал"),
        ]
      default:
        title = "№ " + String(slot) + " · " + LF("Система %ld", slot)
        fields = [
          ("Тип", "Обычная"), ("Режим", "Тревога"), ("Повторы без ожидания", "1"),
          ("Повторы с ожиданием", "1"), ("Открытый микрофон", "10 с"), ("Канал", "Выбранный канал"),
        ]
      }
      rows.append(["name": title, "fields": fields.map { ["label": $0.0, "value": $0.1] }])
      current[section]["rows"] = rows
      document["cps_sections"] = current
      dirty = true
      selection = slot
      return
    }
    var used = Set(list.compactMap { $0["index"] as? Int })
    if table == "rx_groups" { used.formUnion(original.settings.unrecognizedRxGroupSlots ?? []) }
    var id = 1
    while used.contains(id) || (removed[table]?.contains(id) == true) { id += 1 }
    var row: Object = ["name": L("Новая запись")]
    if copy, let active {
      let key = RecordKey(table: table, id: active)
      do { row = try apply(pending[key] ?? [:], key: key, to: record(key)) } catch {
        self.error = error.localizedDescription
        return
      }
      if table == "channels", canExportRDT,
        let template = row["rdt_template_index"] as? Int
          ?? original.settings.channels.first(where: { $0.index == active })?.index
      {
        row["rdt_template_index"] = template
      }
      row["name"] = (row["name"] as? String ?? "") + " 2"
    } else {
      switch table {
      case "channels":
        row.merge(["mode": "analog", "bandwidth_code": 0, "rx_hz": NSNull(), "tx_hz": NSNull()]) {
          _, v in v
        }
      case "contacts": row.merge(["type": "group", "dmr_id": 1]) { _, v in v }
      case "zones":
        row["A"] = []
        row["B"] = []
      case "rx_groups": row["contacts"] = []
      case "scan_lists": row["channels"] = []
      default: return
      }
    }
    row["index"] = id
    list.append(row)
    document[table] = list.sorted { ($0["index"] as? Int ?? 0) < ($1["index"] as? Int ?? 0) }
    selection = id
    dirty = true
    error = nil
  }

  func copyRecord(_ id: Int) {
    guard isRecordTable else { return }
    do {
      let key = RecordKey(table: table, id: id)
      let row = try apply(pending[key] ?? [:], key: key, to: record(key))
      var record: Object = ["table": table, "row": row]
      if let source { record["source"] = source.standardizedFileURL.absoluteString }
      let data = try JSONSerialization.data(withJSONObject: record)
      let board = NSPasteboard.general
      board.clearContents()
      board.setData(data, forType: .init("local.tyt-reader.codeplug-record"))
    } catch { self.error = error.localizedDescription }
  }

  func pasteRecord() {
    guard isRecordTable else { return }
    do {
      guard
        let data = NSPasteboard.general.data(forType: .init("local.tyt-reader.codeplug-record")),
        data.count <= 2_000_000,
        let object = try JSONSerialization.jsonObject(with: data) as? Object,
        object["table"] as? String == table, var row = object["row"] as? Object,
        row["name"] is String
      else { throw RadioReader.Failure("В буфере нет записи для этого раздела.") }
      let sourceIndex = row["rdt_template_index"] as? Int ?? row["index"] as? Int
      row.removeValue(forKey: "rdt_template_index")
      if table == "channels", canExportRDT, let source,
        object["source"] as? String == source.standardizedFileURL.absoluteString, let sourceIndex,
        original.settings.channels.contains(where: { $0.index == sourceIndex })
      {
        row["rdt_template_index"] = sourceIndex
      }
      var list = records(table)
      let used = Set(list.compactMap { $0["index"] as? Int })
      var id = 1
      while used.contains(id) || removed[table]?.contains(id) == true { id += 1 }
      row["index"] = id
      list.append(row)
      document[table] = list.sorted { ($0["index"] as? Int ?? 0) < ($1["index"] as? Int ?? 0) }
      selection = id
      dirty = true
      error = nil
    } catch { self.error = error.localizedDescription }
  }

  func deleteSelected() {
    guard let id = active else { return }
    if table == "section/emergency" && id == 0 {
      error = "Общие параметры принадлежат всем аварийным системам."
      return
    }
    if isSlotTable, let section = sectionIndex(table) {
      var current = sections
      current[section]["rows"] = records(table).filter { $0["index"] as? Int != id }.map { row in
        var result = row
        result.removeValue(forKey: "index")
        return result
      }
      document["cps_sections"] = current
      pending.removeValue(forKey: RecordKey(table: table, id: id))
      dirty = true
      selection = nil
      return
    }
    guard isRecordTable else { return }
    document[table] = records(table).filter { $0["index"] as? Int != id }
    removed[table, default: []].insert(id)
    pending.removeValue(forKey: RecordKey(table: table, id: id))
    selection = nil
    dirty = true
    error = nil
  }

  private func apply(_ changes: [String: String], key: RecordKey, to original: Object) throws
    -> Object
  {
    var row = original
    for field in fieldList(key, original) {
      guard let string = changes[field.key] else { continue }
      let text = string.trimmingCharacters(in: .whitespacesAndNewlines)
      func invalid() -> RadioReader.Failure {
        RadioReader.Failure(LF("№ %ld · %@. Проверьте значение.", key.id, LD(field.name)))
      }
      switch field.kind {
      case .text, .choice:
        if let slash = field.key.firstIndex(of: "/"),
          let index = Int(field.key[field.key.index(after: slash)...])
        {
          let property = String(field.key[..<slash])
          var list = row[property] as? [Object] ?? []
          guard list.indices.contains(index) else { throw invalid() }
          list[index]["value"] = string
          row[property] = list
        } else {
          if field.key == "name" && text.isEmpty { throw invalid() }
          if field.key == "bandwidth_code" {
            guard let code = ["12,5": 0, "20": 1, "25": 2][text] else { throw invalid() }
            row[field.key] = code
          } else if field.key == "emergency_name" {
            guard !text.isEmpty else { throw invalid() }
            row["name"] = "№ " + String(key.id) + " · " + text
          } else if field.key == "ring" {
            row[field.key] = string == "Вкл."
          } else {
            row[field.key] = string
          }
        }
      case .number:
        if text.isEmpty
          && !["dmr_id", "color_code", "time_slot", "bandwidth_code"].contains(field.key)
        {
          row[field.key] = NSNull()
          continue
        }
        guard let value = Int(text), value >= 0, value <= 0xffffff else { throw invalid() }
        if field.key == "encode_slot" && value > 32 { throw invalid() }
        if field.key == "time_slot" && !(1...2).contains(value) { throw invalid() }
        if field.key == "privacy_type_code" && value > 3 { throw invalid() }
        if field.key == "privacy_index_code" {
          let type =
            changes["privacy_type_code"].flatMap(Int.init) ?? original["privacy_type_code"] as? Int
          if value > (type == 3 ? 255 : 15) { throw invalid() }
        }
        if field.key == "color_code" && value > 15 { throw invalid() }
        if field.key == "bandwidth_code" && value > 2 { throw invalid() }
        if field.key.hasSuffix("tone_code") && value > 65535 { throw invalid() }
        row[field.key] = value
      case .tone:
        let token = text.uppercased().replacingOccurrences(of: ",", with: ".")
        if token.isEmpty || ["ВЫКЛ.", "ВЫКЛ", "OFF", "NONE", L("Выкл.").uppercased()].contains(token) {
          row[field.key] = 65535
        } else if token.range(of: #"^D[0-7]{3}[NI]$"#, options: .regularExpression) != nil,
          let digits = Int(String(token.dropFirst().dropLast()), radix: 16)
        {
          row[field.key] = digits | (token.hasSuffix("I") ? 0xc000 : 0x8000)
        } else if token.range(of: #"^[0-9]{2,3}(\.[0-9])?$"#, options: .regularExpression) != nil,
          let tone = Decimal(string: token, locale: Locale(identifier: "en_US_POSIX")),
          tone >= 60 && tone <= 260,
          let code = Int(NSDecimalNumber(decimal: tone * 10).stringValue, radix: 16)
        {
          row[field.key] = code
        } else {
          throw RadioReader.Failure(
            "Субтон: введите «Выкл.», частоту CTCSS (например 82,5) или код DCS (например D023N).")
        }
      case .frequency:
        let decimal = text.replacingOccurrences(of: ",", with: ".")
        guard
          decimal.range(of: #"^[0-9]{1,6}(\.[0-9]{1,6})?$"#, options: .regularExpression) != nil,
          let mhz = Decimal(string: decimal, locale: Locale(identifier: "en_US_POSIX")), mhz > 0
        else { throw invalid() }
        row[field.key] = NSDecimalNumber(decimal: mhz * 1_000_000).int64Value
      case .ids, .zoneMembers:
        let tokens = text.split { $0 == "," || $0 == ";" || $0.isWhitespace }
        let ids = tokens.compactMap { Int($0) }
        guard ids.count == tokens.count, ids.allSatisfy({ $0 >= 0 }) else { throw invalid() }
        if case .zoneMembers = field.kind {
          row[field.key] = ids.map { ["index": $0] }
        } else {
          row[field.key] = ids
        }
      }
    }
    if key.table == "section/vfo", var fields = row["fields"] as? [Object],
      let updated = fields.first(where: { $0["label"] as? String == "Режим" })?["value"] as? String,
      let previous = (original["fields"] as? [Object])?.first(where: {
        $0["label"] as? String == "Режим"
      })?["value"] as? String,
      updated != previous
    {
      let digital = [("Color Code", "1"), ("Time Slot", "1"), ("Контакт", "Нет"), ("Группа приёма", "Нет")]
      let analog = [("Полоса", "12,5 кГц"), ("Субтон приёма", "Выкл."), ("Субтон передачи", "Выкл.")]
      guard ["DMR", "Аналог"].contains(updated) else { throw RadioReader.Failure("Неизвестный режим VFO.") }
      let modeFields = Set((digital + analog).map { $0.0 })
      fields.removeAll { modeFields.contains($0["label"] as? String ?? "") }
      fields += (updated == "DMR" ? digital : analog).map { ["label": $0.0, "value": $0.1] }
      row["fields"] = fields
    }
    if key.table == "channels", let mode = changes["mode"], mode != original["mode"] as? String {
      if mode == "DMR" {
        row["color_code"] = row["color_code"] ?? 1
        row["time_slot"] = row["time_slot"] ?? 1
      }
      for k in mode == "DMR"
        ? ["rx_tone_code", "tx_tone_code"]
        : [
          "color_code", "time_slot", "contact_index", "rx_group_index", "privacy_type_code",
          "privacy_index_code",
        ]
      {
        row.removeValue(forKey: k)
      }
      row.removeValue(forKey: "details")
    }
    return row
  }

  func importCSV() {
    let panel = NSOpenPanel()
    panel.allowedContentTypes = [.commaSeparatedText]
    panel.message = L("Совпадающие номера будут обновлены в черновике. Остальные записи сохранятся.")
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do {
      let current = try materialize()
      saving = true
      Task {
        defer { saving = false }
        do {
          let merged = try await Task.detached { try RadioReader.mergeCSV(url, file: current) }
            .value
          guard let object = try JSONSerialization.jsonObject(with: merged.data) as? Object else {
            throw RadioReader.Failure("Некорректный результат импорта CSV.")
          }
          document = object
          pending.removeAll()
          dirty = true
          error = nil
          if !merged.settings.channels.isEmpty && current.settings.channels.isEmpty {
            table = "channels"
          }
          selection = nil
        } catch { self.error = error.localizedDescription }
      }
    } catch { self.error = error.localizedDescription }
  }

  func materialize() throws -> SettingsFile {
    var result = document
    var sections = self.sections
    var emptySlots = localEmptySlots
    func markAssignment(_ key: RecordKey, empty: Bool) {
      guard !buttonBlockWasRead else { return }
      var slots = Set(emptySlots[key.table] ?? [])
      if empty { slots.insert(key.id) } else { slots.remove(key.id) }
      emptySlots[key.table] = slots.isEmpty ? nil : slots.sorted()
    }
    for (table, _) in choices where table != "quick" && table != "numberKeys" {
      var list = records(table)
      for i in list.indices {
        guard let index = list[i]["index"] as? Int else { continue }
        let key = RecordKey(table: table, id: index)
        if let edit = pending[key] { list[i] = try apply(edit, key: key, to: list[i]) }
      }
      if let i = sectionIndex(table) {
        sections[i]["rows"] = list.map {
          var r = $0
          r.removeValue(forKey: "index")
          return r
        }
      } else if table == "radio" {
        result["dmr_id"] = list.first?["dmr_id"]
      } else if document[table] != nil {
        result[table] = list
      }
    }
    for (key, changes) in pending where key.table == "numberKeys" {
      let r = try apply(changes, key: key, to: record(key))
      let name = "Цифровая клавиша · позиция " + String(key.id)
      let contactID = r["contact_index"] as? Int ?? 0
      markAssignment(key, empty: contactID == 0)
      if !sections.contains(where: { $0["id"] as? String == "buttons" }) {
        sections.append(["id": "buttons", "title": "Кнопки", "rows": [Object]()])
      }
      guard let i = sections.firstIndex(where: { $0["id"] as? String == "buttons" }) else { continue }
      var rows = (sections[i]["rows"] as? [Object] ?? []).filter { $0["name"] as? String != name }
      if contactID > 0 {
        guard let contact = (result["contacts"] as? [Object] ?? []).first(where: { $0["index"] as? Int == contactID })
        else { throw RadioReader.Failure(LF("%@. Выберите существующий контакт.", LD(name))) }
        let value = "№ " + String(contactID) + " · " + (contact["name"] as? String ?? "")
        rows.append(["name": name, "fields": [["label": "Контакт", "value": value]]])
      }
      sections[i]["rows"] = rows
    }
    for (key, changes) in pending where key.table == "quick" {
      let r = try apply(changes, key: key, to: record(key))
      let name = "Быстрый доступ " + String(key.id)
      let action = r["quick_type"] as? String ?? "Нет данных"
      if action == "Нет данных" { continue }
      markAssignment(key, empty: action == "Не назначено")
      for i in sections.indices
      where ["dtmf", "buttons"].contains(sections[i]["id"] as? String ?? "") {
        sections[i]["rows"] = (sections[i]["rows"] as? [Object] ?? []).filter {
          $0["name"] as? String != name
        }
      }
      if action == "Не назначено" { continue }
      let dtmf = action == "DTMF"
      let target = dtmf ? "dtmf" : "buttons"
      if !sections.contains(where: { $0["id"] as? String == target }) {
        sections.append(["id": target, "title": dtmf ? "DTMF" : "Кнопки", "rows": [Object]()])
      }
      guard let i = sections.firstIndex(where: { $0["id"] as? String == target }) else { continue }
      var fields: [[String: String]]
      if dtmf {
        fields = [["label": "Профиль", "value": r["profile"] as? String ?? "DTMF-1"]]
        let prior = self.sections.first { $0["id"] as? String == "dtmf" }?["rows"] as? [Object] ?? []
        let oldFields = prior.first { $0["name"] as? String == name }?["fields"] as? [Object] ?? []
        if changes["encode_slot"] != nil || oldFields.contains(where: { $0["label"] as? String == "Номер Encode" }) {
          fields.append(["label": "Номер Encode", "value": String(r["encode_slot"] as? Int ?? 0)])
        }
      } else {
        let id = r["contact_index"] as? Int ?? 0
        guard
          let contact = (result["contacts"] as? [Object] ?? []).first(where: {
            $0["index"] as? Int == id
          })
        else {
          throw RadioReader.Failure(LF("%@. Выберите существующий контакт.", LD(name)))
        }
        fields = [
          ["label": "Действие", "value": action],
          [
            "label": "Контакт",
            "value": "№ " + String(id) + " · " + (contact["name"] as? String ?? ""),
          ],
        ]
        if action == "Сообщение" {
          fields.append(["label": "Шаблон", "value": String(r["message_slot"] as? Int ?? 1)])
        }
      }
      var rows = sections[i]["rows"] as? [Object] ?? []
      rows.append(["name": name, "fields": fields])
      sections[i]["rows"] = rows
    }
    if !sections.isEmpty || document["cps_sections"] != nil { result["cps_sections"] = sections }
    if !emptySlots.isEmpty { result["local_empty_slots"] = emptySlots }
    else { result.removeValue(forKey: "local_empty_slots") }
    let channels = result["channels"] as? [Object] ?? []
    let channelNames = Dictionary(
      channels.compactMap { r -> (Int, String)? in
        guard let i = r["index"] as? Int, let name = r["name"] as? String else { return nil }
        return (i, name)
      }, uniquingKeysWith: { first, _ in first })
    if var zones = result["zones"] as? [Object] {
      for i in zones.indices {
        for side in ["A", "B"] {
          zones[i][side] = (zones[i][side] as? [Object] ?? []).map { r in
            var m = r
            if let id = r["index"] as? Int, let name = channelNames[id] { m["name"] = name }
            return m
          }
        }
      }
      result["zones"] = zones
    }
    try validateReferences(result)
    return try SettingsFile(
      data: JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]))
  }

  private func validateReferences(_ result: Object) throws {
    let known = Dictionary(
      uniqueKeysWithValues: tables.map { table, _ in
        (table, Set((result[table] as? [Object] ?? []).compactMap { $0["index"] as? Int }))
      })
    for (source, field, target, selected) in [
      ("channels", "scan_list_index", "scan_lists", false),
      ("channels", "contact_index", "contacts", false),
      ("channels", "rx_group_index", "rx_groups", false), ("zones", "A", "channels", false),
      ("zones", "B", "channels", false),
      ("rx_groups", "contacts", "contacts", false), ("scan_lists", "channels", "channels", true),
      ("scan_lists", "primary", "channels", true), ("scan_lists", "secondary", "channels", true),
      ("scan_lists", "revert", "channels", true),
    ] {
      for row in result[source] as? [Object] ?? [] {
        guard let id = row["index"] as? Int else { continue }
        let value = row[field]
        let refs =
          value as? [Int] ?? (value as? [Object])?.compactMap { $0["index"] as? Int }
          ?? (value as? Int).map { [$0] } ?? []
        for ref in refs where ref != 0 || !selected {
          if known[target]?.contains(ref) != true
            && (removed[target]?.contains(ref) == true
              || pending[RecordKey(table: source, id: id)]?[field] != nil)
          {
            throw RadioReader.Failure(
              LF("№ %ld, %@: ссылка на отсутствующую запись № %ld. Исправьте ссылку перед сохранением.", id, field, ref)
            )
          }
        }
      }
    }
  }
}

struct CodeplugEditor: View {
  @ObservedObject var draft: CodeplugDraft
  let save: (SettingsFile, URL) -> Void
  @Environment(\.dismiss) private var dismiss
  private struct Members: Identifiable {
    let id = UUID()
    let field: EditField
    let row: Int
    let candidates: [(Int, String)]
  }
  @State private var members: Members?
  @State private var discard = false
  @State private var filter = ""
  @State private var exportedSequenceFile: String?

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      HStack {
        Text(L(draft.table == "section/dtmf_sequences" ? "Последовательности DTMF" : "Редактор кодплага")).font(.title2.weight(.semibold))
        if !draft.sequencesOnly { Button("Импорт CSV…") { draft.importCSV() } }
        Spacer()
        Picker(
          "Раздел",
          selection: Binding(
            get: { draft.table },
            set: { table in
              guard draft.table != table else { return }
              draft.selection = nil
              filter = ""
              draft.table = table
            })
        ) {
          ForEach(draft.choices, id: \.0) { Text(L($0.1)).tag($0.0) }
        }.fixedSize()
      }
      Divider()
      HSplitView {
        VStack(alignment: .leading) {
          if let note = draft.slotSourceNote {
            Text(L(note)).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
          }
          TextField("Найти запись", text: $filter)
          List(selection: $draft.selection) {
            ForEach(
              draft.rows.filter {
                filter.isEmpty || $0.1.localizedCaseInsensitiveContains(filter)
                  || String($0.0) == filter
              }, id: \.0
            ) { row in
              Text("\(row.0) · \(row.1)").tag(row.0)
                .contextMenu {
                  if draft.isRecordTable {
                    Button("Копировать запись") { draft.copyRecord(row.0) }
                    Button("Вставить как новую запись") { draft.pasteRecord() }
                  }
                }
            }
          }
          if draft.isRecordTable || draft.isSlotTable {
            HStack {
              Button {
                draft.add(copy: false)
              } label: {
                Image(systemName: "plus")
              }.help("Добавить запись").accessibilityLabel("Добавить запись")
              if !draft.isSlotTable {
                Button {
                  draft.pasteRecord()
                } label: {
                  Image(systemName: "doc.on.clipboard")
                }
                .help("Вставить запись TYT Reader").accessibilityLabel("Вставить запись")
                Button {
                  draft.add(copy: true)
                } label: {
                  Image(systemName: "plus.square.on.square")
                }.help("Дублировать").accessibilityLabel("Дублировать").disabled(
                  draft.active == nil)
              }
              Button {
                draft.deleteSelected()
              } label: {
                Image(systemName: "minus")
              }.help("Удалить из копии").accessibilityLabel("Удалить из копии").disabled(
                draft.active == nil)
            }
          }
        }.padding(.trailing, 10).frame(minWidth: 155, idealWidth: 210)
        ScrollView {
          if let id = draft.active {
            Form {
              if ["zones", "rx_groups", "scan_lists"].contains(draft.table) {
                Text("Введите номера участников через запятую в нужном порядке.").font(.callout)
                  .foregroundStyle(.secondary)
              }
              ForEach(draft.fields, id: \.key) { field in
                let value = draft.binding(field, id: id)
                if draft.table == "section/dtmf_sequences" && value.wrappedValue == "Служебный профиль CPS" {
                  LabeledContent(L(field.name), value: L("Служебный профиль CPS"))
                } else if case .choice(let values) = field.kind {
                  Picker(LD(field.name), selection: value) {
                    ForEach(
                      values.contains(value.wrappedValue) ? values : [value.wrappedValue] + values,
                      id: \.self
                    ) { Text(field.choiceLabel($0)).tag($0) }
                  }
                } else if let candidates = draft.candidates(for: field.key) {
                  HStack {
                    TextField(LD(field.name), text: value).accessibilityLabel(LD(field.name))
                    Button("Выбрать…") {
                      members = Members(field: field, row: id, candidates: candidates)
                    }
                    .accessibilityLabel(LF("Выбрать: %@", LD(field.name)))
                  }
                } else {
                  let input = draft.textInput(field, id: id, raw: value)
                  HStack {
                    TextField(LD(field.name), text: input.value).accessibilityLabel(LD(field.name))
                    if let unit = input.unit { Text(unit).foregroundStyle(.secondary) }
                  }
                }
              }
            }.formStyle(.grouped)
          }
        }.frame(minWidth: 340, maxWidth: .infinity)
      }
      if let error = draft.error { Text(LD(error)).foregroundStyle(.red).textSelection(.enabled) }
      if let exportedSequenceFile { Text(LF("Сохранена копия DTMF: %@", exportedSequenceFile)).font(.callout).foregroundStyle(.secondary) }
      HStack {
        Button("Закрыть") { if draft.dirty { discard = true } else { dismiss() } }.keyboardShortcut(
          .cancelAction)
        Spacer()
        if draft.saving { ProgressView().controlSize(.small) }
        if draft.table == "section/dtmf_sequences", !draft.sequencesOnly, !draft.canExportRDT {
          Button("Только DTMF в RDT…") { saveCopy(rdt: true, exportSequences: true) }
        }
        Button("Сохранить JSON…") { saveCopy(rdt: false) }
        Button(L(draft.canExportRDT ? "Сохранить копию RDT…" : "RDT по шаблону…")) {
          saveCopy(rdt: true)
        }
        .buttonStyle(.borderedProminent)
      }
    }
    .padding(18).frame(minWidth: 680, minHeight: 430).frame(idealWidth: 820, idealHeight: 530)
    .disabled(draft.saving).interactiveDismissDisabled(draft.dirty || draft.saving)
    .sheet(item: $members) { config in
      MembersEditor(
        title: LD(config.field.name), candidates: config.candidates,
        multiple: ["A", "B", "channels", "contacts"].contains(config.field.key),
        value: draft.binding(config.field, id: config.row))
    }
    .confirmationDialog("Закрыть редактор и отбросить изменения?", isPresented: $discard) {
      Button("Отбросить изменения", role: .destructive) { dismiss() }
      Button("Продолжить редактирование", role: .cancel) {}
    }
  }

  private func saveCopy(rdt: Bool, exportSequences: Bool = false) {
    do {
      let edited = try draft.materialize()
      var chosen: URL?
      if rdt && !draft.canExportRDT {
        let picker = NSOpenPanel()
        picker.allowedContentTypes = [UTType(filenameExtension: "rdt") ?? .data]
        picker.message = L(exportSequences || draft.sequencesOnly
          ? "Выберите исходный RDT. В новый файл перейдут доступные последовательности DTMF. Непрочитанные позиции и остальные настройки наследуются от выбранного файла."
          : "Выберите RDT для новой копии. Из документа переносятся DMR ID, основные поля пяти таблиц и известные последовательности DTMF. Остальные разделы берутся из шаблона.")
        guard picker.runModal() == .OK, let url = picker.url else { return }
        chosen = url
      }
      let template = chosen
      let panel = NSSavePanel()
      let ext = rdt ? "rdt" : "json"
      panel.allowedContentTypes = [UTType(filenameExtension: ext) ?? .data]
      panel.nameFieldStringValue =
        LF("%@ — копия.%@", draft.source?.deletingPathExtension().lastPathComponent ?? L("Настройки"), ext)
      guard panel.runModal() == .OK, let url = panel.url else { return }
      if let source = draft.source,
        url.standardizedFileURL.resolvingSymlinksInPath()
          == source.standardizedFileURL.resolvingSymlinksInPath()
      {
        throw RadioReader.Failure("Выберите новое имя для копии исходного файла.")
      }
      draft.saving = true
      draft.error = nil
      let source = draft.source
      let baseline = draft.original
      Task {
        defer { draft.saving = false }
        do {
          let saved = try await Task.detached(priority: .userInitiated) {
            if let template {
              var normalized = try RadioReader.normalized(baseline: baseline, edited: edited)
              if exportSequences {
                guard var object = try JSONSerialization.jsonObject(with: normalized.data) as? Object else {
                  throw RadioReader.Failure("Некорректный документ DTMF.")
                }
                object["source_format"] = "dtmf"
                normalized = try SettingsFile(data: JSONSerialization.data(withJSONObject: object))
              }
              try RadioReader.exportFromTemplate(
                source: template, destination: url, snapshot: normalized)
              return try SettingsFile.open(url)
            }
            if rdt, let source {
              try RadioReader.exportRDT(
                source: source, destination: url, baseline: baseline, edited: edited)
              return try SettingsFile.open(url)
            }
            let normalized = try RadioReader.normalized(baseline: baseline, edited: edited)
            try normalized.save(to: url)
            return normalized
          }.value
          if exportSequences {
            exportedSequenceFile = url.lastPathComponent
          } else {
            save(saved, url)
            dismiss()
          }
        } catch { draft.error = error.localizedDescription }
      }
    } catch { draft.error = error.localizedDescription }
  }
}

private struct MembersEditor: View {
  let title: String
  let candidates: [(Int, String)]
  let multiple: Bool
  @Binding var value: String
  @Environment(\.dismiss) private var dismiss
  @State private var search = ""
  @State private var available: Int?
  @State private var position: Int?
  @State private var selected: [Int] = []

  var body: some View {
    let names = Dictionary(candidates, uniquingKeysWith: { first, _ in first })
    VStack(alignment: .leading, spacing: 10) {
      Text(title).font(.headline)
      HStack(alignment: .top, spacing: 12) {
        VStack(alignment: .leading) {
          TextField("Найти номер или название", text: $search)
          List(selection: $available) {
            ForEach(
              candidates.filter {
                search.isEmpty || $0.1.localizedCaseInsensitiveContains(search)
                  || String($0.0) == search
              }, id: \.0
            ) { item in
              Text("\(item.0) · \(item.1)").tag(item.0)
            }
          }
          Button(L(multiple ? "Добавить →" : "Назначить →")) {
            if let available {
              if multiple {
                if !selected.contains(available) { selected.append(available) }
              } else {
                selected = [available]
              }
            }
          }.disabled(available == nil)
        }
        VStack(alignment: .leading) {
          Text(L(multiple ? "В порядке следования" : "Назначение")).font(.subheadline)
          List(selection: $position) {
            ForEach(Array(selected.enumerated()), id: \.offset) { item in
              Text("\(item.element) · " + (names[item.element] ?? L("Запись отсутствует"))).tag(
                item.offset)
            }
          }
          HStack {
            Button("Убрать") {
              if let i = position, selected.indices.contains(i) {
                selected.remove(at: i)
                position = nil
              }
            }.disabled(position == nil)
            if multiple {
              Button("↑") { move(-1) }.accessibilityLabel("Выше").disabled((position ?? 0) < 1)
              Button("↓") { move(1) }.accessibilityLabel("Ниже").disabled(
                (position ?? selected.count) >= selected.count - 1)
            }
          }
        }
      }
      HStack {
        Spacer()
        Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction)
        Button("Применить") {
          value = selected.map(String.init).joined(separator: ", ")
          dismiss()
        }.keyboardShortcut(.defaultAction)
      }
    }
    .padding(16).frame(minWidth: 540, minHeight: 350)
    .onAppear {
      selected = value.split { $0 == "," || $0 == ";" || $0.isWhitespace }.compactMap { Int($0) }
    }
  }

  private func move(_ step: Int) {
    guard let i = position, selected.indices.contains(i + step) else { return }
    selected.swapAt(i, i + step)
    position = i + step
  }
}
