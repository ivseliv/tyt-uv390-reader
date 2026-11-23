import AppKit
import SwiftUI

struct CPSField: Codable, Sendable {
  let label: String
  let value: String

  static func isUserText(label: String) -> Bool {
    switch label {
    case "Имя", "Имя рации", "Название", "Позывной", "Текст", "Строка 1", "Строка 2",
         "Сообщение", "Отправитель", "Получатель", "Путь APRS", "Серийный номер",
         "Модель", "Модель USB", "Модель файла":
      true
    default:
      false
    }
  }

  var displayValue: String {
    value.hasPrefix("№ ") || Self.isUserText(label: label) ? value : LD(value)
  }
}

struct CPSRow: Codable, Sendable {
  let name: String
  let fields: [CPSField]

  static func displayName(_ name: String) -> String {
    // Numbered names can include user-provided channel or emergency system names.
    name.hasPrefix("№ ") ? name : LD(name)
  }

  var displayName: String { Self.displayName(name) }
}

struct CPSSection: Codable, Identifiable, Sendable {
  let id: String
  let title: String
  let rows: [CPSRow]
}

private let directory = [
  ("basic", "Сведения о рации"), ("general", "Общие настройки"), ("menu", "Меню рации"),
  ("buttons", "Назначения кнопок"), ("messages", "Шаблоны сообщений"), ("privacy", "Privacy"),
  ("emergency", "Аварийная сигнализация"), ("contacts", "Контакты"), ("rx_groups", "Группы приёма"),
  ("zones", "Зоны A/B"), ("scan_lists", "Списки сканирования"), ("channels", "Каналы"),
  ("dtmf", "Сигнализация DTMF"), ("vfo", "VFO A/B"), ("gps", "Позиционирование GPS"),
  ("lone_worker", "Одиночный работник"), ("scan_timing", "Таймеры сканирования"),
]

extension RadioSettings {
  var navigation: [(String, String)] {
    let known = Set(directory.map(\.0))
    return directory
      + (cpsSections ?? []).filter { !known.contains($0.id) && !["dtmf_systems", "dtmf_sequences"].contains($0.id) }.map {
        ($0.id, $0.title)
      }
  }

  func title(for id: String) -> String { L(navigation.first { $0.0 == id }?.1 ?? id) }

  func section(_ id: String) -> CPSSection? {
    var rows = cpsSections?.first { $0.id == id }?.rows ?? []
    switch id {
    case "basic":
      if rows.isEmpty {
        var fields: [CPSField] = []
        if let model = usbModel ?? fileModel {
          fields.append(.init(label: usbModel == nil ? "Модель файла" : "Модель USB", value: model))
        }
        if let dmrId { fields.append(.init(label: "DMR ID", value: String(dmrId))) }
        if !fields.isEmpty { rows.append(.init(name: "Идентификация", fields: fields)) }
      }
      rows.append(
        .init(
          name: "Содержимое кодплага",
          fields: [
            .init(label: "Каналы", value: String(channels.count)),
            .init(label: "Зоны", value: String(zones.count)),
            .init(label: "Контакты", value: String(contacts.count)),
          ]))
      if let readNotes, !readNotes.isEmpty {
        rows.append(
          .init(
            name: "Примечания к чтению", fields: readNotes.map { .init(label: "Данные", value: $0) }))
      }
    case "privacy":
      rows = channels.compactMap { ch in
        guard let code = ch.privacyTypeCode else { return nil }
        var fields = [
          CPSField(
            label: "Тип Privacy в записи канала",
            value: [0: "Выкл.", 1: "Basic", 2: "Enhanced", 3: "Universal"][code] ?? "Код \(code)")
        ]
        if code != 0, let index = ch.privacyIndexCode {
          fields.append(.init(label: "Назначенный слот", value: String(index + 1)))
        }
        if let flag = ch.details?.first(where: { $0.label == "Privacy Switch" }) {
          fields.append(flag)
        }
        return .init(name: "№ \(ch.index) · \(ch.name)", fields: fields)
      }
    case "dtmf":
      rows += cpsSections?.first { $0.id == "dtmf_systems" }?.rows ?? []
      rows += cpsSections?.first { $0.id == "dtmf_sequences" }?.rows.filter {
        $0.fields.contains { !["", "Не считано", "Служебный профиль CPS"].contains($0.value) }
      } ?? []
      rows += channels.compactMap { ch in
        let fields = (ch.details ?? []).filter { $0.label.contains("DTMF") && $0.value != "0" }
        return fields.isEmpty ? nil : .init(name: "№ \(ch.index) · \(ch.name)", fields: fields)
      }
    default: break
    }
    return rows.isEmpty ? nil : CPSSection(id: id, title: title(for: id), rows: rows)
  }
}

struct CPSNavigation: View {
  let settings: RadioSettings
  @Binding var selection: String
  var body: some View {
    List(
      selection: Binding<String?>(get: { selection }, set: { if let id = $0 { selection = id } })
    ) {
      ForEach(settings.navigation, id: \.0) { item in
        Text(L(item.1)).tag(item.0)
      }
    }
    .listStyle(.sidebar).accessibilityLabel("Разделы CPS")
  }
}

struct CPSSectionPanel: View {
  let section: CPSSection?
  let title: String
  @State private var filter = ""
  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      Text(title).font(.headline)
      if let section {
        if section.rows.count > 5 { TextField("Найти в разделе", text: $filter) }
        ScrollView {
          LazyVStack(alignment: .leading, spacing: 14) {
            ForEach(
              Array(section.rows.enumerated()).filter { _, row in
                filter.isEmpty || row.displayName.localizedCaseInsensitiveContains(filter)
                  || row.fields.contains {
                    LD($0.label).localizedCaseInsensitiveContains(filter)
                      || $0.displayValue.localizedCaseInsensitiveContains(filter)
                  }
              }, id: \.offset
            ) { _, row in
              VStack(alignment: .leading, spacing: 5) {
                Text(row.displayName).font(.subheadline.weight(.semibold))
                ForEach(Array(row.fields.enumerated()), id: \.offset) { _, field in
                  HStack(alignment: .firstTextBaseline, spacing: 15) {
                    Text(LD(field.label)).foregroundStyle(.secondary).frame(
                      maxWidth: .infinity, alignment: .leading)
                    Text(field.displayValue).frame(maxWidth: .infinity, alignment: .leading)
                  }.textSelection(.enabled)
                }
              }
              Divider()
            }
          }.padding(.trailing, 8)
        }
      } else {
        Text("В открытом источнике нет данных этого раздела.").foregroundStyle(.secondary)
        Spacer()
      }
    }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)

  }
}

private func tone(_ code: Int) -> String {
  if code == 0xffff { return "Выкл." }
  let digits = String(format: "%03X", code & 0xfff)
  if code & 0x8000 != 0 { return "DCS " + digits + (code & 0x4000 == 0 ? " N" : " I") }
  guard let n = Int(String(code, radix: 16)) else { return LF("Код %ld", code) }
  return LF("CTCSS %.1f Гц", Double(n) / 10)
}

private func channelFields(_ c: RadioSettings.Channel, _ s: RadioSettings) -> [CPSField] {
  var result: [CPSField] = []
  func add(_ title: String, _ value: String?) {
    if let value { result.append(.init(label: title, value: value)) }
  }
  func reference(_ id: Int?, _ lookup: (Int) -> String?) -> String? {
    id.map { i in "№ \(i)" + (lookup(i).map { " · " + $0 } ?? "") }
  }
  add("Контакт", reference(c.contactIndex) { id in s.contacts.first { $0.index == id }?.name })
  add(
    "Группа приёма", reference(c.rxGroupIndex) { id in s.rxGroups?.first { $0.index == id }?.name })
  add(
    "Список сканирования",
    reference(c.scanListIndex) { id in s.scanLists?.first { $0.index == id }?.name })
  add("Субтон приёма", c.rxToneCode.map(tone))
  add("Субтон передачи", c.txToneCode.map(tone))
  add("Тип Privacy (код)", c.privacyTypeCode.map(String.init))
  if c.privacyTypeCode != nil && c.privacyTypeCode != 0 {
    add("Слот Privacy", c.privacyIndexCode.map { String($0 + 1) })
  }
  return result + (c.details ?? [])
}

struct ChannelPanel: View {
  let settings: RadioSettings
  @State private var filter = ""
  @State private var selection: Int?
  var body: some View {
    let visible =
      filter.isEmpty
      ? settings.channels
      : settings.channels.filter {
        $0.name.localizedCaseInsensitiveContains(filter) || String($0.index) == filter
      }
    VStack(alignment: .leading, spacing: 6) {
      TextField("Найти канал по номеру или названию", text: $filter)
      VSplitView {
        ChannelTable(channels: visible, selected: $selection).frame(minHeight: 90)
        if let id = selection, let channel = visible.first(where: { $0.index == id }) {
          let fields = channelFields(channel, settings)
          if !fields.isEmpty {
            CPSSectionPanel(
              section: .init(
                id: "channel", title: "",
                rows: [.init(name: LF("Параметры канала № %ld", id), fields: fields)]),
              title: channel.name
            )
            .frame(minHeight: 88, idealHeight: 180).padding(.top, 6)
          }
        }
      }
    }
  }
}

@MainActor
func printCodeplug(_ settings: RadioSettings, title: String) {
  var lines = [title]
  for (id, sectionTitle) in settings.navigation {
    var records: [String] = []
    switch id {
    case "channels":
      records = settings.channels.map { c in
        let rx = c.rxHz.map(String.init) ?? "—"
        let tx = c.txHz.map(String.init) ?? "—"
        return LF("№ %ld  %@  ·  %@  ·  RX %@ Гц  ·  TX %@ Гц", c.index, c.name, L(c.modeLabel), rx, tx)
      }
    case "contacts":
      records = settings.contacts.map {
        "№ \($0.index)  \($0.name)  ·  \(L($0.typeLabel))  ·  \($0.address)"
      }
    case "zones":
      records = settings.zones.map { z in
        "\(z.name)\n  A   " + z.sideA.map { String($0.index) }.joined(separator: ", ") + "\n  B   "
          + z.sideB.map { String($0.index) }.joined(separator: ", ")
      }
    case "rx_groups":
      records = (settings.rxGroups ?? []).map {
        $0.name + "   " + $0.contacts.map(String.init).joined(separator: ", ")
      }
    case "scan_lists":
      records = (settings.scanLists ?? []).map {
        $0.name + "   " + $0.channels.map(String.init).joined(separator: ", ")
      }
    default:
      records = (settings.section(id)?.rows ?? []).map { r in
        r.displayName + "\n" + r.fields.map { "  " + LD($0.label) + "   " + $0.displayValue }.joined(separator: "\n")
      }
    }
    if !records.isEmpty {
      lines.append("\n" + L(sectionTitle))
      lines.append(contentsOf: records)
    }
  }
  let info = NSPrintInfo.shared.copy() as! NSPrintInfo
  info.horizontalPagination = .fit
  info.verticalPagination = .automatic
  let width = max(100, info.paperSize.width - info.leftMargin - info.rightMargin)
  let text = NSTextView(frame: NSRect(x: 0, y: 0, width: width, height: 50))
  text.font = NSFont.systemFont(ofSize: 10)
  text.string = lines.joined(separator: "\n")
  text.isVerticallyResizable = true
  text.textContainer?.containerSize = NSSize(width: width, height: CGFloat.greatestFiniteMagnitude)
  if let tc = text.textContainer, let lm = text.layoutManager {
    lm.ensureLayout(for: tc)
    text.setFrameSize(NSSize(width: width, height: ceil(lm.usedRect(for: tc).height) + 12))
  }
  let operation = NSPrintOperation(view: text, printInfo: info)
  operation.jobTitle = title
  operation.run()
}
