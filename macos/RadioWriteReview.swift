import AppKit
import Foundation

/// Keep the exact proposal returned by preflight until the user approves it.
/// Only the summary is displayed. The native writer validates the underlying plan.
struct RadioWriteReview: Sendable {
    struct Summary: Decodable, Sendable {
        let model: String
        let dmrID: Int
        let blocks: Int
        let sectors: Int
        let changes: [String: Int]

        enum CodingKeys: String, CodingKey {
            case model, blocks, sectors, changes
            case dmrID = "dmr_id"
        }
    }

    struct InvalidPlan: LocalizedError {
        var errorDescription: String? { L("Не удалось проверить план записи. Повторите сравнение с рацией.") }
    }

    static let sections = [
        ("channels", "Каналы"), ("contacts", "Контакты"), ("zones", "Зоны A/B"),
        ("rx_groups", "Группы приёма"), ("scan_lists", "Списки сканирования")
    ]
    let summary: Summary
    let data: Data

    init(_ data: Data, expectedModel: String? = nil, expectedDMRID: Int? = nil) throws {
        guard let summary = try? JSONDecoder().decode(Summary.self, from: data),
              ["MD-UV380", "MD-UV390"].contains(summary.model), expectedModel == nil || summary.model == expectedModel,
              (0...0xFFFFFF).contains(summary.dmrID), expectedDMRID == nil || expectedDMRID == summary.dmrID,
              summary.blocks >= 0, summary.blocks <= 2048,
              (0...summary.blocks).contains(summary.sectors), (summary.sectors == 0) == (summary.blocks == 0),
              Set(summary.changes.keys).isSubset(of: Set(Self.sections.map(\.0) + ["dmr_id"])),
              (summary.changes["dmr_id"] ?? 0) == 0,
              summary.changes.values.allSatisfy({ $0 >= 0 && $0 <= 10000 }),
              (summary.blocks == 0) == !summary.changes.values.contains(where: { $0 > 0 })
        else { throw InvalidPlan() }
        self.summary = summary
        self.data = data
    }

    @MainActor func confirm(source: String, backup: URL, restoring: Bool) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = LF("Записать настройки в %@?", summary.model)
        let counts = Self.sections.compactMap { id, label -> String? in
            guard let count = summary.changes[id], count > 0 else { return nil }
            return "\(L(label)): \(count)"
        }.joined(separator: "\n")
        alert.informativeText = [
            "DMR ID \(summary.dmrID)", LF("Источник: %@", source), L("Изменения в рации"), counts,
            restoring ? L("Будут восстановлены изменённые позиции из резервной копии.") :
                L("Переносятся заполненные позиции поддерживаемых таблиц. Отсутствующие в документе позиции остаются в рации."),
            L("DMR ID, группы приёма 1–53, DTMF, ключи Privacy и калибровка исключены из записи."),
            LF("Секторов флеш-памяти для обновления: %ld.", summary.sectors),
            LF("Резервная копия перед записью: %@", backup.path),
            L("Рация стирает выбранные секторы перед их перезаписью. При обрыве связи или питания сектор может остаться записанным лишь частично."),
            L("Запись из TYT Reader на физическую модель 10 Вт с прошивкой D219.043 ещё не проверена."),
            L("Оставьте рацию подключённой и включённой до окончания проверки.")
        ].joined(separator: "\n")
        alert.addButton(withTitle: L("Отмена")).keyEquivalent = "\r"
        alert.addButton(withTitle: L("Записать в рацию")).keyEquivalent = ""
        return alert.runModal() == .alertSecondButtonReturn
    }

    @MainActor func applyIfApproved(confirm: () -> URL?, write: (Data, URL) async throws -> Void) async throws -> String? {
        if summary.blocks == 0 { return L("В доступных для записи полях изменений нет.") }
        guard let backup = confirm() else { return nil }
        try await write(data, backup)
        return LF("Запись проверена повторным чтением. Резервная копия: %@", backup.path)
    }
}
