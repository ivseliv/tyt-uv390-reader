import base64
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from win_services import CAL_CELLS, Calibration, DecFile, LegacyBank, matching_dec_value, status_report


class LocalServiceDocuments(unittest.TestCase):
    def test_dec_boundaries_unicode_and_opaque_fields(self):
        opaque = bytearray([0xdb]) * DecFile.size
        table = DecFile(opaque)
        for slot, base, size in [(1, 0, 64), (16, 1500, 64), (17, 1600, 32), (255, 17784, 32)]:
            original = table.data[:]
            sample = bytes(range(32))
            table.change(slot, 4, "测试🎙", sample.hex())
            self.assertEqual(table.row(slot), (4, "测试🎙"))
            self.assertEqual(table.value(slot), sample.hex().upper())
            self.assertEqual(table.data[base + 36:base + 68], sample[::-1])
            self.assertEqual(table.data[base + 1:base + 4], original[base + 1:base + 4])
            self.assertEqual(table.data[base + 36 + size:], original[base + 36 + size:])
            self.assertEqual(table.data[:base], original[:base])
        snapshot = table.data[:]
        for name in ["🎙" * 9, "A\nB"]:
            with self.assertRaises(ValueError):
                table.change(255, 4, name)
        with self.assertRaises(ValueError):
            table.change(17, 7, "Fails", "ab" * 64)
        self.assertEqual(snapshot, table.data)
        table.change(17, 4, "仅名称")
        self.assertEqual(table.value(17), sample.hex().upper())
        table.change(17, 0, "", clear=True)
        self.assertEqual(table.row(17), (0, ""))
        self.assertIsNone(table.value(17))

    def test_legacy_local_edits_and_usb_read_only(self):
        orig = bytearray([255]) * LegacyBank.file_size
        orig[0x125:0x12d] = b"MD-UV390"
        model = LegacyBank(data=orig)
        sample = bytes(range(1, 17)).hex().upper()
        model.change(1, 2, sample)
        self.assertEqual(model.value(1, 2), sample)
        off = 0x59c0 + 0x225
        self.assertEqual(model.data[off + 16:off + 32], bytes.fromhex(sample)[::-1])
        self.assertEqual(model.value(2, 1), sample + "FF" * 16)
        self.assertEqual(model.data[:off + 16], orig[:off + 16])
        self.assertEqual(model.data[off + 32:], orig[off + 32:])
        model.change(1, 2, "")
        self.assertEqual(model.data, orig)
        with self.assertRaises(ValueError):
            model.change(0, 1, "0000")
        model.change(0, 16, "AB12")
        bank = {"basic": base64.b64encode(model.data[off + 0x90:off + 0xb0]),
                "enhanced": base64.b64encode(model.data[off:off + 128])}
        read = LegacyBank(usb=bank)
        self.assertEqual(read.value(0, 16), "AB12")
        with self.assertRaises(ValueError):
            read.change(0, 16, "1234")
        with self.assertRaises(ValueError):
            LegacyBank(usb={"basic": b"!", "enhanced": b""})

    def test_calibration_roundtrip_offline_and_validation(self):
        blank = bytes([0xef]) * 8192
        profile = Calibration(blank, {"1.2": "145,51250", "3.1": "410", "12.9": "202", "47.5": "99.123"})
        binary = profile.native()
        decoded = Calibration(binary)
        self.assertEqual(decoded.original("1.2"), "145.51250")
        self.assertEqual(decoded.original("3.1"), "410.00000")
        self.assertEqual(decoded.original("12.9"), "202")
        self.assertEqual(decoded.original("47.5"), "")
        self.assertEqual(binary[:0x18], blank[:0x18])
        with TemporaryDirectory() as temp:
            sample = Path(temp) / "校准.json"
            sample.write_bytes(profile.json())
            opened = Calibration.open(sample)
            self.assertEqual(opened.template, blank)
            self.assertEqual(opened.values, profile.values)
            self.assertEqual(opened.native(), binary)
            doc = json.loads(sample.read_bytes())
            for field, wrong in (("values", {"99.2": "0"}), ("template", "ABC!"), ("version", True)):
                sample.write_text(json.dumps({**doc, field: wrong}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Calibration.open(sample)
        for key, bad in [("47.5", "100.05"), ("2.3", "1000"), ("12.1", "256"), ("47.5", "NaN")]:
            with self.assertRaises(ValueError):
                Calibration(blank, {key: bad})
        self.assertEqual({int(k.split(".")[0]) for k in CAL_CELLS}, set(range(1,49)))

    def test_status_export_whitelist(self):
        rows = [dict(slot=i, type=0 if i==2 else 4, name=f"S{i}", typeName="name", usbStatus="zeros",
                     unrelated="not-to-be-exported") for i in range(1,8)]
        report = dict(model="MD-UV390", slots=rows, other="private")
        filtered = status_report(report, "Слоты 1–4")
        self.assertEqual(set(filtered), {"slots", "model"})
        self.assertEqual([r["slot"] for r in filtered["slots"]], [1,2,3,4])
        self.assertTrue(all(set(r) == {"slot","name","type","typeName","usbStatus"} for r in filtered["slots"]))
        self.assertNotIn(2, [r["slot"] for r in status_report(report, "Активные")["slots"]])
        self.assertEqual(len(status_report(report)["slots"]), 7)

    def test_matching_local_dec_only(self):
        table = DecFile()
        row = {"slot": 1, "name": "专用", "type": 4}
        sample = bytes(range(32)).hex().upper()
        table.change(1, 4, "专用", sample)
        self.assertEqual(matching_dec_value(table, row), sample)
        for wrong in ({"name": "Public"}, {"type": 3}, {"slot": 2}, {"name": "\ufffd"}):
            with self.assertRaises(ValueError):
                matching_dec_value(table, {**row, **wrong})
        for byte in ("FF", "00"):
            table.change(1, 4, "专用", byte * 32)
            with self.assertRaises(ValueError):
                matching_dec_value(table, row)


@unittest.skipUnless(sys.platform == "win32", "native Windows Tk service dialogs")
class ServiceDialogs(unittest.TestCase):
    def test_windows_dialogs_and_metadata_export(self):
        import tkinter as tk
        from tkinter import ttk
        from win_services import show_calibration, show_dec, show_rdt_privacy, show_universal, show_usb_privacy

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        tr = lambda text: text

        def descendants(view):
            for child in view.winfo_children():
                yield child
                yield from descendants(child)

        def check_table(view, count):
            root.update_idletasks()
            tree = next(w for w in descendants(view) if isinstance(w, ttk.Treeview))
            self.assertEqual(len(tree.get_children()), count)

        dec = show_dec(root, tr)
        check_table(dec, 255)
        dec.destroy()
        calibration = show_calibration(root, tr)
        check_table(calibration, sum(cell[0] == "A" for cell in CAL_CELLS.values()))
        calibration.destroy()
        bank = {k: base64.b64encode(bytes(size)) for k, size in [("basic", 32), ("enhanced", 128)]}
        usb = show_usb_privacy(root, tr, bank)
        check_table(usb, 16)
        usb.destroy()
        with TemporaryDirectory() as temp:
            path = Path(temp) / "source.rdt"
            blank = bytearray(LegacyBank.file_size)
            blank[0x125:0x12d] = b"MD-UV390"
            path.write_bytes(blank)
            with patch("tkinter.filedialog.askopenfilename", return_value=str(path)):
                legacy = show_rdt_privacy(root, tr)
            check_table(legacy, 16)
            legacy.destroy()
            row = dict(slot=1, name="例", type=4, typeName="AES-256", usbStatus="zeros", extra="masked")
            universal = show_universal(root, tr, {"model": "MD-UV390", "slots": [row]})
            check_table(universal, 1)
            export = next(w for w in descendants(universal)
                          if isinstance(w, ttk.Button) and w.cget("text") == "Сохранить отчёт…")
            output = Path(temp) / "报告.json"
            with patch("tkinter.filedialog.asksaveasfilename", return_value=str(output)):
                export.invoke()
                saved = output.read_bytes()
                with patch("tkinter.messagebox.showerror") as error:
                    export.invoke()
                    error.assert_called_once()
                self.assertEqual(output.read_bytes(), saved)
                self.assertEqual(json.loads(saved)["slots"], [{k: v for k, v in row.items() if k != "extra"}])
            universal.destroy()
