import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdt_editor import publish_new
from win_document import Document, _helper_path
from win_language import Language, system_language
import write_settings


@unittest.skipUnless(os.name == "posix", "POSIX desktop paths")
class LinuxFrontendTests(unittest.TestCase):
    def test_settings_use_xdg_and_desktop_message_language(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "linux"):
            home = Path(directory)
            base = home / "用户 настройки"
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base), "LC_ALL": "en_US.UTF-8",
                                         "LANGUAGE": "fr:zh_CN:en"}, clear=True):
                tr = Language()
                self.assertEqual(tr.code, "zh-Hans")
                tr.select("ru")
                self.assertTrue(tr.config.is_relative_to(base))
                self.assertEqual(Language().code, "ru")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative", "LC_MESSAGES": "ru_RU.UTF-8",
                                         "LC_ALL": "C", "LANGUAGE": "ru"}, clear=True), patch.object(Path, "home", return_value=home):
                self.assertEqual(system_language(), "en")
                del os.environ["LC_ALL"]
                self.assertEqual(system_language(), "ru")
                self.assertTrue(Language().config.is_relative_to(home / ".config"))

    def test_real_helper_is_relative_to_installation_and_linux_read_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "linux"):
            install = Path(directory).resolve()
            (install / "windows").mkdir()
            (install / "windows" / "read_usb").touch()
            with patch("win_document.__file__", str(install / "win_document.py")):
                with self.assertRaises(FileNotFoundError):
                    _helper_path()
                native = install / "read_usb"
                native.touch()
                self.assertEqual(_helper_path(), native)
                failure = subprocess.CompletedProcess([], 1, b"", b"open USB device: LIBUSB_ERROR_ACCESS (-3)")
                with patch("subprocess.run", return_value=failure) as runner:
                    for read in (Document.read_usb, write_settings.read_baseline):
                        with self.assertRaisesRegex(ValueError, "udev") as error:
                            read()
                        self.assertIn("USB access denied", Language("en").generated(str(error.exception)))
                        self.assertEqual(runner.call_args.args[0][0], str(native))

    def test_linux_programming_holds_inhibitor_only_for_bundled_writer(self):
        native = Path("/usr/lib/tyt-reader/read_usb")
        with patch.object(sys, "platform", "linux"), patch("win_document._helper_path", return_value=native), \
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as runner:
            write_settings._run_helper("--write-settings", data=b"synthetic")
            cmd = runner.call_args.args[0]
            self.assertEqual(cmd[0], "/usr/bin/systemd-inhibit")
            self.assertIn("--what=idle:sleep", cmd)
            self.assertEqual(cmd[-3:], ["--", str(native.resolve()), "--write-settings"])
            self.assertEqual(runner.call_args.kwargs["input"], b"synthetic")
            for mode, helper in (("--read-write-baseline", None), ("--write-settings", native)):
                write_settings._run_helper(mode, helper=helper)
                self.assertEqual(runner.call_args.args[0], [str(native.resolve()), mode])
            runner.return_value = subprocess.CompletedProcess([], 1, b"", b"Failed to inhibit: Access denied")
            with self.assertRaises(write_settings.SleepInhibitError):
                write_settings._run_helper("--write-settings", data=b"synthetic")
            runner.return_value = subprocess.CompletedProcess([], 1, b"", b"WRITE_PREFLIGHT\nWRITE_START 0x110000\nUSB disconnected\n")
            failed = write_settings._run_helper("--write-settings", data=b"synthetic")
            with self.assertRaisesRegex(ValueError, "частично"):
                write_settings._check_write_result(failed, [0x110000], "synthetic.tytbackup")

    @unittest.skipUnless(sys.platform == "linux", "native Linux renameat2")
    def test_linux_fat_publication_keeps_concurrent_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, output = root / "stage", root / "Копия.json"
            with patch("os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hard links")):
                stage.write_bytes(b"first")
                publish_new(stage, output)
                stage.write_bytes(b"second")
                with self.assertRaises(FileExistsError):
                    publish_new(stage, output)
                self.assertEqual(output.read_bytes(), b"first")
                self.assertEqual(stage.read_bytes(), b"second")
