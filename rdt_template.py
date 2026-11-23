"""Transfer the five core tables of a local snapshot into a new TYT RDT copy."""

import argparse
import json
import sys

from cps_dtmf import merge_sequence_edits

from import_rdt import import_rdt
from rdt_editor import ANALOG, DIGITAL, FIELDS, PRIVACY_CODES, TABLES, export_rdt, normalize, rows, same


CORE = {table: fields - {"details", "cps_csv"} for table, fields in FIELDS.items()}
REQUIRED = {"channels": {"mode", "rx_hz", "tx_hz"}, "contacts": {"type", "dmr_id"},
            "zones": {"A", "B"}, "rx_groups": {"contacts"}, "scan_lists": {"channels"}}
DIGITAL_DEFAULTS = {"color_code": 1, "time_slot": 1, "contact_index": None, "rx_group_index": None,
                    "privacy_type_code": 0, "privacy_index_code": 0}
ANALOG_DEFAULTS = {"rx_tone_code": 0xFFFF, "tx_tone_code": 0xFFFF}


def project_row(table, incoming, old, radio_id):
    label = f"{table} №{incoming['index']}"
    missing = (REQUIRED[table] | {"name"}) - incoming.keys()
    if missing:
        raise ValueError(f"{label}: отсутствуют поля {', '.join(sorted(missing))}.")
    core = {key: incoming[key] for key in CORE[table] & incoming.keys()}
    result = dict(old, index=incoming["index"])
    handled = {"index", "details", "cps_csv", "rdt_template_index"}
    if table == "channels":
        mode = core["mode"]
        if mode not in ("analog", "DMR"):
            raise ValueError(f"{label}: режим {mode} несовместим с RDT MD-UV380/390.")
        handled.add("radio_id")
        if incoming.get("radio_id") is not None and (mode != "DMR" or not same(incoming["radio_id"], radio_id)):
            raise ValueError(f"{label}: индивидуальный radio_id отличается от основного DMR ID шаблона или снимка.")
        inactive = DIGITAL | PRIVACY_CODES if mode == "analog" else ANALOG
        for key in inactive:
            if core.pop(key, None) is not None:
                raise ValueError(f"{label}: поле {key} несовместимо с режимом {mode}.")
            result.pop(key, None)
        if mode != old.get("mode"):
            result.pop("details", None)
            # Previously inactive bytes can contain unrelated values after a mode change.
            result.update(DIGITAL_DEFAULTS if mode == "DMR" else ANALOG_DEFAULTS)
    elif table == "contacts":
        if core["type"] not in ("group", "private", "all"):
            raise ValueError(f"{label}: тип {core['type']} несовместим с цифровой таблицей контактов TYT.")
        handled |= {"call_receive_tone", "dtmf_number", "callsign", "broadcast"}
        if any(incoming.get(k) is not None for k in ("dtmf_number", "callsign", "broadcast")):
            raise ValueError(f"{label}: у цифрового контакта указаны дополнительные параметры DTMF или M17.")
        if "call_receive_tone" in incoming:
            code = incoming["call_receive_tone"]
            if type(code) is not int or code not in (0, 1) or "ring" in core and not same(core["ring"], bool(code)):
                raise ValueError(f"{label}: противоречивые настройки звонка ring и call_receive_tone.")
            core["ring"] = bool(code)
    elif table == "scan_lists" and isinstance(core["channels"], list) and 0 in core["channels"]:
        raise ValueError(f"{label}: выбранный канал (!selected) допустим в primary/secondary/revert, "
                         "но для участников списка TYT нужны конкретные каналы.")
    unknown = incoming.keys() - CORE[table] - handled
    if unknown:
        raise ValueError(f"{label}: не поддерживается перенос полей {', '.join(sorted(unknown))}.")
    result.update(core)
    return result


def transfer_rdt(template, output, snapshot):
    if not isinstance(snapshot, dict) or not (TABLES.keys() | {"dmr_id", "cps_sections"}) & snapshot.keys():
        raise ValueError("Ожидается JSON-снимок TYT Reader с основными таблицами или DMR ID.")
    if "csv_source" in snapshot:
        raise ValueError("Это отдельная таблица CSV. Импортируйте её в открытый черновик RDT.")
    baseline = import_rdt(template)
    desired = dict(baseline)
    sequences_only = snapshot.get("source_format") == "dtmf"
    sequences = [s for s in snapshot.get("cps_sections", []) if isinstance(s, dict) and s.get("id") == "dtmf_sequences"]
    if len(sequences) > 1 or sequences_only and not sequences:
        raise ValueError("Ожидается один раздел последовательностей DTMF.")
    if sequences:
        desired["cps_sections"] = [merge_sequence_edits(s, sequences[0]) if s["id"] == "dtmf_sequences" else s
                                   for s in baseline["cps_sections"]]
    if not sequences_only and snapshot.get("dmr_id") is not None:
        desired["dmr_id"] = snapshot["dmr_id"]
    for table in (() if sequences_only else TABLES.keys() & snapshot.keys()):
        native = rows(baseline, table)
        desired[table] = [project_row(table, row, native.get(idx, {}), desired["dmr_id"])
                          for idx, row in rows(snapshot, table).items()]
        if table == "rx_groups":
            missing = snapshot.get("unrecognized_rx_group_slots", [])
            if not isinstance(missing, list) or any(type(i) is not int or not 1 <= i <= 250 for i in missing):
                raise ValueError("Некорректные номера непрочитанных групп приёма.")
            present = {row["index"] for row in desired[table]}
            desired[table].extend(native[i] for i in sorted(set(missing) - present) if i in native)

    def verify(actual):
        if not same(desired["dmr_id"], actual["dmr_id"]):
            raise ValueError("При проверке копии не совпал DMR ID.")
        for table in TABLES:
            wanted, written = rows(desired, table), rows(actual, table)
            if wanted.keys() != written.keys():
                raise ValueError(f"При проверке копии не совпал состав таблицы {table}.")
            for idx, row in wanted.items():
                for key in CORE[table] & row.keys():
                    if not same(normalize(row, key), normalize(written[idx], key)):
                        raise ValueError(f"При проверке копии не совпало поле {table} №{idx}, {key}.")

    export_rdt(template, output, baseline, desired, verify=verify)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", metavar="TEMPLATE.rdt")
    parser.add_argument("output", metavar="OUTPUT.rdt")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(128 * 1024 * 1024 + 1)
        if len(raw) > 128 * 1024 * 1024:
            raise ValueError("Снимок превышает 128 МБ.")
        transfer_rdt(args.template, args.output, json.loads(raw))
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        print(f"Не удалось перенести снимок в RDT. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
