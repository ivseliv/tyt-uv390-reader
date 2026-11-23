"""DTMF system timing, PTT IDs, transmit codes and ordinary decoder IDs in local TYT RDT files."""

# CPS V2.42 reads the first table at radio address 0x30000 with a 0x30-byte
# stride. The file adds the 0x225-byte CPS header in this address range.
BASE = 0x30225
STRIDE = 0x30
# Offset, caption, minimum in displayed units, units per stored byte.
TIMERS = (
    (0, "Задержка первой цифры, мс", 100, 10),
    (1, "Длительность первой цифры, мс", 30, 10),
    (2, "Длительность цифры, мс", 30, 10),
    (3, "Интервал между цифрами, мс", 30, 10),
    (4, "Длительность * и #, мс", 0, 10),
    (5, "Длительность клавиши D, мс", 0, 10),
    (6, "Следующая последовательность, с", 2, 1),
    (7, "Автоматический сброс, с", 1, 1),
)
SIDE_TONE = "Самопрослушивание DTMF"
PTT_ID = "Режим PTT ID"
PTT_MODES = ("Выкл.", "Pre Only", "Post Only", "Pre & Post")
GROUP_CODE = "Групповой символ"
GROUP_CODES = ("Нет", "A", "B", "C", "D", "*", "#")
SEQUENCE_ID = "dtmf_sequences"
SEQUENCE_TITLE = "Последовательности DTMF"
UNKNOWN = "Не считано"
SERVICE = "Служебный профиль CPS"
SYMBOLS = "0123456789ABCD*#"


def sequence_slots():
    # Length byte followed by 15 packed bytes, first symbol in the low nibble.
    # Addresses below are file offsets, relative to the first DTMF system.
    for idx in range(4):
        start = BASE + idx * STRIDE
        yield f"Система {idx + 1}", (("KeyUp Encode", start + 16), ("KeyDown Encode", start + 32)), None
    for idx in range(32):
        yield f"Передача {idx + 1}", (("Последовательность", BASE + 0xC0 + idx * 16),), None
    for idx in range(8):
        start = BASE + 0x2C0 + idx * 24
        yield f"Декодер {idx + 1}", (("DTMF ID", start),), start + 16


def read_sequence(read_at, pos):
    size = read_at(pos, 1)
    if size is None:
        return UNKNOWN
    if len(size) != 1:
        raise ValueError("Файл обрывается перед длиной последовательности DTMF.")
    length = size[0]
    if not 1 <= length <= 30:
        return ""  # The stock CPS renders unused/erased slots as empty.
    packed = read_at(pos + 1, (length + 1) // 2)
    if packed is None or len(packed) != (length + 1) // 2:
        raise ValueError("Файл обрывается внутри последовательности DTMF.")
    return "".join(SYMBOLS[(packed[i // 2] >> (4 * (i % 2))) & 15] for i in range(length))


def sequence_section(read_at=None):
    """Build the editable section. Missing source data is distinct from an explicitly empty code."""
    rows = []
    for name, slots, response in sequence_slots():
        status = None
        if read_at is not None and response is not None:
            kind = read_at(response, 1)
            if kind is None:
                status = UNKNOWN
            elif len(kind) != 1:
                raise ValueError("Файл обрывается перед типом реакции декодера DTMF.")
            elif kind[0] != 0:  # General in the CPS ResponseType table.
                status = SERVICE
        fields = []
        for label, pos in slots:
            value = UNKNOWN if read_at is None else status or read_sequence(read_at, pos)
            fields.append({"label": label, "value": value})
        rows.append({"name": name, "fields": fields})
    return {"id": SEQUENCE_ID, "title": SEQUENCE_TITLE, "rows": rows}


def sequence_values(section):
    if (not isinstance(section, dict) or set(section) != {"id", "title", "rows"}
            or section["id"] != SEQUENCE_ID or section["title"] != SEQUENCE_TITLE
            or not isinstance(section["rows"], list) or len(section["rows"]) != 44):
        raise ValueError("Некорректный раздел последовательностей DTMF.")
    result = {}
    for row, (name, slots, _) in zip(section["rows"], sequence_slots()):
        if (not isinstance(row, dict) or set(row) != {"name", "fields"} or row["name"] != name
                or not isinstance(row["fields"], list) or len(row["fields"]) != len(slots)):
            raise ValueError(f"Некорректная запись DTMF «{name}».")
        for field, (label, pos) in zip(row["fields"], slots):
            if (not isinstance(field, dict) or set(field) != {"label", "value"} or field["label"] != label
                    or not isinstance(field["value"], str)):
                raise ValueError(f"Некорректное поле «{label}» в записи DTMF «{name}».")
            result[pos] = field["value"]
    return result


def validate_sequence_edit(original, edited):
    """Return a normalized independent section, including unchanged unavailable slots."""
    if original is None and edited is None:
        return None
    before = sequence_values(original if original is not None else sequence_section())
    after = sequence_values(edited)
    result = sequence_section()
    for row, (name, slots, response) in zip(result["rows"], sequence_slots()):
        for field, (label, pos) in zip(row["fields"], slots):
            old, value = before[pos], after[pos]
            if value == SERVICE and old == SERVICE and response is not None:
                field["value"] = value
            elif value == UNKNOWN and old == UNKNOWN:
                field["value"] = value
            else:
                if old == SERVICE:
                    raise ValueError(f"«{name}»: код служебного профиля недоступен для правки.")
                if not value.isascii() or len(value) > 30 or any(c.upper() not in SYMBOLS for c in value):
                    raise ValueError(f"«{name}», «{label}»: допустимы до 30 символов 0–9, A–D, * и #.")
                if name == "Передача 1" and not value and old != value:
                    raise ValueError("«Передача 1»: CPS требует непустую первую последовательность.")
                field["value"] = value.upper()
    return result


def merge_sequence_edits(template_section, snapshot_section):
    """Transfer available sequences to a template, carrying its other values through."""
    if snapshot_section is None:
        return template_section
    snapshot = sequence_values(validate_sequence_edit(snapshot_section, snapshot_section))
    template = sequence_values(template_section)
    merged = sequence_section()
    for row, (_, slots, _) in zip(merged["rows"], sequence_slots()):
        for field, (_, pos) in zip(row["fields"], slots):
            incoming = snapshot[pos]
            field["value"] = template[pos] if incoming in (UNKNOWN, SERVICE) else incoming
    return validate_sequence_edit(template_section, merged)


def patch_sequences(target, original, edited):
    if original == edited:
        return
    before = sequence_values(original)
    after = sequence_values(validate_sequence_edit(original, edited))
    patches = []
    for name, slots, response in sequence_slots():
        for _, pos in slots:
            value = after[pos]
            if value == before[pos]:
                continue
            if response is not None:
                target.seek(response)
                if target.read(1) != b"\x00":
                    raise ValueError(f"«{name}»: в исходном RDT позиция относится к служебному профилю.")
            size = (len(value) + 1) // 2
            target.seek(pos + 1)
            data = bytearray(target.read(size))
            if len(data) != size:
                raise ValueError("Файл RDT обрывается внутри последовательности DTMF.")
            for index, symbol in enumerate(value):
                shift = 4 * (index % 2)
                byte = index // 2
                data[byte] = (data[byte] & ~(15 << shift)) | (SYMBOLS.index(symbol) << shift)
            # Stock CPS only touches the length and the nibbles covered by the new value.
            patches.append((pos, bytes([len(value)]) + data))
    for pos, data in patches:
        target.seek(pos)
        target.write(data)


def validate_encode_references(original, edited):
    def active(report):
        sections = {s["id"]: s for s in report.get("cps_sections", [])}
        codes = {}
        if SEQUENCE_ID in sections:
            values = sequence_values(sections[SEQUENCE_ID])
            codes = {name: values[slots[0][1]] for name, slots, _ in sequence_slots() if name.startswith("Передача ")}
        buttons = {}
        for r in sections.get("dtmf", {}).get("rows", []):
            if r["name"].startswith("Быстрый доступ "):
                value = next((f["value"] for f in r["fields"] if f["label"] == "Номер Encode"), None)
                if value is not None:
                    buttons[r["name"]] = value
        return codes, buttons

    old_codes, old_buttons = active(original)
    codes, buttons = active(edited)
    for name, value in buttons.items():
        changed = value != old_buttons.get(name)
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 3 or int(value) > 32:
            if changed:
                raise ValueError(f"«{name}»: укажите номер Encode от 0 до 32.")
            continue
        slot = int(value)
        code = f"Передача {slot}"
        if slot and codes.get(code) == "" and (changed or old_codes.get(code, UNKNOWN) != ""):
            raise ValueError(f"«{name}»: последовательность Encode № {slot} пуста.")


def dtmf_section(read_at):
    rows = []
    for i in range(4):
        base = BASE + i * STRIDE
        data = read_at(base, 9)
        if data is None or len(data) != 9:
            return None
        fields = []
        for offset, label, _, scale in TIMERS:
            fields.append({"label": label, "value": str(data[offset] * scale)})
        group = data[8] >> 4
        fields.append({"label": PTT_ID, "value": PTT_MODES[(data[8] >> 2) & 3]})
        fields.append({"label": GROUP_CODE, "value": GROUP_CODES[group] if group < len(GROUP_CODES) else f"Код {group}"})
        fields.append({"label": SIDE_TONE, "value": "Вкл." if data[8] & 1 else "Выкл."})
        rows.append({"name": f"Система {i + 1}", "fields": fields})
    return {"id": "dtmf_systems", "title": "Системы сигнализации DTMF", "rows": rows}


def patch_dtmf(target, baseline, edited):
    if baseline == edited:
        return
    if not baseline or not edited:
        raise ValueError("Изменился состав разделов DTMF.")
    original_rows, rows = baseline["rows"], edited.get("rows")
    if (edited.keys() != baseline.keys() or edited["id"] != "dtmf_systems"
            or edited["title"] != baseline["title"] or not isinstance(rows, list)
            or len(rows) != len(original_rows) or len(rows) != 4):
        raise ValueError("Неверный раздел DTMF.")
    edits = {}
    enum_flags = {SIDE_TONE: (0, 1, ("Выкл.", "Вкл.")), PTT_ID: (2, 3, PTT_MODES), GROUP_CODE: (4, 15, GROUP_CODES)}
    timers = {label: (offset, low, scale) for offset, label, low, scale in TIMERS}
    for i, (old, new) in enumerate(zip(original_rows, rows)):
        if (not isinstance(new, dict) or new.keys() != old.keys() or new["name"] != old["name"]
                or not isinstance(new["fields"], list) or len(new["fields"]) != len(old["fields"])):
            raise ValueError("Неверная структура системы DTMF.")
        base = BASE + STRIDE * i
        for before, after in zip(old["fields"], new["fields"]):
            if (not isinstance(after, dict) or after.keys() != before.keys()
                    or after["label"] != before["label"] or not isinstance(after["value"], str)):
                raise ValueError("Неверное поле DTMF.")
            if before == after:
                continue
            value = after["value"]
            label = before["label"]
            if label in timers:
                offset, low, scale = timers[label]
                if (not value.isascii() or not value.isdecimal() or len(value) > 4
                        or not low <= int(value) <= 255 * scale or int(value) % scale):
                    raise ValueError(f"«{label}»: допустимо {low}–{255 * scale} с шагом {scale}.")
                edits[base + offset] = int(value) // scale
            else:
                if label not in enum_flags or value not in enum_flags[label][2]:
                    raise ValueError(f"«{label}»: неизвестное значение.")
                shift, mask, labels = enum_flags[label]
                if base + 8 not in edits:
                    target.seek(base + 8)
                    flag = target.read(1)
                    if len(flag) != 1:
                        raise ValueError("Файл RDT обрывается в разделе DTMF.")
                    edits[base + 8] = flag[0]
                edits[base + 8] = (edits[base + 8] & ~(mask << shift)) | (labels.index(value) << shift)
    for offset, value in edits.items():
        target.seek(offset)
        target.write(bytes([value]))
