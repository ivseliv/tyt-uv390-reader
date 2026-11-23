import unittest
from pathlib import Path
from unittest.mock import patch
try:
    import tkinter
except ImportError:
    tkinter = None

from win_language import Language
import win_program


class WindowsProgramTests(unittest.TestCase):
    def setUp(self):
        self.report = {k: [] for k in win_program.TABLES}
        self.report.update(file_model="MD-UV390", dmr_id=123)
        self.proposal = {"model": "MD-UV390", "dmr_id": 123, "blocks": 1, "sectors": 1,
                         "changes": {k: int(k == "channels") for k in win_program.TABLES}}

    def test_write_enabled_only_for_a_complete_model_bound_document(self):
        self.assertTrue(win_program.eligible(self.report))
        for updates in ({"file_model": "unknown"}, {"csv_source": {}}, {"dmr_id": None},
                        {"contacts": None}, {"usb_model": "MD-UV380"}):
            self.assertFalse(win_program.eligible(dict(self.report, **updates)))

    @unittest.skipUnless(tkinter, "Tk is required for Windows dialog tests")
    @patch("tkinter.messagebox", create=True)
    @patch("tkinter.filedialog", create=True)
    def test_cancel_save_or_final_confirmation_and_review_in_all_languages(self, files, alert):
        for lang in ("ru", "en", "zh-Hans"):
            tr = Language(lang)
            files.asksaveasfilename.return_value = ""
            self.assertIsNone(win_program.review(None, tr, self.proposal, "source"))
            alert.askyesno.assert_not_called()
            files.asksaveasfilename.return_value = "new.tytbackup"
            alert.askyesno.return_value = False
            self.assertIsNone(win_program.review(None, tr, self.proposal, "source"))
            alert.askyesno.return_value = True
            self.assertEqual(win_program.review(None, tr, self.proposal, "source"), Path("new.tytbackup"))
            args, kwargs = alert.askyesno.call_args
            self.assertEqual(kwargs["default"], alert.NO)
            self.assertIn(tr("Каналы") + " — 1", args[1])
            for substring in ("MD-UV390", "123", "new.tytbackup", "D219.043"):
                self.assertIn(substring, " ".join(args))
            alert.reset_mock()
        files.reset_mock()
        self.assertIsNone(win_program.review(None, Language("ru"), dict(self.proposal, blocks=0), "source"))
        files.asksaveasfilename.assert_not_called()
        alert.askyesno.assert_not_called()
