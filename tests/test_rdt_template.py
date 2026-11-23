import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from import_qdmr import decode_qdmr
from import_rdt import MODEL_OFFSET, RDT_SIZE, SEGMENTS, import_rdt
from rdt_editor import TABLES
from rdt_template import transfer_rdt
from read_settings import PUBLIC_REGIONS, REGIONS, decode


def usb_snapshot(path):
    # Reproduce the public USB helper packet by reading the matching ranges of a fixture file.
    frame = bytearray()
    with path.open("rb") as stream:
        stream.seek(MODEL_OFFSET)
        frame.extend(stream.read(8))
        for start, end in REGIONS + PUBLIC_REGIONS:
            first, _, offset = next(segment for segment in SEGMENTS if segment[0] <= start < end <= segment[1])
            stream.seek(offset + start - first)
            frame.extend(struct.pack("<II", start, end - start))
            frame.extend(stream.read(end - start))
    return decode(frame)


class RDTTemplateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.blank = self.folder / "шаблон.rdt"
        self.populated = self.folder / "полный.rdt"
        self.output = self.folder / "выход.rdt"
        data = bytearray(RDT_SIZE)
        data[MODEL_OFFSET:MODEL_OFFSET + 8] = b"MD-UV390"
        data[0x410:0x415] = b"local"
        self.blank.write_bytes(data)
        self.report = {
            "dmr_id": 101,
            "contacts": [{"index": 19, "name": "Группа", "dmr_id": 44, "type": "group", "ring": True}],
            "channels": [{"index": 71, "name": "Один", "mode": "DMR", "rx_hz": 433125000, "tx_hz": 433125000,
                          "scan_list_index": 17, "contact_index": 19, "rx_group_index": 14, "time_slot": 2},
                         {"index": 72, "name": "Два", "mode": "analog", "rx_hz": 144500000, "tx_hz": 144500000}],
            "zones": [{"index": 15, "name": "Обе стороны", "A": [{"index": 71}], "B": [{"index": 72}]}],
            "rx_groups": [{"index": 14, "name": "Приём", "contacts": [19]}],
            "scan_lists": [{"index": 17, "name": "Поиск", "channels": [72, 71], "secondary": 71, "primary": 0}],
        }
        transfer_rdt(self.blank, self.populated, self.report)

    def test_usb_packet_to_another_template_and_cli(self):
        native = import_rdt(self.populated)
        stream = usb_snapshot(self.populated)
        stream["cps_sections"] = [{"id": "foreign", "rows": []}]
        stream["channels"][0]["details"] = [{"label": "Мощность", "value": "Неизвестно"}]
        stream["channels"][0]["cps_csv"] = {"Power": "2"}
        before = self.blank.read_bytes()
        script = Path(__file__).resolve().parents[1] / "rdt_template.py"
        subprocess.run([sys.executable, "-B", script, self.blank, self.output],
                       input=json.dumps(stream).encode(), capture_output=True, check=True)
        actual = import_rdt(self.output)
        self.assertEqual(self.blank.read_bytes(), before)
        self.assertEqual(self.output.read_bytes()[0x410:0x415], b"local")
        for table in TABLES:
            self.assertEqual(actual[table], native[table])
        self.assertEqual(actual["dmr_id"], 101)
        self.assertEqual(actual["zones"][0]["B"], [{"index": 72, "name": "Два"}])
        self.assertNotIn("foreign", {section["id"] for section in actual["cps_sections"]})

    def test_missing_table_kept_empty_table_cleared(self):
        before = import_rdt(self.populated)
        transfer_rdt(self.populated, self.output, {"zones": [], "dmr_id": None})
        after = import_rdt(self.output)
        for key in ("dmr_id", "contacts", "scan_lists", "rx_groups", "channels"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["zones"], [])
        self.assertEqual(len(import_rdt(self.populated)["zones"]), 1)

    def test_mode_changes_at_existing_indices_keep_vendor_options(self):
        pos = TABLES["channels"][0] + 70 * 64
        with self.populated.open("r+b") as stream:
            stream.seek(pos + 30)
            stream.write(bytes([0xA3]))  # Also sets a public channel power and unused bits.
        incoming = [{"index": 71, "name": "FM", "mode": "analog", "rx_hz": 433125000, "tx_hz": 433125000},
                    {"index": 72, "name": "DMR", "mode": "DMR", "rx_hz": 144500000, "tx_hz": 144500000}]
        transfer_rdt(self.populated, self.output, {"channels": incoming})
        fm, dmr = import_rdt(self.output)["channels"]
        self.assertEqual((fm["rx_tone_code"], fm["tx_tone_code"]), (65535, 65535))
        self.assertNotIn("time_slot", fm)
        self.assertEqual((dmr["color_code"], dmr["time_slot"], dmr["contact_index"]), (1, 1, None))
        self.assertNotIn("rx_tone_code", dmr)
        self.assertEqual(self.output.read_bytes()[pos + 30], 0xA3)

    def test_unrecognized_rx_groups_in_snapshot_keep_template_rows(self):
        before = import_rdt(self.populated)
        group = before["rx_groups"][0]["index"]
        transfer_rdt(self.populated, self.output, {"rx_groups": [], "unrecognized_rx_group_slots": [group]})
        self.assertEqual(import_rdt(self.output)["rx_groups"], before["rx_groups"])

    def test_standalone_dtmf_template_does_not_clear_any_other_table(self):
        from cps_dtmf import sequence_section
        from normalize_snapshot import normalize
        import copy

        before = self.populated.read_bytes()
        original = {key: [] for key in TABLES}
        original["source_format"] = "dtmf"
        edited = copy.deepcopy(original)
        sequences = sequence_section()
        next(r for r in sequences["rows"] if r["name"] == "Передача 32")["fields"][0]["value"] = "12a#"
        edited["cps_sections"] = [sequences]
        transfer_rdt(self.populated, self.output, normalize(original, edited))
        after = self.output.read_bytes()
        last = 0x304d5
        self.assertEqual(after[last:last+3], bytes([4, 0x21, 0xfa]))
        self.assertEqual(after[:last] + after[last+3:], before[:last] + before[last+3:])
        self.assertEqual(self.populated.read_bytes(), before)

    def test_compatible_qdmr(self):
        report = decode_qdmr({"radioIDs": [{"dmr": {"id": "main", "name": "Main", "number": "701"}}],
                              "channels": [{"digital": {"id": "ch", "name": "DMR", "radioId": "main",
                                                       "rxFrequency": "433.25", "txFrequency": "433.25"}}],
                              "zones": [{"name": "Зона", "A": [], "B": ["ch"]}]})
        transfer_rdt(self.blank, self.output, report)
        actual = import_rdt(self.output)
        self.assertEqual(actual["dmr_id"], 701)
        self.assertEqual(actual["channels"][0]["rx_hz"], 433250000)
        self.assertEqual(actual["zones"][0]["B"], [{"index": 1, "name": "DMR"}])

    def test_incompatible_inputs_and_colliding_output(self):
        changes = [
            ("CSV", {"channels": [], "csv_source": {"collection": "channels"}}),
            ("M17", {"channels": [dict(self.report["channels"][0], mode="M17")]}),
            ("AM", {"channels": [dict(self.report["channels"][0], mode="AM")]}),
            ("dtmf", {"contacts": [dict(self.report["contacts"][0], type="dtmf", dtmf_number="12")]}),
            ("radio_id", {"channels": [dict(self.report["channels"][0], radio_id=213)]}),
            ("tx_tone_code", {"channels": [dict(self.report["channels"][0], tx_tone_code=0xFFFF)]}),
            ("vendorOption", {"contacts": [dict(self.report["contacts"][0], vendorOption=45)]}),
            ("!selected", {"scan_lists": [dict(self.report["scan_lists"][0], channels=[0])]}),
            ("ссылка", {"contacts": []}),
        ]
        for expected, document in changes:
            with self.subTest(expected), self.assertRaisesRegex(ValueError, expected):
                transfer_rdt(self.populated, self.output, copy.deepcopy(document))
            self.assertFalse(self.output.exists())
        before = self.populated.read_bytes()
        with self.assertRaises(FileExistsError):
            transfer_rdt(self.populated, self.populated, self.report)
        self.assertEqual(self.populated.read_bytes(), before)
