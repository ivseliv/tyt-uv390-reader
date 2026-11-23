"""Validate a local report edit and refresh its displayed references. JSON stdin → stdout."""

import json
import re
import sys

from cps_dtmf import validate_encode_references, validate_sequence_edit


TABLES = ("channels", "contacts", "zones", "rx_groups", "scan_lists")


def integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field}: требуется целое число от {low} до {high}.")
    return value


def indexed(report):
    tables = {}
    for table in TABLES:
        rows = report.get(table, [])
        if not isinstance(rows, list):
            raise ValueError(f"Таблица {table} должна содержать список записей.")
        table_index = {}
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError(f"Недопустимая строка в {table}.")
            i = integer(item.get("index"), 1, 2**31 - 1, table)
            if i in table_index:
                raise ValueError(f"В {table} повторяется № {i}.")
            table_index[i] = item
        tables[table] = table_index
    return tables


def named_sections(report):
    values = report.get("cps_sections", [])
    sections = {}
    if not isinstance(values, list):
        raise ValueError("Некорректный список разделов CPS.")
    for section in values:
        if (not isinstance(section, dict) or not isinstance(section.get("id"), str)
                or section["id"] in sections or not isinstance(section.get("rows"), list)):
            raise ValueError("Некорректный или повторный раздел CPS.")
        sections[section["id"]] = section["rows"]
        for row in section["rows"]:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("fields"), list)):
                raise ValueError("Некорректная строка раздела CPS.")
            for field in row["fields"]:
                if (not isinstance(field, dict) or not isinstance(field.get("label"), str)
                        or not isinstance(field.get("value"), str)):
                    raise ValueError(f"Некорректное поле в «{row['name']}».")
    return sections


def validate_tables(old, new):
    def link(v, before, table, target, field, allowed=(), accept=None):
        if v in allowed and type(v) is not bool:
            return
        existing = old[target].get(v) if type(v) is int else None
        current = new[target].get(v) if type(v) is int else None
        if current is not None and (accept is None or current.get("type") in accept):
            return
        # Keep an already unresolved reference as long as this edit did not cause it.
        if v in before and (existing is None or accept is not None and existing.get("type") not in accept):
            return
        raise ValueError(f"{table}, {field}: нет подходящей записи № {v} в {target}.")

    for table, rows in new.items():
        for idx, item in rows.items():
            prev = old[table].get(idx, {})
            label = f"{table} № {idx}"
            if not prev or item.get("name") != prev.get("name"):
                if not isinstance(item.get("name"), str) or not item["name"].strip():
                    raise ValueError(f"{label}: укажите имя.")
            if table == "channels":
                if item.get("mode") != prev.get("mode") and item.get("mode") not in ("analog", "DMR", "AM", "M17"):
                    raise ValueError(f"{label}: неизвестный режим.")
                for field, low, high in (("rx_hz", 1, 2**63 - 1), ("tx_hz", 1, 2**63 - 1),
                                         ("bandwidth_code", 0, 2), ("color_code", 0, 15), ("time_slot", 1, 2),
                                         ("radio_id", 0, 0xFFFFFF), ("rx_tone_code", 0, 65535), ("tx_tone_code", 0, 65535)):
                    value = item.get(field)
                    if value is not None and (value != prev.get(field) or type(value) != type(prev.get(field))):
                        integer(value, low, high, field)
                contact_types = {"m17"} if item.get("mode") == "M17" else {"group", "all", "private"}
                for field, target, accept in (("contact_index", "contacts", contact_types),
                                              ("scan_list_index", "scan_lists", None),
                                              ("rx_group_index", "rx_groups", None)):
                    link(item.get(field), [prev.get(field)], label, target, field, (None,), accept)
            elif table == "contacts":
                if item.get("type") != prev.get("type") and item.get("type") not in ("group", "private", "all", "dtmf", "m17"):
                    raise ValueError(f"{label}: неизвестный тип контакта.")
                if item.get("type") in ("all", "group", "private") and (
                    not prev or item.get("dmr_id") != prev.get("dmr_id") or item.get("type") != prev.get("type")
                ):
                    integer(item.get("dmr_id"), 0, 0xFFFFFF, label)
                if item.get("type") == "dtmf" and (not prev or item.get("dtmf_number") != prev.get("dtmf_number")):
                    if not isinstance(item.get("dtmf_number"), str) or not re.fullmatch(r"[0-9A-Da-d*#]+", item["dtmf_number"]):
                        raise ValueError(f"{label}: некорректный номер DTMF.")
            else:
                for field in {"zones": ("A", "B"), "rx_groups": ("contacts",), "scan_lists": ("channels",)}[table]:
                    values, original = item.get(field, []), prev.get(field, [])
                    if not isinstance(values, list):
                        raise ValueError(f"{label}: {field} должно содержать список.")
                    target = "contacts" if table == "rx_groups" else "channels"
                    if table == "zones":
                        if not all(isinstance(v, dict) and "index" in v for v in values):
                            raise ValueError(f"{label}: недопустимый список каналов.")
                        indices = [v["index"] for v in values]
                        original = [v["index"] for v in original]
                    else:
                        indices = values
                    for v in indices:
                        link(v, original, label, target, field, (0,) if table == "scan_lists" else (),
                             {"group", "private", "all"} if table == "rx_groups" else None)
                    if table == "zones":
                        for member in values:
                            if type(member["index"]) is int and member["index"] in new["channels"]:
                                member["name"] = new["channels"][member["index"]]["name"]
                if table == "scan_lists":
                    for field in ("primary", "secondary", "revert"):
                        link(item.get(field), [prev.get(field)], label, "channels", field, (None, 0))


def fields(row):
    return {field["label"]: field for field in row["fields"]}


def general_ids(rows, dmr_id):
    candidates = []
    main = []
    for row in rows:
        labels = fields(row)
        if row["name"] == "Рация" and "DMR ID" in labels:
            main.append(labels["DMR ID"])
        elif row["name"].startswith("Идентификатор DMR") and "DMR ID" in labels:
            entry = labels["DMR ID"]
            if row["name"] == "Идентификатор DMR по умолчанию":
                main.append(entry)
            elif entry["value"] == str(dmr_id):
                candidates.append(entry)
    return main or (candidates if len(candidates) == 1 else [])


def sync_dmr(baseline, edited, old_sections, new_sections):
    old_id, new_id = baseline.get("dmr_id"), edited.get("dmr_id")
    if old_id != new_id and new_id is not None:
        integer(new_id, 0, 0xFFFFFF, "DMR ID")
    old_rows = old_sections.get("general", [])
    new_rows = new_sections.get("general", [])
    old_aliases = general_ids(old_rows, old_id)
    alias_ids = {id(x) for x in old_aliases}
    for i, old_row in enumerate(old_rows):
        if i >= len(new_rows) or old_row["name"] != new_rows[i]["name"]:
            continue
        old_field = fields(old_row).get("DMR ID")
        if not old_field or id(old_field) not in alias_ids:
            continue
        new_field = fields(new_rows[i]).get("DMR ID")
        if not new_field:
            continue
        if new_field["value"] != old_field["value"]:
            if not re.fullmatch(r"[0-9]{1,8}", new_field["value"]):
                raise ValueError("В разделе общих настроек указан некорректный DMR ID.")
            explicit = integer(int(new_field["value"]), 0, 0xFFFFFF, "DMR ID")
            if new_id != old_id and explicit != new_id:
                raise ValueError("DMR ID в основных и дополнительных настройках различается.")
            edited["dmr_id"] = new_id = explicit
        if new_id != old_id:
            new_field["value"] = "" if new_id is None else str(new_id)


def refresh_cps_refs(old, new, old_sections, new_sections, native):
    targets = {"Канал": "channels", "Контакт": "contacts", "Список сканирования": "scan_lists",
               "Группа приёма": "rx_groups"}
    def row_key(section, name):
        return name.partition(" · ")[0] if section == "emergency" else name

    for section in ("buttons", "gps", "emergency", "vfo"):
        old_rows = {row_key(section, r["name"]): fields(r) for r in old_sections.get(section, [])}
        for row in new_sections.get(section, []):
            old_fields = old_rows.get(row_key(section, row["name"]), {})
            for field in row["fields"]:
                label, value = field["label"], field["value"]
                target = targets.get(label)
                if not target:
                    continue
                changed = old_fields.get(label, {}).get("value") != value
                number_prefix = r"(?:№\s*)?" if native else r"№\s*"
                match = re.fullmatch(number_prefix + r"([0-9]+)(?: · .*)?", value.strip())
                if not match:
                    special = (section == "vfo" and value == "Нет" or label == "Канал" and
                               (value == "Выбранный канал" or section == "gps" and value == "Не задан"))
                    if native and changed and not special:
                        raise ValueError(f"«{row['name']}», {label}: укажите номер записи.")
                    continue
                idx = int(match[1])
                current = new[target].get(idx)
                if current is None:
                    if idx in old[target] or changed:
                        raise ValueError(f"«{row['name']}», {label}: отсутствует № {idx}.")
                    continue
                required = None
                if native and label == "Контакт":
                    required = ("type", {"group", "private", "all"})
                elif native and section == "gps" and label == "Канал":
                    required = ("mode", {"DMR"})
                if required and current.get(required[0]) not in required[1]:
                    previous = old[target].get(idx, {})
                    if changed or previous.get(required[0]) in required[1]:
                        raise ValueError(f"«{row['name']}», {label}: № {idx} несовместим с цифровым режимом.")
                field["value"] = f"№ {idx} · {current['name']}"

    timing = new_sections.get("scan_timing", [])
    for row in timing[:]:
        match = re.fullmatch(r"№ (\d+) · .*", row["name"])
        if match:
            idx = int(match[1])
            if idx in new["scan_lists"]:
                row["name"] = f"№ {idx} · {new['scan_lists'][idx]['name']}"
            elif idx in old["scan_lists"]:
                timing.remove(row)

    if native:
        def slots(groups, kind, pattern):
            return {int(match[1]) for row in groups.get(kind, [])
                    if (match := re.fullmatch(pattern, row["name"]))}

        old_sms = slots(old_sections, "messages", r"Шаблон ([0-9]+)")
        new_sms = slots(new_sections, "messages", r"Шаблон ([0-9]+)")
        previous = {r["name"]: fields(r) for r in old_sections.get("buttons", [])}
        for row in new_sections.get("buttons", []):
            template = fields(row).get("Шаблон")
            if template:
                value = template["value"]
                changed = previous.get(row["name"], {}).get("Шаблон", {}).get("value") != value
                idx = int(value) if re.fullmatch("[0-9]{1,2}", value) else None
                if idx not in new_sms and (idx in old_sms or changed):
                    raise ValueError(f"«{row['name']}»: отсутствует шаблон № {value}.")
        for kind, pattern, label in (("gps", r"GPS ([0-9]+)", "Система GPS"),
                                     ("emergency", r"№ ([0-9]+) · .+", "Система тревоги")):
            removed = slots(old_sections, kind, pattern) - slots(new_sections, kind, pattern)
            if removed:
                numbers = {str(i) for i in removed}
                for ch in new["channels"].values():
                    for field in ch.get("details", []):
                        if field["label"] == label and field["value"] in numbers:
                            raise ValueError(f"{label} № {field['value']} используется каналом № {ch['index']}.")


def refresh_dtmf(old, new, old_sections, new_sections, edited):
    original = old_sections.get("dtmf", [])
    rows = new_sections.get("dtmf", [])
    before = [r for r in old["contacts"].values() if r.get("type") == "dtmf"]
    old_rows = [r for r in original if r["name"] == "Контакт DTMF"]
    new_rows = [r for r in rows if r["name"] == "Контакт DTMF"]
    # Binary CPS tables expose one-touch assignments. qdmr mirrors its DTMF contacts here.
    if not old_rows and not new_rows:
        if edited.get("source_format") != "qdmr" or not any(c.get("type") == "dtmf" for c in new["contacts"].values()):
            return
        if "dtmf" not in new_sections:
            edited.setdefault("cps_sections", []).append({"id": "dtmf", "title": "DTMF", "rows": rows})
    if len(before) != len(old_rows) or len(new_rows) != len(old_rows):
        raise ValueError("Меняйте состав контактов DTMF в основной таблице контактов.")
    for previous, old_row, new_row in zip(before, old_rows, new_rows):
        contact = new["contacts"].get(previous["index"])
        if contact is None or contact.get("type") != "dtmf":
            continue
        for label, key in (("Имя", "name"), ("Номер", "dtmf_number")):
            first, second = fields(old_row).get(label), fields(new_row).get(label)
            if first and second and first["value"] != second["value"]:
                if contact.get(key) != previous.get(key) and contact[key] != second["value"]:
                    raise ValueError(f"Контакт DTMF: «{label}» изменено по-разному в двух разделах.")
                contact[key] = second["value"]
    rows[:] = [r for r in rows if r["name"] != "Контакт DTMF"]
    rows.extend({"name": "Контакт DTMF", "fields": [
        {"label": "Имя", "value": contact["name"]}, {"label": "Номер", "value": contact["dtmf_number"]}]}
        for contact in new["contacts"].values() if contact.get("type") == "dtmf")


def normalize(baseline, edited):
    if not isinstance(baseline, dict) or not isinstance(edited, dict):
        raise ValueError("Ожидаются исходный и изменённый отчёты.")
    old, new = indexed(baseline), indexed(edited)
    old_sections, new_sections = named_sections(baseline), named_sections(edited)
    for i, section in enumerate(edited.get("cps_sections", [])):
        if section["id"] == "dtmf_sequences":
            before = next((s for s in baseline.get("cps_sections", []) if s["id"] == "dtmf_sequences"), None)
            edited["cps_sections"][i] = validate_sequence_edit(before, section)
    validate_encode_references(baseline, edited)
    sync_dmr(baseline, edited, old_sections, new_sections)
    refresh_dtmf(old, new, old_sections, new_sections, edited)
    validate_tables(old, new)
    model = baseline.get("file_model") or baseline.get("usb_model")
    refresh_cps_refs(old, new, old_sections, new_sections, model in ("MD-UV380", "MD-UV390"))
    return edited


def main():
    try:
        document = json.load(sys.stdin)
        result = normalize(document["baseline"], document["edited"])
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (KeyError, ValueError, TypeError, RecursionError) as error:
        print(str(error)[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
