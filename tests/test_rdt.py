import struct
import tempfile
import unittest
from pathlib import Path

from import_rdt import import_rdt


class RDTImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "пример.rdt"
        self.data = bytearray(852533)
        self.data[0x125:0x12D] = b"MD-UV390"

    def label(self, offset, name):
        encoded = name.encode("utf-16-le")
        self.data[offset:offset + len(encoded)] = encoded

    def test_sparse_records_in_both_segments(self):
        # CPS file offsets as documented by editcp, independent of the importer.
        self.data[0x22A9:0x22AC] = (7777).to_bytes(3, "little")
        contact = 0x70235 + 0x24 * 24
        self.data[contact:contact + 4] = bytes([14, 0, 0, 1])
        self.label(contact + 4, "TG14")
        channel = 0x40235 + 0x40 * 18
        self.data[channel] = 2
        self.data[channel + 0x10:channel + 0x18] = bytes.fromhex("0025314300253143")
        struct.pack_into("<H", self.data, channel + 6, 25)
        self.label(channel + 0x20, "Ретранслятор")
        group = 0xEE45 + 0x60
        self.label(group, "Приём")
        struct.pack_into("<H", self.data, group + 0x20, 25)
        self.label(0x14C05, "Зона")
        struct.pack_into("<H", self.data, 0x14C25, 19)
        struct.pack_into("<H", self.data, 0x31285, 19)
        scan = 0x18A85
        self.label(scan, "Скан")
        struct.pack_into("<HHH", self.data, scan + 0x20, 19, 0, 0xFFFF)
        struct.pack_into("<H", self.data, scan + 0x2A, 19)
        self.path.write_bytes(self.data)

        report = import_rdt(self.path)
        self.assertEqual(report["file_model"], "MD-UV390")
        self.assertNotIn("usb_model", report)
        self.assertEqual(report["dmr_id"], 7777)
        channel, = report["channels"]
        self.assertEqual((channel["index"], channel["rx_hz"], channel["contact_index"]),
                         (19, 433125000, 25))
        self.assertEqual(report["contacts"], [
            {"index": 25, "name": "TG14", "dmr_id": 14, "ring": False, "type": "group"}])
        member = {"index": 19, "name": "Ретранслятор"}
        self.assertEqual(report["zones"], [{"index": 1, "name": "Зона", "A": [member], "B": [member]}])
        self.assertEqual(report["rx_groups"], [{"index": 2, "name": "Приём", "contacts": [25]}])
        self.assertEqual(report["scan_lists"], [
            {"index": 1, "name": "Скан", "channels": [19], "primary": 19, "secondary": 0, "revert": None}])
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_unknown_model_and_truncated_file(self):
        self.data[0x125:0x12D] = b"MD-OTHER"
        self.path.write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, "заголовок"):
            import_rdt(self.path)
        self.path.write_bytes(self.data[:0x200])
        with self.assertRaisesRegex(ValueError, "размер"):
            import_rdt(self.path)

    def test_out_of_format_group_is_never_treated_as_an_empty_slot(self):
        import copy
        from rdt_editor import export_rdt
        self.label(0xEE45, "Unknown")
        struct.pack_into("<H", self.data, 0xEE45 + 32, 10001)
        self.path.write_bytes(self.data)
        before = import_rdt(self.path)
        self.assertEqual(before["rx_groups"], [])
        self.assertIn("1", before["read_notes"][0])
        edited = copy.deepcopy(before)
        edited["rx_groups"].append({"index": 1, "name": "Test", "contacts": []})
        with self.assertRaisesRegex(ValueError, "нераспознанные"):
            export_rdt(self.path, self.path.with_name("copy.rdt"), before, edited)
        self.assertEqual(self.path.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
