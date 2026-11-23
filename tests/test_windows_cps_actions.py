from pathlib import Path
import tempfile
import unittest

from cps_dtmf import SEQUENCE_ID, sequence_section
from import_rdt import MODEL_OFFSET, RDT_SIZE
from win_document import Document
from win_cps_actions import add_slot, assignments, delete_slot, save_dtmf, set_assignment, slot_note, slot_number


def by_id(report, section):
    return next(s for s in report["cps_sections"] if s["id"] == section)["rows"]


class WindowsCPSActionsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.template = self.root / "шаблон.rdt"
        image = bytearray(RDT_SIZE)
        image[MODEL_OFFSET:MODEL_OFFSET+8] = b"MD-UV390"
        image[0x420:0x425] = b"other"
        self.template.write_bytes(image)

    def native(self):
        doc = Document.from_template(self.template)
        report = doc.draft()
        report["contacts"] = [{"index": 7, "name": "One", "type": "private", "dmr_id": 1234}]
        doc.apply(report)
        return doc

    def test_sparse_slot_numbers_and_safe_deletion(self):
        doc = self.native()
        self.assertEqual(add_slot(doc, "messages", text="Hello"), "Шаблон 1")
        self.assertEqual(add_slot(doc, "messages", text="СК"), "Шаблон 2")
        delete_slot(doc, "messages", "Шаблон 1")
        self.assertEqual(add_slot(doc, "messages", text="你好"), "Шаблон 1")
        self.assertEqual(add_slot(doc, "gps"), "GPS 1")
        row = add_slot(doc, "emergency", name="Служба")
        self.assertEqual(slot_number("emergency", row), 1)
        self.assertIsNone(slot_number("emergency", "Общие параметры"))
        with self.assertRaises(ValueError):
            delete_slot(doc, "emergency", "Общие параметры")
        dest = self.root / "new.rdt"
        doc.save_rdt(dest)
        actual = Document.open(dest).report
        self.assertEqual(by_id(actual, "messages")[0]["fields"][0]["value"], "你好")
        self.assertEqual(by_id(actual, "gps")[0]["name"], "GPS 1")
        self.assertIn(row, [r["name"] for r in by_id(actual, "emergency")])
        self.assertEqual(self.template.read_bytes()[0x420:0x425], b"other")

    def test_partial_usb_unknown_positions(self):
        doc = Document.new()
        report = doc.draft()
        report.update(usb_model="MD-UV390", read_notes=["Не удалось полностью считать блок Privacy по USB."])
        doc.apply(report)
        self.assertIn("11–32", slot_note(doc.report, "emergency"))
        self.assertEqual(add_slot(doc, "emergency"), "№ 11 · Система 11")
        self.assertEqual(add_slot(doc, "messages"), "Шаблон 1")
        self.assertIsNone(assignments(doc.report, "numberKeys")[0]["contact_index"])
        self.assertEqual(assignments(doc.report, "quick")[0]["quick_type"], "Нет данных")
        row = assignments(doc.report, "numberKeys")[0]
        set_assignment(doc, "numberKeys", dict(row, contact_index=0))
        self.assertEqual(doc.report["local_empty_slots"], {"numberKeys": [1]})
        self.assertEqual(assignments(doc.report, "numberKeys")[0]["contact_index"], 0)
        self.assertIsNone(assignments(doc.report, "numberKeys")[1]["contact_index"])

    def test_quick_number_assignments_reference_protection_and_copy(self):
        doc = self.native()
        sms = add_slot(doc, "messages", text="Online")
        quick = assignments(doc.report, "quick")[4]
        set_assignment(doc, "quick", dict(quick, quick_type="Сообщение", contact_index=7, message_slot=1))
        set_assignment(doc, "numberKeys", dict(assignments(doc.report, "numberKeys")[9], contact_index=7))
        with self.assertRaises(ValueError):
            delete_slot(doc, "messages", sms)
        baseline = doc.draft()
        with self.assertRaises(ValueError):
            set_assignment(doc, "quick", dict(quick, quick_type="Вызов", contact_index=409))
        self.assertEqual(doc.report, baseline)
        dest = self.root / "buttons.rdt"
        doc.save_rdt(dest)
        saved = Document.open(dest)
        self.assertEqual(assignments(saved.report, "quick")[4]["quick_type"], "Сообщение")
        self.assertEqual(assignments(saved.report, "numberKeys")[9]["contact_index"], 7)
        set_assignment(saved, "quick", dict(quick, quick_type="DTMF", profile="DTMF-3", encode_slot=0))
        delete_slot(saved, "messages", sms)
        next_copy = self.root / "dtmf-button.rdt"
        saved.save_rdt(next_copy)
        self.assertEqual(assignments(Document.open(next_copy).report, "quick")[4]["profile"], "DTMF-3")

    def test_only_dtmf_export_inherits_template(self):
        original = self.native()
        edit = original.draft()
        by_id(edit, SEQUENCE_ID)[1]["fields"][0]["value"] = "123*"
        original.apply(edit)
        template = self.root / "existing.rdt"
        original.save_rdt(template)
        doc = Document.new()
        edit = doc.draft()
        edit["dmr_id"] = 999
        edit["cps_sections"] = [sequence_section()]
        by_id(edit, SEQUENCE_ID)[0]["fields"][0]["value"] = "44#"
        doc.apply(edit)
        dest = self.root / "DTMF.rdt"
        save_dtmf(doc, template, dest)
        baseline = Document.open(template).report
        actual = Document.open(dest).report
        self.assertEqual(actual["dmr_id"], baseline["dmr_id"])
        self.assertEqual(actual["contacts"], baseline["contacts"])
        self.assertEqual(by_id(actual, SEQUENCE_ID)[0]["fields"][0]["value"], "44#")
        self.assertEqual(by_id(actual, SEQUENCE_ID)[1]["fields"][0]["value"], "123*")
        self.assertEqual(template.read_bytes()[0x420:0x425], dest.read_bytes()[0x420:0x425])
        self.assertEqual(doc.report["dmr_id"], 999)
        with self.assertRaises(FileExistsError):
            save_dtmf(doc, template, dest)


if __name__ == "__main__":
    unittest.main()
