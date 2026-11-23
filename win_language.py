"""Use the same translated CPS labels as TYT Reader for macOS."""
import json
import locale
import os
import re
import sys
import tempfile
from pathlib import Path


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


LANGUAGES = {"ru": "Русский", "en": "English", "zh-Hans": "简体中文"}
ARGUMENT = re.compile(r"%(?:(\d+)\$)?(?:@|ld|lld|d)")


def formatted(template, args):
    order = iter(args)
    return ARGUMENT.sub(lambda match: str(args[int(match[1]) - 1] if match[1] else next(order)), template)


def system_language():
    language = locale.getlocale()[0] or ""
    if os.name == "nt":
        import ctypes
        name = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(name, len(name)):
            language = name.value
    head = language.lower().replace("_", "-").split("-")[0]
    return {"ru": "ru", "zh": "zh-Hans"}.get(head, "en")


class Language:
    def __init__(self, choice=None):
        config_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config") / "TYT Reader"
        self.config = config_dir / "language.json"
        if choice is None:
            try:
                choice = json.loads(self.config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                choice = "system"
        self.select(choice, persist=False)

    def select(self, choice, persist=True):
        self.choice = choice if choice in (*LANGUAGES, "system") else "system"
        self.code = system_language() if self.choice == "system" else self.choice
        index = {"ru": 0, "en": 1, "zh-Hans": 2}[self.code]
        root = resource_root()
        catalogs = root / "macos" / "i18n"
        paths = sorted(catalogs.glob("*.tsv"), key=lambda p: (p.stem != "model", p.name))
        if not paths:
            raise FileNotFoundError("TYT Reader translation catalogs are missing")
        self.labels = {}
        templates = set()
        for path in paths + sorted((root / "windows").glob("*.tsv")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("# "):
                    continue
                values = [v.replace(r"\n", "\n") for v in line.split("\t")]
                self.labels[values[0]] = values[index]
                if "%@" in values[0] and path.stem == "model":
                    templates.add(values[0])
        self.templates = [(re.compile("(.*?)".join(re.escape(part) for part in k.split("%@"))), k)
                          for k in sorted(templates, key=lambda k: (-len(k.replace("%@", "")), k))]
        if persist:
            self.config.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.config.parent, delete=False, mode="w", encoding="utf-8") as f:
                json.dump(self.choice, f)
            try:
                os.replace(f.name, self.config)
            finally:
                Path(f.name).unlink(missing_ok=True)

    def __call__(self, text, *args):
        template = self.labels.get(text, text)
        return formatted(template, args) if args else template

    def generated(self, text):
        if text in self.labels or self.code == "ru":
            return self(text)
        if len(text) < 2048 and (re.search("[А-Яа-яЁё]", text) or text.startswith("Radio ID ")):
            for regex, key in self.templates:
                match = regex.fullmatch(text)
                if match:
                    return formatted(self.labels[key], match.groups())
        return text
