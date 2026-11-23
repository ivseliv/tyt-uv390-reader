import tempfile
import unittest
from pathlib import Path

from import_qdmr import import_qdmr


class QdmrImportTests(unittest.TestCase):
    def parse(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "codeplug.yaml")
            path.write_text(source, encoding="utf-8")
            return import_qdmr(path)

    def test_forward_references_and_order(self):
        report = self.parse("""
settings: {defaultID: idB}
radioIDs:
  - dmr: {id: idA, name: A, number: 42}
  - dmr: {id: idB, name: B, number: 43}
contacts:
  - dtmf: {id: remote, name: ON, number: '001#'}
  - dmr: {id: tg99, name: 001, type: GroupCall, number: 99}
channels:
  - digital:
      {id: c47, name: Узел, rxFrequency: 438.125 MHz, txFrequency: 430525 kHz,
       colorCode: 7, timeSlot: TS2, contact: tg99, radioID: idA, groupList: g88, scanList: s8}
  - fm:
      id: c2
      name: FM
      rxFrequency: 145.5
      txFrequency: ~
      rxTone: {dcs: i037}
      txTone: {ctcss: 141.300003}
      power: !<!default> ""
zones:
  - {name: Только A, A: &side [c2, c47]}
  - {name: Обе, A: *side, B: [c47]}
groupLists:
  - {id: g88, name: Приём, contacts: [tg99]}
scanLists:
  - id: s8
    name: Скан
    primary: !selected
    secondary: c2
    channels: [c2, !<!selected> "", c47]
""")
        self.assertEqual(43, report["dmr_id"])
        self.assertEqual("ON", report["contacts"][0]["name"])
        self.assertEqual("001#", report["contacts"][0]["dtmf_number"])
        self.assertEqual("001", report["contacts"][1]["name"])
        digital, fm = report["channels"]
        self.assertEqual((438125000, 430525000, 7, 2, 2, 1, 1, 42), tuple(digital[k] for k in
                         ("rx_hz", "tx_hz", "color_code", "time_slot", "contact_index",
                          "rx_group_index", "scan_list_index", "radio_id")))
        self.assertEqual((145500000, None, 0xC037, 0x1413), tuple(fm[k] for k in
                         ("rx_hz", "tx_hz", "rx_tone_code", "tx_tone_code")))
        self.assertEqual([], report["zones"][0]["B"])
        self.assertEqual([{"index": 2, "name": "FM"}, {"index": 1, "name": "Узел"}], report["zones"][1]["A"])
        self.assertEqual([{"index": 1, "name": "Узел"}], report["zones"][1]["B"])
        self.assertEqual([2], report["rx_groups"][0]["contacts"])
        self.assertEqual({"index": 1, "name": "Скан", "primary": 0, "secondary": 2,
                          "revert": None, "channels": [2, 0, 1]}, report["scan_lists"][0])

    def test_invalid_documents(self):
        for invalid in (
            "foo: bar", "channels: []\nchannels: []",
            "contacts: !!python/object:example {}", "channels: []\n---\nchannels: []",
            "channels: [&loop [*loop]]",
            "zones: [{name: Home, A: [absent]}]",
            "channels: [{fm: {id: a, name: A}}, {dmr: {id: a, name: B}}]",
            "contacts: [{dtmf: {id: a, name: A, number: '123'}}]\ngroupLists: [{name: B, contacts: [a]}]",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.parse(invalid)


if __name__ == "__main__":
    unittest.main()
