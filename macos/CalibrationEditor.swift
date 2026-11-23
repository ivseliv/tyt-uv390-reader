import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

// TestModeText row numbers and file-relative offsets from the MD-UV390 CPS 2.42
// Test Mode grid and its .test serializer. Each base below addresses the first
// displayed point. Frequency cells store four packed decimal bytes, 10 Hz/LSB.
struct CalibrationField: Identifiable {
  let id: Int
  let band: String
  let title: String
  let base: Int
  let count: Int
  var frequency: Bool { (1...4).contains(id) }
  var measurement: Bool { id > 46 }
  var points: [Int] { count == 1 ? [5] : Array(1...count) }
  func offset(_ point: Int) -> Int { base + (count == 1 ? 0 : point - 1) * (frequency ? 8 : 1) }
  func key(_ point: Int) -> String { "\(id).\(point)" }
  var unit: String { frequency ? L("МГц") : (measurement ? "%" : L("код CPS")) }

  static let all: [Self] = {
    var rows: [Self] = []
    func append(_ id: Int, _ band: String, _ title: String, _ base: Int, _ count: Int) {
      rows.append(Self(id: id, band: band, title: title, base: base, count: count))
    }
    append(1, "A", "Частота приёма", 0x1b0, 9)
    append(2, "A", "Частота передачи", 0x1b4, 9)
    append(3, "B", "Частота приёма", 0x100, 5)
    append(4, "B", "Частота передачи", 0x104, 5)
    for (index, title) in [
      "VOX 1", "VOX 10", "Низкое напряжение приёма", "Полное напряжение приёма",
      "RSSI 1 (−120 дБм)", "RSSI 4 (−70 дБм)",
    ].enumerated() {
      append(index + 5, "A", title, index, 1)
    }
    append(11, "A", "Подстройка частоты", 0x09, 1)
    append(28, "B", "Подстройка частоты", 0x0c, 1)
    let rowsAB: [(String, Int, Int, Int, Int)] = [
      ("Мощность передачи · высокая", 12, 0x10, 29, 0x19),
      ("Мощность передачи · средняя", 13, 0x190, 30, 0x199),
      ("Мощность передачи · низкая", 14, 0x20, 31, 0x29),
      ("Чувствительность приёма", 15, 0x30, 32, 0x39),
      ("Открытие шумоподавителя 9", 16, 0x40, 41, 0xe0),
      ("Закрытие шумоподавителя 9", 17, 0x50, 42, 0xe5),
      ("Открытие шумоподавителя 1", 18, 0x60, 43, 0xea),
      ("Закрытие шумоподавителя 1", 19, 0x70, 44, 0xef),
      ("Модуляция CTCSS · 67 Гц", 20, 0x90, 33, 0x9b),
      ("Модуляция CTCSS · 151,4 Гц", 21, 0xa0, 34, 0xab),
      ("Модуляция CTCSS · 254,1 Гц", 22, 0xb0, 35, 0xbb),
      ("Модуляция DCS", 23, 0xd0, 36, 0xdb),
      ("DMR · амплитуда I", 24, 0x130, 37, 0x139),
      ("DMR · амплитуда Q", 25, 0x140, 38, 0x149),
      ("Аналог · амплитуда I", 26, 0x170, 39, 0x179),
      ("Аналог · амплитуда Q", 27, 0x180, 40, 0x189),
    ]
    for (title, a, aBase, b, bBase) in rowsAB {
      append(a, "A", title, aBase, 9)
      append(b, "B", title, bBase, 5)
    }
    append(45, "A", "Модуляция аналогового DTMF", 0x1ae, 1)
    append(46, "B", "Модуляция аналогового DTMF", 0x1af, 1)
    // These two measurements appear in AdjustText, outside the Test Mode data grid.
    append(47, "A", "Доля битовых ошибок (BER)", 0, 1)
    append(48, "B", "Доля битовых ошибок (BER)", 0, 1)
    return rows.sorted { $0.id < $1.id }
  }()
}

struct CalibrationIssue: LocalizedError {
  let detail: String
  init(_ detail: String) { self.detail = detail }
  var errorDescription: String? { L(detail) }
}

struct CalibrationProfile: Codable, Equatable {
  static let byteCount = 8192
  var format = "tyt-reader-calibration"
  var version = 1
  var template: Data?
  var values: [String: String] = [:]

  init() {}

  init(native data: Data) throws {
    guard data.count == Self.byteCount else {
      throw CalibrationIssue("Для формата .test CPS 2.42 ожидается файл размером 8192 байта.")
    }
    template = data
  }

  func sourceValue(_ row: CalibrationField, _ point: Int) -> String? {
    guard !row.measurement, let template else { return nil }
    let offset = row.offset(point)
    if !row.frequency { return String(template[offset]) }
    let bytes = template[offset..<offset + 4]
    guard bytes.allSatisfy({ $0 & 15 <= 9 && $0 >> 4 <= 9 }) else { return nil }
    let digits = bytes.reversed().map { String(format: "%02x", $0) }.joined()
    let mhz = Int(digits.prefix(3)) ?? 0
    return "\(mhz),\(digits.suffix(5))"
  }

  static func bytes(for value: String, row: CalibrationField, point: Int) throws -> [UInt8] {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(
      of: ",", with: ".")
    let prefix = LF("%@ · %@, точка %ld.", row.band, L(row.title), point) + " "
    if row.measurement {
      guard
        text.range(of: #"^(?:0|[1-9][0-9]?|100)(?:\.[0-9]{1,8})?$"#, options: .regularExpression)
          != nil,
        let percent = Double(text), percent <= 100
      else {
        throw CalibrationIssue(prefix + L("Введите результат измерения от 0 до 100 %."))
      }
      return []
    }
    if !row.frequency {
      guard text.utf8.allSatisfy({ (48...57).contains($0) }), let code = UInt8(text) else {
        throw CalibrationIssue(prefix + L("Введите целый коэффициент от 0 до 255."))
      }
      return [code]
    }
    guard text.range(of: #"^[0-9]{1,3}(?:\.[0-9]{1,5})?$"#, options: .regularExpression) != nil
    else {
      throw CalibrationIssue(prefix + L("Укажите частоту в МГц с точностью до 0,00001 МГц."))
    }
    let parts = text.split(separator: ".")
    let whole = String(parts[0])
    let fraction = parts.count == 2 ? String(parts[1]) : ""
    let digits =
      String(repeating: "0", count: 3 - whole.count) + whole + fraction
      + String(repeating: "0", count: 5 - fraction.count)
    let chars = Array(digits.utf8)
    return [6, 4, 2, 0].map { (chars[$0] - 48) * 16 + chars[$0 + 1] - 48 }
  }

  func validate() throws {
    guard format == "tyt-reader-calibration", version == 1 else {
      throw CalibrationIssue("Неизвестный формат или версия профиля калибровки.")
    }
    if let template, template.count != Self.byteCount {
      throw CalibrationIssue("В профиле повреждена основа .test.")
    }
    var known = Set<String>()
    for row in CalibrationField.all {
      for point in row.points {
        let key = row.key(point)
        known.insert(key)
        if let value = values[key] { _ = try Self.bytes(for: value, row: row, point: point) }
      }
    }
    guard values.keys.allSatisfy(known.contains) else {
      throw CalibrationIssue("Профиль содержит неизвестный параметр или номер точки.")
    }
  }

  func nativeData(using source: Data? = nil) throws -> Data {
    try validate()
    guard var bytes = template ?? source, bytes.count == Self.byteCount else {
      throw CalibrationIssue(
        "Выберите исходный файл .test, чтобы сохранить заполненные поля и остальные байты оригинала."
      )
    }
    for row in CalibrationField.all where !row.measurement {
      for point in row.points {
        guard let value = values[row.key(point)] else { continue }
        let newBytes = try Self.bytes(for: value, row: row, point: point)
        let offset = row.offset(point)
        bytes.replaceSubrange(offset..<offset + newBytes.count, with: newBytes)
      }
    }
    return bytes
  }

  static func open(_ url: URL) throws -> Self {
    let data = try LocalCopy.read(url, maximumSize: 256 * 1024)
    switch url.pathExtension.lowercased() {
    case "test": return try Self(native: data)
    case "json":
      guard let profile = try? JSONDecoder().decode(Self.self, from: data) else {
        throw CalibrationIssue("Выберите JSON профиля калибровки TYT Reader.")
      }
      try profile.validate()
      return profile
    default: throw CalibrationIssue("Откройте .test штатной CPS или JSON профиля калибровки.")
    }
  }

  func json() throws -> Data {
    try validate()
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    return try enc.encode(self)
  }
}

struct CalibrationEditor: View {
  @Environment(\.dismiss) private var dismiss
  @State private var profile = CalibrationProfile()
  @State private var baseline = CalibrationProfile()
  @State private var filename: String?
  @State private var band = "A"
  @State private var selected: Int? = 1
  @State private var error: String?
  @State private var status: String?
  @State private var abandon: Action?
  private enum Action: String, Identifiable {
    case open, new, close
    var id: String { rawValue }
  }
  private var dirty: Bool { profile != baseline }
  private var rows: [CalibrationField] { CalibrationField.all.filter { $0.band == band } }

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      HStack {
        VStack(alignment: .leading, spacing: 2) {
          Text("Сервисная калибровка").font(.title2.bold())
          Text(filename ?? L("Новый локальный профиль")).foregroundStyle(.secondary)
        }
        Spacer()
        Button("Новый") { perform(.new) }
        Button("Открыть профиль…") { perform(.open) }
      }
      Picker(
        "Сторона",
        selection: Binding(
          get: { band },
          set: { selection in
            band = selection
            selected = CalibrationField.all.first(where: { $0.band == selection })?.id
          })
      ) {
        Text("Сторона A").tag("A")
        Text("Сторона B").tag("B")
      }.pickerStyle(.segmented).frame(maxWidth: 320)
      HSplitView {
        List(rows, selection: $selected) { row in
          Text(L(row.title)).tag(row.id)
        }.frame(minWidth: 195, idealWidth: 235)
        if let row = rows.first(where: { $0.id == selected }) {
          VStack(alignment: .leading, spacing: 10) {
            Text(L(row.title)).font(.headline)
            if row.measurement {
              Text("Результат внешнего измерения BER в процентах. Сохраняется в JSON профиля.")
                .foregroundStyle(.secondary)
            } else if row.frequency {
              Text("Контрольные частоты в МГц. Шаг файла 10 Гц.").foregroundStyle(.secondary)
            } else {
              Text("Коэффициенты CPS в диапазоне 0–255.").foregroundStyle(.secondary)
            }
            ScrollView {
              Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 7) {
                GridRow {
                  Text("Точка")
                  Text("Исходное значение")
                  Text(row.measurement ? L("Измерено, %") : LF("Правка, %@", row.unit))
                }.font(.caption).foregroundStyle(.secondary)
                ForEach(row.points, id: \.self) { point in
                  GridRow {
                    Text(String(point)).monospacedDigit()
                    Text(
                      profile.sourceValue(row, point)
                        ?? (profile.template != nil && row.frequency ? L("Не распознано") : "—")
                    )
                    .monospacedDigit().foregroundStyle(.secondary)
                    TextField(
                      L(row.measurement ? "Результат" : "Без правки"),
                      text: Binding(
                        get: { profile.values[row.key(point)] ?? "" },
                        set: { new in
                          if new.trimmingCharacters(in: .whitespaces).isEmpty {
                            profile.values.removeValue(forKey: row.key(point))
                          } else {
                            profile.values[row.key(point)] = new
                          }
                          error = nil
                          status = nil
                        })
                    )
                    .textFieldStyle(.roundedBorder).frame(
                      minWidth: 100, idealWidth: 125, maxWidth: 165
                    )
                    .accessibilityLabel(LF("%@, точка %ld, %@", L(row.title), point, row.unit))
                  }
                }
              }.padding(.vertical, 2)
            }
          }.padding(.leading, 14).frame(minWidth: 350, maxWidth: .infinity, alignment: .leading)
        }
      }
      if let error {
        Text(error).foregroundStyle(.red).textSelection(.enabled)
      } else if let status {
        Text(status).foregroundStyle(.secondary)
      }
      HStack {
        Button("Закрыть") { perform(.close) }.keyboardShortcut(.cancelAction)
        Spacer()
        Button("Сохранить профиль JSON…") { save(native: false) }
        Button("Сохранить копию .test…") { save(native: true) }.buttonStyle(.borderedProminent)
      }
    }.padding(18).frame(minWidth: 650, idealWidth: 790, minHeight: 430, idealHeight: 550)
      .interactiveDismissDisabled(dirty)
      .alert(item: $abandon) { action in
        Alert(
          title: Text("Отбросить изменения профиля?"),
          primaryButton: .destructive(Text("Отбросить")) { run(action) },
          secondaryButton: .cancel(Text("Вернуться")))
      }
  }

  private func perform(_ action: Action) {
    if dirty { abandon = action } else { run(action) }
  }

  private func run(_ action: Action) {
    switch action {
    case .close: dismiss()
    case .new:
      profile = CalibrationProfile()
      baseline = profile
      filename = nil
      error = nil
      status = nil
    case .open:
      let panel = NSOpenPanel()
      panel.allowedContentTypes = [Self.testType, .json]
      guard panel.runModal() == .OK, let url = panel.url else { return }
      do {
        let imported = try CalibrationProfile.open(url)
        profile = imported
        baseline = imported
        filename = url.lastPathComponent
        error = nil
        status = nil
      } catch { self.error = error.localizedDescription }
    }
  }
  private static var testType: UTType { UTType(filenameExtension: "test") ?? .data }

  private func save(native: Bool) {
    do {
      try profile.validate()
      var template = profile.template
      if native && template == nil {
        let open = NSOpenPanel()
        open.allowedContentTypes = [Self.testType]
        open.message =
          L("Выберите исходный .test для новой копии. В неё перейдут заполненные коэффициенты и частоты.")
        guard open.runModal() == .OK, let url = open.url else { return }
        template = try CalibrationProfile.open(url).template
      }
      let data = native ? try profile.nativeData(using: template) : try profile.json()
      let save = NSSavePanel()
      save.allowedContentTypes = native ? [Self.testType] : [.json]
      save.nameFieldStringValue = L(native ? "Калибровка — копия.test" : "Калибровка — профиль.json")
      if native && (profile.values["47.5"] != nil || profile.values["48.5"] != nil) {
        save.message =
          L("Файл .test содержит таблицу CPS. Введённые измерения BER сохраняются отдельно в JSON.")
      }
      guard save.runModal() == .OK, let url = save.url else { return }
      try LocalCopy.write(data, to: url)
      if native {
        let measurements = profile.values.filter { $0.key == "47.5" || $0.key == "48.5" }
        profile = try CalibrationProfile(native: data)
        baseline = profile
        profile.values = measurements
      } else {
        baseline = profile
      }
      filename = url.lastPathComponent
      error = nil
      status = LF("Сохранено в %@", url.lastPathComponent)
    } catch { self.error = error.localizedDescription }
  }
}
