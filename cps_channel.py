"""Editable public channel fields of the stock UV390 (qdmr v0.15.1 and editcp)."""

YES_NO = {0: "Выкл.", 1: "Вкл."}
INVERTED = {0: "Вкл.", 1: "Выкл."}
REFERENCE = {0: "Low", 1: "Medium", 2: "High"}
# label, byte, low bit, mask, code-to-label or integer multiplier, units
COMMON = (
    ("Мощность", 30, 0, 3, {0: "Low", 2: "Middle", 3: "High"}, ""),
    ("Только приём", 1, 1, 1, YES_NO, ""),
    ("Шумоподавитель", 15, 0, 255, 1, ""),
    ("Тайм-аут передачи", 8, 0, 63, 15, " с"),
    ("Пауза после тайм-аута", 9, 0, 255, 1, " с"),
    ("Условие передачи", 4, 6, 3, {0: "Always", 1: "Channel Free", 2: "CTCSS/DCS", 3: "Color Code"}, ""),
    ("Автосканирование", 0, 4, 1, YES_NO, ""),
    ("VOX", 4, 4, 1, YES_NO, ""),
    ("Одиночный работник", 0, 7, 1, YES_NO, ""),
    ("Прямая связь", 1, 0, 1, YES_NO, ""),
    ("Опорная частота RX", 3, 0, 3, REFERENCE, ""),
    ("Опорная частота TX", 4, 0, 3, REFERENCE, ""),
    ("Система тревоги", 10, 0, 255, 1, ""),
)
DIGITAL = (
    ("Privacy Switch", 5, 2, 1, YES_NO, ""),
    ("Подтверждение индивидуального вызова", 2, 6, 1, YES_NO, ""),
    ("Подтверждение данных", 2, 7, 1, YES_NO, ""),
    ("Подтверждение тревоги", 3, 3, 1, YES_NO, ""),
    ("Показ PTT ID", 3, 7, 1, INVERTED, ""),
    ("Условие в активном вызове", 5, 4, 3, {0: "Always", 1: "Follow Admit Criteria", 2: "TX Interrupt"}, ""),
    ("Система GPS", 13, 0, 31, 1, ""),
    ("Отправка GPS", 31, 0, 1, INVERTED, ""),
    ("Приём GPS", 31, 1, 1, INVERTED, ""),
    ("Разрешить прерывание", 31, 2, 1, INVERTED, ""),
    ("DCDM", 31, 3, 1, INVERTED, ""),
    ("Ведущая станция DCDM", 31, 4, 1, INVERTED, ""),
)
ANALOG = (
    ("Профиль DTMF RX", 28, 0, 255, 1, ""),
    ("Профиль DTMF TX", 29, 0, 255, 1, ""),
    ("Маска декодеров DTMF", 14, 0, 255, 1, ""),
    ("Сигнал завершения передачи", 5, 6, 3, {0: "259,2 Гц", 1: "55,2 Гц", 3: "Выкл."}, ""),
)
# Limits for user-entered values, independent of the bit-field width.
LIMITS = {"Шумоподавитель": 10, "Тайм-аут передачи": 37, "Система тревоги": 32,
          "Система GPS": 16, "Профиль DTMF RX": 4, "Профиль DTMF TX": 4}


def channel_specs(record):
    return COMMON + {1: ANALOG, 2: DIGITAL}.get(record[0] & 3, ())


def channel_details(record):
    fields = []
    for label, offset, shift, mask, values, unit in channel_specs(record):
        code = (record[offset] >> shift) & mask
        if isinstance(values, dict):
            text = values.get(code, f"Код {code}")
        else:
            text = f"{code * values}{unit}"
        fields.append({"label": label, "value": text})
    return fields


def patch_channel_details(record, old_fields, new_fields):
    """Modify only changed documented bits in a writable 64-byte channel record."""
    old = {f["label"]: f["value"] for f in old_fields}
    new = {f["label"]: f["value"] for f in new_fields}
    if old.keys() != new.keys() or len(new) != len(new_fields):
        raise ValueError("Нельзя менять состав дополнительных полей канала.")
    specs = {s[0]: s[1:] for s in channel_specs(record)}
    for label, value in new.items():
        if old[label] == value:
            continue
        if label not in specs or not isinstance(value, str):
            raise ValueError(f"Поле «{label}» недоступно в этом режиме.")
        offset, shift, mask, values, unit = specs[label]
        if isinstance(values, dict):
            choices = {v.casefold(): k for k, v in values.items()}
            code = choices.get(value.strip().casefold())
            if code is None:
                raise ValueError(f"«{label}»: допустимы " + ", ".join(values.values()))
        else:
            token = value.strip()
            if unit and token.endswith(unit):
                token = token[:-len(unit)].strip()
            if not token.isascii() or not token.isdecimal() or len(token) > 6:
                raise ValueError(f"«{label}»: требуется целое число{unit}.")
            code, remainder = divmod(int(token), values)
            maximum = LIMITS.get(label, mask)
            if remainder or code > maximum:
                raise ValueError(f"«{label}»: диапазон 0–{maximum * values}{unit}, шаг {values}.")
        record[offset] = (record[offset] & ~(mask << shift)) | (code << shift)


def yaml_details(item):
    # Only named public properties; manufacturer extensions can also contain secrets.
    fields = []
    paths = (
        (None, {"power": "Мощность", "rxOnly": "Только приём", "timeout": "Тайм-аут передачи",
                "squelch": "Шумоподавитель", "admit": "Условие передачи", "vox": "VOX"}),
        ("tyt", {"autoScan": "Автосканирование", "rxRefFrequency": "Опорная частота RX",
                 "txRefFrequency": "Опорная частота TX", "displayPTTId": "Показ PTT ID",
                 "emergencyAlarmConfirmed": "Подтверждение тревоги", "inCallCriterion": "Условие в активном вызове",
                 "allowInterrupt": "Разрешить прерывание", "dcdmLeader": "Ведущая станция DCDM",
                 "killTone": "Сигнал завершения передачи", "dmrSquelch": "Шумоподавитель DMR"}),
        ("extended", {"talkaround": "Прямая связь", "privateCallConfirm": "Подтверждение индивидуального вызова",
                      "dataConfirm": "Подтверждение данных", "dcdm": "DCDM", "loneWorker": "Одиночный работник",
                      "sms": "SMS", "smsConfirm": "Подтверждение SMS", "reverseBurst": "Reverse Burst"}),
    )
    for section, mapping in paths:
        block = item if section is None else item.get(section)
        if not isinstance(block, dict):
            continue
        for key, label in mapping.items():
            value = block.get(key)
            if isinstance(value, str):
                fields.append({"label": label, "value": {"true": "Вкл.", "false": "Выкл."}.get(value, value)})
            elif getattr(value, "value", None) == "!default":
                fields.append({"label": label, "value": "По умолчанию"})
    return fields
