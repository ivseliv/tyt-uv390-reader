"""File and stock USB operations for the Windows interface. No device writes."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata

from cps_csv import export_csv, import_csv, merge_csv
from export_qdmr import MAX_BYTES as MAX_YAML_BYTES, export_qdmr
from import_settings import load_file
from normalize_snapshot import TABLES, indexed, named_sections, normalize
from rdt_editor import export_rdt, publish_new
from rdt_template import transfer_rdt
from read_settings import decode


MODELS = ("MD-UV380", "MD-UV390")
JSON_LIMIT = 128 * 1024 * 1024
BAD_JSON = "Этот JSON не похож на файл настроек TYT Reader."
PRIVACY_TYPES = ("Выкл.", "ARC4", "DES", "AES-128", "AES-256", "Enhance",
                 "Proprietary algorithm", "MAES-512")
VALUE_STATUS = ("notApplicable", "zeros", "ones", "dataPresent")


def save_new(path, data):
    """Publish a complete local file under a new name, even for concurrent exports."""
    path = Path(path)
    if os.path.lexists(path):
        raise FileExistsError("Выберите новое имя: выходной файл уже существует.")
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".tyt-save-") as folder:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        publish_new(stream.name, path)


def _check_snapshot(report):
    if not isinstance(report, dict) or "usb_privacy" in report:
        raise ValueError(BAD_JSON)
    if not all(isinstance(report.get(key), list) for key in ("channels", "zones", "contacts")):
        raise ValueError(BAD_JSON)
    tables = indexed(report)
    named_sections(report)
    for rows in tables.values():
        if not all(isinstance(row.get("name"), str) for row in rows.values()):
            raise ValueError(BAD_JSON)
    for table, field in (("contacts", "type"), ("channels", "mode")):
        if not all(isinstance(row.get(field), str) for row in tables[table].values()):
            raise ValueError(BAD_JSON)
    for zone in tables["zones"].values():
        for side in ("A", "B"):
            if not isinstance(zone.get(side), list) or not all(
                isinstance(member, dict) and type(member.get("index")) is int
                and (member.get("name") is None or isinstance(member["name"], str))
                for member in zone[side]
            ):
                raise ValueError(BAD_JSON)
    for table, field in (("rx_groups", "contacts"), ("scan_lists", "channels")):
        for row in tables[table].values():
            if not isinstance(row.get(field), list) or any(type(i) is not int for i in row[field]):
                raise ValueError(BAD_JSON)
    for field, kind in (("read_notes", str), ("unrecognized_rx_group_slots", int)):
        values = report.get(field, [])
        if not isinstance(values, list) or any(type(v) is not kind for v in values):
            raise ValueError(BAD_JSON)


def _open_json(path):
    with path.open("rb") as stream:
        data = stream.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise ValueError("Снимок превышает 128 МБ.")
    report = json.loads(data)
    if isinstance(report, dict):
        for key in ("rx_groups", "scan_lists", "cps_sections", "read_notes", "unrecognized_rx_group_slots"):
            if report.get(key) is None:
                report.pop(key, None)
    _check_snapshot(report)
    if (report.get("usb_model") or report.get("file_model")) in MODELS:
        # Older TYT Reader versions decoded unsupported receive-group records.
        invalid = {r["index"] for r in report.get("rx_groups", [])
                   if any(not 1 <= k <= 10000 for k in r["contacts"])
                   or any(c == "\ufffd" or unicodedata.category(c) in ("Cc", "Cf") for c in r["name"])}
        if invalid:
            report["rx_groups"] = [r for r in report["rx_groups"] if r["index"] not in invalid]
            report["unrecognized_rx_group_slots"] = sorted(invalid.union(report.get("unrecognized_rx_group_slots", [])))
            slots = ", ".join(map(str, sorted(invalid)))
            report.setdefault("read_notes", []).append(
                f"В позициях групп приёма {slots} старого снимка данные не соответствуют формату TYT. "
                "Эти записи пропущены при открытии копии.")
    return report


def _helper_path():
    root = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        folders = (Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)), Path(sys.executable).parent)
    else:
        folders = (root / "windows" / "bin", root / "windows", root)
    filename = "read_usb.exe" if os.name == "nt" else "read_usb"
    for folder in folders:
        candidate = folder / filename
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("В приложении отсутствует компонент чтения. Пересоберите приложение.")


def _usb_read(mode, helper):
    executable = Path(helper).resolve() if helper is not None else _helper_path()
    # Arguments here are fixed by the two public methods. No arbitrary commands,
    # addresses or output paths can reach the native helper from a settings file.
    result = subprocess.run([str(executable), mode], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        log = result.stderr.decode("utf-8", errors="replace").strip()
        if "USB 0483:df11 not found" in log:
            raise ValueError("Рация не обнаружена. Проверьте USB-кабель и питание рации.")
        if "Found multiple USB" in log:
            raise ValueError("Подключите только одну рацию и повторите чтение.")
        raise ValueError(log[:1000] or "Считать данные по USB не удалось.")
    return result.stdout


def read_universal_usb(helper=None):
    """Return only the slot metadata from the stock helper (compatible with the macOS JSON export)."""
    payload = _usb_read("--universal-privacy", helper)
    if len(payload) != 8 + 34 * 255 or payload[:8] not in (b"MD-UV380", b"MD-UV390"):
        raise ValueError("Ответ USB не соответствует таблице Universal Privacy для MD-UV380/390.")
    slots = []
    for index in range(255):
        offset = 8 + 34 * index
        kind, status = payload[offset], payload[offset + 33]
        if status >= len(VALUE_STATUS):
            raise ValueError(f"Неизвестный статус значения USB в позиции {index + 1}.")
        name = payload[offset+1:offset+33].decode("utf-16-le", errors="replace").split("\0")[0].split("\uffff")[0]
        name_for_type = PRIVACY_TYPES[kind] if kind < len(PRIVACY_TYPES) else f"Код {kind}"
        slots.append({"slot": index + 1, "name": name, "type": kind, "typeName": name_for_type,
                      "usbStatus": VALUE_STATUS[status]})
    return {"model": payload[:8].decode("ascii"), "slots": slots}


class Document:
    """Editable Windows document. Use draft() for edits and commit them through apply()."""

    def __init__(self, report, source=None, usb_privacy=None):
        _check_snapshot(report)
        self.report = report
        self.source = Path(source).absolute() if source is not None else None
        self.usb_privacy = usb_privacy
        self._original = copy.deepcopy(report) if self.source and self.source.suffix.lower() == ".rdt" else None

    @classmethod
    def new(cls):
        return cls({table: [] for table in TABLES})

    @classmethod
    def open(cls, path):
        path = Path(path)
        if path.suffix.lower() == ".json":
            report = _open_json(path)
        elif path.suffix.lower() == ".csv":
            report = import_csv(path)
        else:
            report = load_file(path)
        return cls(report, source=path)

    @classmethod
    def from_template(cls, path):
        if Path(path).suffix.lower() != ".rdt":
            raise ValueError("Выберите штатный RDT. Редактор создаст черновик со всеми настройками шаблона.")
        return cls.open(path)

    @classmethod
    def read_usb(cls, helper=None):
        result = decode(_usb_read("--include-privacy", helper), include_privacy=True)
        return cls(result["settings"], usb_privacy=result.get("usb_privacy"))

    def draft(self):
        return copy.deepcopy(self.report)

    def apply(self, edited_report):
        _check_snapshot(edited_report)
        # normalize refreshes references in-place; a failed edit must not change the document.
        self.report = normalize(self.report, copy.deepcopy(edited_report))

    def merge_csv(self, path):
        self.apply(merge_csv(self.report, import_csv(path)))

    def _destination(self, path):
        dest = Path(path)
        if self.source is not None and self.source.resolve() == dest.resolve():
            raise ValueError("Выберите новое имя для копии исходного файла.")
        return dest

    def save_json(self, path):
        data = json.dumps(self.report, indent=2, ensure_ascii=False, allow_nan=False)
        save_new(self._destination(path), (data + "\n").encode("utf-8"))

    def save_csv(self, path, kind):
        if kind not in ("channels", "contacts"):
            raise ValueError("В JSON отсутствует выбранная таблица.")
        save_new(self._destination(path), export_csv(self.report, kind))

    def save_yaml(self, path):
        data = export_qdmr(self.report).encode("utf-8")
        if len(data) > MAX_YAML_BYTES:
            raise ValueError("Экспорт превысил размер YAML 32 МиБ.")
        save_new(self._destination(path), data)

    def save_rdt(self, path, template=None):
        destination = self._destination(path)
        if template is not None:
            transfer_rdt(Path(template), destination, self.report)
        elif self._original is not None:
            export_rdt(self.source, destination, self._original, self.report)
        else:
            raise ValueError("Выберите исходный RDT для переноса настроек.")
