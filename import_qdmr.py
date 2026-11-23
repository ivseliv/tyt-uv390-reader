"""Read the channel and contact tables from qdmr's device-independent YAML format."""

import re
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path

from vendor import yaml
from cps_sections import yaml_sections
from cps_channel import yaml_details


class Tag(Enum):
    DEFAULT = "!default"
    SELECTED = "!selected"


MAX_BYTES = 32 * 1024 * 1024


class CodeplugLoader(yaml.SafeLoader):
    # qdmr converts scalars according to their field. IDs, names like "ON", and
    # DCS/DTMF codes with leading zeros must keep their spelling.
    yaml_implicit_resolvers = {
        ch: [(tag, regex) for tag, regex in rules if tag == "tag:yaml.org,2002:null"]
        for ch, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    yaml_constructors = {
        tag: constructor for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
        if tag is None or tag.rsplit(":", 1)[-1] in ("map", "seq", "str", "null")
    }
    for scalar_type in ("int", "float", "bool"):
        yaml_constructors["tag:yaml.org,2002:" + scalar_type] = yaml.SafeLoader.construct_scalar
    depth = 0
    expanded_size = 0

    def __init__(self, stream):
        super().__init__(stream)
        self.anchor_sizes = {}

    def compose_node(self, parent, index):
        event = self.peek_event()
        if self.depth >= 64:
            raise ValueError("Слишком глубокая вложенность YAML.")
        start_size = self.expanded_size
        self.depth += 1
        node = super().compose_node(parent, index)
        self.depth -= 1
        if isinstance(event, yaml.AliasEvent):
            if event.anchor not in self.anchor_sizes:
                raise ValueError("Циклическая ссылка YAML.")
            self.expanded_size += self.anchor_sizes[event.anchor]
        else:
            self.expanded_size += 1 + (len(node.value) if isinstance(node, yaml.ScalarNode) else 0)
            if event.anchor:
                self.anchor_sizes[event.anchor] = self.expanded_size - start_size
        if self.expanded_size > MAX_BYTES:
            raise ValueError("Объём данных YAML после раскрытия ссылок слишком велик.")
        return node

    def construct_mapping(self, node, deep=False):
        result = {}
        for key, value in self.construct_pairs(node, deep=deep):
            if not isinstance(key, str) or key in result:
                raise ValueError(f"Недопустимое или повторное имя поля YAML в строке {node.start_mark.line + 1}.")
            result[key] = value
        return result


def read_tag(loader, node):
    if loader.construct_scalar(node):
        raise ValueError(f"Некорректный тег qdmr в строке {node.start_mark.line + 1}.")
    return Tag(node.tag)


for tag in Tag:
    CodeplugLoader.add_constructor(tag.value, read_tag)


def sequence(mapping, field):
    value = mapping.get(field, [])
    if not isinstance(value, list):
        raise ValueError(f"Поле {field} должно содержать список.")
    return value


def string(value, field):
    if not isinstance(value, str):
        raise ValueError(f"В поле {field} ожидалась строка.")
    return value


def integer(value, field, upper):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value) or int(value) > upper:
        raise ValueError(f"Недопустимое значение {field} в YAML.")
    return int(value)


def frequency(value, field):
    if value is None:
        return None
    match = re.fullmatch(r"\+?(\d+(?:\.\d*)?)\s*(Hz|[kMG](?:Hz)?)?",
                         string(value, field).strip(), re.IGNORECASE | re.ASCII)
    if not match or len(match[1]) > 64:
        raise ValueError(f"Некорректная частота в поле {field}.")
    prefix = (match[2] or "MHz")[0].lower()
    multiplier = {"h": 1, "k": 1000, "m": 1000000, "g": 1000000000}[prefix]
    hz = Decimal(match[1]) * multiplier
    if hz > 2**63 - 1:
        raise ValueError(f"Слишком большая частота в поле {field}.")
    return int(hz.to_integral_value(rounding=ROUND_HALF_UP))


def tone_code(value, field):
    if value is None:
        return 0xFFFF
    if isinstance(value, dict) and len(value) == 1:
        kind, tone = next(iter(value.items()))
        tone = string(tone, field).strip()
        if kind == "ctcss":
            match = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*(?:Hz)?", tone, re.I)
            if match:
                tenths = int((Decimal(match[1]) * 10).to_integral_value(rounding=ROUND_HALF_UP))
                if 0 < tenths <= 3999:
                    return int(str(tenths), 16)
        if kind == "dcs":
            match = re.fullmatch(r"([-in]?)([0-7]{1,3})", tone, re.I)
            if match:
                flag = 0xC000 if match[1].lower() in ("i", "-") else 0x8000
                return flag | int(match[2], 16)
    raise ValueError(f"Некорректный субтон в поле {field}.")


class Table:
    """Enumerate in file order and resolve qdmr IDs independently of their spelling."""

    def __init__(self, doc, section, kinds=None):
        self.rows = []
        self.index = {}
        for index, record in enumerate(sequence(doc, section), 1):
            kind = None
            if kinds:
                if not isinstance(record, dict) or len(record) != 1:
                    raise ValueError(f"Ожидалась запись с типом в {section}, позиция {index}.")
                kind, record = next(iter(record.items()))
                if kind not in kinds:
                    raise ValueError(f"Неизвестный тип записи в {section}, позиция {index}.")
            if not isinstance(record, dict):
                raise ValueError(f"Некорректная запись в {section}, позиция {index}.")
            base = {"index": index, "name": string(record.get("name"), f"{section}.name")}
            if "id" in record:
                ident = string(record["id"], f"{section}.id")
                if not ident or ident in self.index:
                    raise ValueError(f"Пустой или повторный ID в {section}, позиция {index}.")
                self.index[ident] = index
            self.rows.append((kind, record, base))

    def resolve(self, ref, field, selected=False, kind=None):
        if selected and ref is Tag.SELECTED:
            return 0
        if not isinstance(ref, str) or ref not in self.index:
            raise ValueError(f"Ссылка в поле {field} не соответствует ни одной записи.")
        index = self.index[ref]
        if kind is not None and self.rows[index - 1][0] != kind:
            raise ValueError(f"Запись, указанная в поле {field}, имеет другой тип.")
        return index

    def optional(self, record, field, selected=False, kind=None):
        ref = record.get(field)
        return None if ref is None else self.resolve(ref, field, selected, kind)


def decode_qdmr(doc):
    if not isinstance(doc, dict) or not doc.keys() & {
        "radioIDs", "channels", "contacts", "zones", "groupLists", "scanLists",
        "settings", "sms", "positioning", "tytExtension"
    }:
        raise ValueError("В файле отсутствуют разделы кодплага qdmr.")
    settings = doc.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("Некорректный раздел settings.")
    radio_ids = Table(doc, "radioIDs", {"dmr", "m17"})
    contacts = Table(doc, "contacts", {"dmr", "dtmf", "m17"})
    modes = {"fm": "analog", "analog": "analog", "dmr": "DMR", "digital": "DMR", "am": "AM", "m17": "M17"}
    channels = Table(doc, "channels", modes)
    groups = Table(doc, "groupLists")
    scans = Table(doc, "scanLists")
    zones = Table(doc, "zones")

    id_numbers = {row["index"]: integer(item.get("number"), "radioIDs.number", 0xFFFFFF)
                  for kind, item, row in radio_ids.rows if kind == "dmr"}
    default_id = radio_ids.optional(settings, "defaultID")
    if default_id is not None and default_id not in id_numbers:
        raise ValueError("Поле defaultID должно ссылаться на идентификатор DMR.")
    dmr_id = id_numbers.get(default_id) if default_id is not None else next(iter(id_numbers.values()), None)

    call_types = {"GroupCall": "group", "PrivateCall": "private", "AllCall": "all", "All-Call": "all"}
    for kind, item, row in contacts.rows:
        if kind == "dmr":
            if item.get("ring") in ("true", "false"):
                row["ring"] = item["ring"] == "true"
            call = string(item.get("type", "PrivateCall"), "contacts.type")
            if call not in call_types:
                raise ValueError("Неизвестный тип вызова в контакте DMR.")
            number = item.get("number", "16777215" if call_types[call] == "all" else None)
            row.update(type=call_types[call], dmr_id=integer(number, "contacts.number", 0xFFFFFF))
        elif kind == "dtmf":
            row.update(type=kind, dtmf_number=string(item.get("number"), "contacts.number"))
        else:
            row.update(type=kind, callsign=string(item.get("call", ""), "contacts.call"))
            row["broadcast"] = item.get("isBroadcast", "false") == "true"

    for kind, item, row in channels.rows:
        mode = modes[kind]
        details = yaml_details(item)
        if details:
            row["details"] = details
        row.update(mode=mode, rx_hz=frequency(item.get("rxFrequency"), "rxFrequency"),
                   tx_hz=frequency(item.get("txFrequency"), "txFrequency"),
                   scan_list_index=scans.optional(item, "scanListRef" if "scanListRef" in item else "scanList"))
        if mode in ("DMR", "M17"):
            row["contact_index"] = contacts.optional(item, "contact", kind=mode.lower())
        if mode == "DMR":
            slot = string(item.get("timeSlot", "TS1"), "timeSlot")
            if slot not in ("TS1", "TS2"):
                raise ValueError("Неизвестное значение timeSlot.")
            row.update(time_slot=int(slot[-1]), color_code=integer(item.get("colorCode", "1"), "colorCode", 15),
                       rx_group_index=groups.optional(item, "groupList"), bandwidth_code=0)
            radio_id = item.get("radioId", item.get("radioID"))
            if radio_id is not None and radio_id is not Tag.DEFAULT:
                row["radio_id"] = id_numbers[radio_ids.resolve(radio_id, "radioId", kind="dmr")]
        elif mode == "analog":
            bandwidth = string(item.get("bandwidth", "Narrow"), "bandwidth")
            if bandwidth not in ("Narrow", "Wide"):
                raise ValueError("Неизвестная полоса аналогового канала.")
            row.update(bandwidth_code=0 if bandwidth == "Narrow" else 2,
                       rx_tone_code=tone_code(item.get("rxTone"), "rxTone"),
                       tx_tone_code=tone_code(item.get("txTone"), "txTone"))

    for _, item, zone in zones.rows:
        for side in ("A", "B"):
            indices = [channels.resolve(ref, side) for ref in sequence(item, side)]
            zone[side] = [{"index": idx, "name": channels.rows[idx - 1][2]["name"]} for idx in indices]
    for _, item, group in groups.rows:
        group["contacts"] = [contacts.resolve(ref, "groupLists.contacts", kind="dmr")
                             for ref in sequence(item, "contacts")]
    for _, item, scan in scans.rows:
        scan["channels"] = [channels.resolve(ref, "scanLists.channels", selected=True)
                            for ref in sequence(item, "channels")]
        for field in ("primary", "secondary", "revert"):
            scan[field] = channels.optional(item, field, selected=True)

    report = {"dmr_id": dmr_id, "source_format": "qdmr"}
    for field, table in (("channels", channels), ("contacts", contacts), ("zones", zones),
                         ("rx_groups", groups), ("scan_lists", scans)):
        report[field] = [row for _, _, row in table.rows]
    default_index = default_id if default_id is not None else next(iter(id_numbers), None)
    report["cps_sections"] = yaml_sections(doc, default_index, channels, contacts)
    return report


def import_qdmr(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл YAML превышает 32 МиБ.")
    try:
        doc = yaml.load(raw, Loader=CodeplugLoader)
    except yaml.YAMLError as error:
        # Keep raw YAML lines (which may include commercial extensions) out of diagnostics.
        mark = getattr(error, "problem_mark", None)
        location = f" в строке {mark.line + 1}" if mark is not None else ""
        raise ValueError(f"Некорректный YAML{location}.") from None
    return decode_qdmr(doc)
