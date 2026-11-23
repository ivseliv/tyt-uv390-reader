"""Save local TYT CPS edits as a new RDT file. No device operations."""

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile

from cps_channel import channel_details, patch_channel_details
from cps_csv_record import channel_cells, patch_channel_cells
from cps_section_editor import patch_sections, verify_sections
from import_rdt import RDT_SIZE, import_rdt


# File offsets of MD-UV380/390 records documented by editcp's MD-UV390 layout.
TABLES = {
    "channels": (0x40235, 0x40, 3000),
    "contacts": (0x70235, 0x24, 10000),
    "zones": (0x14C05, 0x40, 250),
    "rx_groups": (0xEE45, 0x60, 250),
    "scan_lists": (0x18A85, 0x68, 250),
}
ZONE_EXTENSION = 0x31225
# Stock MD-UV390 CPS empty channel slot, including the normal 400 MHz defaults.
# Initializing the complete slot prevents deleted entries from leaking settings into new ones.
EMPTY_CHANNEL = bytes.fromhex("61 14 00 e0 24 c0 00 00 04 00 00 00 00 00 00 01 "
                              "00 00 00 40 00 00 00 40 ff ff ff ff 00 00 ff ff") + bytes(32)
EMPTY_SCAN = bytes(32) + bytes.fromhex("ff ff ff ff ff ff f1 14 08 ff") + bytes(62)
DIGITAL = {"contact_index", "rx_group_index", "color_code", "time_slot"}
ANALOG = {"rx_tone_code", "tx_tone_code"}
PRIVACY_CODES = {"privacy_type_code", "privacy_index_code"}
COMMON = {"name", "mode", "rx_hz", "tx_hz", "bandwidth_code", "scan_list_index", "details", "cps_csv"}
FIELDS = {"channels": COMMON | DIGITAL | ANALOG | PRIVACY_CODES,
          "contacts": {"name", "dmr_id", "type", "ring"}, "zones": {"name", "A", "B"},
          "rx_groups": {"name", "contacts"}, "scan_lists": {"name", "channels", "primary", "secondary", "revert"}}
MISSING = object()


def integer(value, minimum, maximum, field):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field}: требуется целое число от {minimum} до {maximum}.")
    return value


def label_bytes(value):
    if not isinstance(value, str) or not value or not value.isprintable() or not value.strip():
        raise ValueError("Имя должно содержать печатные символы.")
    data = value.encode("utf-16-le")
    if len(data) > 32:
        raise ValueError("Имя длиннее 16 кодовых единиц UTF-16.")
    return data.ljust(32, b"\0")


def frequency(value):
    integer(value, 10, 999_999_990, "Частота, Гц")
    if value % 10:
        raise ValueError("Формат частоты TYT поддерживает шаг 10 Гц.")
    return bytes.fromhex(f"{value // 10:08d}")[::-1]


def analog_tone(value):
    integer(value, 0, 0xFFFF, "Код тона")
    if value == 0xFFFF:
        return value
    if value & 0x8000:
        if value & 0x3000 or any((value >> shift) & 15 > 7 for shift in (0, 4, 8)):
            raise ValueError("Некорректный двоично-десятичный код DCS.")
    elif not (0 < value <= 0x3999 and all(((value >> bit) & 15) < 10 for bit in (0, 4, 8, 12))):
        raise ValueError("Некорректный двоично-десятичный код CTCSS.")
    return value


def rows(report, table):
    if not isinstance(report.get(table), list):
        raise ValueError(f"Отсутствует таблица {table}.")
    result = {}
    for row in report[table]:
        if not isinstance(row, dict):
            raise ValueError(f"Ошибочная строка в {table}.")
        idx = integer(row.get("index"), 1, TABLES[table][2], table)
        if idx in result:
            raise ValueError(f"Повторяется номер {idx} в {table}.")
        result[idx] = row
    return result


def normalize(row, field):
    value = row.get(field, MISSING)
    if field in ("A", "B") and value is not MISSING:
        if not isinstance(value, list) or not all(isinstance(item, dict) and "index" in item for item in value):
            raise ValueError(f"Сторона {field} должна содержать ссылки на каналы.")
        return [item["index"] for item in value]
    return value


def same(left, right):
    # Python treats True as 1. JSON has separate booleans and numbers.
    if type(left) != type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    return left == right


def read_at(file, offset, size):
    file.seek(offset)
    data = file.read(size)
    if len(data) != size:
        raise ValueError("Не удалось прочитать целую запись RDT.")
    return bytearray(data)


def write_at(file, offset, data):
    file.seek(offset)
    file.write(data)


def new_record(table):
    if table == "channels":
        return bytearray(EMPTY_CHANNEL)
    if table == "contacts":
        return bytearray(b"\xff\xff\xff\xc0" + bytes(32))
    if table == "scan_lists":
        return bytearray(EMPTY_SCAN)
    return bytearray(TABLES[table][1])


def members(values, slots, limit):
    if not isinstance(values, list) or len(values) > slots:
        raise ValueError(f"В списке должно быть не более {slots} участников.")
    for v in values:
        integer(v, 1, limit, "Номер участника")
    if len(set(values)) != len(values):
        raise ValueError("Список содержит повторяющиеся номера участников.")
    return struct.pack(f"<{slots}H", *(values + [0] * (slots - len(values))))


def channel_field(record, key, value, old):
    if key == "mode":
        if value not in ("DMR", "analog"):
            raise ValueError("В RDT поддерживаются режимы analog и DMR.")
        record[0] = (record[0] & 0xFC) | {"DMR": 2, "analog": 1}[value]
    elif key == "bandwidth_code":
        record[0] = (record[0] & ~12) | integer(value, 0, 2, key) << 2
    elif key in ("rx_hz", "tx_hz"):
        offset = 16 if key == "rx_hz" else 20
        record[offset:offset + 4] = frequency(value)
    elif key == "color_code":
        record[1] = (record[1] & 15) | integer(value, 0, 15, key) << 4
    elif key == "time_slot":
        record[1] = (record[1] & ~12) | integer(value, 1, 2, key) << 2
    elif key == "privacy_type_code":
        record[2] = (record[2] & ~0x30) | integer(value, 0, 3, key) << 4
    elif key == "privacy_index_code":
        extended = record[2] & 0x30 == 0x30
        index = integer(value, 0, 255 if extended else 15, key)
        record[2] = (record[2] & ~15) | (index & 15)
        if extended:
            record[30] = (record[30] & 15) | (index & 0xF0)
    elif key in ("rx_group_index", "scan_list_index", "contact_index"):
        limit = 10000 if key == "contact_index" else 250
        index = 0 if value is None else integer(value, 1, limit, key)
        if key == "contact_index":
            struct.pack_into("<H", record, 6, index)
        else:
            record[12 if key == "rx_group_index" else 11] = index
    elif key in ANALOG:
        struct.pack_into("<H", record, 24 if key == "rx_tone_code" else 26, analog_tone(value))
    elif key == "details":
        baseline_fields = old.get("details") if old else None
        if old.get("mode") != {1: "analog", 2: "DMR"}.get(record[0] & 3):
            baseline_fields = None
        patch_channel_details(record, baseline_fields or channel_details(record), value)
    elif key == "cps_csv":
        patch_channel_cells(record, old.get("cps_csv") or channel_cells(record), value)


def patch_row(file, table, idx, old, edited, current, template=None):
    base, size, _ = TABLES[table]
    pos = base + (idx - 1) * size
    name_pos = {"channels": 32, "contacts": 4}.get(table, 0)
    if edited is None:  # Making the name empty invalidates this slot without shifting later slots.
        if current is None:
            return {}
        if any(normalize(current, k) != normalize(old, k) for k in current.keys() | old.keys()):
            raise ValueError(f"Конфликт при удалении {table} №{idx}: исходная запись изменилась.")
        write_at(file, pos + name_pos, bytes(32))
        if table == "contacts":
            byte = read_at(file, pos + 3, 1)[0]
            write_at(file, pos + 3, bytes([byte & ~3]))
        return {}
    if old is None and current is not None:
        raise ValueError(f"Ячейка {table} №{idx} уже занята в исходном файле.")
    if old is not None and current is None:
        raise ValueError(f"Запись {table} №{idx} была удалена из исходного файла.")
    is_new = old is None
    if is_new and table == "rx_groups" and read_at(file, pos, 2) not in (b"\x00\x00", b"\xff\xff"):
        raise ValueError(f"Позиция группы приёма №{idx} содержит нераспознанные данные. Выберите свободный номер.")
    old = old or {}
    current = current or {}
    if template:
        old, current, record = template
    else:
        record = new_record(table) if is_new else read_at(file, pos, size)
    extension_pos = ZONE_EXTENSION + (idx - 1) * 224
    extension = (bytearray(224) if is_new else read_at(file, extension_pos, 224)) if table == "zones" else None
    mode = edited.get("mode")
    changed_mode = table == "channels" and old.get("mode") != mode
    expected = {}
    if is_new:
        required = {"channels": {"mode", "rx_hz", "tx_hz"}, "contacts": {"type", "dmr_id"}}
        if not (required.get(table, set()) | {"name"}) <= edited.keys():
            raise ValueError(f"Недостаточно данных для новой записи {table} №{idx}.")
    keys = old.keys() | edited.keys()
    # Apply CSV changes first so explicit edits from the native controls take precedence.
    priority = {"cps_csv": 0, "mode": 1, "privacy_type_code": 2, "privacy_index_code": 3}
    for key in sorted(keys - {"index", "rdt_template_index"}, key=lambda k: (priority.get(k, 4), k)):
        previous, value = normalize(old, key), normalize(edited, key)
        refresh_index = (key == "privacy_index_code" and type(value) is int
                         and old.get("privacy_type_code") != edited.get("privacy_type_code"))
        if same(value, previous) and not refresh_index:
            continue
        if table == "channels" and changed_mode:
            inactive = DIGITAL | PRIVACY_CODES if mode == "analog" else ANALOG
            if key == "details" and value is MISSING:
                continue
            if key in inactive and value in (None, MISSING):
                continue
        if key not in FIELDS[table] or value is MISSING:
            raise ValueError(f"Поле {table}.{key} нельзя изменить этим редактором.")
        if ((not is_new or template) and normalize(current, key) != previous and normalize(current, key) != value):
            raise ValueError(f"Конфликт изменений в {table} №{idx}, поле {key}.")
        if key == "name":
            record[name_pos:name_pos + 32] = label_bytes(value)
        elif table == "channels":
            if key in DIGITAL | PRIVACY_CODES and mode != "DMR" or key in ANALOG and mode != "analog":
                raise ValueError(f"Поле {key} не относится к режиму {mode}.")
            channel_field(record, key, value, old)
        elif table == "contacts" and key == "dmr_id":
            record[:3] = integer(value, 1, 0xFFFFFF, key).to_bytes(3, "little")
        elif table == "contacts" and key == "type":
            types = {"group": 1, "private": 2, "all": 3}
            if value not in types:
                raise ValueError("Допустимые типы контакта: group, private, all.")
            record[3] = (record[3] & ~3) | types[value]
        elif table == "contacts" and key == "ring":
            if type(value) is not bool:
                raise ValueError("Контакт: звонок должен иметь значение true или false.")
            record[3] = (record[3] & ~0x20) | int(value) << 5
        elif table == "zones":
            side = members(value, 64, 3000)
            if key == "A":
                record[32:] = side[:32]
                extension[:96] = side[32:]
            else:
                extension[96:] = side
        elif table == "rx_groups":
            record[32:] = members(value, 32, 10000)
        elif table == "scan_lists" and key == "channels":
            record[42:] = members(value, 31, 3000)
        elif table == "scan_lists":
            channel = 0xFFFF if value is None else integer(value, 0, 3000, key)
            struct.pack_into("<H", record, {"primary": 32, "secondary": 34, "revert": 36}[key], channel)
        expected[key] = channel_details(record) if key == "details" else value
    if "cps_csv" in expected:
        expected["cps_csv"] = channel_cells(record)
    write_at(file, pos, record)
    if extension is not None:
        write_at(file, extension_pos, extension)
    return expected


def broken_refs(path, report):
    tables = {table: rows(report, table) for table in TABLES}
    errors = set()
    def check(source, section, key, indices, target, group_only=False):
        for idx in indices:
            if idx in (0, None):
                continue
            ref = tables[target].get(idx)
            if ref is None or group_only and ref["type"] != "group":
                errors.add((source, section, key, target, idx))
    for idx, channel in tables["channels"].items():
        for key, target in (("scan_list_index", "scan_lists"), ("rx_group_index", "rx_groups"),
                            ("contact_index", "contacts")):
            check("channels", idx, key, [channel.get(key)], target)
    for idx, zone in tables["zones"].items():
        for key in ("A", "B"):
            check("zones", idx, key, normalize(zone, key), "channels")
    for idx, group in tables["rx_groups"].items():
        check("rx_groups", idx, "contacts", group["contacts"], "contacts", True)
    for idx, scan in tables["scan_lists"].items():
        check("scan_lists", idx, "channels", scan["channels"], "channels")
        for key in ("primary", "secondary", "revert"):
            check("scan_lists", idx, key, [scan.get(key)], "channels")
    # VFO A/B use the same public channel reference offsets as memory channels.
    with path.open("rb") as file:
        for side, pos in (("A", 0x2F125), ("B", 0x2F165)):
            vfo = read_at(file, pos, 13)
            mode = vfo[0] & 3
            if mode in (1, 2):
                check("VFO", side, "scan_list_index", [vfo[11]], "scan_lists")
            if mode == 2:
                check("VFO", side, "contact_index", [int.from_bytes(vfo[6:8], "little")], "contacts")
                check("VFO", side, "rx_group_index", [vfo[12]], "rx_groups")
    # GPS, emergency and quick-access rows include the name when the reference resolves.
    for section in report.get("cps_sections", []):
        if section["id"] not in ("buttons", "gps", "emergency"):
            continue
        for row in section["rows"]:
            for field in row["fields"]:
                match = re.fullmatch(r"№ (\d+)", field["value"])
                if match and field["label"] in ("Канал", "Контакт"):
                    errors.add((section["id"], row["name"], field["label"], "channels" if field["label"] == "Канал"
                                else "contacts", int(match.group(1))))
    return errors


def patch_file(path, baseline, edited):
    current = import_rdt(path)
    existing_problems = broken_refs(path, current)
    if not isinstance(baseline, dict) or not isinstance(edited, dict):
        raise ValueError("Ожидаются исходный и изменённый JSON отчёты.")
    if baseline.get("file_model") != current["file_model"]:
        raise ValueError("Модель исходного RDT отличается от открытого документа.")
    for key in (baseline.keys() | edited.keys()) - (TABLES.keys() | {"dmr_id", "cps_sections"}):
        if not same(baseline.get(key, MISSING), edited.get(key, MISSING)):
            raise ValueError(f"Поле {key} доступно только для чтения.")
    checks = {}
    with path.open("r+b") as file:
        if not same(edited.get("dmr_id"), baseline.get("dmr_id")):
            new_id = integer(edited.get("dmr_id"), 1, 0xFFFFFF, "DMR ID")
            if current["dmr_id"] not in (baseline["dmr_id"], new_id):
                raise ValueError("Конфликт изменения DMR ID.")
            write_at(file, 0x22A9, new_id.to_bytes(3, "little"))
        for table in TABLES:
            before, new, now = (rows(data, table) for data in (baseline, edited, current))
            # Capture templates before applying any pending modifications to their source slots.
            templates = {}
            for idx, row in new.items():
                if "rdt_template_index" not in row:
                    continue
                if table != "channels" or idx in before:
                    raise ValueError("Шаблон можно указать только для нового канала.")
                src = integer(row["rdt_template_index"], 1, 3000, "Номер исходного канала")
                if src not in before or src not in now or before[src] != now[src]:
                    raise ValueError(f"Исходный канал №{src} отсутствует или изменился.")
                base, size, _ = TABLES["channels"]
                templates[idx] = (before[src], now[src], read_at(file, base + (src - 1) * size, size))
            for idx in sorted(before.keys() | new.keys()):
                if same(before.get(idx), new.get(idx)):
                    continue
                checks[table, idx] = (idx in new, patch_row(file, table, idx, before.get(idx),
                                                             new.get(idx), now.get(idx), templates.get(idx)))
        if edited.get("cps_sections") != baseline.get("cps_sections"):
            if current.get("cps_sections") != baseline.get("cps_sections"):
                raise ValueError("Конфликт изменений дополнительных настроек CPS.")
            patch_sections(file, baseline, edited)
    actual = import_rdt(path)
    if edited.get("cps_sections") != baseline.get("cps_sections"):
        verify_sections(baseline, edited, actual)
    if edited.get("dmr_id") != baseline.get("dmr_id") and actual.get("dmr_id") != edited["dmr_id"]:
        raise ValueError("После сохранения DMR ID не совпал.")
    indexed = {table: rows(actual, table) for table in TABLES}
    for (table, idx), (present, fields) in checks.items():
        row = indexed[table].get(idx)
        if (row is not None) != present:
            raise ValueError(f"При проверке RDT не совпала запись {table} №{idx}.")
        if row is not None:
            for key, value in fields.items():
                if normalize(row, key) != value:
                    raise ValueError(f"При проверке RDT не совпало поле {table} №{idx}, {key}.")
    errors = broken_refs(path, actual) - existing_problems
    if errors:
        source, idx, field, target, missing = next(iter(errors))
        raise ValueError(f"{source} №{idx}, {field}: недопустимая ссылка на {target} №{missing}.")
    return actual


def publish_new(staged, output):
    if sys.platform == "win32":
        # Both paths are on the destination volume. Windows rename fails if
        # the destination already exists, including on FAT/exFAT volumes.
        os.rename(staged, output)
        return
    try:
        os.link(staged, output)
        return
    except OSError as error:
        if sys.platform != "darwin" or error.errno not in {
            errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS,
        }:
            raise
    # FAT and exFAT do not provide hard links. macOS extends rename with a kernel-level
    # no-overwrite flag, so another exporter winning the race never loses its output.
    rename_exclusive = ctypes.CDLL(None, use_errno=True).renamex_np
    rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename_exclusive.restype = ctypes.c_int
    if rename_exclusive(os.fsencode(staged), os.fsencode(output), 0x00000004) == -1:  # RENAME_EXCL
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(output))


def export_rdt(original, output, baseline, edited, *, verify=None):
    original, output = Path(original), Path(output)
    if os.path.lexists(output):
        raise FileExistsError("Выберите новое имя: выходной файл уже существует.")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".tyt-rdt-") as directory:
        with original.open("rb") as source, tempfile.NamedTemporaryFile(dir=directory, delete=False) as stage:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != RDT_SIZE:
                raise ValueError("Исходный файл не является поддерживаемым RDT.")
            shutil.copyfileobj(source, stage)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Исходный файл изменился во время копирования.")
        staged = Path(stage.name)
        actual = patch_file(staged, baseline, edited)
        if verify is not None:
            verify(actual)
        publish_new(staged, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-from-template", metavar="TEMPLATE.rdt",
                        help="open a complete, independent local draft using a stock RDT file")
    parser.add_argument("original", nargs="?")
    parser.add_argument("output", nargs="?")
    args = parser.parse_args()
    try:
        if args.new_from_template:
            if args.original or args.output:
                parser.error("В режиме нового документа укажите только --new-from-template ШАБЛОН.rdt.")
            # Factory button assignments, GPS and VFO can refer to contacts and channels.
            # Keep the complete graph when creating the independent draft.
            draft = import_rdt(args.new_from_template)
            json.dump(draft, sys.stdout, ensure_ascii=False, allow_nan=False)
            sys.stdout.write("\n")
            return 0
        if not args.original or not args.output:
            parser.error("Для сохранения копии укажите исходный и новый RDT-файлы.")
        document = json.load(sys.stdin)
        export_rdt(args.original, args.output, document["baseline"], document["edited"])
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Не удалось создать RDT. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
