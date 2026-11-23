#!/usr/bin/env python3
"""Read the settings of a stock TYT MD-UV380 or MD-UV390 into JSON."""

import argparse
import base64
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from cps_channel import channel_details
from cps_csv_record import channel_cells, privacy_index


REGIONS = (
    (0x002084, 0x002087),
    (0x00EC20, 0x01EDF0),
    (0x031000, 0x03EAC0),
    (0x110000, 0x197E40),
)

PUBLIC_REGIONS = (
    (0x2000, 0x200c), (0x2040, 0x2068), (0x2080, 0x2098), (0x20ab, 0x20ac),
    (0x20b0, 0x20f7), (0x2102, 0x2106), (0x2111, 0x2112), (0x2114, 0x2140),
    (0x2180, 0x5800), (0x5c00, 0x5f80), (0x2ef00, 0x2ef80), (0x3ec40, 0x3ed40),
)

SHARED_PUBLIC_REGIONS = ((0x5800, 0x59c0), (0x5a70, 0x5c00))
ENHANCED_REGION = (0x59c0, 0x5a40)
BASIC_REGION = (0x5a50, 0x5a70)
PRIVACY_REGIONS = (ENHANCED_REGION, BASIC_REGION)
SHARED_UNAVAILABLE = (0xffffffff, 0)


def records(data, offset, size, count):
    for index in range(count):
        start = offset + index * size
        yield index + 1, data[start : start + size]


def name(data, offset=0):
    label = data[offset : offset + 32].decode("utf-16-le", errors="replace")
    label = label.split("\0", 1)[0].rstrip("\uffff")
    return label if label and label[0].isprintable() else ""


def number(data, offset, size=2):
    return int.from_bytes(data[offset : offset + size], "little")


def frequency_hz(data, offset):
    digits = data[offset : offset + 4][::-1].hex()
    return int(digits) * 10 if digits.isdecimal() else None


def members(data, offset, count):
    result = []
    for index in struct.unpack_from(f"<{count}H", data, offset):
        if index in (0, 0xFFFF):
            break
        result.append(index)
    return result


def decode(payload, include_privacy=False):
    data = memoryview(payload)
    model = data[:8].tobytes().decode("ascii")
    if model not in ("MD-UV380", "MD-UV390"):
        raise ValueError("Неизвестная модель в ответе прототипа")
    layout = REGIONS + PUBLIC_REGIONS + SHARED_PUBLIC_REGIONS + PRIVACY_REGIONS
    blocks = {}
    cursor = 8
    shared_unavailable = False
    for start, end in layout:
        if cursor == len(data) and ((start, end) in (PUBLIC_REGIONS[0], SHARED_PUBLIC_REGIONS[0])
                                    or ((start, end) == ENHANCED_REGION and not include_privacy)):
            break  # Complete first/second-generation or current public-only stream.
        if len(data) - cursor < 8:
            raise ValueError("Неполный ответ USB-прототипа")
        address, length = struct.unpack_from("<II", data, cursor)
        cursor += 8
        if (start, end) == SHARED_PUBLIC_REGIONS[0] and (address, length) == SHARED_UNAVAILABLE:
            shared_unavailable = True
            break
        if (address, length) != (start, end - start) or cursor + length > len(data):
            raise ValueError("Неполный или неожиданный блок в ответе USB-прототипа")
        blocks[start, end] = data[cursor : cursor + length].tobytes()
        cursor += length
    if cursor != len(data):
        raise ValueError("Лишние данные в ответе USB-прототипа")

    privacy = None
    if BASIC_REGION in blocks:
        basic, enhanced = blocks.pop(BASIC_REGION), blocks.pop(ENHANCED_REGION)
        if include_privacy:
            privacy = {"basic": base64.b64encode(basic).decode("ascii"),
                       "enhanced": base64.b64encode(enhanced).decode("ascii")}
    has_shared_settings = SHARED_PUBLIC_REGIONS[0] in blocks
    settings = decode_regions([blocks[key] for key in REGIONS], include_csv=True)
    if not settings["channels"]:
        channel_table = blocks[REGIONS[-1]]
        if not (has_shared_settings or shared_unavailable) or any(
                record[0x20:0x22] not in (b"\0\0", b"\xff\xff")
                for _, record in records(channel_table, 0, 0x40, 3000)):
            raise ValueError("В ответе нет распознаваемых каналов. Проверьте разметку кодплага")
    if PUBLIC_REGIONS[0] in blocks:
        from cps_basic import basic_section
        from cps_sections import rdt_sections

        # A message and an emergency record each straddle a USB block boundary.
        if has_shared_settings:
            blocks[0x2180, 0x59c0] = blocks.pop((0x2180, 0x5800)) + blocks.pop(SHARED_PUBLIC_REGIONS[0])
            blocks[0x5a70, 0x5f80] = blocks.pop(SHARED_PUBLIC_REGIONS[1]) + blocks.pop((0x5c00, 0x5f80))

        def read_cps_offset(offset, length):
            if offset < 0x2225 or offset + length > 0x40225:
                return None
            address = offset - 0x225
            for (start, end), block in blocks.items():
                if start <= address and address + length <= end:
                    return block[address-start:address-start+length]
            return None

        first = basic_section(read_cps_offset, model)
        settings["cps_sections"] = ([first] if first else []) + rdt_sections(read_cps_offset, settings)
    if shared_unavailable:
        settings.setdefault("read_notes", []).append(
            "Не удалось полностью считать блок Privacy по USB. Ключи, последние шаблоны сообщений "
            "и часть аварийных параметров недоступны.")
    elif not has_shared_settings and (PUBLIC_REGIONS[0] in blocks or include_privacy):
        settings.setdefault("read_notes", []).append(
            "Эта версия USB-помощника пропускает блок Privacy. Для чтения ключей и соседних "
            "настроек используйте обновлённый TYT Reader.")
    snapshot = {"usb_model": model, **settings}
    if not include_privacy:
        return snapshot
    result = {"settings": snapshot}
    if privacy is not None:
        result["usb_privacy"] = privacy
    return result


def decode_regions(regions, include_csv=False):
    """Decode the same four public settings ranges from either USB or a CPS file."""
    dmr_id, lists, extensions, channel_contact = regions
    channels = {}
    for index, record in records(channel_contact, 0, 0x40, 3000):
        label = name(record, 0x20)
        if not label:
            continue
        mode = record[0] & 3
        channel = {
            "index": index,
            "name": label,
            "mode": {1: "analog", 2: "DMR"}.get(mode, f"unknown ({mode})"),
            "rx_hz": frequency_hz(record, 0x10),
            "tx_hz": frequency_hz(record, 0x14),
            "bandwidth_code": (record[0] >> 2) & 3,
            "scan_list_index": record[0x0B] or None,
            "details": channel_details(record),
        }
        if mode == 2:
            channel.update(
                color_code=record[1] >> 4,
                time_slot=2 if (record[1] >> 2) & 3 == 2 else 1,
                contact_index=number(record, 6) or None,
                rx_group_index=record[0x0C] or None,
                privacy_type_code=(record[2] >> 4) & 3,
                privacy_index_code=privacy_index(record),
            )
        elif mode == 1:
            channel.update(rx_tone_code=number(record, 0x18), tx_tone_code=number(record, 0x1A))
        if include_csv:
            try:
                channel["cps_csv"] = channel_cells(record)
            except ValueError:
                # Keep the readable fields of a damaged or unknown channel record.
                pass
        channels[index] = channel

    contacts = []
    for index, record in records(channel_contact, 0x30000, 0x24, 10000):
        label, kind = name(record, 4), record[3] & 3
        if label and kind:
            contacts.append({"index": index, "name": label, "dmr_id": number(record, 0, 3),
                             "ring": bool(record[3] & 0x20), "type": {1: "group", 2: "private", 3: "all"}[kind]})

    zones = []
    for index, record in records(lists, 0x0149E0 - 0x00EC20, 0x40, 250):
        label = name(record)
        if not label:
            continue
        ext_start = (index - 1) * 0xE0
        extension = extensions[ext_start : ext_start + 0xE0]
        side_a = members(record, 0x20, 16) + members(extension, 0, 48)
        side_b = members(extension, 0x60, 64)
        zone = {"index": index, "name": label}
        for side, indices in (("A", side_a), ("B", side_b)):
            zone[side] = [
                {"index": channel, "name": channels.get(channel, {}).get("name")}
                for channel in indices
            ]
        zones.append(zone)

    rx_groups = []
    unreadable_groups = []
    for index, record in records(lists, 0, 0x60, 250):
        if label := name(record):
            group_contacts = members(record, 0x20, 32)
            if not label.isprintable() or "\ufffd" in label or any(i > 10000 for i in group_contacts):
                unreadable_groups.append(index)
                continue
            rx_groups.append({"index": index, "name": label, "contacts": group_contacts})

    scan_lists = []
    for index, record in records(lists, 0x018860 - 0x00EC20, 0x68, 250):
        if label := name(record):
            scan = {"index": index, "name": label, "channels": members(record, 0x2A, 31)}
            for field, offset in (("primary", 0x20), ("secondary", 0x22), ("revert", 0x24)):
                channel = number(record, offset)
                scan[field] = None if channel == 0xFFFF else channel
            scan_lists.append(scan)

    settings = {
        "dmr_id": number(dmr_id, 0, 3),
        "channels": list(channels.values()),
        "zones": zones,
        "contacts": contacts,
        "rx_groups": rx_groups,
        "scan_lists": scan_lists,
    }
    if unreadable_groups:
        slots = ", ".join(str(i) for i in unreadable_groups)
        settings["unrecognized_rx_group_slots"] = unreadable_groups
        settings["read_notes"] = [f"В позициях групп приёма {slots} данные не соответствуют формату TYT. Эти позиции пропущены."]
    return settings


def save_new(path, settings):
    # Stage the complete report with private permissions before publishing it.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as staged:
        json.dump(settings, staged, ensure_ascii=False, indent=2)
        staged.write("\n")
        staged.flush()
        os.link(staged.name, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="path to a new JSON file, or - for standard output")
    parser.add_argument("--reader", type=Path, default=Path(__file__).resolve().with_name("read_usb"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--include-privacy", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.include_privacy and args.output != "-":
        parser.error("--include-privacy используется только для передачи данных через stdout")
    path = None if args.output == "-" else Path(args.output).expanduser().absolute()
    try:
        if path is not None and os.path.lexists(path):
            raise FileExistsError(f"Файл уже существует: {path}")
        if path is not None and not path.parent.is_dir():
            raise FileNotFoundError(f"Папка для результата отсутствует: {path.parent}")
        command = [args.reader, "--include-privacy"] if args.include_privacy else [args.reader]
        read = subprocess.run(command, stdout=subprocess.PIPE, timeout=120)
        if args.include_privacy and read.returncode == 2:
            # Older helpers reject unknown options before opening a USB session.
            read = subprocess.run([args.reader], stdout=subprocess.PIPE, timeout=120)
        read.check_returncode()
        settings = decode(read.stdout, include_privacy=args.include_privacy)
        if path is None:
            report = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
            sys.stdout.buffer.write(report.encode("utf-8"))
            return 0
        save_new(path, settings)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Считывание не завершено: {error}", file=sys.stderr)
        return 1

    print(f"Сохранено {len(settings['channels'])} каналов, {len(settings['zones'])} зон, "
          f"{len(settings['contacts'])} контактов. Файл: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
