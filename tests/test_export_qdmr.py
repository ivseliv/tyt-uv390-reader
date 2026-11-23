import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from export_qdmr import export_qdmr
from import_qdmr import CodeplugLoader, Tag, decode_qdmr, import_qdmr
from vendor import yaml


ROOT = Path(__file__).resolve().parent.parent
# A device-independent file with both channel modes and all five linked tables.
# dmrconf encode/decode in the interoperability test is strictly file-to-file.
EXAMPLE = """
version: 0.15.1
settings: {defaultID: home, power: Mid}
radioIDs:
  - dmr: {id: other, name: Spare, number: 101}
  - dmr: {id: home, name: Station, number: 102}
contacts:
  - dmr: {id: local, name: TG 99, number: 99, type: GroupCall, ring: true}
channels:
  - fm:
      id: analog
      name: FM
      rxFrequency: 145.4125 MHz
      txFrequency: 145.4125 MHz
      bandwidth: Wide
      rxTone: {dcs: i037}
      txTone: {ctcss: 67.0 Hz}
      rxOnly: true
      power: !default ''
      extended: {talkaround: true}
  - dmr:
      id: digital
      name: DMR
      rxFrequency: 438.5 MHz
      txFrequency: 430.9 MHz
      timeSlot: TS2
      colorCode: 7
      radioId: other
      contact: local
      groupList: tg
      scanList: scanA
      tyt: {autoScan: false}
groupLists:
  - {id: tg, name: RX, contacts: [local]}
zones:
  - {name: Local, A: [digital, analog], B: [analog]}
scanLists:
  - id: scanA
    name: Scan
    channels: [analog, digital]
    primary: !selected ''
    secondary: digital
    revert: !selected ''
    tyt: {holdTime: 500, prioritySampleTime: 2000}
"""


def load(text):
    return yaml.load(text, Loader=CodeplugLoader)


class QdmrExportTests(unittest.TestCase):
    def setUp(self):
        self.report = decode_qdmr(load(EXAMPLE))

    def test_roundtrip_projection_and_safe_properties(self):
        self.report["channels"][0]["details"].append({"label": "Private extension", "value": "must-be-absent"})
        self.report["cps_sections"].append({"id": "unknown", "rows": [{"name": "must-be-absent"}]})
        text = export_qdmr(self.report)
        self.assertNotIn("must-be-absent", text)
        doc = load(text)
        self.assertEqual("0.15.1", doc["version"])
        self.assertEqual({"id101", "id102"}, {row["dmr"]["id"] for row in doc["radioIDs"]})
        self.assertEqual("id102", doc["settings"]["defaultID"])
        self.assertEqual("Mid", doc["settings"]["power"])
        self.assertEqual("scan1", doc["channels"][1]["dmr"]["scanListRef"])
        self.assertEqual("id101", doc["channels"][1]["dmr"]["radioId"])
        self.assertIs(doc["channels"][0]["fm"]["power"], Tag.DEFAULT)
        self.assertEqual("true", doc["channels"][0]["fm"]["rxOnly"])
        self.assertEqual("true", doc["channels"][0]["fm"]["extended"]["talkaround"])
        self.assertEqual(self.report["scan_lists"], decode_qdmr(doc)["scan_lists"])
        for key in ("zones", "contacts", "rx_groups", "dmr_id"):
            self.assertEqual(self.report[key], decode_qdmr(doc)[key])
        self.assertEqual({"holdTime": "500", "prioritySampleTime": "2000"}, doc["scanLists"][0]["tyt"])

    def test_sparse_indices_and_current_names(self):
        self.report["channels"][1]["index"] = 20
        self.report["channels"][0]["name"] = "Исправлено"
        self.report["zones"][0]["A"][0]["index"] = 20
        self.report["scan_lists"][0].update(channels=[0, 20], secondary=20)
        result = load(export_qdmr(self.report))
        self.assertEqual({"id": "ch20", "name": "DMR"}, {k: result["channels"][1]["dmr"][k] for k in ("id", "name")})
        self.assertEqual(["ch20", "ch1"], result["zones"][0]["A"])
        self.assertEqual([Tag.SELECTED, "ch20"], result["scanLists"][0]["channels"])
        self.assertEqual("Исправлено", result["channels"][0]["fm"]["name"])

    def test_dtmf_number_spelling(self):
        self.report["contacts"].append({"index": 12, "name": "ON", "type": "dtmf", "dtmf_number": "001#"})
        exported = load(export_qdmr(self.report))
        self.assertEqual({"id": "cont12", "name": "ON", "number": "001#"}, exported["contacts"][1]["dtmf"])

    def test_unrepresentable_rows_fail_without_partial_output(self):
        for table, changed, message in (
            ("channels", {"mode": "M17"}, "Канал № 1"),
            ("channels", {"bandwidth_code": 1}, "полосы FM"),
            ("channels", {"rx_tone_code": 0xeeee}, "субтона"),
            ("zones", {"B": [{"index": 99}]}, "Зона № 1, B"),
            ("contacts", {"type": "pc"}, "Контакт № 1"),
            ("rx_groups", {"contacts": [82]}, "Контакт» № 82"),
            ("scan_lists", {"primary": 73}, "Канал» № 73"),
        ):
            with self.subTest(change=changed):
                report = copy.deepcopy(self.report)
                report[table][0].update(changed)
                with self.assertRaisesRegex(ValueError, message):
                    export_qdmr(report)
        report = copy.deepcopy(self.report)
        report["channels"][1]["privacy_type_code"] = 2
        with self.assertRaisesRegex(ValueError, "Канал № 2: настроен режим Privacy"):
            export_qdmr(report)
        report = copy.deepcopy(self.report)
        report["contacts"][0]["type"] = "private"
        with self.assertRaisesRegex(ValueError, "несовместимый тип"):
            export_qdmr(report)
        failed = subprocess.run([sys.executable, str(ROOT / "export_qdmr.py")], input=json.dumps(report).encode(),
                                capture_output=True, check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(b"", failed.stdout)
        self.assertIn("несовместимый тип", failed.stderr.decode())

    @unittest.skipUnless(shutil.which("dmrconf"), "dmrconf отсутствует")
    def test_dmrconf_file_encode_and_decode(self):
        with tempfile.TemporaryDirectory() as temp:
            source, binary, decoded = (Path(temp, name) for name in ("input.yaml", "image.dfu", "readback.yaml"))
            with source.open("wb") as output:
                subprocess.run([sys.executable, str(ROOT / "export_qdmr.py")], input=json.dumps(self.report).encode(),
                               stdout=output, stderr=subprocess.PIPE, check=True)
            for command, first, second in (("encode", source, binary), ("decode", binary, decoded)):
                subprocess.run(["dmrconf", command, "--radio=uv390", first, second], check=True, capture_output=True)
            readback = import_qdmr(decoded)
            self.assertEqual(102, readback["dmr_id"])
            self.assertEqual(0xc037, readback["channels"][0]["rx_tone_code"])
            self.assertEqual(0x0670, readback["channels"][0]["tx_tone_code"])
            self.assertEqual(145412500, readback["channels"][0]["rx_hz"])
            self.assertEqual(("DMR", 7, 2, 1, 1, 1), tuple(readback["channels"][1][key] for key in
                             ("mode", "color_code", "time_slot", "contact_index", "rx_group_index", "scan_list_index")))
            self.assertEqual(self.report["zones"], readback["zones"])
            self.assertEqual(self.report["rx_groups"], readback["rx_groups"])
            self.assertEqual(self.report["scan_lists"], readback["scan_lists"])
            self.assertEqual(self.report["contacts"], readback["contacts"])


if __name__ == "__main__":
    unittest.main()
