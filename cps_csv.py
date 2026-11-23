"""Read English TYT CPS CSV tables and TYT Reader reports without using USB."""

import csv
import io
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path


# English captions in export order from MD-UV390 CPS V2.42.
CPS_CHANNELS = (
    "Channel Mode", "Channel Name", "RX Frequency(MHz)", "TX Frequency(MHz)",
    "Band Width", "Scan List", "Squelch", "RX Ref Frequency", "TX Ref Frequency",
    "TOT[s]", "TOT Rekey Delay[s]", "Power", "Admit Criteria", "Auto Scan",
    "Rx Only", "Lone Worker", "VOX", "Allow Talkaround", "Send GPS Info",
    "Receive GPS Info", "Private Call Confirmed", "Emergency Alarm Ack",
    "Data Call Confirmed", "Allow Interrupt", "DCDM Switch", "Leader/MS",
    "Emergency System", "Contact Name", "Group List", "Color Code", "Repeater Slot",
    "In Call Criteria", "Privacy", "Privacy No.", "GPS System", "CTCSS/DCS Dec",
    "CTCSS/DCS Enc", "Rx Signaling System", "Tx Signaling System", "QT Reverse",
    "Non-QT/DQT Turn-off Freq", "Display PTT ID", "Reverse Burst/Turn-off Code",
    "Decode 1", "Decode 2", "Decode 3", "Decode 4", "Decode 5", "Decode 6",
    "Decode 7", "Decode 8",
)
CPS_CONTACTS = ("Contact Name", "Call Type", "Call ID", "Call Receive Tone")
READER_CHANNELS = (
    "index", "name", "mode", "rx_hz", "tx_hz", "bandwidth_code", "scan_list_index",
    "color_code", "time_slot", "contact_index", "rx_group_index", "privacy_type_code",
    "privacy_index_code", "rx_tone_code", "tx_tone_code",
)
READER_CONTACTS = ("index", "name", "type", "dmr_id", "dtmf_number", "callsign", "call_receive_tone")
LAYOUTS = {
    CPS_CHANNELS: ("channels", "cps"), CPS_CONTACTS: ("contacts", "cps"),
    READER_CHANNELS: ("channels", "reader"), READER_CONTACTS: ("contacts", "reader"),
}
TEXT = {"name", "type", "mode", "dtmf_number", "callsign"}
MAX_CSV = 32 * 1024 * 1024


def unsigned(value, maximum=2**63 - 1):
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ValueError("Ожидалось целое неотрицательное число.")
    result = int(value)
    if result > maximum:
        raise ValueError("Число выходит за допустимые пределы.")
    return result


def hz(value):
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,6})?", value):
        raise ValueError("Неверная частота в МГц.")
    return int(Decimal(value) * 1_000_000)


def tone(value):
    if value.lower() in ("", "none", "off", "0", "0.0"):
        return 0xFFFF
    dcs = re.fullmatch(r"D([0-7]{3})([NI])", value, re.I)
    if dcs:
        return int(dcs[1], 16) | (0x8000 if dcs[2].upper() == "N" else 0xC000)
    if re.fullmatch(r"\d{1,3}(?:\.\d)?", value, re.ASCII):
        tenths = int(Decimal(value) * 10)
        if 0 < tenths <= 3999:
            return int(str(tenths), 16)
    # The source cell is preserved even when the displayed tone is unknown.
    return None


def from_cps(item, kind, index):
    if kind == "contacts":
        call = {"1": "group", "2": "private", "3": "all", "4": "dispatch", "5": "pc"}
        if item["Call Type"] not in call:
            raise ValueError("Неверный тип вызова CPS.")
        ring = unsigned(item["Call Receive Tone"], 1)
        return {"index": index, "name": item["Contact Name"], "type": call[item["Call Type"]],
                "dmr_id": unsigned(item["Call ID"], 0xffffff),
                "call_receive_tone": ring, "ring": bool(ring)}

    mode = {"1": "analog", "2": "DMR"}.get(item["Channel Mode"])
    if mode is None:
        raise ValueError("Неверный режим канала CPS.")
    row = {"index": index, "name": item["Channel Name"], "mode": mode,
           "rx_hz": hz(item["RX Frequency(MHz)"]), "tx_hz": hz(item["TX Frequency(MHz)"]),
           "bandwidth_code": unsigned(item["Band Width"], 3),
           "scan_list_index": unsigned(item["Scan List"]) or None}
    if mode == "DMR":
        row.update(color_code=unsigned(item["Color Code"], 15),
                   time_slot=1 + unsigned(item["Repeater Slot"], 1),
                   rx_group_index=unsigned(item["Group List"]) or None,
                   contact_index=unsigned(item["Contact Name"]) or None,
                   privacy_type_code=unsigned(item["Privacy"]),
                   privacy_index_code=unsigned(item["Privacy No."]))
    else:
        row.update(rx_tone_code=tone(item["CTCSS/DCS Dec"]), tx_tone_code=tone(item["CTCSS/DCS Enc"]))
    return row


def from_reader(item):
    row = {}
    for key, value in item.items():
        if value != "":
            row[key] = value if key in TEXT else unsigned(value)
    if not row.get("name") or not row.get("index") or not (row.get("mode") or row.get("type")):
        raise ValueError("Отсутствует номер, название или тип записи.")
    if "call_receive_tone" in row:
        if row["call_receive_tone"] not in (0, 1):
            raise ValueError("Недопустимый код звонка контакта.")
        row["ring"] = bool(row["call_receive_tone"])
    return row


def cps_tone(code):
    if code in (None, 0xFFFF):
        return "None"
    if code == 0:
        return "0.0"
    if type(code) is not int or not 0 <= code <= 0xFFFF:
        raise ValueError("Неверный код CTCSS/DCS.")
    digits = f"{code & 0x0FFF:03X}"
    if code & 0xF000 in (0x8000, 0xC000) and re.fullmatch("[0-7]{3}", digits):
        return f"D{digits}{'N' if code & 0xF000 == 0x8000 else 'I'}"
    digits = f"{code:04X}"
    if digits.isdecimal() and 0 < int(digits) <= 3999:
        return f"{int(digits) / 10:.1f}"
    raise ValueError("Неверный код CTCSS/DCS.")


CHANNEL_FIELDS = {
    "bandwidth_code": ("Band Width", 3), "color_code": ("Color Code", 15),
    "scan_list_index": ("Scan List", 250), "contact_index": ("Contact Name", 10000),
    "rx_group_index": ("Group List", 250), "privacy_type_code": ("Privacy", 255),
    "privacy_index_code": ("Privacy No.", 255),
}


def updated_cps_cells(row, kind, index, header):
    saved = row.get("cps_csv")
    if not isinstance(saved, dict) or not all(isinstance(saved.get(k), str) for k in header):
        raise ValueError("Для новой записи отсутствуют исходные колонки CPS. Сохраните документ в JSON.")
    if row.get("index") != index:
        raise ValueError("В CSV CPS номер задаётся положением строки. Сохраните разреженную таблицу в JSON.")
    cells = saved.copy()
    original = from_cps(saved, kind, index)
    changed = {key for key in original.keys() | row.keys() if row.get(key) != original.get(key)}
    if "name" in changed:
        if not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("Имя записи CPS должно быть заполнено.")
        cells["Channel Name" if kind == "channels" else "Contact Name"] = row["name"]
    if kind == "contacts":
        if "type" in changed:
            if row.get("type") not in CONTACT_CODES:
                raise ValueError("Этот тип контакта не поддерживается в цифровых контактах CPS.")
            cells["Call Type"] = CONTACT_CODES[row["type"]]
        for field, caption, maximum in (("dmr_id", "Call ID", 0xFFFFFF),
                                        ("call_receive_tone", "Call Receive Tone", 1)):
            if field in changed and (field != "call_receive_tone" or field in row):
                cells[caption] = str(unsigned(str(row.get(field)), maximum))
        if "ring" in changed and isinstance(row.get("ring"), bool):
            cells["Call Receive Tone"] = str(int(row["ring"]))
        return cells

    if "mode" in changed:
        if row.get("mode") not in ("analog", "DMR"):
            raise ValueError("CPS поддерживает каналы FM и DMR.")
        cells["Channel Mode"] = "1" if row["mode"] == "analog" else "2"
    for field, caption in (("rx_hz", "RX Frequency(MHz)"), ("tx_hz", "TX Frequency(MHz)")):
        if field in changed:
            value = unsigned(str(row.get(field)), 999999999)
            if value % 10:
                raise ValueError("Шаг частоты TYT CPS составляет 10 Гц.")
            cells[caption] = f"{Decimal(value) / 1_000_000:.5f}"
    for field, (caption, maximum) in CHANNEL_FIELDS.items():
        if field in changed:
            value = row.get(field)
            if row["mode"] == "analog" and field not in ("bandwidth_code", "scan_list_index") and value is None:
                continue
            if value is None and field in ("scan_list_index", "contact_index", "rx_group_index"):
                value = 0
            cells[caption] = str(unsigned(str(value), maximum))
    if "time_slot" in changed and row["mode"] == "DMR":
        if row.get("time_slot") not in (1, 2):
            raise ValueError("Для DMR необходим таймслот 1 или 2.")
        cells["Repeater Slot"] = str(row["time_slot"] - 1)
    for field, caption in (("rx_tone_code", "CTCSS/DCS Dec"), ("tx_tone_code", "CTCSS/DCS Enc")):
        if field in changed and row["mode"] == "analog":
            cells[caption] = cps_tone(row.get(field))
    if row.get("details"):
        from cps_channel import channel_details, patch_channel_details
        from cps_csv_record import channel_cells, patch_channel_cells
        view = bytearray(64)
        patch_channel_cells(view, {}, cells)
        previous = channel_details(view)
        displayed = {field["label"]: field["value"] for field in row["details"]}
        updated = [{"label": field["label"], "value": displayed.get(field["label"], field["value"])}
                   for field in previous]
        if previous != updated:
            before = channel_cells(view)
            patch_channel_details(view, previous, updated)
            after = channel_cells(view)
            cells.update({key: val for key, val in after.items() if val != before[key]})
    return cells


def import_csv(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_CSV + 1)
    if len(data) > MAX_CSV:
        raise ValueError("CSV превышает 32 МБ.")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be"
        data = data[2:]
    else:
        encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        content = data.decode(encoding)
    except UnicodeDecodeError:
        if encoding != "utf-8":
            raise ValueError("Некорректная кодировка CSV.") from None
        encoding = "cp1251"  # legacy ANSI on Russian Windows
        content = data.decode(encoding)
    if "\0" in content:
        raise ValueError("CSV содержит нулевые байты.")
    records = csv.reader(io.StringIO(content, newline=""), strict=True)
    header = tuple(next(records, ()))
    header = tuple(cell.strip() for cell in header)
    if header not in LAYOUTS:
        raise ValueError("Неизвестные заголовки CSV. Ожидается таблица каналов или контактов "
                         "английской TYT CPS либо CSV-отчёт TYT Reader.")
    kind, style = LAYOUTS[header]
    report = {name: [] for name in ("channels", "contacts", "zones", "rx_groups", "scan_lists")}
    seen = set()
    slot = 0
    for cells in records:
        if not cells:
            continue
        slot += 1
        if len(cells) != len(header) or slot > 100000:
            raise ValueError(f"Неверное число полей или слишком много записей в строке {records.line_num}.")
        item = dict(zip(header, cells))
        if style == "cps" and not item["Channel Name" if kind == "channels" else "Contact Name"]:
            continue  # An empty CPS slot retains its ordinal but does not replace a named record.
        try:
            row = from_cps(item, kind, slot) if style == "cps" else from_reader(item)
            if not row["name"] or row["index"] in seen:
                raise ValueError("Пустое имя или повтор номера записи.")
        except ValueError as error:
            raise ValueError(f"Строка {records.line_num}: {error}") from None
        seen.add(row["index"])
        if style == "cps":
            row["cps_csv"] = item
            if kind == "channels":
                from cps_channel import channel_details
                from cps_csv_record import patch_channel_cells
                image = bytearray(64)
                patch_channel_cells(image, {}, item)
                row["details"] = [field for field in channel_details(image) if field["label"] != "Privacy Switch"]
        report[kind].append(row)
    report["csv_source"] = {"collection": kind, "format": style, "encoding": encoding}
    return report


CONTACT_CODES = {"group": "1", "private": "2", "all": "3", "dispatch": "4", "pc": "5"}


def native_contact_cells(row):
    tone_value = row.get("call_receive_tone")
    if isinstance(row.get("ring"), bool):
        tone_value = int(row["ring"])
    elif tone_value is None and isinstance(row.get("cps_csv"), dict):
        raw_tone = row["cps_csv"].get("Call Receive Tone")
        tone_value = int(raw_tone) if raw_tone in ("0", "1") else None
    if (not isinstance(row.get("name"), str) or not row["name"] or row.get("type") not in CONTACT_CODES
            or type(row.get("dmr_id")) is not int or not 0 <= row["dmr_id"] <= 0xffffff
            or type(tone_value) is not int or tone_value not in (0, 1)):
        return None
    return {"Contact Name": row["name"], "Call Type": CONTACT_CODES[row["type"]],
            "Call ID": str(row["dmr_id"]), "Call Receive Tone": str(tone_value)}


def complete_cps_row(row, kind):
    maximum = 3000 if kind == "channels" else 10000
    if not isinstance(row, dict) or type(row.get("index")) is not int or not 1 <= row["index"] <= maximum:
        return False
    if kind == "contacts":
        return native_contact_cells(row) is not None
    cells = row.get("cps_csv")
    return (row.get("mode") in ("analog", "DMR") and isinstance(cells, dict)
            and all(isinstance(cells.get(k), str) for k in CPS_CHANNELS))


def encode_table(text, encoding):
    bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(encoding, b"")
    try:
        return bom + text.encode(encoding)
    except UnicodeEncodeError:
        # Preserve newly entered Unicode names outside the Windows code page.
        return text.encode("utf-8-sig")


def export_csv(report, kind):
    if not isinstance(report, dict) or not isinstance(report.get(kind), list):
        raise ValueError("В JSON отсутствует выбранная таблица.")
    source = report.get("csv_source") or {}
    from_cps_file = source.get("collection") == kind and source.get("format") == "cps"
    native_model = report.get("usb_model") or report.get("file_model")
    use_cps = (bool(report[kind]) or from_cps_file or native_model in ("MD-UV380", "MD-UV390"))
    use_cps = use_cps and all(complete_cps_row(item, kind) for item in report[kind])
    if source.get("collection") == kind and source.get("format") == "reader":
        use_cps = False
    if use_cps:
        header = CPS_CHANNELS if kind == "channels" else CPS_CONTACTS
        encoding = source.get("encoding") if from_cps_file else "cp1251"
        if encoding not in ("cp1251", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            raise ValueError("Неизвестная кодировка исходного CSV.")
    else:
        header = READER_CHANNELS if kind == "channels" else READER_CONTACTS
        encoding = "utf-8-sig"
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(header)
    previous = 0
    for index, item in enumerate(report[kind], 1):
        if not isinstance(item, dict):
            raise ValueError("Повреждённая строка в снимке.")
        if use_cps:
            index = item.get("index")
            maximum = 3000 if kind == "channels" else 10000
            if type(index) is not int or not previous < index <= maximum:
                raise ValueError("Неверный порядок или номер слота CPS.")
            if index > previous + 1:
                placeholder = empty_cps_slot(kind)
                for _ in range(previous + 1, index):
                    writer.writerow(placeholder)
            saved = item.get("cps_csv")
            if kind == "contacts" and (not isinstance(saved, dict) or not set(CPS_CONTACTS) <= saved.keys()):
                item = native_contact_cells(item)
            else:
                item = updated_cps_cells(item, kind, index, header)
            previous = index
        elif kind == "contacts" and isinstance(item.get("ring"), bool):
            item = {**item, "call_receive_tone": int(item["ring"])}
        writer.writerow([item.get(key) for key in header])
    return encode_table(text.getvalue(), encoding)


def empty_cps_slot(kind):
    if kind == "contacts":
        return ["", "0", "0", "0"]
    from cps_csv_record import channel_cells
    from rdt_editor import EMPTY_CHANNEL
    cells = channel_cells(EMPTY_CHANNEL)
    return [cells[key] for key in CPS_CHANNELS]


def export_rdt_csv(path, kind):
    from import_rdt import MODEL_OFFSET, RDT_SIZE
    from rdt_editor import TABLES
    from read_settings import name
    from cps_csv_record import channel_cells

    if kind not in ("channels", "contacts"):
        raise ValueError("Можно экспортировать каналы или контакты.")
    header = CPS_CHANNELS if kind == "channels" else CPS_CONTACTS
    base, size, slots = TABLES[kind]
    with Path(path).open("rb", buffering=0) as source:
        if os.fstat(source.fileno()).st_size != RDT_SIZE:
            raise ValueError("Неизвестный размер RDT.")
        source.seek(MODEL_OFFSET)
        if source.read(8) not in (b"MD-UV380", b"MD-UV390"):
            raise ValueError("Неизвестный заголовок RDT.")
        source.seek(base)
        table = source.read(size * slots)
    if len(table) != size * slots:
        raise ValueError("Не удалось прочитать таблицу RDT.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    previous = 0
    placeholder = empty_cps_slot(kind)
    for index in range(1, slots + 1):
        record = table[(index - 1) * size:index * size]
        label = name(record, 32 if kind == "channels" else 4)
        if not label:
            continue
        if kind == "channels":
            try:
                cells = channel_cells(record)
            except ValueError as error:
                raise ValueError(f"Канал №{index}: {error}") from None
        else:
            call_type = record[3] & 3
            if not call_type:
                continue
            cells = {"Contact Name": label, "Call Type": str(call_type),
                     "Call ID": str(int.from_bytes(record[:3], "little")),
                     "Call Receive Tone": str((record[3] >> 5) & 1)}
        for _ in range(previous + 1, index):
            writer.writerow(placeholder)
        writer.writerow([cells[field] for field in header])
        previous = index
    return encode_table(output.getvalue(), "cp1251")


def merged_channel(old, source):
    from cps_channel import channel_details
    from cps_csv_record import patch_channel_cells
    from rdt_editor import ANALOG, DIGITAL, EMPTY_CHANNEL, PRIVACY_CODES

    row = old.copy()
    row.update({k: v for k, v in source.items() if k not in ("details", "cps_csv")})
    mode = row.get("mode")
    if mode not in ("analog", "DMR"):
        raise ValueError("В RDT поддерживаются каналы FM и DMR.")
    for key in (DIGITAL | PRIVACY_CODES if mode == "analog" else ANALOG):
        row.pop(key, None)
    if "cps_csv" in source:
        cells = source["cps_csv"]
        temp = bytearray(EMPTY_CHANNEL)
        patch_channel_cells(temp, {}, cells)
        row["cps_csv"] = cells.copy()
        details = channel_details(temp)
        old_fields = {f["label"]: f["value"] for f in old.get("details", [])}
        for field in details:
            # The privacy switch has no column in the vendor's CSV schema.
            if field["label"] == "Privacy Switch" and field["label"] in old_fields:
                field["value"] = old_fields[field["label"]]
        row["details"] = details
    elif old.get("mode") != mode:
        row.pop("details", None)
    return row


def merge_csv(report, csv_report):
    from rdt_editor import label_bytes, rows
    if not isinstance(report, dict) or not isinstance(csv_report, dict):
        raise ValueError("Ожидался снимок настроек.")
    kind = (csv_report.get("csv_source") or {}).get("collection")
    if kind not in ("channels", "contacts"):
        raise ValueError("Не удалось определить таблицу для объединения.")
    incoming = rows(csv_report, kind)
    existing = rows(report, kind)
    renamed = {}
    for index, source in incoming.items():
        label_bytes(source["name"])
        old = existing.get(index, {})
        if kind == "channels":
            row = merged_channel(old, source)
            if old.get("name") != row["name"]:
                renamed[index] = row["name"]
        else:
            row = old.copy()
            row.update({k: v for k, v in source.items() if k not in ("cps_csv", "call_receive_tone")})
            if "call_receive_tone" in source:
                row["ring"] = bool(source["call_receive_tone"])
            if (row.get("type") not in ("group", "private", "all") or type(row.get("dmr_id")) is not int
                    or not 1 <= row["dmr_id"] <= 0xffffff):
                raise ValueError(f"Контакт №{index} не поддерживается в таблице цифровых контактов RDT.")
        existing[index] = row
    result = {**report, kind: [existing[i] for i in sorted(existing)]}
    if renamed:
        result["zones"] = [
            {**z, **{side: [{**m, "name": renamed[m["index"]]} if m["index"] in renamed else m
                           for m in z[side]] for side in ("A", "B")}}
            for z in report["zones"]
        ]
    return result

def main(args):
    try:
        if len(args) == 2 and args[0] == "import":
            data = json.dumps(import_csv(args[1]), ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif len(args) == 3 and args[0] == "export-rdt" and args[1] in ("channels", "contacts"):
            data = export_rdt_csv(args[2], args[1])
        elif len(args) == 2 and (args[0] == "merge" or args[0] == "export" and args[1] in ("channels", "contacts")):
            raw = sys.stdin.buffer.read(128 * 1024 * 1024 + 1)
            if len(raw) > 128 * 1024 * 1024:
                raise ValueError("Снимок превышает 128 МБ.")
            report = json.loads(raw)
            if args[0] == "merge":
                merged = merge_csv(report, import_csv(args[1]))
                data = json.dumps(merged, ensure_ascii=False, allow_nan=False).encode("utf-8")
            else:
                data = export_csv(report, args[1])
        else:
            raise ValueError("Укажите import/merge <файл.csv>, export channels|contacts или export-rdt channels|contacts <файл.rdt>.")
        sys.stdout.buffer.write(data)
        return 0
    except (OSError, ValueError, csv.Error, RecursionError, TypeError, AttributeError, KeyError) as error:
        print(str(error)[:450], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
