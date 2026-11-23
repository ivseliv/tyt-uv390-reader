"""Plan and apply a model-bound read/modify/write of ordinary TYT codeplug tables."""

import argparse
import base64
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from import_rdt import RDT_SIZE, SEGMENTS
from normalize_snapshot import indexed, validate_tables
from rdt_editor import TABLES, normalize, patch_row, rows, same
from rdt_template import CORE, project_row
from read_settings import REGIONS, decode_regions

BLOCK = 1024
SECTOR = 0x10000
SECTORS = (0x10000, 0x30000, *range(0x110000, 0x1A0000, SECTOR))
READ_ONLY = (0x2000, *range(0xEC00, 0x10000, BLOCK))
ADDRESSES = (*READ_ONLY, *(sector + offset for sector in SECTORS for offset in range(0, SECTOR, BLOCK)))
MODELS = ("MD-UV380", "MD-UV390")
HEADER = struct.Struct("<8s8s12sI")
ADDRESS = struct.Struct("<I")
BASE_MAGIC = b"TYTBAS01"
PLAN_MAGIC = b"TYTWRT01"
PLAN_LENGTH = HEADER.size + len(ADDRESSES) * (4 + 2 * BLOCK)
# Limits are exclusive. Separate zone extensions contribute to the same zone count.
WRITABLE = (
    ("rx_groups", 0xEC20, 0x149E0, 96), ("zones", 0x149E0, 0x18860, 64),
    ("scan_lists", 0x18860, 0x1EDF0, 104), ("zones", 0x31000, 0x3EAC0, 224),
    ("channels", 0x110000, 0x13EE00, 64), ("contacts", 0x140000, 0x197E40, 36),
)
CLI_ERRORS = (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, subprocess.SubprocessError)


def _decode_wire(data, magic):
    writing = magic == PLAN_MAGIC
    width = 4 + BLOCK * (2 if writing else 1)
    if len(data) != HEADER.size + len(ADDRESSES) * width:
        raise ValueError("Некорректная длина ответа кодплага.")
    actual, model, binding, count = HEADER.unpack_from(data)
    model = model.decode("ascii")
    if actual != magic or model not in MODELS or count != len(ADDRESSES):
        raise ValueError("Некорректный заголовок штатного кодплага TYT.")
    before, after = {}, {}
    pos = HEADER.size
    for addr in ADDRESSES:
        if ADDRESS.unpack_from(data, pos)[0] != addr:
            raise ValueError("Неожиданный блок при передаче кодплага.")
        start = pos + 4
        before[addr] = data[start:start + BLOCK]
        if writing:
            after[addr] = data[start + BLOCK:start + 2 * BLOCK]
        pos += width
    guard = before[0x2000]
    if any(guard[:0x84]) or any(guard[0x87:]) or any(before[0xec00][:32]):
        raise ValueError("Компонент чтения вернул данные вне полей проверки.")
    return model, binding, before, after


def decode_baseline(data):
    return _decode_wire(data, BASE_MAGIC)[:3]


def decode_plan(data):
    model, binding, before, after = _decode_wire(data, PLAN_MAGIC)
    _changes(before, after)
    return model, binding, before, after


def _changes(before, after):
    touched = {key: set() for key in TABLES}
    blocks = 0
    sectors = set()
    if before.keys() != after.keys() or before.keys() != set(ADDRESSES):
        raise ValueError("Получен неполный набор блоков кодплага.")
    for addr in ADDRESSES:
        old, new = before[addr], after[addr]
        if len(old) != BLOCK or len(new) != BLOCK:
            raise ValueError("Некорректный размер блока кодплага.")
        if old == new:
            continue
        if addr in READ_ONLY:
            raise ValueError("Изменение затрагивает сектор, доступный только для чтения.")
        blocks += 1
        sectors.add(addr // SECTOR)
        for offset, (x, y) in enumerate(zip(old, new)):
            if x == y:
                continue
            address = addr + offset
            for table, start, end, size in WRITABLE:
                if start <= address < end:
                    touched[table].add((address - start) // size + 1)
                    break
            else:
                raise ValueError("Изменение выходит за границы поддерживаемых таблиц.")
    return blocks, len(sectors), {"dmr_id": 0, **{k: len(v) for k, v in touched.items()}}


def proposal_from_blocks(model, binding, before, after, changes=None):
    if model not in MODELS or not isinstance(binding, bytes) or len(binding) != 12:
        raise ValueError("Не удалось определить целевую рацию.")
    blocks, sectors, actual_changes = _changes(before, after)
    if changes is not None and not same(changes, actual_changes):
        raise ValueError("Предпросмотр расходится с изменениями кодплага.")
    result = bytearray(HEADER.pack(PLAN_MAGIC, model.encode("ascii"), binding, len(ADDRESSES)))
    for addr in ADDRESSES:
        result.extend(ADDRESS.pack(addr))
        result.extend(before[addr])
        result.extend(after[addr])
    return {"model": model, "dmr_id": int.from_bytes(before[0x2000][0x84:0x87], "little"),
            "blocks": blocks, "sectors": sectors, "changes": actual_changes,
            "plan": base64.b64encode(result).decode("ascii")}


class SleepInhibitError(Exception):
    """logind refused the sleep inhibitor before the radio helper started."""


def _run_helper(mode, *, helper=None, data=None):
    inhibit_sleep = sys.platform == "linux" and helper is None and mode == "--write-settings"
    if helper is None:
        if os.name == "nt" or sys.platform == "linux":
            from win_document import _helper_path
            helper = _helper_path()
        else:
            helper = Path(__file__).with_name("read_usb")
    command = [str(Path(helper).resolve()), mode]
    if inhibit_sleep:
        command = ["/usr/bin/systemd-inhibit", "--what=idle:sleep", "--mode=block", "--who=TYT Reader",
                   "--why=Writing and verifying radio settings", "--", *command]
    # A wall-clock timeout killing the helper after an erase could leave the radio
    # partially programmed. The native transport bounds each transfer instead.
    result = subprocess.run(command, input=data, capture_output=True,
                            stdin=subprocess.DEVNULL if data is None else None,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if inhibit_sleep and result.returncode and b"WRITE_" not in result.stderr:
        if result.stderr.startswith((b"Failed to inhibit:", b"Failed to connect to bus", b"Failed to connect to system")):
            raise SleepInhibitError()
    return result


def read_baseline(helper=None):
    result = _run_helper("--read-write-baseline", helper=helper)
    if result.returncode:
        log = result.stderr.decode("utf-8", "replace").strip()
        if sys.platform == "linux":
            from win_document import usb_read_error
            raise ValueError(usb_read_error(log))
        raise ValueError(log[-2048:] or "Не удалось выполнить операцию с кодплагом рации.")
    return decode_baseline(result.stdout)


def _file_offset(address):
    for start, end, offset in SEGMENTS:
        if start <= address < end:
            return offset + address - start
    raise ValueError("Адрес вне допустимой разметки TYT RDT.")


def _make_image(blocks):
    # Only the authorized codeplug sectors and sanitized comparison guards are
    # present here. The in-memory RDT layout lets us reuse the verified field editor.
    image = io.BytesIO(bytes(RDT_SIZE))
    for addr in ADDRESSES:
        image.seek(_file_offset(addr))
        image.write(blocks[addr])
    return image


def _decode_image(image):
    regions = []
    for start, end in REGIONS:
        image.seek(_file_offset(start))
        regions.append(image.read(end - start))
    return decode_regions(regions)


def _source_model(report):
    if not isinstance(report, dict) or "csv_source" in report:
        raise ValueError("Выберите полный снимок TYT Reader или штатный кодплаг RDT.")
    models = {report[k] for k in ("usb_model", "file_model") if k in report}
    if len(models) != 1 or not models.issubset(MODELS):
        raise ValueError("Нужен кодплаг с указанием модели MD-UV380 или MD-UV390.")
    if type(report.get("dmr_id")) is not int or not 0 <= report["dmr_id"] <= 0xffffff:
        raise ValueError("В исходном документе требуется корректный DMR ID.")
    for table in TABLES:
        rows(report, table)
    return models.pop()


def _check_new_slot(image, table, idx):
    base, size, _ = TABLES[table]
    name_offset = {"channels": 32, "contacts": 4}.get(table, 0)
    image.seek(base + (idx - 1) * size + name_offset)
    if image.read(2) not in (b"\0\0", b"\xff\xff"):
        raise ValueError(f"Не удалось распознать занятую позицию {table} №{idx}.")


def _invalid_group_members(tables):
    """TYT CPS receive groups contain group-call contacts only."""
    contacts = tables["contacts"]
    return {(idx, member) for idx, row in tables["rx_groups"].items() for member in row["contacts"]
            if contacts.get(member, {}).get("type") != "group"}


def plan_settings(report, baseline):
    source_model = _source_model(report)
    model, binding, before = baseline
    if source_model != model:
        raise ValueError(f"Модель кодплага {source_model} отличается от подключённой рации {model}.")
    image = _make_image(before)
    original = _decode_image(image)
    if original["dmr_id"] != report["dmr_id"]:
        raise ValueError("DMR ID отличается. Сектор с DMR ID доступен только для чтения.")
    unavailable = report.get("unrecognized_rx_group_slots", [])
    if not isinstance(unavailable, list) or any(type(i) is not int or not 1 <= i <= 250 for i in unavailable):
        raise ValueError("Некорректный список непрочитанных групп приёма.")
    protected_groups = set(unavailable) | set(original.get("unrecognized_rx_group_slots", []))
    original_tables = indexed(original)
    targets = {k: dict(v) for k, v in original_tables.items()}
    checks = []
    for table in TABLES:
        for idx, incoming in rows(report, table).items():
            old = original_tables[table].get(idx)
            if table == "rx_groups" and idx in protected_groups:
                raise ValueError(f"Нельзя изменить непрочитанную группу приёма №{idx}.")
            edited = project_row(table, incoming, old or {}, original["dmr_id"])
            if table == "channels" and "details" in incoming:
                details = incoming["details"]
                if not isinstance(details, list) or any(
                        not isinstance(f, dict) or not isinstance(f.get("label"), str) or
                        not isinstance(f.get("value"), str) for f in details):
                    raise ValueError(f"Некорректные дополнительные поля в канале №{idx}.")
                if details:
                    edited["details"] = details
            if table == "rx_groups" and idx <= 53 and (
                    old is None or any(not same(normalize(edited, k), normalize(old, k)) for k in CORE[table])):
                raise ValueError(f"Группа приёма №{idx} расположена в секторе только для чтения (позиции 1–53).")
            targets[table][idx] = edited
            if same(old, edited):
                continue
            if old is None:
                _check_new_slot(image, table, idx)
            elif "\ufffd" in old["name"] or not old["name"].isprintable():
                raise ValueError(f"Не удалось распознать занятую позицию {table} №{idx}.")
            expected = patch_row(image, table, idx, old, edited, old)
            if expected:
                checks.append((table, idx, expected))
    validate_tables(original_tables, targets)
    invalid = _invalid_group_members(targets) - _invalid_group_members(original_tables)
    if invalid:
        group, member = min(invalid)
        raise ValueError(f"В группе приёма №{group} контакт №{member} должен быть групповым.")
    # Check the result decoded from the actual new bytes, including mode-dependent
    # record fields and the separately stored A/B zone extensions.
    decoded = indexed(_decode_image(image))
    for table, idx, changed in checks:
        actual = decoded[table].get(idx)
        if actual is None or any(not same(normalize(actual, k), v) for k, v in changed.items()):
            raise ValueError(f"Не прошла проверка записи {table} №{idx}.")
    after = {}
    for addr in ADDRESSES:
        image.seek(_file_offset(addr))
        after[addr] = image.read(BLOCK)
    return proposal_from_blocks(model, binding, before, after)


def prepare(report, *, helper=None):
    _source_model(report)
    return plan_settings(report, read_baseline(helper))


def _check_write_result(result, sectors, backup):
    """A successful exit alone cannot acknowledge a verified flash write."""
    lines = result.stderr.decode("utf-8", "replace").splitlines()
    markers = [line for line in lines if line.startswith("WRITE_")]
    expected = ["WRITE_PREFLIGHT"]
    for sector in sectors:
        expected.extend((f"WRITE_START 0x{sector:06x}", f"WRITE_VERIFIED 0x{sector:06x}"))
    expected.append(f"WRITE_COMPLETE {len(sectors)}")
    if markers == expected and result.returncode == 0:
        return
    if markers == ["WRITE_PREFLIGHT"] and result.returncode == 1:
        detail = next((line.strip().rstrip(".") for line in reversed(lines) if line and not line.startswith("WRITE_")), "USB")
        raise ValueError(f"Проверка остановила запись до стирания. Сообщение USB: {detail[:300]}. Резервная копия: {backup}")
    if len(markers) > 1 and markers == expected[:len(markers)]:
        verified = min((len(markers) - 1) // 2, len(sectors))
        if verified == len(sectors):
            raise ValueError(f"Все секторы ({verified}) сверены. Завершение сеанса не подтверждено. Резервная копия: {backup}")
        if markers[-1].startswith("WRITE_START"):
            addr = f"0x{sectors[verified]:06X}"
            raise ValueError(f"Сектор {addr} мог записаться частично. Сверено секторов: {verified} из {len(sectors)}. Резервная копия: {backup}")
        raise ValueError(f"Сверено секторов: {verified} из {len(sectors)}. Запись остановилась между секторами. Резервная копия: {backup}")
    raise ValueError(f"Подтверждение записи отсутствует или повреждено. Состояние рации неизвестно. Резервная копия: {backup}")


def apply(proposal, *, backup, helper=None):
    from write_backup import save_backup

    encoded = proposal.get("plan") if isinstance(proposal, dict) else None
    if not isinstance(encoded, str) or len(encoded) != ((PLAN_LENGTH + 2) // 3) * 4:
        raise ValueError("Некорректный план записи кодплага.")
    raw = base64.b64decode(encoded, validate=True)
    model, binding, before, after = decode_plan(raw)
    blocks, sectors, changes = _changes(before, after)
    summary = {"model": model, "blocks": blocks, "sectors": sectors, "changes": changes,
               "dmr_id": int.from_bytes(before[0x2000][0x84:0x87], "little")}
    if not all(same(proposal.get(key), value) for key, value in summary.items()):
        raise ValueError("Предпросмотр кодплага не соответствует текущей операции.")
    if not blocks:
        raise ValueError("Все выбранные настройки уже совпадают.")
    saved = save_backup(backup, model, {addr: (before[addr], after[addr]) for addr in ADDRESSES})
    affected = sorted({addr // SECTOR * SECTOR for addr in ADDRESSES if before[addr] != after[addr]})
    try:
        result = _run_helper("--write-settings", helper=helper, data=raw)
    except SleepInhibitError as exc:
        raise ValueError("Служба logind не разрешила блокировку режима сна. Откройте приложение в локальном сеансе Ubuntu.\n"
                         f"Компонент записи не запустился. Резервная копия: {saved}") from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise ValueError(f"Компонент записи не запустился. Резервная копия: {saved}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Состояние рации после сбоя связи неизвестно. Резервная копия: {saved}") from exc
    _check_write_result(result, affected, saved)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("prepare", "restore-preview", "apply"):
        p = sub.add_parser(mode)
        p.add_argument("--reader", type=Path)
        if mode != "prepare":
            p.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "restore-preview":
            from write_backup import prepare_restore
            result = prepare_restore(args.backup, helper=args.reader)
        else:
            limit = 128 * 1024 * 1024 if args.mode == "prepare" else 3 * 1024 * 1024
            data = sys.stdin.buffer.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Входные данные превышают допустимый размер кодплага.")
            obj = json.loads(data)
            if args.mode == "apply":
                apply(obj, backup=args.backup, helper=args.reader)
                return 0
            result = prepare(obj, helper=args.reader)
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return 0
    except CLI_ERRORS as exc:
        print(str(exc)[:2048], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
