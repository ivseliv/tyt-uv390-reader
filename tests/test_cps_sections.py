import copy
import io
import struct
import unittest

from cps_section_editor import patch_sections
from cps_sections import rdt_sections
from import_qdmr import decode_qdmr


class CPSTests(unittest.TestCase):
    def setUp(self):
        self.data = bytearray(0x40000 + 0x225)
        self.report = {"dmr_id": 0, "scan_lists": [{"index": 3, "name": "Скан"}],
                       "rx_groups": [{"index": 11, "name": "Приём"}],
                       "channels": [{"index": 17, "name": "Канал"}], "contacts": [{"index": 15, "name": "TG"}]}
        self.set(0x2040, "Старт".encode("utf-16-le"))
        self.set(0x20F0, bytes([7, 5, 0, 0, 8, 0, 0]))
        self.set(0x2102, bytes([14, 31, 2, 3]))
        self.set(0x2111, b"\x04")
        self.set(0x2114, bytes([0xE9, 0, 0, 0]))
        self.set(0x2180 + 2 * 0x120, "Привет".encode("utf-16-le"))
        self.set(0x5A80 + 0x28, "Тест".encode("utf-16-le"))
        self.set(0x5A80 + 0x28 + 32, struct.pack("<BBBBH", 0x12, 3, 4, 5, 17))
        self.set(0x2EF40, bytes([2, 0xA8]))
        self.set(0x2EF50, bytes.fromhex("0067504300675043"))
        self.set(0x3EC40 + 16, struct.pack("<HBBH", 17, 2, 255, 15))
        self.set(0x18860 + 2 * 0x68 + 0x27, bytes([20, 8]))
        self.report["cps_sections"] = self.decode(self.data)

    def set(self, address, data):
        self.data[address + 0x225:address + 0x225 + len(data)] = data

    def decode(self, data):
        def read_at(start, size):
            for lo, hi in [(0x2098, 0x20A8), (0x59C0, 0x5A70)]:
                self.assertTrue(start + size <= lo + 0x225 or start >= hi + 0x225)
            return data[start:start + size]
        return rdt_sections(read_at, self.report)

    def lookup(self, report, section, name, label):
        r = next(item for item in report["cps_sections"] if item["id"] == section)
        fields = next(item["fields"] for item in r["rows"] if item["name"] == name)
        return next(item for item in fields if item["label"] == label)

    def test_public_read_and_roundtrip(self):
        edited = copy.deepcopy(self.report)
        edits = [
            ("general", "Заставка", "Строка 1", "Старт", "Новый"),
            ("menu", "Пункты меню", "Оповещение о вызове", "Выкл.", "Вкл."),
            ("buttons", "Боковые кнопки", "Кнопка 1 · короткое нажатие", "Сканирование", "Монитор"),
            ("dtmf", "Быстрый доступ 1", "Профиль", "DTMF-2", "DTMF-4"),
            ("messages", "Шаблон 3", "Текст", "Привет", "База"),
            ("gps", "GPS 2", "Период", "60 с", "90 с"),
            ("emergency", "№ 2 · Тест", "Режим", "Тревога и вызов", "Тревога с голосом"),
            ("emergency", "№ 2 · Тест", "Канал", "№ 17 · Канал", "Выбранный канал"),
            ("lone_worker", "Таймеры", "Время для ответа", "0 мин", "4 мин"),
            ("vfo", "VFO B", "RX", "435 067 000 Гц", "433 125 000 Гц"),
            ("scan_timing", "№ 3 · Скан", "Опрос приоритетного канала", "2000 мс", "2500 мс"),
        ]
        for sec, name, field, old, new in edits:
            value = self.lookup(edited, sec, name, field)
            self.assertEqual(old, value["value"])
            value["value"] = new
        dest = io.BytesIO(self.data)
        patch_sections(dest, self.report, edited)
        encoded = dest.getvalue()
        self.assertEqual(0x07, encoded[0x20F1 + 0x225])
        self.assertEqual(0xEB, encoded[0x2114 + 0x225])
        self.assertEqual(0x22, encoded[0x5A80 + 0x28 + 32 + 0x225])
        self.assertEqual(edited["cps_sections"], self.decode(encoded))
        for start, end in ((0x2098, 0x20A8), (0x59C0, 0x5A70)):
            self.assertEqual(self.data[start + 0x225:end + 0x225], encoded[start + 0x225:end + 0x225])

    def test_one_touch_transmit_sequence_and_legacy_field(self):
        from cps_dtmf import sequence_section, validate_encode_references
        self.set(0x302b0, b"\x01\xf0")  # TX 32, a one-symbol sequence.
        seq = sequence_section(lambda pos, n: self.data[pos:pos+n])
        self.report["cps_sections"].append(seq)
        edited = copy.deepcopy(self.report)
        ref = self.lookup(edited, "dtmf", "Быстрый доступ 1", "Номер Encode")
        ref["value"] = "32"
        validate_encode_references(self.report, edited)
        dest = io.BytesIO(self.data)
        patch_sections(dest, self.report, edited)
        raw = dest.getvalue()
        self.assertEqual(raw[0x2339:0x233d], bytes([0xE9, 32, 0, 0]))
        self.assertEqual(self.lookup({"cps_sections": self.decode(raw)}, "dtmf", "Быстрый доступ 1", "Номер Encode")["value"], "32")
        cleared = copy.deepcopy(edited)
        self.lookup(cleared, "dtmf_sequences", "Передача 32", "Последовательность")["value"] = ""
        with self.assertRaisesRegex(ValueError, "пуста"):
            validate_encode_references(edited, cleared)
        ref["value"] = "4"
        with self.assertRaisesRegex(ValueError, "пуста"):
            validate_encode_references(self.report, edited)
        ref["value"] = "33"
        with self.assertRaisesRegex(ValueError, "от 0 до 32"):
            validate_encode_references(self.report, edited)
        # Original TYT Reader reports carried the DTMF system choice only.
        for report in (self.report, edited):
            sec = next(s for s in report["cps_sections"] if s["id"] == "dtmf")
            sec["rows"][0]["fields"] = [sec["rows"][0]["fields"][0]]
        self.lookup(edited, "dtmf", "Быстрый доступ 1", "Профиль")["value"] = "DTMF-3"
        dest = io.BytesIO(raw)
        patch_sections(dest, self.report, edited)
        self.assertEqual(dest.getvalue()[0x2339:0x233d], bytes([0xEA, 32, 0, 0]))

    def test_create_profiles_convert_vfo_and_move_quick_action(self):
        from cps_sections import row
        from cps_section_editor import verify_sections
        from copy import deepcopy
        edited = deepcopy(self.report)
        table = {s["id"]: s for s in edited["cps_sections"]}
        old_alarm = table["emergency"]["rows"][1]
        new_alarm = deepcopy(old_alarm)
        new_alarm["name"] = "№ 32 · Экипаж"
        table["emergency"]["rows"].append(new_alarm)
        table["gps"]["rows"].append(row("GPS 16", [("Период", "90 с"), ("Канал", "Выбранный канал"),
                                                    ("Контакт", "№ 15 · TG")]))
        table["dtmf"]["rows"] = []
        table["buttons"]["rows"].append(row("Быстрый доступ 1", [("Действие", "Сообщение"), ("Контакт", "№ 15 · TG"),
                                                                 ("Шаблон", "3")]))
        table["buttons"]["rows"].append(row("Цифровая клавиша · позиция 10", [("Контакт", "№ 15 · TG")]))
        vfo = next(r for r in table["vfo"]["rows"] if r["name"] == "VFO B")
        vfo["fields"] = [f for f in vfo["fields"]
                         if f["label"] not in ("Color Code", "Time Slot", "Контакт", "Группа приёма")]
        vfo["fields"][0]["value"] = "Аналог"
        vfo["fields"].extend(row("", [("Полоса", "12,5 кГц"), ("Субтон приёма", "Выкл."),
                                      ("Субтон передачи", "Выкл.")])["fields"])

        dest = io.BytesIO(self.data)
        patch_sections(dest, self.report, edited)
        after = {**self.report, "cps_sections": self.decode(dest.getvalue())}
        verify_sections(self.report, edited, after)
        self.assertEqual(0xD1, dest.getvalue()[0x2114 + 0x225])
        self.assertEqual(0xA8, dest.getvalue()[0x2EF40 + 1 + 0x225])

        back = deepcopy(self.report)
        patch_sections(dest, after, back)
        restored = {**self.report, "cps_sections": self.decode(dest.getvalue())}
        verify_sections(after, back, restored)
        self.assertEqual(self.report["cps_sections"], restored["cps_sections"])

    def test_vfo_references_power_rx_only_tones_and_masks(self):
        from cps_section_editor import verify_sections
        self.set(0x2EF00, bytes([0xE1, 0x91]))
        self.set(0x2EF00 + 24, bytes.fromhex("850823C0"))
        self.set(0x2EF00 + 30, bytes([0xA4]))
        self.report["cps_sections"] = self.decode(self.data)
        edited = copy.deepcopy(self.report)
        changes = [
            ("VFO A", "Полоса", "12,5 кГц", "25 кГц"), ("VFO A", "Субтон приёма", "88,5", "100.0 Гц"),
            ("VFO A", "Субтон передачи", "D023I", "D734N"), ("VFO A", "Мощность", "Low", "High"),
            ("VFO A", "Только приём", "Выкл.", "Вкл."), ("VFO A", "Список сканирования", "Нет", "№ 3"),
            ("VFO B", "Контакт", "Нет", "15"), ("VFO B", "Группа приёма", "Нет", "№ 11 · Приём"),
        ]
        for name, key, old, new in changes:
            field = self.lookup(edited, "vfo", name, key)
            self.assertEqual(field["value"], old)
            field["value"] = new
        staged = io.BytesIO(self.data)
        patch_sections(staged, self.report, edited)
        actual = {**self.report, "cps_sections": self.decode(staged.getvalue())}
        verify_sections(self.report, edited, actual)
        base = 0x2EF00 + 0x225
        data = staged.getvalue()
        self.assertEqual(data[base:base + 2], bytes.fromhex("E993"))
        self.assertEqual(data[base + 24:base + 28], bytes.fromhex("00103487"))
        self.assertEqual(data[base + 30], 0xA7)
        self.assertEqual(data[base + 11], 3)
        self.assertEqual(data[base + 64 + 6:base + 64 + 8], bytes((15, 0)))
        self.assertEqual(data[base + 64 + 12], 11)
        for field, bad in [("Контакт", "№ 1"), ("Группа приёма", "№ 11 · Другое"),
                           ("Список сканирования", "9999")]:
            edit = copy.deepcopy(self.report)
            self.lookup(edit, "vfo", "VFO B", field)["value"] = bad
            original = io.BytesIO(self.data)
            with self.assertRaises(ValueError):
                patch_sections(original, self.report, edit)
            self.assertEqual(original.getvalue(), self.data)

    def test_vfo_reference_rename_and_delete_in_complete_rdt(self):
        import tempfile
        from pathlib import Path
        from import_rdt import MODEL_OFFSET, RDT_SIZE, import_rdt
        from rdt_editor import export_rdt

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            template, linked, renamed = (root / name for name in ("template.rdt", "linked.rdt", "renamed.rdt"))
            raw = bytearray(RDT_SIZE)
            raw[MODEL_OFFSET:MODEL_OFFSET + 8] = b"MD-UV390"
            raw[0x2EF40 + 0x225] = 2
            template.write_bytes(raw)
            before = import_rdt(template)
            edit = copy.deepcopy(before)
            edit["contacts"] = [{"index": 1, "type": "private", "dmr_id": 187, "name": "TX"}]
            edit["rx_groups"] = [{"index": 2, "name": "RX", "contacts": []}]
            edit["scan_lists"] = [{"index": 3, "name": "Скан", "channels": []}]
            assignments = (("Контакт", "contacts"), ("Группа приёма", "rx_groups"),
                           ("Список сканирования", "scan_lists"))
            for field, table in assignments:
                self.lookup(edit, "vfo", "VFO B", field)["value"] = str(edit[table][0]["index"])
            export_rdt(template, linked, before, edit)
            saved = import_rdt(linked)
            edit = copy.deepcopy(saved)
            for field, table in assignments:
                edit[table][0]["name"] = "Новое"
            export_rdt(linked, renamed, saved, edit)
            saved = import_rdt(renamed)
            for field, table in assignments:
                idx = saved[table][0]["index"]
                self.assertEqual(self.lookup(saved, "vfo", "VFO B", field)["value"], f"№ {idx} · Новое")
                losing_target = copy.deepcopy(saved)
                losing_target[table] = []
                destination = root / "broken.rdt"
                with self.assertRaisesRegex(ValueError, "VFO"):
                    export_rdt(renamed, destination, saved, losing_target)
                self.assertFalse(destination.exists())

    def test_profile_in_use_and_duplicate_quick_assignment(self):
        from cps_sections import row
        for sec in ("gps", "buttons"):
            with self.subTest(section=sec):
                edited = copy.deepcopy(self.report)
                groups = {s["id"]: s for s in edited["cps_sections"]}
                if sec == "gps":
                    groups[sec]["rows"] = []
                    edited["channels"][0]["details"] = [{"label":"Система GPS","value":"2"}]
                else:
                    groups[sec]["rows"].append(row("Быстрый доступ 1", [("Действие", "Вызов"),
                                                                       ("Контакт", "№ 15 · TG")]))
                dest = io.BytesIO(self.data)
                with self.assertRaises(ValueError):
                    patch_sections(dest, self.report, edited)
                self.assertEqual(self.data, dest.getvalue())

    def test_invalid_edit_never_commits_partial_patch(self):
        edited = copy.deepcopy(self.report)
        self.lookup(edited, "general", "Заставка", "Строка 1")["value"] = "Утро"
        self.lookup(edited, "scan_timing", "№ 3 · Скан", "Опрос приоритетного канала")["value"] = "17 мс"
        dest = io.BytesIO(self.data)
        with self.assertRaisesRegex(ValueError, "Опрос приоритетного канала"):
            patch_sections(dest, self.report, edited)
        self.assertEqual(self.data, dest.getvalue())

    def test_yaml_uses_public_fields_only(self):
        doc = {"settings": {"boot": {"message1": "Привет", "password": "excluded-1"},
                            "tyt": {"pcProgPassword": "excluded-2", "loneWorkerResponseTime": "7"}},
               "tytExtension": {"menuSettings": {"password": "excluded-3", "scan": "true"}},
               "commercial": {"encryptionKeys": ["excluded-4"]},
               "sms": {"templates": [{"name": "Старт", "message": "Проверка", "unknown": "excluded-5"}]}}
        report = decode_qdmr(doc)
        self.assertNotIn("excluded", str(report))
        self.assertEqual("Проверка", self.lookup(report, "messages", "Шаблон 1", "Текст")["value"])


if __name__ == "__main__":
    unittest.main()
