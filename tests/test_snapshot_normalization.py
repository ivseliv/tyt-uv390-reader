import copy
import io
import unittest

from cps_section_editor import patch_sections, verify_sections
from cps_sections import rdt_sections
from import_qdmr import decode_qdmr
from normalize_snapshot import normalize


class SnapshotTests(unittest.TestCase):
    def test_derived_names_and_ids(self):
        src = decode_qdmr({
            "radioIDs": [{"dmr": {"id": "id1", "name": "Radio", "number": "41"}}],
            "contacts": [{"dmr": {"id": "c1", "name": "TG9", "type": "GroupCall", "number": "9"}},
                         {"dtmf": {"id": "c2", "name": "Local", "number": "001*"}}],
            "channels": [{"dmr": {"id": "ch1", "name": "Home", "rxFrequency": "433.125"}}],
            "scanLists": [{"name": "Scan", "channels": ["ch1"], "tyt": {"holdTime": "500"}}],
            "zones": [{"id": "z", "name": "Zone", "A": ["ch1"]}],
            "positioning": [{"dmr": {"name": "GPS", "contact": "c1", "revert": "ch1"}}],
        })
        edit = copy.deepcopy(src)
        edit["dmr_id"] = 55
        edit["channels"][0]["name"] = "Channel"
        edit["scan_lists"][0]["name"] = "Scan2"
        edit["contacts"][0]["name"] = "TG"
        edit["contacts"][1]["dtmf_number"] = "A21#"
        result = normalize(src, edit)
        sections = {s["id"]: s["rows"] for s in result["cps_sections"]}
        self.assertEqual("55", sections["general"][0]["fields"][1]["value"])
        self.assertEqual("A21#", sections["dtmf"][0]["fields"][1]["value"])
        self.assertEqual(["GPS", "№ 1 · TG", "№ 1 · Channel"], [f["value"] for f in sections["gps"][0]["fields"]])
        self.assertEqual("№ 1 · Scan2", sections["scan_timing"][0]["name"])
        self.assertEqual("Channel", result["zones"][0]["A"][0]["name"])

        edit = copy.deepcopy(src)
        general = next(s for s in edit["cps_sections"] if s["id"] == "general")
        general["rows"][0]["fields"][1]["value"] = "32"
        self.assertEqual(32, normalize(src, edit)["dmr_id"])

    def test_newly_dangling_reference_rejected(self):
        before = {"channels": [{"index": 2, "name": "A"}], "contacts": [],
                  "zones": [{"name": "Z", "index": 1, "A": [{"index": 2}, {"index": 99}], "B": []}]}
        self.assertEqual("A", normalize(before, copy.deepcopy(before))["zones"][0]["A"][0]["name"])
        after = copy.deepcopy(before)
        after["channels"] = []
        with self.assertRaisesRegex(ValueError, "№ 2"):
            normalize(before, after)

    def test_local_tyt_slot_references_and_partial_snapshot(self):
        def section(key, title, **values):
            return {"id": key, "title": key, "rows": [{"name": title, "fields": [
                {"label": key, "value": value} for key, value in values.items()]}]}
        src = {"usb_model": "MD-UV390", "contacts": [{"index": 7, "name": "A", "type": "private"}],
               "channels": [{"index": 3, "name": "UHF", "mode": "DMR"}],
               "scan_lists": [{"index": 2, "name": "S", "channels": []}],
               "rx_groups": [{"index": 5, "name": "G", "contacts": []}],
               "cps_sections": [
                   section("vfo", "VFO A", **{"Режим": "DMR", "Контакт": "№ 7 · A",
                       "Группа приёма": "№ 5 · G", "Список сканирования": "№ 2 · S"}),
                   section("gps", "GPS 1", **{"Канал": "№ 3 · UHF", "Контакт": "№ 7 · A"}),
                   section("buttons", "Цифровая клавиша · позиция 10", Контакт="№ 7 · A")]}
        edit = copy.deepcopy(src)
        for key in ("contacts", "channels", "scan_lists", "rx_groups"):
            edit[key][0]["name"] += "2"
        edit["cps_sections"][1]["rows"][0]["fields"][1]["value"] = "7"
        result = normalize(src, edit)
        values = [[f["value"] for f in s["rows"][0]["fields"]] for s in result["cps_sections"]]
        self.assertEqual([["DMR", "№ 7 · A2", "№ 5 · G2", "№ 2 · S2"], ["№ 3 · UHF2", "№ 7 · A2"],
                          ["№ 7 · A2"]], values)
        for key in ("rx_groups", "scan_lists"):
            edit = copy.deepcopy(src)
            edit[key].clear()
            with self.assertRaisesRegex(ValueError, "VFO A"):
                normalize(src, edit)
        edit = copy.deepcopy(src)
        edit["cps_sections"][1]["rows"][0]["fields"][0]["value"] = "Нет данных"
        with self.assertRaisesRegex(ValueError, "GPS 1"):
            normalize(src, edit)
        old_usb = {key: value for key, value in src.items() if key != "cps_sections"}
        edit = copy.deepcopy(old_usb)
        edit["cps_sections"] = [copy.deepcopy(src["cps_sections"][2])]
        edit["local_empty_slots"] = {"numberKeys": [2]}
        self.assertEqual(edit, normalize(old_usb, copy.deepcopy(edit)))
        self.assertNotIn("cps_sections", normalize(old_usb, copy.deepcopy(old_usb)))

    def test_deleting_local_templates_and_profiles_keeps_live_references(self):
        def row(name, field, value):
            return {"name": name, "fields": [{"label": field, "value": value}]}
        src = {"file_model": "MD-UV390", "channels": [{"index": 1, "name": "Дом", "mode": "DMR",
                       "details": [{"label": "Система GPS", "value": "4"}]}],
               "cps_sections": [{"id": "messages", "title": "SMS", "rows": [row("Шаблон 2", "Текст", "Тест")]},
                                {"id": "gps", "title": "GPS", "rows": [row("GPS 4", "Канал", "Выбранный канал")]},
                                {"id": "buttons", "title": "Кнопки", "rows": [row("Быстрый доступ 5", "Шаблон", "2")]}]}
        for position, message in ((0, "шаблон № 2"), (1, "Система GPS № 4")):
            edit = copy.deepcopy(src)
            del edit["cps_sections"][position]
            with self.assertRaisesRegex(ValueError, message):
                normalize(src, edit)

    def test_sparse_sms_creation_deletion(self):
        file = io.BytesIO(bytes(0x40235))
        report = {"channels": [], "contacts": [], "scan_lists": []}
        def read_at(offset, size):
            file.seek(offset)
            return file.read(size)
        def actual():
            return {**report, "cps_sections": rdt_sections(read_at, report)}
        before = actual()
        edited = copy.deepcopy(before)
        edited["cps_sections"].append({"id": "messages", "title": "Шаблоны сообщений", "rows": [
            {"name": "Шаблон 50", "fields": [{"label": "Текст", "value": "Последняя ячейка"}]}]})
        patch_sections(file, before, edited)
        parsed = actual()
        verify_sections(before, edited, parsed)
        patch_sections(file, parsed, before)
        self.assertEqual(before, actual())
        verify_sections(parsed, before, actual())


if __name__ == "__main__":
    unittest.main()
