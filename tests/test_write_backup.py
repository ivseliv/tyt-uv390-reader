import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import write_backup
import write_settings


class RestoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "radio.tytbackup"
        self.before = dict.fromkeys(write_settings.ADDRESSES, bytes(1024))
        self.after = self.before.copy()
        for addr in (0x110000, 0x120000, 0x140000):
            self.after[addr] = bytes(40) + b"A" + bytes(983)
        self.pairs = {a: (data, self.after[a]) for a, data in self.before.items()}
        write_backup.save_backup(self.path, "MD-UV390", self.pairs)

    def restore(self, state, model="MD-UV390"):
        with patch("write_settings.read_baseline", return_value=(model, bytes(12), state)):
            return write_backup.prepare_restore(self.path)

    def test_complete_and_unstarted_sectors_can_be_undone(self):
        self.assertEqual(self.restore(self.after)["sectors"], 3)
        self.assertEqual(self.restore(self.before)["blocks"], 0)
        some = dict(self.before)
        some[0x120000] = self.after[0x120000]
        self.assertEqual(self.restore(some)["changes"]["channels"], 1)
        self.assertEqual(self.restore(some)["sectors"], 1)

    def test_protects_other_devices_and_subsequent_edits(self):
        with self.assertRaises(ValueError):
            self.restore(self.after, "MD-UV380")
        missing = self.after.copy()
        del missing[0x2000]
        with self.assertRaises(ValueError):
            self.restore(missing)
        partial = self.after.copy()
        partial[0x110400] = bytes([255]) * 1024
        with self.assertRaises(ValueError):
            self.restore(partial)

    def test_backup_rejects_corruption_before_usb_and_is_exclusive(self):
        original = self.path.read_bytes()
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_backup.save_backup(self.path, "MD-UV390", self.pairs)
        self.assertEqual(self.path.read_bytes(), original)
        for corrupt in (original[:-1], original[:-1] + bytes([original[-1] ^ 1]), original + b"x"):
            self.path.write_bytes(corrupt)
            with patch("write_settings.read_baseline") as read, self.assertRaises(ValueError):
                write_backup.prepare_restore(self.path)
            read.assert_not_called()
