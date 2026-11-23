"""Export the public FM/DMR tables of a TYT Reader snapshot as qdmr 0.15.1 YAML."""

import json
import re
import sys

from import_qdmr import MAX_BYTES, Tag
from vendor import yaml


class CodeplugDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return isinstance(data, Tag) or super().ignore_aliases(data)


CodeplugDumper.add_representer(Tag, lambda dumper, tag: dumper.represent_scalar(tag.value, ""))

TABLES = {
    "contacts": ("cont", "Контакт"), "rx_groups": ("grp", "Группа приёма"),
    "channels": ("ch", "Канал"), "zones": ("zone", "Зона"), "scan_lists": ("scan", "Список сканирования"),
}
CALL_TYPES = {"group": "GroupCall", "private": "PrivateCall", "all": "AllCall"}
# Fields projected by import_qdmr / cps_channel.yaml_details. Only these public
# properties are reconstructed; arbitrary extension maps are never copied.
CHANNEL_FIELDS = {
    "": {"Мощность": "power", "Только приём": "rxOnly", "Тайм-аут передачи": "timeout",
         "Шумоподавитель": "squelch", "Условие передачи": "admit", "VOX": "vox"},
    "tyt": {"Автосканирование": "autoScan", "Опорная частота RX": "rxRefFrequency",
            "Опорная частота TX": "txRefFrequency", "Показ PTT ID": "displayPTTId",
            "Подтверждение тревоги": "emergencyAlarmConfirmed", "Условие в активном вызове": "inCallCriterion",
            "Разрешить прерывание": "allowInterrupt", "Ведущая станция DCDM": "dcdmLeader",
            "Сигнал завершения передачи": "killTone", "Шумоподавитель DMR": "dmrSquelch"},
    "extended": {"Прямая связь": "talkaround", "Подтверждение индивидуального вызова": "privateCallConfirm",
                 "Подтверждение данных": "dataConfirm", "DCDM": "dcdm", "Одиночный работник": "loneWorker",
                 "SMS": "sms", "Подтверждение SMS": "smsConfirm", "Reverse Burst": "reverseBurst"},
}
PUBLIC_VALUES = {"Вкл.": True, "Выкл.": False, "По умолчанию": Tag.DEFAULT}


def integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field} должен быть целым числом от {low} до {high}.")
    return value


def array(record, field):
    value = record.get(field, [])
    if not isinstance(value, list):
        raise ValueError(f"Поле {field} должно содержать список.")
    return value


def fields(items):
    return {field["label"]: field["value"] for field in items if isinstance(field, dict)
            and isinstance(field.get("label"), str) and isinstance(field.get("value"), str)}


def frequency(value, field):
    if value is None:
        return None
    hz = integer(value, field, 0, 2**63 - 1)
    return f"{hz // 1000000}.{hz % 1000000:06d}".rstrip("0").rstrip(".") + " MHz"


def tone(value, field):
    if value is None or value == 0xFFFF:
        return None
    integer(value, field, 0, 0xFFFF)
    digits = f"{value & 0xfff:03x}"
    if value >> 12 in (8, 12) and re.fullmatch("[0-7]{3}", digits):
        return {"dcs": ("i" if value >> 12 == 12 else "n") + digits}
    digits = f"{value:04x}"
    if re.fullmatch("[0-3][0-9]{3}", digits) and value:
        return {"ctcss": f"{int(digits[:-1])}.{digits[-1]} Hz"}
    raise ValueError(f"{field} содержит неизвестный код субтона 0x{value:04X}.")


class Table:
    def __init__(self, report, key):
        self.prefix, self.label = TABLES[key]
        self.rows = {}
        for row in array(report, key):
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ValueError(f"{self.label} без имени в таблице {key}.")
            slot = integer(row.get("index"), f"{self.label}, номер", 1, 2**31 - 1)
            if slot in self.rows:
                raise ValueError(f"{self.label} № {slot} встречается дважды.")
            self.rows[slot] = row

    def records(self):
        for slot, row in self.rows.items():
            yield row, {"id": self.prefix + str(slot), "name": row["name"]}, f"{self.label} № {slot}"

    def ref(self, slot, origin, *, selected=False, types=None):
        if selected and type(slot) is int and slot == 0:
            return Tag.SELECTED
        if type(slot) is not int or slot not in self.rows:
            number = str(slot) if type(slot) is int else "?"
            raise ValueError(f"{origin}: отсутствует запись «{self.label}» № {number}.")
        if types and self.rows[slot].get("type") not in types:
            raise ValueError(f"{origin}: «{self.label}» № {slot} имеет несовместимый тип.")
        return self.prefix + str(slot)


def channel_properties(channel, from_yaml):
    details = fields(array(channel, "details"))
    result = {}
    for section, mapping in CHANNEL_FIELDS.items():
        block = {}
        for label, key in mapping.items():
            if label not in details:
                continue
            value = details[label]
            if from_yaml:
                block[key] = PUBLIC_VALUES.get(value, value)
            elif key == "rxOnly" and value in ("Вкл.", "Выкл."):
                block[key] = PUBLIC_VALUES[value]
            elif key == "power" and value in ("Low", "Middle", "High"):
                block[key] = "Mid" if value == "Middle" else value
            elif key in ("autoScan", "displayPTTId", "emergencyAlarmConfirmed", "allowInterrupt", "dcdmLeader",
                         "talkaround", "privateCallConfirm", "dataConfirm", "dcdm", "loneWorker") and value in ("Вкл.", "Выкл."):
                block[key] = PUBLIC_VALUES[value]
            elif key in ("rxRefFrequency", "txRefFrequency") and value in ("Low", "Medium", "High"):
                block[key] = value
            elif key == "squelch" and channel["mode"] == "analog" and value.isascii() and value.isdecimal() and int(value) <= 10:
                block[key] = int(value)
            elif key == "timeout" and re.fullmatch(r"[0-9]{1,4} с", value):
                block[key] = value[:-2] + " s"
        if block:
            if section:
                result[section] = block
            else:
                result.update(block)
    cells = channel.get("cps_csv")
    if "rxOnly" not in result and isinstance(cells, dict) and cells.get("Rx Only") in ("0", "1"):
        result["rxOnly"] = cells["Rx Only"] == "1"
    return result


def section_rows(report, section_id):
    for section in array(report, "cps_sections"):
        if isinstance(section, dict) and section.get("id") == section_id:
            for row in array(section, "rows"):
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    yield row["name"], fields(array(row, "fields"))


def general_properties(report):
    # These values are the original qdmr scalar spellings saved by yaml_sections.
    # A binary CPS section uses different units and enumerations.
    blocks = {
        "Основные": ("", {"Мощность": "power", "Шумоподавитель": "squelch", "Усиление микрофона": "micLevel",
                          "Озвучивание": "speech", "VOX": "vox", "Тайм-аут передачи": "tot",
                          "Строка 1": "introLine1", "Строка 2": "introLine2"}),
        "Заставка": ("boot", {"Режим": "display", "Строка 1": "message1", "Строка 2": "message2",
                              "Начальный канал": "defaultChannel"}),
        "Аудио": ("audio", {"Шумоподавитель": "squelch", "Шумоподавитель DMR": "dmrSquelch",
                            "Усиление микрофона": "micGain", "Усиление FM": "fmMicGain", "Усиление M17": "m17MicGain",
                            "Громкость динамика": "maxSpeakerVolume", "Громкость наушников": "maxHeadphoneVolume",
                            "VOX": "vox", "Задержка VOX": "voxDelay", "Озвучивание": "speech"}),
        "DMR": ("dmr", {"Индивидуальные вызовы": "privateCallMatch", "Групповые вызовы": "groupCallMatch",
                        "Удержание индивидуального вызова": "privateCallHangTime", "Удержание группового вызова": "groupCallHangTime",
                        "Передача Talker Alias": "sendTalkerAlias", "Кодировка Talker Alias": "talkerAliasEncoding",
                        "Преамбула передачи": "preamble"}),
    }
    settings = {}
    for title, data in section_rows(report, "general"):
        if title in blocks:
            section, names = blocks[title]
            values = {key: PUBLIC_VALUES.get(data[label], data[label]) for label, key in names.items() if label in data}
            if section:
                settings[section] = values
            else:
                settings.update(values)
    return settings


def radio_numbers(report, channels):
    default = report.get("dmr_id")
    names = {}
    for title, data in section_rows(report, "general"):
        if title in ("Идентификатор DMR", "Идентификатор DMR по умолчанию"):
            digits = data.get("DMR ID", "")
            if re.fullmatch(r"[0-9]{1,8}", digits):
                number = integer(int(digits), "DMR ID", 0, 0xffffff)
                if number in names:
                    raise ValueError(f"Несколько записей radioIDs содержат DMR ID {number}. "
                                     "Снимок не позволяет восстановить ссылки между одноимёнными номерами.")
                names[number] = data.get("Имя", str(number))
    for number in [default] + [row.get("radio_id") for row in channels.rows.values() if row.get("mode") == "DMR"]:
        if number is not None:
            integer(number, "DMR ID", 0, 0xffffff)
            names.setdefault(number, str(number))
    return [{"dmr": {"id": f"id{n}", "name": name, "number": n}} for n, name in names.items()]


def codeplug(report):
    if not isinstance(report, dict) or not {"channels", "contacts", "zones", "rx_groups", "scan_lists"} & report.keys():
        raise ValueError("В JSON отсутствуют таблицы TYT Reader.")
    contacts, groups, channels, zones, scans = (Table(report, key) for key in TABLES)
    from_yaml = report.get("source_format") == "qdmr"
    default_id = report.get("dmr_id")
    result = {
        "version": "0.15.1", "settings": {"defaultID": f"id{default_id}"} if default_id is not None else {},
        "radioIDs": radio_numbers(report, channels), "contacts": [], "groupLists": [], "channels": [],
        "zones": [], "scanLists": [],
    }
    if from_yaml:
        result["settings"].update(general_properties(report))
    for row, obj, origin in contacts.records():
        kind = row.get("type")
        if kind in CALL_TYPES:
            obj["type"] = CALL_TYPES[kind]
            obj["number"] = integer(row.get("dmr_id"), origin + ", DMR ID", 0, 0xffffff)
            if row.get("ring") is not None:
                if type(row["ring"]) is not bool:
                    raise ValueError(f"{origin}: некорректное значение сигнала вызова.")
                obj["ring"] = row["ring"]
            kind = "dmr"
        elif kind == "dtmf" and isinstance(row.get("dtmf_number"), str):
            obj["number"] = row["dtmf_number"]
        else:
            raise ValueError(f"{origin}: поддерживаются контакты DMR и DTMF.")
        result["contacts"].append({kind: obj})

    for row, obj, origin in groups.records():
        obj["contacts"] = [contacts.ref(slot, origin, types={"group"}) for slot in array(row, "contacts")]
        result["groupLists"].append(obj)

    for row, obj, origin in channels.records():
        mode = row.get("mode")
        if mode not in ("analog", "DMR"):
            raise ValueError(f"{origin}: поддерживаются каналы FM и DMR.")
        obj["rxFrequency"] = frequency(row.get("rx_hz"), origin + ", частота RX")
        obj["txFrequency"] = frequency(row.get("tx_hz"), origin + ", частота TX")
        obj.update(channel_properties(row, from_yaml))
        if row.get("scan_list_index") is not None:
            obj["scanListRef"] = scans.ref(row["scan_list_index"], origin)
        if mode == "analog":
            band = row.get("bandwidth_code")
            if type(band) is not int or band not in (0, 2):
                raise ValueError(f"{origin}: qdmr поддерживает полосы FM 12,5 и 25 кГц.")
            obj["bandwidth"] = "Narrow" if band == 0 else "Wide"
            obj["rxTone"] = tone(row.get("rx_tone_code"), origin + ", субтон RX")
            obj["txTone"] = tone(row.get("tx_tone_code"), origin + ", субтон TX")
            result["channels"].append({"fm": obj})
        else:
            if row.get("privacy_type_code") not in (None, 0):
                raise ValueError(f"{origin}: настроен режим Privacy. Публичный YAML не содержит параметров ключа.")
            obj["colorCode"] = integer(row.get("color_code"), origin + ", цветовой код", 0, 15)
            slot = integer(row.get("time_slot"), origin + ", таймслот", 1, 2)
            obj["timeSlot"] = f"TS{slot}"
            if row.get("contact_index") is not None:
                obj["contact"] = contacts.ref(row["contact_index"], origin, types=CALL_TYPES)
            if row.get("rx_group_index") is not None:
                obj["groupList"] = groups.ref(row["rx_group_index"], origin)
            if row.get("radio_id") is None and default_id is None:
                raise ValueError(f"{origin}: отсутствует DMR ID станции по умолчанию.")
            obj["radioId"] = Tag.DEFAULT if row.get("radio_id") is None else f"id{row['radio_id']}"
            result["channels"].append({"dmr": obj})

    for row, obj, origin in zones.records():
        for side in ("A", "B"):
            obj[side] = [channels.ref(entry.get("index") if isinstance(entry, dict) else entry, f"{origin}, {side}")
                         for entry in array(row, side)]
        result["zones"].append(obj)

    scan_timers = {}
    for title, data in section_rows(report, "scan_timing"):
        match = re.match(r"№ ([1-9][0-9]*) · ", title)
        if match:
            timers = {}
            for label, key in (("Удержание сигнала", "holdTime"), ("Опрос приоритетного канала", "prioritySampleTime")):
                value = data.get(label + ", мс", data.get(label, ""))
                if re.fullmatch(r"[0-9]{1,9}( мс)?", value):
                    timers[key] = int(value.split()[0])
            if timers:
                scan_timers[int(match[1])] = timers
    for row, obj, origin in scans.records():
        obj["channels"] = [channels.ref(slot, origin, selected=True) for slot in array(row, "channels")]
        for key in ("primary", "secondary", "revert"):
            if row.get(key) is not None:
                obj[key] = channels.ref(row[key], origin + ", " + key, selected=True)
        if row["index"] in scan_timers:
            obj["tyt"] = scan_timers[row["index"]]
        result["scanLists"].append(obj)
    return result


def export_qdmr(report):
    return yaml.dump(codeplug(report), Dumper=CodeplugDumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, explicit_start=True)


def main():
    try:
        data = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("Снимок превышает 64 МиБ.")
        output = export_qdmr(json.loads(data)).encode("utf-8")
        if len(output) > MAX_BYTES:
            raise ValueError("Экспорт превысил размер YAML 32 МиБ.")
        sys.stdout.buffer.write(output)
    except (ValueError, TypeError, OverflowError, RecursionError, OSError, yaml.YAMLError) as error:
        print(f"Экспорт YAML не выполнен: {str(error)[:400]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
