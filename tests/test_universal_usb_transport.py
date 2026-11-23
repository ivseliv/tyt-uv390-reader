import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class UniversalUSBTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("TYT_TEST_USB_MOCK"):
            cls.program = Path(os.environ["TYT_TEST_USB_MOCK"]).resolve(strict=True)
            return
        if not shutil.which("cc") or not shutil.which("pkg-config"):
            raise unittest.SkipTest("C compiler and pkg-config required for USB transport tests")
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "libusb-1.0"], capture_output=True, text=True)
        if flags.returncode:
            raise unittest.SkipTest("libusb headers required for USB transport tests")
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.program = Path(cls.scratch.name) / "mock"
        source = Path(__file__).with_name("universal_usb_mock.c")
        subprocess.run(["cc", "-std=c17", "-Wall", "-Wextra", "-Werror", str(source),
                        *shlex.split(flags.stdout), "-o", str(cls.program)], check=True)

    def test_metadata_only(self):
        radio_data = bytearray([0x5e]) * 20480
        expected = bytearray(b"MD-UV390")
        offset = 0
        for slot in range(1, 256):
            capacity = 64 if slot <= 16 else 32
            # LF and Ctrl-Z are data, including when a Windows CRT pipe is used.
            name = ("USB\n\x1a" if slot == 1 else f"Слот {slot}").encode("utf-16le").ljust(32, b"\0")
            # Include key padding that differs from the meaningful bytes.
            kind, width, fill, status = ((0, 0, 0x11, 0), (1, 5, 0, 1), (2, 8, 255, 2),
                                         (3, 16, 0x66, 3), (6, 64, 0x88, 3))[slot % 5]
            if width > capacity:
                status = 0
            radio_data[offset] = kind
            radio_data[offset + 4:offset + 36] = name
            used = min(width, capacity)
            radio_data[offset + 36:offset + 36 + used] = bytes([fill]) * used
            offset += 36 + capacity
            expected.extend(bytes([kind]) + name + bytes([status]))
        for mode in ("success", "end-status-stall", "end-disconnect"):
            with self.subTest(mode=mode):
                result = subprocess.run([self.program, mode], input=radio_data, capture_output=True, check=True)
                self.assertEqual(result.stdout, expected)
                if mode == "success":
                    self.assertEqual(result.stderr, b"")
                else:
                    self.assertIn(b"after accepted end command", result.stderr)

    def test_device_refusal_and_incomplete_table(self):
        for mode, confirmed in (("deny", 0), ("begin-stall", 0), ("short", 6),
                                ("end-out-stall", 20), ("end-timeout", 20)):
            with self.subTest(mode=mode):
                result = subprocess.run([self.program, mode], input=bytes(20480), capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(f"{confirmed}/20 blocks confirmed".encode(), result.stderr)
