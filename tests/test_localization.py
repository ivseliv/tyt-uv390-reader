import json
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "macos"
LANGUAGES = ("ru", "en", "zh-Hans")

# Match actual conversions, not standalone percentage signs such as "100 %".
# Position, length modifier and conversion determine which CVarArg Foundation reads.
CONVERSION = re.compile(
    r"%(?:%|(?:(?P<position>[1-9][0-9]*)\$)?[-+#0']*"
    r"[0-9]*(?:\.[0-9]+)?(?P<length>hh|ll|[hljztLq])?"
    r"(?P<type>[@diouxXfFeEgGaAcCsSpn]))"
)


def argument_types(template):
    types = {}
    sequential = 1
    for match in CONVERSION.finditer(template):
        if match.group() == "%%":
            continue
        if match["position"]:
            index = int(match["position"])
        else:
            index = sequential
            sequential += 1
        signature = (match["length"] or "") + match["type"]
        if index in types and types[index] != signature:
            raise ValueError(f"Conflicting conversions for argument {index} in {template!r}")
        types[index] = signature
    return types


class LocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.temp = Path(cls.scratch.name)
        cls.contents = cls.temp / "LocalizationCheck.app" / "Contents"
        resources = cls.contents / "Resources"
        subprocess.run([sys.executable, "-IB", MACOS / "build-localizations.py", resources], check=True)
        cls.catalogs = {
            lang: plistlib.loads((resources / f"{lang}.lproj" / "Localizable.strings").read_bytes())
            for lang in LANGUAGES
        }
        cls.patterns = plistlib.loads((resources / "DisplayPatterns.plist").read_bytes())

    def test_catalog_keys_and_format_arguments(self):
        russian = self.catalogs["ru"]
        for lang, table in self.catalogs.items():
            self.assertEqual(set(table), set(russian), lang)
        # An omitted LF template would otherwise silently fall back to Russian for all languages.
        for swift in MACOS.glob("*.swift"):
            for key in re.findall(r'\bLF\("([^"\\]*)"', swift.read_text(encoding="utf-8")):
                self.assertIn(key, russian, swift.name)
        for key in russian:
            expected = argument_types(key)
            if expected:
                for lang, table in self.catalogs.items():
                    with self.subTest(key=key, language=lang):
                        self.assertEqual(argument_types(table[key]), expected)
        for pattern in self.patterns:
            self.assertIn(pattern, russian)
            self.assertEqual(set(argument_types(pattern).values()), {"@"}, pattern)
        self.assertIn("Диапазон %@ (МГц)", self.patterns)
        self.assertIn("Строка %@: %@", self.patterns)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires macOS and Swift")
    def test_bundle_language_and_user_data(self):
        info = plistlib.loads((MACOS / "Info.plist").read_bytes())
        self.assertEqual(set(info["CFBundleLocalizations"]), set(LANGUAGES))
        info.update(CFBundleExecutable="LocalizationCheck", CFBundleIdentifier="local.tyt.test.localization")
        (self.contents / "Info.plist").write_bytes(plistlib.dumps(info))
        executable = self.contents / "MacOS" / "LocalizationCheck"
        executable.parent.mkdir()
        source = self.temp / "Check.swift"
        source.write_text("""
import Foundation
@main struct Check {
    static func main() throws {
        var data = Data("MD-UV390".utf8) + Data(repeating: 0, count: 255 * 34)
        data[8] = 99
        var offset = 9
        for codeUnit in "Каналы".utf16 {
            data[offset] = UInt8(truncatingIfNeeded: codeUnit)
            data[offset + 1] = UInt8(codeUnit >> 8)
            offset += 2
        }
        let slot = try UniversalUSBReport(data: data).slots[0]
        let result: [String: String] = [
            "main": L("Редактировать копию…"),
            "editor": L("Позиционирование"),
            "model": LD("Шумоподавитель"),
            "band": LD("Диапазон VHF (МГц)"),
            "row": LD("Строка 3: R1-100%"),
            "name": slot.name,
            "unknownType": slot.displayTypeName,
            "reportCode": slot.status.reportCode,
            "caption": LF("Слот %ld · %@", slot.id, "DMR")
        ]
        FileHandle.standardOutput.write(try JSONEncoder().encode(result))
    }
}
""", encoding="utf-8")
        files = ["Localization.swift", "LocalCopy.swift", "PrivacyEditor.swift", "UniversalUSBViewer.swift"]
        subprocess.run(["swiftc", "-swift-version", "6", "-parse-as-library",
                        "-module-cache-path", self.temp / "modules",
                        *[MACOS / name for name in files], source, "-o", executable], check=True)
        for language, table in self.catalogs.items():
            with self.subTest(language=language):
                # Use a new process: Foundation caches Bundle.main's preferred localization.
                output = subprocess.check_output([executable, "-AppleLanguages", f"({language})"])
                result = json.loads(output)
                for item, key in [("main", "Редактировать копию…"), ("editor", "Позиционирование"),
                                  ("model", "Шумоподавитель")]:
                    self.assertEqual(result[item], table[key])
                    if language != "ru":
                        self.assertNotEqual(result[item], key)
                self.assertEqual(result["band"], table["Диапазон %@ (МГц)"].replace("%@", "VHF"))
                self.assertEqual(result["name"], "Каналы")  # a real slot name, even if it matches a UI label
                self.assertEqual(result["reportCode"], "notApplicable")
                self.assertEqual(result["unknownType"], table["Код %ld"].replace("%ld", "99"))
                self.assertEqual(result["row"], {"ru": "Строка 3: R1-100%", "en": "Row 3: R1-100%",
                                                 "zh-Hans": "第 3 行：R1-100%"}[language])
                self.assertEqual(result["caption"], table["Слот %ld · %@"].replace("%ld", "1").replace("%@", "DMR"))


if __name__ == "__main__":
    unittest.main()
