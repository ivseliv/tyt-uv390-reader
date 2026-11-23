"""Compile shared SwiftUI, AppKit and CPS display strings into native bundle resources."""
import plistlib
import sys
from pathlib import Path


def compile_catalog(source, resources):
    translations = {language: {} for language in ("ru", "en", "zh-Hans")}
    patterns = set()
    # Model text overlaps some UI labels. The UI catalog defines the shared wording.
    for path in sorted(source.glob("*.tsv"), key=lambda p: (p.stem != "model", p.name)):
        seen = set()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.startswith("# "):
                continue
            fields = [field.replace(r"\n", "\n") for field in line.split("\t")]
            if len(fields) != 3 or not all(fields):
                raise ValueError(f"{path}:{number}: expected three nonempty tab-separated strings")
            key = fields[0]
            if key in seen:
                raise ValueError(f"{path}:{number}: duplicate {key!r}")
            seen.add(key)
            for table, value in zip(translations.values(), fields):
                table[key] = value
            if "%@" in key and path.stem == "model":
                patterns.add(key)
    for language, table in translations.items():
        directory = resources / f"{language}.lproj"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Localizable.strings").write_bytes(plistlib.dumps(table, fmt=plistlib.FMT_BINARY))
    ordered = sorted(patterns, key=lambda s: (-len(s.replace("%@", "")), s))
    (resources / "DisplayPatterns.plist").write_bytes(plistlib.dumps(ordered, fmt=plistlib.FMT_BINARY))


if __name__ == "__main__":
    compile_catalog(Path(__file__).with_name("i18n"), Path(sys.argv[1]))
