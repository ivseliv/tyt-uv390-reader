"""Local files and stock USB reads for the Tk interface."""

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
from normalize_snapshot import TABLES, indexed, normalize
from rdt_editor import export_rdt, publish_new
from rdt_template import transfer_rdt
from read_settings import decode


MODELS = ("MD-UV380", "MD-UV390")
JSON_LIMIT = 128 * 1024 * 1024
BAD_JSON = "Этот JSON не похож на файл настроек TYT Reader."
PRIVACY_TYPES = ("Выкл.", "ARC4", "DES", "AES-128", "AES-256", "Enhance",
                 "Proprietary algorithm", "MAES-512")
VALUE_STATUS = ("notApplicable", "zeros", "ones", "dataPresent")
CHANNEL_NUMBERS = ("rx_hz", "tx_hz", "bandwidth_code", "color_code", "time_slot", "contact_index",
                   "rx_group_index", "scan_list_index", "privacy_type_code", "privacy_index_code",
                   "rx_tone_code", "tx_tone_code", "radio_id", "rdt_template_index")


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


def _text(value):
    if not isinstance(value, str):
        raise ValueError(BAD_JSON)
    # JSON permits escaped lone surrogates in some parsers. Tcl/Tk cannot display them.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(BAD_JSON) from None


def _optional_numbers(row, keys):
    for key in keys:
        value = row.get(key)
        if value is not None and (type(value) is not int or not -(1 << 63) <= value < 1 << 63):
            raise ValueError(BAD_JSON)


def _check_fields(fields):
    if not isinstance(fields, list):
        raise ValueError(BAD_JSON)
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError(BAD_JSON)
        _text(field.get("label"))
        _text(field.get("value"))


def _check_sections(sections):
    if not isinstance(sections, list):
        raise ValueError(BAD_JSON)
    seen = set()
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("rows"), list):
            raise ValueError(BAD_JSON)
        _text(section.get("title"))
        _text(section.get("id"))
        key = section["id"]
        if key in seen or "\x00" in key:
            raise ValueError(BAD_JSON)
        seen.add(key)
        for row in section["rows"]:
            if not isinstance(row, dict):
                raise ValueError(BAD_JSON)
            _text(row.get("name"))
            _check_fields(row.get("fields"))
            if key in ("dtmf", "buttons") and row["name"].startswith("Быстрый доступ "):
                for field in row["fields"]:
                    if field["label"] in ("Номер Encode", "Шаблон"):
                        try:
                            int(field["value"])
                        except ValueError:
                            raise ValueError(BAD_JSON) from None


def _check_snapshot(report):
    if not isinstance(report, dict) or "usb_privacy" in report:
        raise ValueError(BAD_JSON)
    if not all(isinstance(report.get(key), list) for key in ("channels", "zones", "contacts")):
        raise ValueError(BAD_JSON)
    _optional_numbers(report, ("dmr_id",))
    for key in ("file_model", "usb_model", "source_format"):
        if report.get(key) is not None:
            _text(report[key])
    slots = report.get("local_empty_slots", {})
    if not isinstance(slots, dict):
        raise ValueError(BAD_JSON)
    for key in ("quick", "numberKeys"):
        values = slots.get(key, [])
        if not isinstance(values, list) or any(type(i) is not int for i in values):
            raise ValueError(BAD_JSON)
    tables = indexed(report)
    _check_sections(report.get("cps_sections", []))
    for rows in tables.values():
        for row in rows.values():
            _text(row.get("name"))
            _check_fields(row.get("details", []))
    for table, field in (("contacts", "type"), ("channels", "mode")):
        for row in tables[table].values():
            _text(row.get(field))
    for row in tables["channels"].values():
        _optional_numbers(row, CHANNEL_NUMBERS)
    for row in tables["scan_lists"].values():
        _optional_numbers(row, ("primary", "secondary", "revert"))
    for row in tables["contacts"].values():
        _optional_numbers(row, ("dmr_id", "call_receive_tone"))
        for key in ("dtmf_number", "callsign"):
            if row.get(key) is not None:
                _text(row[key])
        for key in ("ring", "broadcast"):
            if row.get(key) is not None and type(row[key]) is not bool:
                raise ValueError(BAD_JSON)
    for zone in tables["zones"].values():
        for side in ("A", "B"):
            if not isinstance(zone.get(side), list) or not all(
                isinstance(member, dict) and type(member.get("index")) is int
                for member in zone[side]
            ):
                raise ValueError(BAD_JSON)
            for member in zone[side]:
                if member.get("name") is not None:
                    _text(member["name"])
    for table, field in (("rx_groups", "contacts"), ("scan_lists", "channels")):
        for row in tables[table].values():
            if not isinstance(row.get(field), list) or any(type(i) is not int for i in row[field]):
                raise ValueError(BAD_JSON)
    for field, kind in (("read_notes", str), ("unrecognized_rx_group_slots", int)):
        values = report.get(field, [])
        if not isinstance(values, list) or any(type(v) is not kind for v in values):
            raise ValueError(BAD_JSON)
    for note in report.get("read_notes", []):
        _text(note)


def _open_json(path):
    with path.open("rb") as stream:
        data = stream.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise ValueError("Снимок превышает 128 МБ.")
    def invalid_constant(token):
        raise ValueError(BAD_JSON)
    try:
        report = json.loads(data, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(BAD_JSON) from None
    if isinstance(report, dict):
        for key in ("rx_groups", "scan_lists", "cps_sections", "read_notes", "unrecognized_rx_group_slots",
                    "local_empty_slots"):
            if report.get(key) is None:
                report.pop(key, None)
        for table in TABLES:
            if isinstance(report.get(table), list):
                for row in report[table]:
                    if isinstance(row, dict) and row.get("details") is None:
                        row.pop("details", None)
    try:
        _check_snapshot(report)
    except ValueError:
        raise ValueError(BAD_JSON) from None
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
    elif os.name == "nt":
        folders = (root / "windows" / "bin", root / "windows", root)
    else:
        folders = (root,)
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
        raise ValueError(usb_read_error(log))
    return result.stdout


def usb_read_error(log):
    if sys.platform == "linux" and "LIBUSB_ERROR_ACCESS" in log:
        return "Нет доступа к USB. Переподключите включённую рацию после установки правила udev TYT Reader и войдите в локальный сеанс."
    if "USB 0483:df11 not found" in log:
        return "Рация не обнаружена. Проверьте USB-кабель и питание рации."
    if "Found multiple USB" in log:
        return "Подключите только одну рацию и повторите чтение."
    return log[:1000] or "Считать данные по USB не удалось."


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
    """Editable Tk document. Use draft() for edits and commit them through apply()."""

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
