import Foundation

/// Use this for app-owned labels that reach SwiftUI as String and for AppKit panels.
func L(_ key: String) -> String {
    Bundle.main.localizedString(forKey: key, value: key, table: nil)
}

func LF(_ key: String, _ arguments: CVarArg...) -> String {
    String(format: L(key), locale: Locale.current, arguments: arguments)
}

/// Legacy snapshots and CPS helpers carry Russian display text. Match only catalogued
/// templates at presentation time; never rewrite the document or its record names.
private struct DisplayPattern {
    let key: String
    let expression: NSRegularExpression

    static let catalog: [Self] = {
        guard let url = Bundle.main.url(forResource: "DisplayPatterns", withExtension: "plist"),
              let data = try? Data(contentsOf: url),
              let keys = try? PropertyListDecoder().decode([String].self, from: data) else { return [] }
        return keys.compactMap { key in
            let regex = key.components(separatedBy: "%@")
                .map(NSRegularExpression.escapedPattern(for:)).joined(separator: "(.*?)")
            guard let expression = try? NSRegularExpression(pattern: "\\A" + regex + "\\z") else { return nil }
            return Self(key: key, expression: expression)
        }
    }()
}

func LD(_ text: String) -> String {
    localizedDisplay(text, depth: 0)
}

private func localizedDisplay(_ text: String, depth: Int) -> String {
    let direct = L(text)
    guard direct == text, Bundle.main.preferredLocalizations.first != "ru",
          text.hasPrefix("Radio ID ") || text.rangeOfCharacter(from: russianLetters) != nil else { return direct }
    let range = NSRange(text.startIndex..., in: text)
    for pattern in DisplayPattern.catalog {
        guard let match = pattern.expression.firstMatch(in: text, range: range) else { continue }
        var arguments = (1..<match.numberOfRanges).map { (text as NSString).substring(with: match.range(at: $0)) }
        // These helpers wrap an inner diagnostic as the final argument. Keep all
        // other captures verbatim: they can hold file names, callsigns or messages.
        switch pattern.key {
        case "Строка %@: %@", "Канал №%@: %@", "«%@», поле «%@»: %@",
             "Не удалось создать RDT. %@", "Не удалось перенести снимок в RDT. %@":
            if depth < 4, let detail = arguments.last {
                arguments[arguments.count - 1] = localizedDisplay(detail, depth: depth + 1)
            }
        default: break
        }
        return String(format: L(pattern.key), arguments: arguments)
    }
    return text
}

private let russianLetters = CharacterSet(charactersIn: "\u{0410}"..."\u{044F}")
    .union(CharacterSet(charactersIn: "Ёё"))

enum AppLanguagePreference {
    /// Read only the app domain so that the global macOS preference remains distinct.
    static var choice: String {
        guard let id = Bundle.main.bundleIdentifier,
              let domain = UserDefaults.standard.persistentDomain(forName: id),
              let preferred = (domain["AppleLanguages"] as? [String])?.first else { return "system" }
        return Bundle.preferredLocalizations(from: ["ru", "en", "zh-Hans"], forPreferences: [preferred]).first ?? "system"
    }

    static func select(_ language: String) {
        if language == "system" {
            UserDefaults.standard.removeObject(forKey: "AppleLanguages")
        } else {
            UserDefaults.standard.set([language], forKey: "AppleLanguages")
        }
    }
}
