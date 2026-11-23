"""Local CPS slot and assignment editing. Changes are committed via Document.apply."""

import re

from cps_dtmf import SEQUENCE_ID
from cps_section_editor import text_bytes
from cps_sections import TITLES
from rdt_editor import label_bytes
from rdt_template import transfer_rdt
from win_document import MODELS


SLOT_LIMITS = {"messages": 50, "gps": 16, "emergency": 32}
SLOT_PATTERNS = {"messages": r"Шаблон ([1-9][0-9]*)", "gps": r"GPS ([1-9][0-9]*)",
                 "emergency": r"№ ([1-9][0-9]*) · .+"}
DIGITAL = {"group", "private", "all"}


def _native(report):
    return (report.get("file_model") or report.get("usb_model")) in MODELS


def _section(report, kind, create=False):
    found = next((s for s in report.get("cps_sections", []) if s["id"] == kind), None)
    if found is None and create:
        found = {"id": kind, "title": TITLES[kind], "rows": []}
        report.setdefault("cps_sections", []).append(found)
    return found


def _rows(report, kind):
    return (_section(report, kind) or {}).get("rows", [])


def slot_sections(report):
    return tuple(SLOT_LIMITS) if _native(report) else ("messages",)


def slot_number(kind, row_name):
    match = re.fullmatch(SLOT_PATTERNS[kind], row_name) if kind in SLOT_PATTERNS else None
    if match and int(match[1]) <= SLOT_LIMITS[kind]:
        return int(match[1])
    return None


def _partial(report):
    return bool(report.get("usb_model") and any(
        "блок Privacy" in note for note in report.get("read_notes", [])))


def slot_note(report, kind):
    if _partial(report):
        if kind == "emergency":
            return "По USB считаны позиции 11–32. Исходные записи 1–10 неизвестны."
        if kind == "messages":
            return "По USB считаны позиции 1–48. Исходные записи 49–50 неизвестны."
    if kind in ("quick", "numberKeys") and _native(report) and not _buttons_known(report):
        return "Для непрочитанной позиции исходное назначение неизвестно. Можно задать его в локальной копии."
    return None


def _fields(pairs):
    return [{"label": name, "value": value} for name, value in pairs]


def add_slot(doc, kind, *, text="Новое сообщение", name=None):
    """Add a local entry using a free native slot, preferring the known area of partial USB snapshots."""
    report = doc.draft()
    if kind not in slot_sections(report):
        raise ValueError("В открытом источнике нет данных этого раздела.")
    section = _section(report, kind, create=True)
    occupied = {slot_number(kind, row["name"]) for row in section["rows"]}
    order = range(1, SLOT_LIMITS[kind] + 1)
    if kind == "emergency" and _partial(report):
        order = (*range(11, 33), *range(1, 11))
    index = next((i for i in order if i not in occupied), None)
    if index is None:
        raise ValueError("Все позиции заняты.")
    if kind == "messages":
        if not isinstance(text, str) or not text:
            raise ValueError("Ожидался текст до 144 знаков UTF-16.")
        text_bytes(text, 288)
        title = f"Шаблон {index}"
        fields = [("Текст", text)]
    elif kind == "gps":
        contacts = [c for c in report["contacts"] if c["type"] in DIGITAL]
        if not contacts:
            raise ValueError("Сначала добавьте цифровой контакт.")
        contact = next((c for c in contacts if c["type"] == "private"), contacts[0])
        title = f"GPS {index}"
        fields = [("Период", "30 с"), ("Контакт", f"№ {contact['index']} · {contact['name']}"),
                  ("Канал", "Выбранный канал")]
    else:
        profile_name = f"Система {index}" if name is None else name
        if not profile_name.strip() or not profile_name.isprintable():
            raise ValueError("Укажите печатное имя аварийной системы.")
        label_bytes(profile_name)
        title = f"№ {index} · {profile_name}"
        fields = [("Тип", "Обычная"), ("Режим", "Тревога"), ("Повторы без ожидания", "1"),
                  ("Повторы с ожиданием", "1"), ("Открытый микрофон", "10 с"), ("Канал", "Выбранный канал")]
    section["rows"].append({"name": title, "fields": _fields(fields)})
    doc.apply(report)
    return title


def delete_slot(doc, kind, row_name):
    """Remove only an actual numbered slot; normalize verifies channel/quick-button references."""
    report = doc.draft()
    if kind not in slot_sections(report) or slot_number(kind, row_name) is None:
        raise ValueError("Эта строка доступна только для чтения.")
    section = _section(report, kind)
    row = next((r for r in _rows(report, kind) if r["name"] == row_name), None)
    if row is None:
        raise ValueError("Запись отсутствует")
    section["rows"].remove(row)
    doc.apply(report)


def save_dtmf(doc, template, path):
    sections = [s for s in doc.report.get("cps_sections", []) if s["id"] == SEQUENCE_ID]
    if len(sections) != 1:
        raise ValueError("Ожидается один раздел последовательностей DTMF.")
    transfer_rdt(template, path, {"source_format": "dtmf", "cps_sections": sections})


def _buttons_known(report):
    return any(r["name"] in ("Долгое нажатие", "Боковые кнопки") for r in _rows(report, "buttons"))


def _assignment_name(kind, index):
    return ("Быстрый доступ " if kind == "quick" else "Цифровая клавиша · позиция ") + str(index)


def _assignment_count(kind):
    if kind not in ("quick", "numberKeys"):
        raise ValueError("Недопустимая таблица назначений.")
    return 6 if kind == "quick" else 10


def assignments(report, kind):
    count = _assignment_count(kind)
    rows = _rows(report, "buttons") + (_rows(report, "dtmf") if kind == "quick" else [])
    fields = {row["name"]: {f["label"]: f["value"] for f in row["fields"]} for row in rows}
    known_buttons = _buttons_known(report)
    blank = report.get("local_empty_slots", {}).get(kind, [])
    result = []
    for index in range(1, count + 1):
        name = _assignment_name(kind, index)
        values = fields.get(name, {})
        known = known_buttons or index in blank
        match = re.fullmatch(r"№\s*([0-9]+)(?: · .*)?", values.get("Контакт", ""))
        contact = int(match[1]) if match else None
        record = {"index": index, "name": name, "contact_index": contact}
        if kind == "quick":
            action = values.get("Действие") or ("DTMF" if "Профиль" in values else
                                              "Не назначено" if known else "Нет данных")
            record.update(quick_type=action, contact_index=contact or 0,
                          message_slot=int(values.get("Шаблон", "1")), profile=values.get("Профиль", "DTMF-1"),
                          encode_slot=int(values.get("Номер Encode", "0")))
        elif contact is None and known:
            record["contact_index"] = 0
        result.append(record)
    return result


def set_assignment(doc, kind, row):
    count = _assignment_count(kind)
    index = row.get("index")
    if type(index) is not int or not 1 <= index <= count or not _native(doc.report):
        raise ValueError("Недопустимая позиция назначения.")
    report = doc.draft()
    name = _assignment_name(kind, index)
    contact_id = row.get("contact_index")
    if ((kind == "numberKeys" and contact_id is None)
            or (kind == "quick" and row.get("quick_type") == "Нет данных")):
        return
    empty = contact_id == 0 if kind == "numberKeys" else row.get("quick_type") == "Не назначено"
    sources = ("buttons", "dtmf") if kind == "quick" else ("buttons",)
    had_encode = any(r["name"] == name and any(f["label"] == "Номер Encode" for f in r["fields"])
                     for r in _rows(report, "dtmf"))
    for source in sources:
        section = _section(report, source)
        if section is not None:
            section["rows"] = [r for r in section["rows"] if r["name"] != name]
    if not _buttons_known(report):
        marks = report.setdefault("local_empty_slots", {})
        slots = set(marks.get(kind, []))
        if empty:
            slots.add(index)
        else:
            slots.discard(index)
        if slots:
            marks[kind] = sorted(slots)
        else:
            marks.pop(kind, None)
        if not marks:
            report.pop("local_empty_slots")
    if not empty:
        mode = row.get("quick_type") if kind == "quick" else None
        target = "dtmf" if mode == "DTMF" else "buttons"
        if mode == "DTMF":
            profile = row.get("profile", "DTMF-1")
            if profile not in ("DTMF-1", "DTMF-2", "DTMF-3", "DTMF-4"):
                raise ValueError(f"«{name}»: укажите профиль DTMF-1…DTMF-4.")
            encode = row.get("encode_slot") or 0
            if type(encode) is not int or not 0 <= encode <= 32:
                raise ValueError(f"«{name}»: укажите номер Encode от 0 до 32.")
            data = [("Профиль", profile)]
            if had_encode or encode:
                data.append(("Номер Encode", str(encode)))
        else:
            if kind == "quick" and mode not in ("Вызов", "Сообщение"):
                raise ValueError("Недопустимое действие быстрого доступа.")
            contact = next((c for c in report["contacts"] if c["index"] == contact_id and c["type"] in DIGITAL), None)
            if type(contact_id) is not int or contact is None:
                raise ValueError(f"{name}. Выберите существующий контакт.")
            data = [] if kind == "numberKeys" else [("Действие", mode)]
            data.append(("Контакт", f"№ {contact_id} · {contact['name']}"))
            if mode == "Сообщение":
                template = row.get("message_slot")
                if type(template) is not int or not 1 <= template <= 50:
                    raise ValueError(f"«{name}»: не существует шаблона № {template}.")
                data.append(("Шаблон", str(template)))
        _section(report, target, create=True)["rows"].append({"name": name, "fields": _fields(data)})
    doc.apply(report)
