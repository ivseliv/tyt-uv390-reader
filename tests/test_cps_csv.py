import csv
import io
import tempfile
import unittest
from pathlib import Path

from cps_basic import basic_section
from cps_csv import CPS_CHANNELS, CPS_CONTACTS, READER_CONTACTS, export_csv, import_csv


class FileTables(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "пример.csv"

    def write(self, header, row, encoding="utf-8"):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerow(row)
        self.path.write_bytes(text.getvalue().encode(encoding))

    def test_cps_channel_preserves_every_column(self):
        row = dict.fromkeys(CPS_CHANNELS, "0")
        row.update({"Channel Mode": "2", "Channel Name": "Ретранслятор",
                    "RX Frequency(MHz)": "439.28750", "TX Frequency(MHz)": "433.28750",
                    "Repeater Slot": "1", "Color Code": "7", "In Call Criteria": "1"})
        self.write(CPS_CHANNELS, row.values(), "cp1251")
        snapshot = import_csv(self.path)
        channel, = snapshot["channels"]
        self.assertEqual((channel["rx_hz"], channel["tx_hz"], channel["time_slot"], channel["color_code"]),
                         (439287500, 433287500, 2, 7))
        self.assertEqual(export_csv(snapshot, "channels"), self.path.read_bytes())
        self.assertEqual(snapshot["contacts"], [])
        channel["name"] = "Обновлённый"
        channel["rx_hz"] = 438200000
        channel["color_code"] = 3
        channel["time_slot"] = 1
        next(field for field in channel["details"] if field["label"] == "Мощность")["value"] = "Middle"
        fields, = csv.DictReader(io.StringIO(export_csv(snapshot, "channels").decode("cp1251")))
        self.assertEqual((fields["Channel Name"], fields["RX Frequency(MHz)"], fields["TX Frequency(MHz)"]),
                         ("Обновлённый", "438.20000", "433.28750"))
        self.assertEqual((fields["Repeater Slot"], fields["Color Code"], fields["In Call Criteria"]),
                         ("0", "3", "1"))
        self.assertEqual(fields["Power"], "1")

    def test_contacts_and_bad_dmr_id(self):
        self.write(CPS_CONTACTS, ("Группа", "1", "14", "1"))
        snapshot = import_csv(self.path)
        self.assertEqual(snapshot["contacts"][0]["call_receive_tone"], 1)
        self.assertEqual(snapshot["contacts"][0]["dmr_id"], 14)
        self.assertEqual(export_csv(snapshot, "contacts"), self.path.read_bytes())
        snapshot["contacts"][0].update(name="Дежурный", type="private", dmr_id=123)
        self.path.write_bytes(export_csv(snapshot, "contacts"))
        contact, = import_csv(self.path)["contacts"]
        self.assertEqual((contact["name"], contact["type"], contact["dmr_id"]), ("Дежурный", "private", 123))
        snapshot["contacts"].append({"index": 2, "name": "Новый", "dmr_id": 43, "type": "private"})
        report = io.StringIO(export_csv(snapshot, "contacts").decode("utf-8-sig"))
        self.assertEqual(tuple(next(csv.reader(report))), READER_CONTACTS)
        self.write(CPS_CONTACTS, ("Broken", "1", "16777216", "0"))
        with self.assertRaisesRegex(ValueError, "Строка 2"):
            import_csv(self.path)

    def test_snapshot_export_sparse_indices_and_quoted_names(self):
        snapshot = {"channels": [{"index": 17, "name": 'Метка "А", Б', "mode": "AM", "rx_hz": 123150000}]}
        self.path.write_bytes(export_csv(snapshot, "channels"))
        self.assertEqual(import_csv(self.path)["channels"], snapshot["channels"])
        snapshot = {"contacts": [{"index": 82, "name": "DTMF", "type": "dtmf", "dtmf_number": "00012A"}]}
        self.path.write_bytes(export_csv(snapshot, "contacts"))
        self.assertEqual(import_csv(self.path)["contacts"], snapshot["contacts"])

    def test_wrong_header(self):
        self.write(["Channel Name", "password"], ["a", "x"])
        with self.assertRaisesRegex(ValueError, "заголовки"):
            import_csv(self.path)

    def test_basic_section_uses_only_public_bytes(self):
        blocks = {(0x139, 8): bytes.fromhex("0040004860134017"),
                  (0x2226, 11): bytes.fromhex("2025112214105900020402")}
        result = basic_section(lambda offset, size: blocks[(offset, size)], "MD-UV390")
        values = [f["value"] for f in result["rows"][0]["fields"]]
        self.assertEqual(values, ["MD-UV390", "400–480", "136–174", "22.11.2025 14:10:59", "2.42"])
        self.assertIsNone(basic_section(lambda *_: None, None))

class RDTCSVTest(unittest.TestCase):
    def setUp(self):
        from rdt_editor import EMPTY_CHANNEL
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / "test.rdt"
        image = bytearray(852533)
        image[0x125:0x12d] = b"MD-UV390"
        for index in (1, 4):
            pos = 0x40235 + (index - 1) * 64
            record = bytearray(EMPTY_CHANNEL)
            record[0] = record[0] & ~3 | 2
            record[32:36] = f"C{index}".encode("utf-16-le")
            image[pos:pos + 64] = record
        for index in (1, 3):
            pos = 0x70235 + (index - 1) * 36
            image[pos:pos + 4] = bytes([index, 0, 0, 1])
            image[pos + 4:pos + 8] = f"G{index}".encode("utf-16-le")
        self.base.write_bytes(image)

    def test_sparse_channel_slots_and_extended_merge(self):
        from cps_csv import export_rdt_csv, merge_csv
        from import_rdt import import_rdt
        from rdt_editor import export_rdt
        data = export_rdt_csv(self.base, "channels")
        path = Path(self.directory.name) / "channels.csv"
        path.write_bytes(data)
        parsed = import_csv(path)
        self.assertEqual([c["index"] for c in parsed["channels"]], [1, 4])
        self.assertEqual(export_csv(parsed, "channels"), data)
        baseline = import_rdt(self.base)
        self.assertEqual(export_csv(baseline, "channels"), data)
        self.assertEqual(export_csv(baseline, "contacts"), export_rdt_csv(self.base, "contacts"))
        usb = {**baseline, "usb_model": baseline["file_model"]}
        del usb["file_model"]
        self.assertEqual(export_csv(usb, "channels"), data)
        self.assertEqual(merge_csv(baseline, parsed), baseline)
        rows = list(csv.DictReader(io.StringIO(data.decode("cp1251"))))
        rows[0].update({"Power": "1", "QT Reverse": "3", "RX Frequency(MHz)": "435.50000",
                        "Channel Name": "ДМР", "Decode 1": "1", "Privacy": "3",
                        "Privacy No.": "130", "Contact Name": "3"})
        # A partial import updates slot 1 only. Slot 4 and all contacts must survive.
        with path.open("w", encoding="cp1251", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CPS_CHANNELS)
            writer.writeheader()
            writer.writerow(rows[0])
        merged = merge_csv(baseline, import_csv(path))
        channel = merged["channels"][0]
        self.assertIn({"label":"Мощность", "value":"Middle"}, channel["details"])
        self.assertEqual(channel["privacy_index_code"], 130)
        mem = list(csv.DictReader(io.StringIO(export_csv(merged, "channels").decode("cp1251"))))
        self.assertEqual(mem[0]["Channel Name"], "ДМР")
        self.assertEqual(mem[0]["Privacy No."], "130")
        output = Path(self.directory.name) / "saved.rdt"
        export_rdt(self.base, output, baseline, merged)
        reread = import_rdt(output)
        self.assertEqual(reread["channels"][0]["cps_csv"], rows[0])
        self.assertEqual(reread["channels"][1], baseline["channels"][1])
        self.assertEqual(reread["contacts"], baseline["contacts"])

    def test_contact_ring_and_slot_creation(self):
        from cps_csv import export_rdt_csv, merge_csv
        from import_rdt import import_rdt
        from rdt_editor import export_rdt
        data = export_rdt_csv(self.base, "contacts")
        lines = list(csv.reader(io.StringIO(data.decode("cp1251"))))
        self.assertEqual([r[0] for r in lines[1:]], ["G1", "", "G3"])
        path = Path(self.directory.name) / "contacts.csv"
        with path.open("w", encoding="cp1251", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerows([CPS_CONTACTS, ["Связь", "1", "14", "1"], ["Новый", "2", "32", "0"]])
        original = self.base.read_bytes()
        baseline = import_rdt(self.base)
        merged = merge_csv(baseline, import_csv(path))
        output = Path(self.directory.name) / "saved.rdt"
        export_rdt(self.base, output, baseline, merged)
        groups = import_rdt(output)["contacts"]
        self.assertEqual([r["index"] for r in groups], [1,2,3])
        self.assertEqual((groups[0]["name"], groups[0]["ring"]), ("Связь", True))
        self.assertEqual(groups[2], baseline["contacts"][1])
        # Preserve names outside CP1251 and the CPS column layout.
        merged["contacts"][0]["name"] = "無線"
        unicode_export = export_csv(merged, "contacts")
        self.assertTrue(unicode_export.startswith(b"\xef\xbb\xbf"))
        unicode_rows = list(csv.DictReader(io.StringIO(unicode_export.decode("utf-8-sig"))))
        self.assertEqual(unicode_rows[0], {"Contact Name": "無線", "Call Type": "1",
                                          "Call ID": "14", "Call Receive Tone": "1"})
        self.assertEqual(self.base.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
