"""Presentation and typed edits for the five codeplug tables. Stored names stay verbatim."""
import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from cps_choices import choices
from cps_csv import cps_tone, tone


TABLES = {"channels": "Каналы", "contacts": "Контакты", "zones": "Зоны A/B",
          "rx_groups": "Группы приёма", "scan_lists": "Списки сканирования"}
LIMITS = {"channels": 3000, "contacts": 10000, "zones": 250, "rx_groups": 250, "scan_lists": 250}
CHOICES = choices()
OPTIONS = {"mode": ["analog", "DMR", "AM", "M17"], "bandwidth_code": [0, 1, 2],
           "type": ["group", "private", "all", "dtmf", "m17"], "ring": [False, True], "time_slot": [1, 2]}
LABELS = {"name": "Имя", "type": "Тип вызова", "dmr_id": "DMR ID", "mode": "Режим",
          "rx_hz": "RX, МГц", "tx_hz": "TX, МГц", "bandwidth_code": "Полоса, кГц",
          "color_code": "Color Code", "time_slot": "Time Slot", "contact_index": "№ контакта",
          "rx_group_index": "Группа приёма", "scan_list_index": "Список сканирования",
          "privacy_type_code": "Тип Privacy (код)", "privacy_index_code": "Индекс Privacy (с нуля)",
          "rx_tone_code": "Субтон приёма", "tx_tone_code": "Субтон передачи",
          "A": "Каналы стороны A", "B": "Каналы стороны B", "contacts": "Номера контактов",
          "channels": "Номера каналов", "primary": "Приоритет 1", "secondary": "Приоритет 2",
          "revert": "Канал передачи", "callsign": "Позывной", "dtmf_number": "Номер DTMF",
          "ring": "Звуковое оповещение"}
OPTION_LABELS = {"analog": "Аналог", "group": "Групповой", "private": "Индивидуальный", "all": "Общий вызов",
                 "m17": "M17", "dtmf": "DTMF"}
FREE_TEXT = {"Имя", "Имя рации", "Название", "Позывной", "Текст", "Строка 1", "Строка 2",
             "Сообщение", "Отправитель", "Получатель", "Путь APRS", "Серийный номер",
             "Модель", "Модель USB", "Модель файла"}


def option_label(key, value, tr):
    if value is None or value == "":
        return ""
    if key == "bandwidth_code" and value in (0, 1, 2):
        return ["12.5", "20", "25"][value]
    if key == "ring":
        return tr("Вкл." if value else "Выкл.")
    return tr(OPTION_LABELS.get(str(value), str(value)))


def mhz(value):
    return "" if value is None else format(Decimal(value) / 1_000_000, "f")


def next_slot(table, report):
    used = {row["index"] for row in report.get(table, [])}
    if table == "rx_groups":
        used.update(report.get("unrecognized_rx_group_slots", []))
    for idx in range(1, LIMITS[table] + 1):
        if idx not in used:
            return idx
    raise ValueError("Все позиции заняты.")


def new_record(table, report, name, duplicate=None, native=False):
    idx = next_slot(table, report)
    if duplicate is not None:
        row = copy.deepcopy(duplicate)
        if native and table == "channels":
            row["rdt_template_index"] = row.get("rdt_template_index", row["index"])
        row["name"] += " 2"
    else:
        row = {"name": name}
        row.update({"channels": {"mode": "analog", "bandwidth_code": 0},
                    "contacts": {"type": "group", "dmr_id": 1}, "zones": {"A": [], "B": []},
                    "rx_groups": {"contacts": []}, "scan_lists": {"channels": []}}[table])
    row["index"] = idx
    return row


@dataclass
class Field:
    key: str
    label: str
    value: object
    kind: str = "text"
    options: tuple = ()


def fields_for(table, row, native=False):
    if table in TABLES:
        keys = {"channels": ["name", "mode", "rx_hz", "tx_hz", "bandwidth_code", "scan_list_index",
                             "rx_tone_code", "tx_tone_code", "color_code", "time_slot", "contact_index",
                             "rx_group_index", "privacy_type_code", "privacy_index_code"],
                "contacts": ["name", "type", "dmr_id", "ring", "dtmf_number", "callsign"],
                "zones": ["name", "A", "B"], "rx_groups": ["name", "contacts"],
                "scan_lists": ["name", "channels", "primary", "secondary", "revert"]}[table]
        for key in keys:
            opts = OPTIONS.get(key, ())
            if native and key in ("mode", "type"):
                opts = opts[:2 if key == "mode" else 3]
            if opts:
                kind = "option"
            elif key in ("rx_hz","tx_hz"):
                kind = "frequency"
            elif key in ("rx_tone_code","tx_tone_code"):
                kind = "tone"
            elif key in ("A","B","channels","contacts"):
                kind = "members"
            elif key in ("name","callsign","dtmf_number"):
                kind = "text"
            else:
                kind = "number"
            yield Field(key, LABELS[key], row.get(key), kind, tuple(opts))
    for i, f in enumerate(row.get("details" if table in TABLES else "fields", [])):
        opts = CHOICES.get(table, {}).get(f["label"], ())
        if not opts and f["value"] in ("Вкл.", "Выкл."):
            opts = ("Вкл.", "Выкл.")
        yield Field(f"field/{i}", f["label"], f["value"], "choice" if opts else "field", tuple(opts))


def field_text(f, tr):
    if f.value is None:
        return ""
    if f.kind in ("option", "choice"):
        return option_label(f.key, f.value, tr)
    if f.kind == "frequency":
        return mhz(f.value)
    if f.kind == "tone":
        try:
            return cps_tone(f.value)
        except ValueError:
            return f"0x{f.value:04X}"
    if f.kind == "members":
        return ", ".join(str(item["index"] if isinstance(item, dict) else item) for item in f.value)
    return str(f.value)


def parse_field(f, text, tr):
    if f.kind in ("text", "field"):
        return text
    if f.kind in ("option", "choice"):
        if text == "":
            return None
        for v in f.options:
            if option_label(f.key, v, tr) == text:
                return v
    if f.kind == "members":
        try:
            numbers = [int(token.strip()) for token in text.split(",")] if text.strip() else []
        except ValueError:
            raise ValueError(tr("Номера слотов должны быть целыми числами.")) from None
        return [{"index": n} for n in numbers] if f.key in ("A", "B") else numbers
    if f.kind == "tone" and text.strip().casefold() in ("", "none", "off", "выкл.", tr("Выкл.").casefold()):
        return 0xffff
    if not text.strip():
        return None
    if f.kind == "frequency":
        try:
            hz = Decimal(text.replace(",", ".")) * 1_000_000
            if hz.is_finite() and 0 < hz <= 2**63 - 1 and hz == hz.to_integral_value():
                return int(hz)
        except InvalidOperation:
            pass
        raise ValueError(tr("Частота должна быть положительным числом в МГц."))
    if f.kind == "tone":
        code = tone(text.replace(",", "."))
        if code is not None:
            return code
    if f.kind == "number":
        try:
            value = int(text.strip())
            maximum = {"privacy_type_code": 3, "privacy_index_code": 255, "color_code": 15,
                       "encode_slot": 32, "message_slot": 50}.get(f.key, 0xffffff)
            if 0 <= value <= maximum:
                return value
        except ValueError:
            pass
    raise ValueError(tr("Неверное значение для поля «%@».", tr(f.label)))


def apply_field(row, field, value):
    if field.key.startswith("field/"):
        row["details" if "details" in row else "fields"][int(field.key[6:])]["value"] = value
    else:
        row[field.key] = value
