import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import write_settings


class StockWriteTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("TYT_TEST_WRITE_MOCK"):
            cls.mock = Path(os.environ["TYT_TEST_WRITE_MOCK"]).resolve(strict=True)
        else:
            if not shutil.which("cc") or not shutil.which("pkg-config"):
                raise unittest.SkipTest("C compiler and pkg-config required")
            flags = subprocess.run(["pkg-config", "--cflags", "--libs", "libusb-1.0"],
                                   capture_output=True, text=True)
            if flags.returncode:
                raise unittest.SkipTest("libusb headers required")
            scratch = tempfile.TemporaryDirectory()
            cls.addClassCleanup(scratch.cleanup)
            cls.mock = Path(scratch.name) / "stock_mock"
            subprocess.run(["cc", "-std=c17", "-Wall", "-Wextra", "-Werror", str(
                Path(__file__).with_name("stock_write_mock.c")), *shlex.split(flags.stdout),
                            "-o", str(cls.mock)], check=True)
        cls.baseline = subprocess.run([cls.mock, "baseline"], capture_output=True, check=True).stdout
        header = struct.unpack_from("<8s8s12sI", cls.baseline)
        assert header == (b"TYTBAS01", b"MD-UV390", bytes((1, 2, 2, 4, 1)) + bytes(7), 710)
        assert len(cls.baseline) == 32 + 710 * 1028
        cls.records = dict(struct.iter_unpack("<I1024s", cls.baseline[32:]))

    def plan(self, updates=()):
        after = {a: bytearray(v) for a, v in self.records.items()}
        for addr, value in updates:
            after[addr & ~1023][addr % 1024] = value
        p = bytearray(b"TYTWRT01" + self.baseline[8:32])
        for addr, before in self.records.items():
            p += struct.pack("<I", addr) + before + after[addr]
        return p

    def run_case(self, mode, data, result, counts):
        p = subprocess.run([self.mock, mode], input=data, capture_output=True)
        self.assertEqual(p.returncode, result, p.stderr)
        self.assertEqual(p.stdout, b"")
        logs = p.stderr.decode().splitlines()
        counters = list(map(int, logs[-1].removeprefix("MOCK_RESULT ").split()))
        self.assertEqual(counters, counts, logs)
        self.assertEqual(sum(s.startswith("WRITE_COMPLETE ") for s in logs), int(result == 0))
        return logs

    def test_guards_and_preservation(self):
        self.assertEqual(self.records[0x2000], bytes(132) + bytes((26, 10)) + bytes(890))
        self.assertEqual(self.records[0xec00][:32], bytes(32))
        actual = list(self.records)
        expected = [0x2000, *range(0xec00, 0x10000, 1024), *range(0x10000, 0x20000, 1024),
                    *range(0x30000, 0x40000, 1024), *range(0x110000, 0x1a0000, 1024)]
        self.assertEqual(actual, expected)
        self.run_case("noop", self.plan(), 0, [0, 0, 710, 0])
        p = self.plan([(0x10008, 66), (0x140027, 0xc1), (0x140028, 65)])
        for mode in ("success", "end-disconnect"):
            with self.subTest(mode=mode):
                logs = self.run_case(mode, p, 0, [2, 128, 966, 1])
                self.assertIn("WRITE_VERIFIED 0x010000", logs)
                self.assertIn("WRITE_VERIFIED 0x140000", logs)

    def test_preflight_rejects_malformed_and_forbidden_changes(self):
        p = self.plan([(0x110020, 90)])
        for corrupt in (p[:-1], p + b"!", b"B" + p[1:], p[:8] + b"MD-UV380" + p[16:],
                        p[:16] + bytes(12) + p[28:], p[:28] + bytes(4) + p[32:],
                        p[:32] + bytes(4) + p[36:]):
            self.run_case("malformed", corrupt, 1, [0, 0, 0, 0])
        for addr, value in ((0x2084, 1), (0xfc30, 1), (0x1edf0, 1), (0x30000, 1),
                            (0x13ee00, 1), (0x197e40, 1), (0x110000, 0x20),
                            (0x140003, 0x80), (0x18860 + 38, 0xf1)):
            with self.subTest(address=hex(addr)):
                self.run_case("forbidden", self.plan([(addr, value)]), 1, [0, 0, 0, 0])

    def test_errors_stop_without_retries(self):
        plan = self.plan([(0x149e0, 81), (0x110020, 69)])
        scenarios = (("short-read", [0, 0, 710, 0]), ("stale", [0, 0, 710, 0]),
                     ("late-stale", [0, 0, 711, 0]), ("refuse-erase", [1, 0, 774, 0]),
                     ("short-program", [1, 6, 774, 0]), ("bad-status", [1, 1, 774, 0]),
                     ("bad-readback", [1, 64, 838, 0]), ("second-stale", [1, 64, 839, 0]))
        for mode, counts in scenarios:
            with self.subTest(mode=mode):
                self.run_case(mode, plan, 1, counts)

    def test_document_plan_programs_all_five_tables_in_native_simulator(self):
        model, binding, before = write_settings.decode_baseline(self.baseline)
        source = {
            "file_model": model, "dmr_id": int.from_bytes(before[0x2000][0x84:0x87], "little"),
            "contacts": [{"index": 2, "name": "Group", "type": "group", "dmr_id": 1234}],
            "channels": [{"index": 2, "name": "Digital", "mode": "DMR", "rx_hz": 433500000,
                          "tx_hz": 433500000, "contact_index": 2, "rx_group_index": 54}],
            "zones": [{"index": 2, "name": "AB", "A": [{"index": 2}], "B": [{"index": 2}]}],
            "rx_groups": [{"index": 54, "name": "Members", "contacts": [2]}],
            "scan_lists": [{"index": 2, "name": "Scan", "channels": [2]}],
        }
        proposal = write_settings.plan_settings(source, (model, binding, before))
        self.assertEqual(proposal["changes"],
                         {"contacts": 1, "channels": 1, "zones": 1, "rx_groups": 1, "scan_lists": 1, "dmr_id": 0})
        self.assertEqual(proposal["sectors"], 4)
        def program_simulator(mode, *, helper, data):
            self.assertEqual(mode, "--write-settings")
            return subprocess.run([self.mock, "success"], input=data, capture_output=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(write_settings, "_run_helper", program_simulator):
            backup = Path(tmp) / "saved.tytbackup"
            write_settings.apply(proposal, backup=backup)
            self.assertTrue(backup.is_file())
