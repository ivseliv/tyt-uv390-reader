"""Additional read-only TYT codeplug tables. All binary readers take absolute CPS offsets.

Binary field definitions: hmatuschek/qdmr v0.15.1 lib/{tyt,uv390,dm1701}_codeplug.cc
and tyt_extensions.hh. Number-key slots: dalefarnsworth-dmr/codeplug MD-UV390 layout.
Only the listed public fields are read. None from read_at means the source lacks that slice.
"""

import struct

from cps_csv import cps_tone, tone


TITLES = {
    "general": "Общие настройки", "menu": "Меню рации", "buttons": "Кнопки",
    "messages": "Шаблоны сообщений", "gps": "Позиционирование", "emergency": "Аварийная сигнализация",
    "lone_worker": "Одиночный работник", "vfo": "VFO A/B", "dtmf": "DTMF",
    "scan_timing": "Таймеры сканирования",
}


def display(value):
    if isinstance(value, bool):
        return "Вкл." if value else "Выкл."
    return str(value)


def row(name, fields):
    return {"name": name, "fields": [{"label": key, "value": display(value)} for key, value in fields
                                     if value is not None and value != ""]}


def finish(groups):
    return [{"id": key, "title": TITLES[key], "rows": rows} for key, rows in groups.items() if rows]


def word(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def label(data):
    # UTF-16LE slots can end with either 0000 or ffff.
    return data.decode("utf-16-le", "replace").split("\x00", 1)[0].split("\uffff", 1)[0]


def bit(data, n):
    return bool(data & (1 << n))


def enum_value(value, names):
    if isinstance(names, dict):
        return names.get(value, f"Код {value}")
    return names[value] if value < len(names) else f"Код {value}"


def target(number, names, empty="Нет"):
    if number == 0:
        return empty
    if number in names:
        return f"№ {number} · {names[number]}"
    return f"№ {number}"


# Name, exported qdmr key, byte, bit (least significant first), inverted polarity.
MENU_BITS = (
    ("Сообщения", "textMessage", 1, 0, False), ("Оповещение о вызове", "callAlert", 1, 1, False),
    ("Редактирование контактов", "contactEditing", 1, 2, False), ("Ручной набор", "manualDial", 1, 3, False),
    ("Проверка рации", "remoteRadioCheck", 1, 4, False), ("Удалённый мониторинг", "remoteMonitor", 1, 5, False),
    ("Включение рации", "remoteRadioEnable", 1, 6, False), ("Отключение рации", "remoteRadioDisable", 1, 7, False),
    ("Сканирование", "scan", 2, 1, False), ("Редактирование списка", "scanListEditing", 2, 2, False),
    ("Пропущенные вызовы", "callLogMissed", 2, 3, False), ("Принятые вызовы", "callLogAnswered", 2, 4, False),
    ("Исходящие вызовы", "callLogOutgoing", 2, 5, False), ("Прямая связь", "talkaround", 2, 6, False),
    ("Сигналы", "alertTone", 2, 7, False), ("Мощность", "power", 3, 0, False),
    ("Подсветка", "backlight", 3, 1, False), ("Заставка", "bootScreen", 3, 2, False),
    ("Блокировка клавиатуры", "keypadLock", 3, 3, False), ("Индикатор LED", "ledIndicator", 3, 4, False),
    ("Шумоподавитель", "squelch", 3, 5, False), ("VOX", "vox", 3, 7, False),
    ("Режим экрана", "displayMode", 4, 1, False), ("Программирование", "radioProgramming", 4, 2, True),
    ("GPS", "gpsInformation", 4, 3, True), ("Запись", "recording", 4, 5, False),
    ("Приём групповых вызовов", "groupCallMatch", 5, 2, False),
    ("Приём индивидуальных вызовов", "privateCallMatch", 5, 3, False),
    ("Таймер меню", "menuHangtimeItem", 5, 4, False), ("Режим передачи", "txMode", 5, 5, False),
    ("Настройки зон", "zoneSettings", 5, 6, False), ("Новая зона", "newZone", 5, 7, False),
    ("Редактирование зон", "editZone", 6, 0, False), ("Новый список сканирования", "newScanList", 6, 1, False),
)

BUTTONS = {
    0: "Не назначено", 1: "Все звуковые сигналы", 2: "Включить тревогу", 3: "Отключить тревогу",
    4: "Мощность", 5: "Монитор", 6: "Пропустить канал при сканировании", 13: "Ретранслятор / прямая связь",
    14: "Сканирование", 21: "Шумоподавитель", 22: "Режим Privacy", 23: "VOX", 24: "Следующая зона",
    25: "Переключить зону", 26: "Заряд батареи", 30: "Индивидуальный вызов", 31: "Одиночный работник",
    34: "Запись", 35: "Воспроизведение", 36: "Удаление записей", 38: "Тон 1750 Гц", 47: "Сторона A/B",
    48: "Правая клавиша", 49: "Левая клавиша", 55: "Предыдущая зона",
    **{6 + n: f"Быстрый доступ {n}" for n in range(1, 7)},
}
PRESSES = ("Кнопка 1 · короткое нажатие", "Кнопка 1 · долгое нажатие",
           "Кнопка 2 · короткое нажатие", "Кнопка 2 · долгое нажатие")
TX_MODES = ("Последний канал вызова", "Последний канал вызова и ручной выбор",
            "Назначенный канал", "Назначенный канал и ручной выбор")
GPS_FORMATS = ("Сообщение", "Обычный")
KEYPAD_TIMES = {0xFF: "Вручную", 1: "5 с", 2: "10 с", 3: "15 с"}
INTRO_SCREENS = ("Текст", "Логотип")
MONITOR_TYPES = ("Без звука", "Открытый шумоподавитель")
PERMIT_TONES = ("Нет", "DMR", "Аналог", "DMR и аналог")
EMERGENCY_TYPES = ("Отключена", "Обычная", "Тихая", "Тихая с голосом")
EMERGENCY_MODES = ("Тревога", "Тревога и вызов", "Тревога с голосом")
MAIN_MODES = ("Частота", "Каналы")
SIDE_MODES = ("VFO", "Каналы")
VFO_POWER = {0: "Low", 2: "Middle", 3: "High"}
VFO_WIDTH = ("12,5 кГц", "20 кГц", "25 кГц")
VFO_LINKS = {"Контакт": ("contacts", 6, 2), "Список сканирования": ("scan_lists", 11, 1),
             "Группа приёма": ("rx_groups", 12, 1)}
VFO_TONES = {"Субтон приёма": 24, "Субтон передачи": 26}
EXTRA_IDS = {f"Radio ID {slot + 1}": 0x20D4 + 4 * slot for slot in range(3)}


def vfo_tone(raw):
    if raw == 0 or raw >> 8 == 0xFF:
        return "Выкл."
    try:
        return cps_tone(raw).replace(".", ",")
    except ValueError:
        return f"Код 0x{raw:04X}"


def vfo_tone_code(value):
    if value == "Выкл.":
        return 0xFFFF
    value = value.strip()
    if value.endswith("Гц"):
        value = value[:-2].strip()
    code = tone(value.replace(",", "."))
    if code is None:
        raise ValueError("Укажите «Выкл.», частоту CTCSS в Гц или код DCS, например D023N.")
    return code


def rdt_sections(read_at, report):
    sections = {key: [] for key in TITLES}
    channels = {r["index"]: r["name"] for r in report["channels"]}
    contacts = {r["index"]: r["name"] for r in report["contacts"]}
    vfo_names = {table: {r["index"]: r["name"] for r in report.get(table, [])}
                 for table, _, _ in VFO_LINKS.values()}

    def read(addr, size):
        # All sections below are in the first radio segment (file 0x2225 = radio 0x2000).
        data = read_at(addr + 0x225, size)
        if data is not None and len(data) != size:
            raise ValueError("Файл обрывается внутри дополнительных настроек CPS.")
        return data

    def add(section, title, fields):
        entry = row(title, fields)
        if entry["fields"]:
            sections[section].append(entry)

    intro = read(0x2040, 40)
    if intro:
        add("general", "Заставка", [("Строка 1", label(intro[:20])), ("Строка 2", label(intro[20:]))])
    data = read(0x2080, 0x18)
    if data:
        add("general", "Рация", [
            ("DMR ID", int.from_bytes(data[4:7], "little")),
            ("Выбор канала передачи", TX_MODES[data[0] >> 6]),
            ("Монитор", MONITOR_TYPES[bit(data[0], 4)]),
            ("Индикатор LED", bit(data[0], 2)), ("Энергосбережение при приёме", bit(data[1], 1)),
            ("Преамбула пробуждения", bit(data[1], 0)), ("Все звуки отключены", not bit(data[1], 2)),
            ("Сигнал свободного канала", not bit(data[1], 4)),
            ("Сигнал разрешения передачи", enum_value(data[1] >> 6, PERMIT_TONES)),
            ("Заставка", INTRO_SCREENS[bit(data[2], 4)]), ("Озвучивание каналов", bit(data[2], 1)),
            ("Звук клавиш", bit(data[2], 5)), ("Чувствительность VOX", data[11]),
            ("Преамбула передачи", f"{data[8] * 60} мс"), ("Удержание группового вызова", f"{data[9] * 100} мс"),
            ("Удержание индивидуального вызова", f"{data[10] * 100} мс"),
            ("Предупреждение о разряде", f"{data[14] * 5} с"),
            ("Оповещение о вызове", f"{data[15] * 5} с" if data[15] else "Непрерывно"),
            ("Подсветка", f"{(data[21] & 3) * 5} с" if data[21] & 3 else "Всегда"),
            ("Автоблокировка клавиатуры", enum_value(data[22], KEYPAD_TIMES)),
        ])
        add("vfo", "Режимы дисплея", [
            ("Основной режим", MAIN_MODES[data[23] == 255]),
            ("Сторона A", SIDE_MODES[bit(data[3], 2)]),
            ("Сторона B", SIDE_MODES[bit(data[3], 7)]),
        ])
        add("lone_worker", "Таймеры", [("Время для ответа", f"{data[16]} мин"),
                                       ("Интервал напоминания", f"{data[17]} с")])
        add("scan_timing", "Удержание на канале", [("DMR", f"{data[19] * 100} мс"),
                                                   ("Аналог", f"{data[20] * 100} мс")])
    identity = read(0x20B0, 33)
    zone = read(0x20AB, 1)
    mic = read(0x20E0, 1)
    keyboard = read(0x20E2, 1)
    public = read(0x20D2, 1)
    extra = []
    if identity:
        extra += [("Имя рации", label(identity[:32])), ("Задержка канала", f"{identity[32] * 100} мс")]
    if zone:
        extra += [("Часовой пояс", f"UTC{(zone[0] >> 3) - 12:+d}"), ("Групповые вызовы", bit(zone[0], 0)),
                  ("Индивидуальные вызовы", bit(zone[0], 1))]
    if mic:
        extra.extend([("Усиление микрофона (код CPS)", (mic[0] >> 3) & 7),
                      ("Редактирование Radio ID на рации", not bit(mic[0], 6)),
                      ("Формат отчёта GPS", GPS_FORMATS[mic[0] >> 7])])
    if keyboard:
        extra.extend([("Блокировка передней клавиатуры", not bit(keyboard[0], 2)),
                      ("Озвучивание при включении", bit(keyboard[0], 5))])
    if public:
        extra.append(("Общая зона", bit(public[0], 2)))
    add("general", "Дополнительно", extra)
    identifiers = []
    for title, address in EXTRA_IDS.items():
        value = read(address, 3)
        if value:
            identifiers.append((title, int.from_bytes(value, "little")))
    add("general", "Дополнительные идентификаторы", identifiers)

    menu = read(0x20F0, 7)
    if menu:
        fields = [("Время показа", f"{menu[0]} с" if menu[0] else "Без ограничения")]
        fields.extend((name, bit(menu[byte], shift) != inverted) for name, _, byte, shift, inverted in MENU_BITS)
        add("menu", "Пункты меню", fields)
    buttons = read(0x2102, 4)
    duration = read(0x2111, 1)
    if buttons:
        add("buttons", "Боковые кнопки", [(name, enum_value(code, BUTTONS)) for name, code in zip(PRESSES, buttons)])
    if duration:
        add("buttons", "Долгое нажатие", [("Длительность", f"{duration[0] * 250} мс")])
    quick = read(0x2114, 44)
    if quick:
        for i in range(6):
            action, message, contact = struct.unpack_from("<BBH", quick, i * 4)
            mode, command = (action >> 4) & 3, action & 15
            if mode == 1:
                action_name = enum_value(command, {0: "Вызов", 1: "Сообщение"})
                add("buttons", f"Быстрый доступ {i + 1}", [("Действие", action_name), ("Контакт", target(contact, contacts)),
                                                         ("Шаблон", message if command == 1 else None)])
            elif mode == 2 and 8 <= command <= 11:
                add("dtmf", f"Быстрый доступ {i + 1}", [("Профиль", f"DTMF-{command - 7}"), ("Номер Encode", message)])
        for i in range(10):
            idx = word(quick, 24 + 2 * i)
            if idx not in (0, 0xFFFF):
                add("buttons", f"Цифровая клавиша · позиция {i + 1}", [("Контакт", target(idx, contacts))])

    for i in range(50):
        block = read(0x2180 + i * 0x120, 0x120)
        if block and word(block, 0) not in (0, 0xFFFF):
            add("messages", f"Шаблон {i + 1}", [("Текст", label(block))])

    alarm = read(0x5A70, 4)
    if alarm:
        add("emergency", "Общие параметры", [("Мониторинг при тревоге", bit(alarm[0], 2)),
            ("Удалённый мониторинг", bit(alarm[0], 1)), ("Удалённое отключение", bit(alarm[0], 0)),
            ("Длительность мониторинга", f"{alarm[1] * 10} с"), ("Тайм-аут передачи", f"{alarm[2] * 25} мс"),
            ("Лимит сообщений", alarm[3])])
    for i in range(32):
        data = read(0x5A80 + i * 0x28, 0x28)
        if not data or not word(data, 0x24) or not label(data[:32]):
            continue
        ch = word(data, 0x24)
        add("emergency", f"№ {i + 1} · {label(data[:32])}", [
            ("Тип", enum_value(data[32] & 3, EMERGENCY_TYPES)),
            ("Режим", enum_value((data[32] >> 4) & 3, EMERGENCY_MODES)),
            ("Повторы без ожидания", data[33]), ("Повторы с ожиданием", data[34]),
            ("Открытый микрофон", f"{data[35] * 10} с"),
            ("Канал", "Выбранный канал" if ch == 0xFFFF else target(ch, channels)),
        ])

    for i, side in enumerate("AB"):
        data = read(0x2EF00 + i * 0x40, 0x21)
        if not data or data[0] & 3 not in (1, 2):
            continue
        digital = data[0] & 3 == 2
        fields = [("Режим", "DMR" if digital else "Аналог")]
        for name, offset in (("RX", 16), ("TX", 20)):
            bcd = data[offset:offset + 4][::-1].hex()
            if bcd.isdecimal():
                fields.append((name, f"{int(bcd) * 10:,} Гц".replace(",", " ")))
        if data[32] < 20:
            fields.append(("Шаг", f"{(data[32] + 1) * 2500} Гц"))
        fields.extend([("Мощность", enum_value(data[30] & 3, VFO_POWER)), ("Только приём", bit(data[1], 1))])
        for field, (table, offset, size) in VFO_LINKS.items():
            if digital or table == "scan_lists":
                fields.append((field, target(int.from_bytes(data[offset:offset + size], "little"), vfo_names[table])))
        if digital:
            fields.extend([("Color Code", data[1] >> 4), ("Time Slot", 2 if (data[1] >> 2) & 3 == 2 else 1)])
        else:
            fields.append(("Полоса", enum_value(data[0] >> 2 & 3, VFO_WIDTH)))
            fields.extend((field, vfo_tone(word(data, offset))) for field, offset in VFO_TONES.items())
        add("vfo", f"VFO {side}", fields)

    for i in range(16):
        data = read(0x3EC40 + i * 16, 6)
        if not data or not (0 < data[2] <= 240) or word(data, 4) in (0, 0xFFFF):
            continue
        ch = word(data, 0)
        add("gps", f"GPS {i + 1}", [("Период", f"{data[2] * 30} с"), ("Контакт", target(word(data, 4), contacts)),
            ("Канал", "Не задан" if ch == 0xFFFF else target(ch, channels, "Выбранный канал"))])

    for scan in report["scan_lists"]:
        data = read(0x18860 + (scan["index"] - 1) * 0x68 + 0x27, 2)
        if data:
            add("scan_timing", f'№ {scan["index"]} · {scan["name"]}', [
                ("Удержание сигнала", f"{data[0] * 25} мс"), ("Опрос приоритетного канала", f"{data[1] * 250} мс")])
    return finish(sections)


def yaml_sections(doc, default_radio_index, channels, contacts):
    """Project only named public fields, including when a file has vendor-specific extensions."""
    sections = {key: [] for key in TITLES}

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def entries(value):
        return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []

    def add(section, title, data, specs):
        fields = []
        for key, caption in specs:
            value = data.get(key)
            if key in ("systems", "talkPermit", "callStart", "callEnd", "path") and isinstance(value, list):
                value = ", ".join(item for item in value if isinstance(item, str))
            if isinstance(value, str) and value != "":
                fields.append((caption, {"true": "Вкл.", "false": "Выкл."}.get(value, value)))
        if fields:
            sections[section].append(row(title, fields))

    settings = mapping(doc.get("settings"))
    tyt = mapping(settings.get("tyt"))
    ext = mapping(doc.get("tytExtension"))
    add("general", "Основные", settings, (
        ("power", "Мощность"), ("squelch", "Шумоподавитель"), ("micLevel", "Усиление микрофона"),
        ("speech", "Озвучивание"), ("vox", "VOX"), ("tot", "Тайм-аут передачи"),
        ("introLine1", "Строка 1"), ("introLine2", "Строка 2"),
    ))
    add("general", "Заставка", mapping(settings.get("boot")), (
        ("display", "Режим"), ("message1", "Строка 1"), ("message2", "Строка 2"), ("defaultChannel", "Начальный канал"),
    ))
    add("general", "Аудио", mapping(settings.get("audio")), (
        ("squelch", "Шумоподавитель"), ("dmrSquelch", "Шумоподавитель DMR"), ("micGain", "Усиление микрофона"),
        ("fmMicGain", "Усиление FM"), ("m17MicGain", "Усиление M17"), ("maxSpeakerVolume", "Громкость динамика"),
        ("maxHeadphoneVolume", "Громкость наушников"), ("vox", "VOX"), ("voxDelay", "Задержка VOX"),
        ("speech", "Озвучивание"),
    ))
    add("general", "Тоны", mapping(settings.get("tone")), (
        ("silent", "Без звука"), ("keyTone", "Сигнал клавиш"), ("smsTone", "Сигнал SMS"),
        ("ringtone", "Сигнал вызова"), ("bootTone", "Сигнал включения"), ("talkPermit", "Разрешение передачи"),
        ("callStart", "Начало вызова"), ("callEnd", "Конец вызова"), ("channelIdle", "Канал свободен"),
    ))
    add("general", "DMR", mapping(settings.get("dmr")), (
        ("privateCallMatch", "Индивидуальные вызовы"), ("groupCallMatch", "Групповые вызовы"),
        ("privateCallHangTime", "Удержание индивидуального вызова"), ("groupCallHangTime", "Удержание группового вызова"),
        ("sendTalkerAlias", "Передача Talker Alias"), ("talkerAliasEncoding", "Кодировка Talker Alias"),
        ("preamble", "Преамбула передачи"),
    ))
    add("general", "Параметры TYT", tyt, (
        ("monitorType", "Монитор"), ("allLEDsDisabled", "Все LED отключены"), ("powerSaveMode", "Энергосбережение"),
        ("wakeupPreamble", "Преамбула пробуждения"), ("lowBatteryWarnInterval", "Предупреждение о разряде, с"),
        ("callAlertToneContinuous", "Непрерывный сигнал вызова"), ("callAlertToneDuration", "Длительность сигнала, с"),
        ("backlightAlwaysOn", "Постоянная подсветка"), ("backlightDuration", "Длительность подсветки, с"),
        ("keypadLockManual", "Ручная блокировка клавиатуры"), ("keypadLockTime", "Блокировка клавиатуры, с"),
        ("channelHangTime", "Задержка канала, мс"),
    ))
    for i, entry in enumerate(entries(doc.get("radioIDs")), 1):
        radio = mapping(entry.get("dmr"))
        title = "Идентификатор DMR по умолчанию" if i == default_radio_index else "Идентификатор DMR"
        add("general", title, radio, (("name", "Имя"), ("number", "DMR ID")))
    add("menu", "Пункты меню", mapping(ext.get("menuSettings")),
        [("hangTime", "Время показа, с"), ("hangtimeIsInfinite", "Неограниченное время")]
        + [(key, name) for name, key, _, _, _ in MENU_BITS])
    add("buttons", "Назначения", mapping(ext.get("buttonSettings")), [
        ("sideButton1Short", PRESSES[0]), ("sideButton1Long", PRESSES[1]),
        ("sideButton2Short", PRESSES[2]), ("sideButton2Long", PRESSES[3]),
        ("sideButton3Short", "Кнопка 3 · короткое нажатие"), ("sideButton3Long", "Кнопка 3 · долгое нажатие"),
        ("progButton1Short", "Программируемая 1 · короткое"), ("progButton1Long", "Программируемая 1 · долгое"),
        ("progButton2Short", "Программируемая 2 · короткое"), ("progButton2Long", "Программируемая 2 · долгое"),
        ("longPressDuration", "Долгое нажатие, мс"),
    ])
    sms = mapping(doc.get("sms"))
    add("messages", "SMS", sms, (("format", "Формат сообщений"),))
    for i, template in enumerate(entries(sms.get("templates")), 1):
        add("messages", f"Шаблон {i}", template, (("name", "Название"), ("message", "Текст")))
    add("gps", "GNSS", mapping(settings.get("gnss")), (
        ("systems", "Системы"), ("units", "Единицы измерения"),
        ("fixedPositionEnabled", "Фиксированная позиция"), ("fixedPosition", "Позиция"),
    ))
    for i, entry in enumerate(entries(doc.get("positioning")), 1):
        for kind in ("dmr", "aprs"):
            data = dict(mapping(entry.get(kind)))
            for key, table in (("contact", contacts), ("revert", channels)):
                ref = data.get(key)
                if isinstance(ref, str) and ref in table.index:
                    index = table.index[ref]
                    data[key] = f'№ {index} · {table.rows[index - 1][2]["name"]}'
            # The reserved !selected tag is distinct from the string used for a channel ID.
            if getattr(data.get("revert"), "value", None) == "!selected":
                data = {**data, "revert": "Выбранный канал"}
            add("gps", f"{kind.upper()} · {i}", data, (
                ("name", "Название"), ("period", "Период"), ("contact", "Контакт"), ("revert", "Канал"),
                ("source", "Отправитель"), ("destination", "Получатель"), ("path", "Путь APRS"),
                ("icon", "Значок"), ("message", "Сообщение"),
            ))
    add("lone_worker", "Таймеры", tyt, (("loneWorkerResponseTime", "Время для ответа, мин"),
                                        ("loneWorkerReminderTime", "Интервал напоминания, с")))
    add("vfo", "Режимы дисплея", tyt, (("channelMode", "Канальный режим"), ("channelModeA", "Канальный режим A"),
                                       ("channelModeB", "Канальный режим B")))
    for entry in entries(doc.get("contacts")):
        add("dtmf", "Контакт DTMF", mapping(entry.get("dtmf")), (("name", "Имя"), ("number", "Номер")))
    add("scan_timing", "Удержание на канале", tyt, (("digitalScanHangTime", "DMR, мс"), ("analogScanHangTime", "Аналог, мс")))
    for i, scan in enumerate(entries(doc.get("scanLists")), 1):
        name = scan.get("name")
        title = f"№ {i} · {name}" if isinstance(name, str) else f"№ {i}"
        add("scan_timing", title, mapping(scan.get("tyt")),
            (("holdTime", "Удержание сигнала, мс"), ("prioritySampleTime", "Опрос приоритетного канала, мс")))
    return finish(sections)
