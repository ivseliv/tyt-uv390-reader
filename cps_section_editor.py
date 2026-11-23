"""Patch known public CPS fields in a staged, already validated local RDT copy."""

import re

from cps_dtmf import patch_dtmf, patch_sequences, validate_encode_references
from cps_sections import (
    BUTTONS, EMERGENCY_MODES, EMERGENCY_TYPES, EXTRA_IDS, GPS_FORMATS, INTRO_SCREENS,
    KEYPAD_TIMES, MAIN_MODES, MENU_BITS, MONITOR_TYPES, PERMIT_TONES, PRESSES, SIDE_MODES, TX_MODES,
    VFO_LINKS, VFO_POWER, VFO_TONES, VFO_WIDTH, vfo_tone_code,
)


class PublicEdits:
    def __init__(self, target):
        self.target = target
        self.pending = {}

    def put(self, addr, data):
        # Only this module's constant radio-segment field addresses reach here.
        for i, byte in enumerate(data):
            self.pending[addr + 0x225 + i] = byte

    def bits(self, addr, shift, width, number):
        offset = addr + 0x225
        if offset not in self.pending:
            self.target.seek(offset)
            previous = self.target.read(1)
            if len(previous) != 1:
                raise ValueError("Неполный файл RDT.")
            self.pending[offset] = previous[0]
        mask = ((1 << width) - 1) << shift
        self.pending[offset] = (self.pending[offset] & ~mask) | (number << shift)

    def commit(self):
        for offset, value in sorted(self.pending.items()):
            self.target.seek(offset)
            self.target.write(bytes((value,)))


def enabled(value):
    try:
        return {"Вкл.": 1, "Выкл.": 0, "true": 1, "false": 0}[value]
    except KeyError:
        raise ValueError("Ожидается значение «Вкл.» или «Выкл.».") from None


def number(value, minimum, maximum, step=1, unit=""):
    number_text = value.strip()
    if unit and number_text.endswith(unit):
        number_text = number_text[:-len(unit)].strip()
    number_text = number_text.replace(" ", "").replace("\u202f", "").replace("\xa0", "")
    if not re.fullmatch(r"[0-9]{1,12}", number_text):
        raise ValueError("Ожидалось целое число.")
    parsed = int(number_text)
    if not minimum <= parsed <= maximum or parsed % step:
        raise ValueError(f"Допустимо {minimum}…{maximum} {unit} с шагом {step}.")
    return parsed // step


def choice(value, choices):
    if isinstance(choices, dict):
        for key, name in choices.items():
            if value == name:
                return key
    elif value in choices:
        return choices.index(value)
    raise ValueError("Неизвестное значение настройки.")


def text_bytes(value, size):
    encoded = value.encode("utf-16-le")
    if len(encoded) > size or "\x00" in value or "\uffff" in value:
        raise ValueError(f"Ожидался текст до {size // 2} знаков UTF-16.")
    return encoded.ljust(size, b"\0")


def reference(value, entries, zero=None):
    if value == zero:
        return 0
    match = re.fullmatch(r"(?:№\s*)?(\d+)(?: · (.*))?", value)
    if not match:
        raise ValueError("Укажите номер из списка.")
    index = int(match[1])
    name = next((entry["name"] for entry in entries if entry["index"] == index), None)
    if name is None or (match[2] is not None and match[2] != name):
        raise ValueError("Такой записи нет в выбранном списке.")
    return index


# (byte address, bit, invert user-visible boolean)
FLAGS = {
    "Индикатор LED": (0x2080, 2, False), "Энергосбережение при приёме": (0x2081, 1, False),
    "Преамбула пробуждения": (0x2081, 0, False), "Все звуки отключены": (0x2081, 2, True),
    "Сигнал свободного канала": (0x2081, 4, True), "Озвучивание каналов": (0x2082, 1, False),
    "Звук клавиш": (0x2082, 5, False), "Групповые вызовы": (0x20AB, 0, False),
    "Индивидуальные вызовы": (0x20AB, 1, False), "Общая зона": (0x20D2, 2, False),
    "Редактирование Radio ID на рации": (0x20E0, 6, True),
    "Блокировка передней клавиатуры": (0x20E2, 2, True),
    "Озвучивание при включении": (0x20E2, 5, False),
}
# byte address, minimum and maximum engineering value, unit step per stored byte, rendered unit
SCALED = {
    "Чувствительность VOX": (0x208B, 1, 10, 1, ""),
    "Преамбула передачи": (0x2088, 0, 8640, 60, "мс"),
    "Удержание группового вызова": (0x2089, 0, 7000, 100, "мс"),
    "Удержание индивидуального вызова": (0x208A, 0, 7000, 100, "мс"),
    "Предупреждение о разряде": (0x208E, 0, 635, 5, "с"),
    "Задержка канала": (0x20D0, 0, 7000, 100, "мс"),
}
MENU_LABELS = {name: (0x20F0 + byte, shift, invert) for name, _, byte, shift, invert in MENU_BITS}


def write_general(patch, field, value, baseline, edited):
    if field in FLAGS:
        addr, shift, inverted = FLAGS[field]
        patch.bits(addr, shift, 1, enabled(value) ^ inverted)
    elif field in SCALED:
        addr, low, high, step, unit = SCALED[field]
        patch.put(addr, bytes((number(value, low, high, step, unit),)))
    elif field in ("Имя рации", "Строка 1", "Строка 2"):
        addr, size = {"Имя рации": (0x20B0, 32), "Строка 1": (0x2040, 20), "Строка 2": (0x2054, 20)}[field]
        patch.put(addr, text_bytes(value, size))
    elif field == "DMR ID":
        new_id = number(value, 0, 0xFFFFFF)
        if edited["dmr_id"] != baseline["dmr_id"] and edited["dmr_id"] != new_id:
            raise ValueError("DMR ID в основной таблице отличается.")
        patch.put(0x2084, new_id.to_bytes(3, "little"))
    elif field == "Часовой пояс":
        match = re.fullmatch(r"UTC([+-]\d{1,2})", value)
        if not match or not -12 <= int(match[1]) <= 12:
            raise ValueError("Ожидался часовой пояс от UTC-12 до UTC+12.")
        patch.bits(0x20AB, 3, 5, int(match[1]) + 12)
    elif field == "Усиление микрофона (код CPS)":
        patch.bits(0x20E0, 3, 3, number(value, 0, 6))
    elif field in EXTRA_IDS:
        patch.put(EXTRA_IDS[field], number(value, 0, 0xFFFFFF).to_bytes(3, "little"))
    elif field == "Выбор канала передачи":
        patch.bits(0x2080, 6, 2, choice(value, TX_MODES))
    elif field == "Формат отчёта GPS":
        patch.bits(0x20E0, 7, 1, choice(value, GPS_FORMATS))
    elif field == "Автоблокировка клавиатуры":
        patch.put(0x2096, bytes((choice(value, KEYPAD_TIMES),)))
    elif field == "Заставка":
        patch.bits(0x2082, 4, 1, choice(value, INTRO_SCREENS))
    elif field == "Монитор":
        patch.bits(0x2080, 4, 1, choice(value, MONITOR_TYPES))
    elif field == "Сигнал разрешения передачи":
        patch.bits(0x2081, 6, 2, choice(value, PERMIT_TONES))
    elif field == "Оповещение о вызове":
        patch.put(0x208F, bytes((0 if value == "Непрерывно" else number(value, 5, 1200, 5, "с"),)))
    elif field == "Подсветка":
        patch.bits(0x2095, 0, 2, 0 if value == "Всегда" else number(value, 5, 15, 5, "с"))
    else:
        raise ValueError("Сведения доступны только для просмотра.")


def write_field(patch, section, title, field, value, baseline, edited):
    if section == "general":
        write_general(patch, field, value, baseline, edited)
    elif section == "menu":
        if field == "Время показа":
            patch.put(0x20F0, bytes((0 if value == "Без ограничения" else number(value, 1, 30, 1, "с"),)))
        else:
            addr, shift, inverted = MENU_LABELS[field]
            patch.bits(addr, shift, 1, enabled(value) ^ inverted)
    elif section == "buttons" and field in PRESSES:
        patch.put(0x2102 + PRESSES.index(field), bytes((choice(value, BUTTONS),)))
    elif section == "buttons" and title == "Долгое нажатие":
        patch.put(0x2111, bytes((number(value, 1000, 3750, 250, "мс"),)))
    elif section == "lone_worker":
        addr, unit = {"Время для ответа": (0x2090, "мин"), "Интервал напоминания": (0x2091, "с")}[field]
        patch.put(addr, bytes((number(value, 1, 255, 1, unit),)))
    elif section == "scan_timing":
        if title == "Удержание на канале":
            addr = {"DMR": 0x2093, "Аналог": 0x2094}[field]
            patch.put(addr, bytes((number(value, 500, 10000, 100, "мс"),)))
        else:
            idx = number(title[2:].split(" · ")[0] if title.startswith("№ ") else "", 1, 250)
            offset, low, high, step = {"Удержание сигнала": (0x27, 50, 6375, 25),
                                      "Опрос приоритетного канала": (0x28, 750, 7750, 250)}[field]
            patch.put(0x18860 + (idx - 1) * 0x68 + offset, bytes((number(value, low, high, step, "мс"),)))
    elif section == "vfo" and title == "Режимы дисплея":
        if field == "Основной режим":
            patch.put(0x2097, bytes((255 * choice(value, MAIN_MODES),)))
        else:
            patch.bits(0x2083, {"Сторона A": 2, "Сторона B": 7}[field], 1, choice(value, SIDE_MODES))
    elif section == "vfo" and title in ("VFO A", "VFO B"):
        addr = 0x2EF00 if title == "VFO A" else 0x2EF40
        if field in ("RX", "TX"):
            frequency = number(value, 10, 999999990, 10, "Гц")
            patch.put(addr + (16 if field == "RX" else 20), bytes.fromhex(f"{frequency:08d}")[::-1])
        elif field == "Шаг":
            patch.put(addr + 32, bytes((number(value, 2500, 50000, 2500, "Гц") - 1,)))
        elif field == "Color Code":
            patch.bits(addr + 1, 4, 4, number(value, 0, 15))
        elif field == "Time Slot":
            patch.bits(addr + 1, 2, 2, number(value, 1, 2))
        elif field == "Только приём":
            patch.bits(addr + 1, 1, 1, enabled(value))
        elif field == "Мощность":
            patch.bits(addr + 30, 0, 2, choice(value, VFO_POWER))
        elif field == "Полоса":
            patch.bits(addr, 2, 2, choice(value, VFO_WIDTH))
        elif field in VFO_LINKS:
            table, offset, size = VFO_LINKS[field]
            ref = reference(value, edited[table], "Нет")
            maximum = 10000 if table == "contacts" else 250
            if not 0 <= ref <= maximum:
                raise ValueError(f"Номер записи должен быть в диапазоне 1–{maximum}.")
            patch.put(addr + offset, ref.to_bytes(size, "little"))
        elif field in VFO_TONES:
            patch.put(addr + VFO_TONES[field], vfo_tone_code(value).to_bytes(2, "little"))
        else:
            raise ValueError("Неизвестное поле VFO.")
    elif section == "gps":
        idx = number(title[4:] if title.startswith("GPS ") else "", 1, 16)
        addr = 0x3EC40 + (idx - 1) * 16
        if field == "Период":
            patch.put(addr + 2, bytes((number(value, 30, 7200, 30, "с"),)))
        elif field == "Контакт":
            patch.put(addr + 4, reference(value, edited["contacts"]).to_bytes(2, "little"))
        elif field == "Канал":
            ref = 0xFFFF if value == "Не задан" else reference(value, edited["channels"], "Выбранный канал")
            patch.put(addr, ref.to_bytes(2, "little"))
        else:
            raise ValueError("Неизвестное поле GPS.")
    elif section == "emergency":
        if title == "Общие параметры":
            bits = {"Мониторинг при тревоге": 2, "Удалённый мониторинг": 1, "Удалённое отключение": 0}
            if field in bits:
                patch.bits(0x5A70, bits[field], 1, enabled(value))
            else:
                offset, step, unit = {"Длительность мониторинга": (1, 10, "с"), "Тайм-аут передачи": (2, 25, "мс"),
                                      "Лимит сообщений": (3, 1, "")}[field]
                patch.put(0x5A70 + offset, bytes((number(value, 0, step * 255, step, unit),)))
        else:
            idx = number(title[2:].split(" · ")[0] if title.startswith("№ ") else "", 1, 32)
            addr = 0x5A80 + (idx - 1) * 0x28
            if field == "Тип":
                patch.bits(addr + 32, 0, 2, choice(value, EMERGENCY_TYPES))
            elif field == "Режим":
                patch.bits(addr + 32, 4, 2, choice(value, EMERGENCY_MODES))
            elif field == "Канал":
                ref = 0xFFFF if value == "Выбранный канал" else reference(value, edited["channels"])
                patch.put(addr + 36, ref.to_bytes(2, "little"))
            else:
                offset, step, unit = {"Повторы без ожидания": (33, 1, ""), "Повторы с ожиданием": (34, 1, ""),
                                      "Открытый микрофон": (35, 10, "с")}[field]
                patch.put(addr + offset, bytes((number(value, 0, step * 255, step, unit),)))
    else:
        raise ValueError("Это поле доступно только для просмотра.")


def sms_slots(section):
    if section is None:
        return {}
    if (section.keys() != {"id", "title", "rows"} or section["title"] != "Шаблоны сообщений"
            or not isinstance(section["rows"], list)):
        raise ValueError("Изменилась структура раздела шаблонов CPS.")
    result = {}
    for entry in section["rows"]:
        if not isinstance(entry, dict) or entry.keys() != {"name", "fields"} or not isinstance(entry["name"], str):
            raise ValueError("Ошибка в записи шаблона SMS.")
        match = re.fullmatch(r"Шаблон ([1-9][0-9]?)", entry["name"])
        if not match:
            raise ValueError("Имена слотов SMS должны иметь формат «Шаблон N», N = 1…50.")
        idx = number(match[1], 1, 50)
        fields = entry["fields"]
        if (not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict)
                or fields[0].keys() != {"label", "value"} or fields[0]["label"] != "Текст"
                or not isinstance(fields[0]["value"], str) or not fields[0]["value"] or idx in result):
            raise ValueError(f"Шаблон {idx}: требуется уникальный слот с одним непустым полем «Текст».")
        result[idx] = fields[0]["value"]
    return result


def section_map(sections):
    if not isinstance(sections, list):
        raise ValueError("Ожидался список дополнительных настроек.")
    by_id = {}
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("id"), str) or section["id"] in by_id:
            raise ValueError("Недопустимый или повторный раздел CPS.")
        by_id[section["id"]] = section
    return by_id


def scan_rows_by_index(rows):
    result = {}
    for entry in rows:
        title = entry.get("name") if isinstance(entry, dict) else None
        match = re.fullmatch(r"№ ([1-9][0-9]*) · .*", title) if isinstance(title, str) else None
        idx = int(match[1]) if match else 0
        if (not match and title != "Удержание на канале") or idx in result:
            raise ValueError("Некорректная строка таймеров сканирования.")
        result[idx] = entry
    return result


def take_slots(sections, key, prefix, limit, with_name=False):
    """Split out editable slots while leaving static controls for ordinary field diffs."""
    section = sections.get(key)
    if section is None:
        return {}
    from cps_sections import TITLES
    if section.keys() != {"id", "title", "rows"} or section["title"] != TITLES[key] or not isinstance(section["rows"], list):
        raise ValueError(f"Неверная структура раздела «{TITLES[key]}».")
    pattern = re.escape(prefix) + r"([1-9][0-9]*)" + (r" · (.+)" if with_name else "")
    slots, other = {}, []
    for entry in section["rows"]:
        name = entry.get("name") if isinstance(entry, dict) else None
        match = re.fullmatch(pattern, name) if isinstance(name, str) else None
        if not match:
            other.append(entry)
            continue
        index = number(match[1], 1, limit)
        if index in slots or entry.keys() != {"name", "fields"}:
            raise ValueError(f"Повторная или недопустимая запись «{name}».")
        slots[index] = entry
    if other:
        sections[key] = {**section, "rows": other}
    else:
        del sections[key]
    return slots


def field_values(entry):
    values = {}
    if not isinstance(entry.get("fields"), list):
        raise ValueError("В записи отсутствуют поля.")
    for field in entry["fields"]:
        if (not isinstance(field, dict) or field.keys() != {"label", "value"}
                or not isinstance(field["label"], str) or not isinstance(field["value"], str)
                or field["label"] in values):
            raise ValueError(f"Некорректное поле в «{entry['name']}».")
        values[field["label"]] = field["value"]
    return values


PROFILE_SPECS = {
    "gps": ("GPS ", 16, ("Период", "Контакт", "Канал"), "Система GPS"),
    "emergency": ("№ ", 32, ("Тип", "Режим", "Повторы без ожидания", "Повторы с ожиданием",
                             "Открытый микрофон", "Канал"), "Система тревоги"),
}


def patch_profiles(patch, original, updated, baseline, edited):
    for key, (prefix, count, labels, channel_label) in PROFILE_SPECS.items():
        before = take_slots(original, key, prefix, count, with_name=key == "emergency")
        after = take_slots(updated, key, prefix, count, with_name=key == "emergency")
        deleted = before.keys() - after.keys()
        if deleted:
            for ch in edited.get("channels", []):
                for field in ch.get("details", []):
                    if field["label"] == channel_label and field["value"] in {str(i) for i in deleted}:
                        raise ValueError(f"{channel_label} № {field['value']} используется каналом № {ch['index']}.")
        for idx in before.keys() | after.keys():
            old, new = before.get(idx), after.get(idx)
            if old == new:
                continue
            if new is None:
                if key == "gps":
                    addr = 0x3EC40 + (idx - 1) * 16
                    patch.put(addr + 2, bytes(1))
                    patch.put(addr + 4, bytes(2))
                else:
                    patch.put(0x5A80 + (idx - 1) * 40 + 36, bytes(2))
                continue
            values = field_values(new)
            if set(values) != set(labels):
                raise ValueError(f"«{new['name']}»: требуются поля {', '.join(labels)}.")
            prior = field_values(old) if old else {}
            if key == "emergency" and (not old or old["name"] != new["name"]):
                name = new["name"].split(" · ", 1)[1]
                if not name.strip() or not name.isprintable():
                    raise ValueError("Укажите печатное имя аварийной системы.")
                patch.put(0x5A80 + (idx - 1) * 40, text_bytes(name, 32))
            for label, value in values.items():
                if value != prior.get(label):
                    if key == "gps" and label == "Канал" and value not in ("Выбранный канал", "Не задан"):
                        reference(value, [c for c in edited["channels"] if c.get("mode", "DMR") == "DMR"])
                    write_field(patch, key, new["name"], label, value, baseline, edited)


def quick_slots(groups):
    digital = take_slots(groups, "buttons", "Быстрый доступ ", 6)
    analog = take_slots(groups, "dtmf", "Быстрый доступ ", 6)
    if digital.keys() & analog.keys():
        raise ValueError("Одна кнопка быстрого доступа назначена одновременно для DMR и DTMF.")
    result = {idx: ("DMR", row) for idx, row in digital.items()}
    result.update({idx: ("DTMF", row) for idx, row in analog.items()})
    return result


def patch_quick(patch, original, updated, edited, sms):
    before, after = quick_slots(original), quick_slots(updated)
    for idx in before.keys() | after.keys():
        addr = 0x2114 + (idx - 1) * 4
        if before.get(idx) == after.get(idx):
            continue
        if idx not in after:
            patch.bits(addr, 4, 2, 0)
            continue
        kind, entry = after[idx]
        old_kind, old_row = before.get(idx, (None, None))
        values = field_values(entry)
        old_fields = field_values(old_row) if old_row is not None and kind == old_kind else {}
        if kind == "DTMF":
            if values.keys() not in ({"Профиль"}, {"Профиль", "Номер Encode"}) or not re.fullmatch(r"DTMF-[1-4]", values["Профиль"]):
                raise ValueError(f"«{entry['name']}»: укажите профиль DTMF-1…DTMF-4.")
            patch.bits(addr, 4, 2, 2)
            patch.bits(addr, 0, 4, 7 + int(values["Профиль"][-1]))
            if "Номер Encode" in values and values["Номер Encode"] != old_fields.get("Номер Encode"):
                patch.put(addr + 1, bytes((number(values["Номер Encode"], 0, 32),)))
            elif "Номер Encode" not in values and old_kind != "DTMF":
                patch.put(addr + 1, b"\0")
        else:
            command = values.get("Действие")
            expected = {"Действие", "Контакт", "Шаблон"} if command == "Сообщение" else {"Действие", "Контакт"}
            if values.keys() != expected:
                raise ValueError(f"«{entry['name']}»: для вызова нужен контакт, для сообщения — также шаблон.")
            if command != old_fields.get("Действие"):
                patch.bits(addr, 0, 4, choice(command, ("Вызов", "Сообщение")))
                patch.bits(addr, 4, 2, 1)
            if values["Контакт"] != old_fields.get("Контакт") or kind != old_kind:
                ref = reference(values["Контакт"], edited["contacts"])
                patch.put(addr + 2, ref.to_bytes(2, "little"))
            if command == "Сообщение" and values["Шаблон"] != old_fields.get("Шаблон"):
                ref = number(values["Шаблон"], 1, 50)
                if ref not in sms:
                    raise ValueError(f"«{entry['name']}»: не существует шаблона № {ref}.")
                patch.put(addr + 1, bytes((ref,)))

    first = take_slots(original, "buttons", "Цифровая клавиша · позиция ", 10)
    last = take_slots(updated, "buttons", "Цифровая клавиша · позиция ", 10)
    for idx in first.keys() | last.keys():
        if first.get(idx) == last.get(idx):
            continue
        ref = 0
        if idx in last:
            values = field_values(last[idx])
            if values.keys() != {"Контакт"}:
                raise ValueError("Цифровой клавише можно назначить один контакт.")
            ref = reference(values["Контакт"], edited["contacts"])
        patch.put(0x212C + (idx - 1) * 2, ref.to_bytes(2, "little"))


def align_vfo(patch, old, new):
    if old["name"] not in ("VFO A", "VFO B"):
        return old
    before, after = field_values(old), field_values(new)
    if before.get("Режим") == after.get("Режим"):
        return old
    code = choice(after.get("Режим"), {1: "Аналог", 2: "DMR"})
    base = 0x2EF00 if old["name"] == "VFO A" else 0x2EF40
    patch.bits(base, 0, 2, code)
    digital = {"Color Code", "Time Slot", "Контакт", "Группа приёма"}
    analog = {"Полоса", *VFO_TONES}
    specific = digital | analog
    if before.keys() - specific != after.keys() - specific or (specific & after.keys()) != (digital if code == 2 else analog):
        raise ValueError("Набор полей VFO не соответствует выбранному аналоговому или цифровому режиму.")
    before["Режим"] = after["Режим"]
    return {"name": old["name"], "fields": [
        {"label": label, "value": before.get(label)} for label in after]}


def patch_sections(target, baseline, edited):
    """Apply changed values only after validating all edits. The caller owns the staged file."""
    original = baseline.get("cps_sections", [])
    updated = edited.get("cps_sections", [])
    if updated == original:
        return
    validate_encode_references(baseline, edited)
    original, updated = section_map(original), section_map(updated)
    patch = PublicEdits(target)
    old_dtmf, new_dtmf = original.pop("dtmf_systems", None), updated.pop("dtmf_systems", None)
    old_sequences = original.pop("dtmf_sequences", None)
    new_sequences = updated.pop("dtmf_sequences", None)
    old_sms, new_sms = sms_slots(original.pop("messages", None)), sms_slots(updated.pop("messages", None))
    deleted_sms = old_sms.keys() - new_sms.keys()
    if deleted_sms:
        for entry in updated.get("buttons", {}).get("rows", []):
            for field in entry["fields"]:
                if field["label"] == "Шаблон" and field["value"] in {str(i) for i in deleted_sms}:
                    raise ValueError(f"Шаблон {field['value']} используется в «{entry['name']}».")
    for idx in old_sms.keys() | new_sms.keys():
        if old_sms.get(idx) != new_sms.get(idx):
            patch.put(0x2180 + (idx - 1) * 0x120, text_bytes(new_sms.get(idx, ""), 0x120))
    patch_profiles(patch, original, updated, baseline, edited)
    patch_quick(patch, original, updated, edited, new_sms)
    if updated.keys() != original.keys():
        raise ValueError("Менять набор дополнительных разделов CPS в RDT нельзя.")
    for key, old in original.items():
        new = updated[key]
        if (not isinstance(new, dict) or new.keys() != old.keys() or
                old["id"] != new.get("id") or old["title"] != new.get("title")):
            raise ValueError(f"Структура раздела «{old['title']}» изменилась.")
        rows = new.get("rows")
        if key == "scan_timing" and isinstance(rows, list):
            old_scans, new_scans = scan_rows_by_index(old["rows"]), scan_rows_by_index(rows)
            added_scans = {i["index"] for i in edited["scan_lists"]} - {i["index"] for i in baseline["scan_lists"]}
            removed_scans = {i["index"] for i in baseline["scan_lists"]} - {i["index"] for i in edited["scan_lists"]}
            if old_scans.keys() - new_scans.keys() - removed_scans or new_scans.keys() - old_scans.keys() - added_scans:
                raise ValueError("Строка таймеров не соответствует списку сканирования.")
            scan_names = {r["index"]: r["name"] for r in edited["scan_lists"]}
            for idx, entry in new_scans.items():
                if (idx and entry["name"] != old_scans.get(idx, {}).get("name")
                        and entry["name"] != f"№ {idx} · {scan_names.get(idx)}"):
                    raise ValueError("Название строки таймера должно совпадать с названием списка.")
            for idx in new_scans.keys() - old_scans.keys():
                old_scans[idx] = {"name": new_scans[idx]["name"], "fields": [
                    {"label": "Удержание сигнала", "value": "500 мс"},
                    {"label": "Опрос приоритетного канала", "value": "2000 мс"}]}
            old = {**old, "rows": [{**old_scans[idx], "name": new_scans[idx]["name"]} for idx in new_scans]}
        if not isinstance(rows, list) or len(rows) != len(old["rows"]):
            raise ValueError(f"В разделе «{old['title']}» изменился набор записей.")
        for prev, curr in zip(old["rows"], rows):
            if not isinstance(curr, dict) or curr.keys() != prev.keys() or curr.get("name") != prev["name"]:
                raise ValueError(f"Имя записи «{prev['name']}» доступно только для просмотра.")
            if key == "vfo":
                prev = align_vfo(patch, prev, curr)
            fields = curr.get("fields")
            if not isinstance(fields, list) or len(fields) != len(prev["fields"]):
                raise ValueError(f"Изменился набор полей записи «{prev['name']}».")
            for before, after in zip(prev["fields"], fields):
                if (not isinstance(after, dict) or after.keys() != before.keys() or
                        after.get("label") != before["label"] or not isinstance(after.get("value"), str)):
                    raise ValueError(f"Некорректная структура поля «{before['label']}».")
                if before["value"] == after["value"]:
                    continue
                try:
                    write_field(patch, old["id"], prev["name"], before["label"], after["value"], baseline, edited)
                except (KeyError, ValueError) as error:
                    raise ValueError(f"«{prev['name']}», поле «{before['label']}»: {error}") from None
    patch_dtmf(target, old_dtmf, new_dtmf)
    patch_sequences(target, old_sequences, new_sequences)
    patch.commit()


def verified_rows(sections, key):
    rows = sections.get(key, {}).get("rows", [])
    if key == "scan_timing":
        return scan_rows_by_index(rows)
    return {r["name"]: r for r in rows}


def same_readback(asked, actual):
    if asked == actual or (asked in ("true", "false") and ("Вкл." if asked == "true" else "Выкл.") == actual):
        return True
    if actual is None:
        return asked == ""
    target_match = re.fullmatch(r"№ (\d+)(?: · .*)?", actual)
    requested_match = re.fullmatch(r"(?:№\s*)?(\d+)(?: · .*)?", asked)
    if target_match and requested_match:
        return int(target_match[1]) == int(requested_match[1])
    # Local edits can omit the rendered unit and group digits with spaces.
    actual_number = re.fullmatch(r"([\d\s]+)(Гц|мс|с|мин)?", actual)
    requested_number = re.fullmatch(r"([\d\s]+)(Гц|мс|с|мин)?", asked.strip())
    if actual_number and requested_number and requested_number[2] in (None, actual_number[2]):
        return "".join(actual_number[1].split()).lstrip("0") == "".join(requested_number[1].split()).lstrip("0")
    return False


def verify_sections(baseline, edited, actual):
    """Verify only requested edits against the decoder, allowing updated names on linked rows."""
    wanted = section_map(edited.get("cps_sections", []))
    before = section_map(baseline.get("cps_sections", []))
    parsed = section_map(actual.get("cps_sections", []))
    for key, prefix, limit, named in (("gps", "GPS ", 16, False), ("emergency", "№ ", 32, True),
                                     ("buttons", "Быстрый доступ ", 6, False),
                                     ("dtmf", "Быстрый доступ ", 6, False),
                                     ("buttons", "Цифровая клавиша · позиция ", 10, False)):
        old = take_slots(dict(before), key, prefix, limit, named)
        new = take_slots(dict(wanted), key, prefix, limit, named)
        if old != new and new.keys() != take_slots(dict(parsed), key, prefix, limit, named).keys():
            raise ValueError(f"После сохранения изменился набор записей в разделе {key}.")
    if wanted.get("messages") != before.get("messages"):
        if sms_slots(wanted.get("messages")) != sms_slots(parsed.get("messages")):
            raise ValueError("После сохранения состав или текст шаблонов SMS не совпал.")
    for section, group in wanted.items():
        if section == "messages" or group == before.get(section):
            continue
        first, last = verified_rows(before, section), verified_rows(parsed, section)
        for key, entry in verified_rows(wanted, section).items():
            prior = {f["label"]: f["value"] for f in first.get(key, {}).get("fields", [])}
            readback = {f["label"]: f["value"] for f in last.get(key, {}).get("fields", [])}
            for field in entry["fields"]:
                label, value = field["label"], field["value"]
                if section == "dtmf_sequences" and value.upper() == readback.get(label):
                    continue
                if (section == "vfo" and label in VFO_TONES and value != prior.get(label)
                        and readback.get(label) is not None and vfo_tone_code(value) == vfo_tone_code(readback[label])):
                    continue
                if value != prior.get(label) and not same_readback(value, readback.get(label)):
                    raise ValueError(f"После сохранения не совпало поле «{label}» в «{entry['name']}».")
