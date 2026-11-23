import copy
import errno
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from import_rdt import import_rdt
from rdt_editor import export_rdt, publish_new


class RDTEditorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.original = self.root / "исходник.rdt"
        self.output = self.root / "копия.rdt"
        raw = bytearray(852533)
        raw[0x125:0x12D] = b"MD-UV390"
        raw[0x420:0x426] = b"opaque"
        self.original.write_bytes(raw)
        self.baseline = import_rdt(self.original)
        self.new = copy.deepcopy(self.baseline)

    def populate(self):
        self.new.update(
            dmr_id=193,
            channels=[{"index": 2810, "name": "Канал 1", "mode": "DMR", "rx_hz": 433125000,
                       "tx_hz": 433125000, "bandwidth_code": 0, "contact_index": 9817,
                       "time_slot": 2, "color_code": 4, "rx_group_index": 249, "scan_list_index": 247},
                      {"index": 3000, "name": "FM", "mode": "analog", "rx_hz": 145500000,
                       "tx_hz": 145500000, "rx_tone_code": 0x0885, "tx_tone_code": 0x8023}],
            contacts=[{"index": 9817, "name": "TG", "type": "group", "dmr_id": 1711}],
            rx_groups=[{"index": 249, "name": "Приём", "contacts": [9817]}],
            zones=[{"index": 250, "name": "Зона", "A": [{"index": 2810}, {"index": 3000}], "B": [{"index": 3000}]}],
            scan_lists=[{"index": 247, "name": "Поиск", "channels": [3000, 2810], "primary": 0,
                         "secondary": 2810, "revert": None}])
        export_rdt(self.original, self.output, self.baseline, self.new)
        return import_rdt(self.output)

    def test_sparse_create_relations_copy_and_mode(self):
        original = self.original.read_bytes()
        report = self.populate()
        self.assertEqual(self.original.read_bytes(), original)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.read_bytes()[0x420:0x426], b"opaque")
        self.assertEqual(report["zones"][0]["B"], [{"index": 3000, "name": "FM"}])
        self.assertEqual(report["scan_lists"][0]["channels"], [3000, 2810])
        self.assertEqual(report["rx_groups"][0]["contacts"], [9817])
        self.assertEqual(report["channels"][0]["time_slot"], 2)
        self.assertEqual(report["channels"][1]["rx_tone_code"], 0x0885)
        self.assertEqual(report["contacts"][0]["dmr_id"], 1711)
        self.assertEqual(report["dmr_id"], 193)
        edited = copy.deepcopy(report)
        edited["channels"][0].update(mode="analog", rx_tone_code=0xFFFF, tx_tone_code=0xFFFF)
        for key in ("contact_index", "time_slot", "color_code", "rx_group_index", "privacy_type_code", "privacy_index_code", "details"):
            del edited["channels"][0][key]
        dest = self.root / "changed.rdt"
        export_rdt(self.output, dest, report, edited)
        self.assertEqual(import_rdt(dest)["channels"][0]["mode"], "analog")

    def test_noop_and_empty_result(self):
        report = self.populate()
        duplicate = self.root / "identical.rdt"
        export_rdt(self.output, duplicate, report, report)
        self.assertEqual(duplicate.read_bytes(), self.output.read_bytes())
        empty = copy.deepcopy(report)
        for section in ("channels", "contacts", "zones", "scan_lists", "rx_groups"):
            empty[section] = []
        dest = self.root / "empty.rdt"
        export_rdt(duplicate, dest, report, empty)
        checked = import_rdt(dest)
        self.assertTrue(all(not checked[name] for name in ("contacts", "channels", "zones", "rx_groups", "scan_lists")))

    def test_unrelated_edit_preserved_conflict_fails(self):
        report = self.populate()
        edited = copy.deepcopy(report)
        edited["channels"][0]["name"] = "Переименован"
        offset = 0x40235 + 2809 * 64
        with self.output.open("r+b") as file:
            file.seek(offset + 0x10)
            file.write(bytes.fromhex("00005014"))  # external edit on the same channel
        dest = self.root / "merged.rdt"
        export_rdt(self.output, dest, report, edited)
        self.assertEqual(import_rdt(dest)["channels"][0]["rx_hz"], 145000000)
        edited["channels"][0]["rx_hz"] = 146000000
        dest = self.root / "conflict.rdt"
        with self.assertRaisesRegex(ValueError, "Конфликт"):
            export_rdt(self.output, dest, report, edited)
        self.assertFalse(dest.exists())

    def test_validation_and_no_overwrite(self):
        report = self.populate()
        changes = [
            ("частота", lambda d: d["channels"][0].update(tx_hz=433000001)),
            ("имя", lambda d: d["channels"][0].update(name="А"*17)),
            ("удалённый контакт", lambda d: d.update(contacts=[])),
            ("сканирование", lambda d: d["scan_lists"][0].update(channels=[3000, 3000])),
            ("тип в группе", lambda d: d["contacts"][0].update(type="private")),
            ("код Privacy", lambda d: d["channels"][0].update(privacy_type_code=4)),
            ("тип поля вызова", lambda d: d["contacts"][0].update(ring=int(d["contacts"][0]["ring"]))),
        ]
        for title, apply in changes:
            with self.subTest(title):
                invalid = copy.deepcopy(report)
                apply(invalid)
                dest = self.root / "invalid.rdt"
                with self.assertRaises(ValueError):
                    export_rdt(self.output, dest, report, invalid)
                self.assertFalse(dest.exists())
        with self.assertRaises(FileExistsError):
            export_rdt(self.output, self.output, report, report)

    def test_preexisting_dangling_reference_kept(self):
        report = self.populate()
        with self.output.open("r+b") as file:
            file.seek(0x40235 + (2810 - 1) * 64 + 6)
            file.write(struct.pack("<H", 20))
        report = import_rdt(self.output)
        edited = copy.deepcopy(report)
        edited["dmr_id"] = 42
        dest = self.root / "preserved.rdt"
        export_rdt(self.output, dest, report, edited)
        self.assertEqual(import_rdt(dest)["channels"][0]["contact_index"], 20)

    def test_duplicate_from_existing_template_preserves_unexposed_bits(self):
        self.populate()
        # An unexposed channel bit. Duplicating an existing channel copies its complete record.
        with self.output.open("r+b") as file:
            source_pos = 0x40235 + (2810 - 1) * 64
            file.seek(source_pos + 0x1e)
            file.write(bytes([0xA2]))
        report = import_rdt(self.output)
        edited = copy.deepcopy(report)
        row = copy.deepcopy(edited["channels"][0])
        row.update(index=10, name="Дубликат", rdt_template_index=2810)
        edited["channels"].append(row)
        dest = self.root / "cloned.rdt"
        export_rdt(self.output, dest, report, edited)
        clone = next(ch for ch in import_rdt(dest)["channels"] if ch["index"] == 10)
        self.assertEqual(clone["name"], "Дубликат")
        self.assertEqual(clone["details"], report["channels"][0]["details"])
        with dest.open("rb") as file:
            file.seek(0x40235 + 9 * 64 + 0x1e)
            self.assertEqual(file.read(1), b"\xa2")

    def test_vfo_dangling_reference_prevents_delete(self):
        self.populate()
        with self.output.open("r+b") as file:
            file.seek(0x2F125)
            file.write(b"\x02")
            file.seek(0x2F125 + 6)
            file.write(struct.pack("<H", 9817))
        report = import_rdt(self.output)
        edited = copy.deepcopy(report)
        edited["contacts"].clear()
        edited["rx_groups"][0]["contacts"].clear()
        edited["channels"][0]["contact_index"] = None
        with self.assertRaisesRegex(ValueError, "VFO"):
            export_rdt(self.output, self.root / "broken.rdt", report, edited)

    def test_both_full_zone_sides_and_changed_public_privacy_selectors(self):
        self.new["channels"] = [{"index": n, "name": f"Канал {n}", "mode": "DMR", "rx_hz": 450000000,
                                  "tx_hz": 450000000, "privacy_type_code": 3, "privacy_index_code": 15}
                                 for n in range(1, 65)]
        ids = [{"index": n} for n in range(1, 65)]
        self.new["zones"] = [{"index": 249, "name": "Полная", "A": ids, "B": list(reversed(ids))}]
        export_rdt(self.original, self.output, self.baseline, self.new)
        report = import_rdt(self.output)
        self.assertEqual([x["index"] for x in report["zones"][0]["A"]], list(range(1, 65)))
        self.assertEqual([x["index"] for x in report["zones"][0]["B"]], list(range(64, 0, -1)))
        self.assertEqual(report["channels"][0]["privacy_type_code"], 3)
        self.assertEqual(report["channels"][0]["privacy_index_code"], 15)

    def test_new_from_template_keeps_external_references_and_original(self):
        self.populate()
        with self.output.open("r+b") as file:
            file.seek(0x2351)  # Number Key 0 refers to the group contact.
            file.write(struct.pack("<H", 9817))
        before = self.output.read_bytes()
        script = Path(__file__).resolve().parents[1] / "rdt_editor.py"
        result = subprocess.run([sys.executable, "-B", script, "--new-from-template", self.output],
                                check=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout), import_rdt(self.output))
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_cps_242_general_fields_and_independent_display_modes(self):
        # Set an unrelated bit in the display selector. CPS 2.42 uses mask 0x04 for A.
        with self.original.open("r+b") as file:
            file.seek(0x22A8)
            file.write(b"\x0a")
            file.seek(0x22F9)
            file.write(bytes.fromhex("010000ee 020000dd 030000cc"))
        report = import_rdt(self.original)
        edited = copy.deepcopy(report)
        changes = {
            "general": {"Radio ID 1": "1193046", "Radio ID 2": "17", "Radio ID 3": "23",
                        "Выбор канала передачи": "Назначенный канал", "Автоблокировка клавиатуры": "10 с",
                        "Формат отчёта GPS": "Обычный", "Блокировка передней клавиатуры": "Выкл.",
                        "Озвучивание при включении": "Вкл.", "Редактирование Radio ID на рации": "Выкл.",
                        "Часовой пояс": "UTC+3"},
            "vfo": {"Сторона A": "Каналы", "Сторона B": "Каналы"},
        }
        for group in edited["cps_sections"]:
            for row in group["rows"]:
                for field in row["fields"]:
                    if field["label"] in changes.get(group["id"], {}):
                        field["value"] = changes[group["id"]][field["label"]]
        export_rdt(self.original, self.output, report, edited)
        raw = self.output.read_bytes()
        self.assertEqual(raw[0x22A8], 0x8E)
        self.assertEqual(raw[0x22A5] >> 6, 2)
        self.assertEqual(raw[0x22BB], 2)
        self.assertEqual(raw[0x22D0] >> 3, 15)
        self.assertEqual(raw[0x22F9:0x2305], bytes.fromhex("563412ee 110000dd 170000cc"))
        self.assertEqual(raw[0x2305], 0xC0)
        self.assertEqual(raw[0x2307], 0x24)

    @unittest.skipUnless(sys.platform == "darwin", "renamex_np is a macOS API")
    def test_publish_without_hard_links_is_still_exclusive(self):
        denied = OSError(errno.ENOTSUP, "File system does not support hard links")
        with mock.patch("rdt_editor.os.link", side_effect=denied):
            export_rdt(self.original, self.output, self.baseline, self.baseline)
            self.assertEqual(self.output.read_bytes(), self.original.read_bytes())
            contender = self.root / "concurrent.rdt"
            contender.write_bytes(b"new data")
            with self.assertRaises(FileExistsError):
                publish_new(contender, self.output)
            self.assertEqual(contender.read_bytes(), b"new data")
            self.assertEqual(self.output.read_bytes(), self.original.read_bytes())


if __name__ == "__main__":
    unittest.main()
