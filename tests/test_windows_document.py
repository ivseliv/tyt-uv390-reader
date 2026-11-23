import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from import_rdt import MODEL_OFFSET, RDT_SIZE
from win_document import Document, read_universal_usb, save_new


class WindowsDocumentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def save_fixture(self, name, report):
        path = self.root / name
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return path

    def test_open_and_save_unicode_core_tables_and_formats(self):
        doc = Document.new()
        report = doc.draft()
        report.update(dmr_id=123, channels=[{"name": "信道", "index": 1, "mode": "analog",
                                           "rx_hz": 145500000, "tx_hz": 145500000, "bandwidth_code": 0}],
                      zones=[{"index": 2, "name": "Зона", "A": [{"index": 1}], "B": []}],
                      contacts=[{"index": 1, "name": "連絡", "type": "group", "dmr_id": 42, "ring": False}])
        doc.apply(report)
        self.assertEqual(doc.report["zones"][0]["A"][0]["name"], "信道")
        json_path = self.root / "копия.json"
        doc.save_json(json_path)
        self.assertEqual(Document.open(json_path).report, doc.report)
        with self.assertRaises(ValueError):
            Document.open(json_path).save_json(json_path)

        csv_path = self.root / "联系人.csv"
        doc.save_csv(csv_path, "contacts")
        csv = Document.open(csv_path)
        self.assertEqual(csv.report["contacts"][0]["name"], "連絡")
        blank = Document.new()
        blank.merge_csv(csv_path)
        self.assertEqual(blank.report["contacts"][0]["dmr_id"], 42)
        yaml_path = self.root / "副本.yaml"
        doc.save_yaml(yaml_path)
        self.assertEqual(Document.open(yaml_path).report["channels"][0]["name"], "信道")

        invalid = doc.draft()
        invalid["channels"].clear()
        with self.assertRaises(ValueError):
            doc.apply(invalid)
        self.assertEqual(doc.report["channels"][0]["index"], 1)

    def test_open_old_json_and_reject_unrelated_json(self):
        report = Document.new().report
        report.update(usb_model="MD-UV390", rx_groups=[{"index": 51, "name": "Broken", "contacts": [10001]},
                                                       {"index": 54, "name": "良", "contacts": []}],
                      scan_lists=None, unrecognized_rx_group_slots=[50])
        old = self.save_fixture("old.json", report)
        loaded = Document.open(old)
        self.assertEqual(loaded.report["unrecognized_rx_group_slots"], [50, 51])
        self.assertEqual([r["index"] for r in loaded.report["rx_groups"]], [54])
        self.assertIn("51", loaded.report["read_notes"][0])
        self.assertEqual(json.loads(old.read_bytes()), report)
        for data in ({"foo": []}, [], {**Document.new().report, "usb_privacy": {"basic": "sensitive"}},
                     {**Document.new().report, "zones": [{"index": 2, "name": "", "A": [3], "B": []}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                Document.open(self.save_fixture("bad.json", data))

    def test_rdt_edits_and_template_keep_opaque_data_and_original(self):
        data = bytearray(RDT_SIZE)
        data[MODEL_OFFSET:MODEL_OFFSET + 8] = b"MD-UV390"
        data[0x410:0x416] = b"opaque"
        original = self.root / "исходник.rdt"
        original.write_bytes(data)
        doc = Document.from_template(original)
        for name in ("Один", "Два"):
            edit = doc.draft()
            edit["contacts"] = [{"index": 2, "name": name, "type": "group", "dmr_id": 42, "ring": False}]
            doc.apply(edit)
        dest = self.root / "本地.rdt"
        doc.save_rdt(dest)
        self.assertEqual(Document.open(dest).report["contacts"][0]["name"], "Два")
        self.assertEqual(dest.read_bytes()[0x410:0x416], b"opaque")
        self.assertEqual(original.read_bytes(), data)
        transferred = Document.new()
        incoming = transferred.draft()
        incoming["contacts"] = doc.report["contacts"]
        incoming["dmr_id"] = 23
        transferred.apply(incoming)
        copy = self.root / "template-copy.rdt"
        transferred.save_rdt(copy, template=original)
        self.assertEqual(Document.open(copy).report["dmr_id"], 23)
        self.assertEqual(copy.read_bytes()[0x410:0x416], b"opaque")

    def test_no_overwrite_even_in_a_race(self):
        path = self.root / "сохранение.json"
        def attempt(data):
            try:
                save_new(path, data)
                return data
            except FileExistsError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = [r for r in pool.map(attempt, (b"one", b"two")) if r is not None]
        self.assertEqual(results, [path.read_bytes()])
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_usb_boundary_private_bank_is_never_persisted(self):
        envelope = {"settings": Document.new().report, "usb_privacy": {"basic": "synthetic-secret"}}
        done = subprocess.CompletedProcess([], 0, stdout=b"synthetic-response", stderr=b"")
        with patch("win_document.subprocess.run", return_value=done) as run, patch("win_document.decode", return_value=envelope):
            doc = Document.read_usb(helper=self.root/"read_usb.exe")
        self.assertEqual(run.call_args.args[0][-1], "--include-privacy")
        self.assertEqual(doc.usb_privacy, envelope["usb_privacy"])
        dest = self.root / "снимок.json"
        doc.save_json(dest)
        self.assertEqual(json.loads(dest.read_bytes()), envelope["settings"])
        self.assertIsNone(Document.open(dest).usb_privacy)

    def test_stock_usb_metadata_only_and_incomplete_response(self):
        rows = bytearray(255 * 34)
        rows[0] = 4
        name = "Слот一".encode("utf-16le")
        rows[1:1+len(name)] = name
        rows[33] = 1
        payload = b"MD-UV390" + rows
        done = subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")
        with patch("win_document.subprocess.run", return_value=done) as run:
            report = read_universal_usb(helper=self.root / "read_usb.exe")
        self.assertEqual(run.call_args.args[0][-1], "--universal-privacy")
        self.assertEqual(len(report["slots"]), 255)
        self.assertEqual(report["slots"][0], {"slot": 1, "name": "Слот一", "type": 4,
                                              "typeName": "AES-256", "usbStatus": "zeros"})
        for payload in (payload[:-1], payload + b"!", payload[:41] + b"\xff" + payload[42:]):
            with patch("win_document._usb_read", return_value=payload), self.assertRaises(ValueError):
                read_universal_usb()
        done = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"USB 0483:df11 not found.")
        with patch("win_document.subprocess.run", return_value=done), self.assertRaisesRegex(ValueError, "USB"):
            read_universal_usb(helper=self.root / "read_usb.exe")


if __name__ == "__main__":
    unittest.main()
