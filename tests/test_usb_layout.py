import base64
import json
import re
import struct
import unittest
from pathlib import Path

from cps_channel import channel_details, patch_channel_details
from read_settings import (BASIC_REGION, ENHANCED_REGION, PRIVACY_REGIONS, PUBLIC_REGIONS,
                           REGIONS, SHARED_PUBLIC_REGIONS, SHARED_UNAVAILABLE, decode)


class StockReaderLayout(unittest.TestCase):
    def test_reader_and_decoder_agree_on_public_and_optional_ranges(self):
        c = (Path(__file__).resolve().parents[1] / "read_usb.c").read_text()
        def ranges(table_name):
            table = c.split(f" {table_name}[] = {{", 1)[1].split("};", 1)[0]
            return tuple((int(a, 16), int(b, 16)) for a, b in re.findall(
                r"\{(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\}", table))
        self.assertEqual(ranges("regions"), REGIONS + PUBLIC_REGIONS)
        self.assertEqual(ranges("shared_regions"), SHARED_PUBLIC_REGIONS + PRIVACY_REGIONS)
        self.assertTrue(all(0x5800 <= a < b <= 0x5c00 for a, b in ranges("shared_regions")))

    @staticmethod
    def payload(layout, fields=(), has_channel=True):
        fields = list(fields)
        if has_channel:
            fields.extend(((0x110000, b"\x01"), (0x110020, "A".encode("utf-16le"))))
        payload = bytearray(b"MD-UV390")
        for start, end in layout:
            block = bytearray(end - start)
            for address, value in fields:
                lo, hi = max(start, address), min(end, address + len(value))
                if lo < hi:
                    block[lo-start:hi-start] = value[lo-address:hi-address]
            payload.extend(struct.pack("<II", start, len(block)))
            payload.extend(block)
        return bytes(payload)

    def test_legacy_helpers_and_unavailable_shared_block(self):
        old = self.payload(REGIONS + PUBLIC_REGIONS)
        self.assertNotIn("cps_sections", decode(self.payload(REGIONS)))
        report = decode(old, include_privacy=True)
        self.assertNotIn("usb_privacy", report)
        self.assertIn("USB-помощника", report["settings"]["read_notes"][0])
        denied = old + struct.pack("<II", *SHARED_UNAVAILABLE)
        report = decode(denied, include_privacy=True)
        self.assertNotIn("usb_privacy", report)
        self.assertEqual(report["settings"]["channels"][0]["name"], "A")
        self.assertIn("Не удалось полностью считать", report["settings"]["read_notes"][0])
        for invalid in (old[:-1], denied[:-1], denied + b"?", old + struct.pack("<II", 0, 0)):
            with self.assertRaises(ValueError):
                decode(invalid)

    def test_shared_records_and_private_output(self):
        enhanced, basic = bytes(range(128)), bytes(range(100, 132))
        fields = [(ENHANCED_REGION[0], enhanced), (BASIC_REGION[0], basic)]
        for slot in (49, 50):
            fields.append((0x2180 + (slot-1)*0x120, f"SMS {slot}".encode("utf-16le")))
        for slot in (1, 10, 11):
            address = 0x5a80 + (slot-1)*40
            fields.extend(((address, f"Alarm {slot}".encode("utf-16le")), (address+36, b"\x01\x00")))
        public = REGIONS + PUBLIC_REGIONS + SHARED_PUBLIC_REGIONS
        payload = self.payload(public + PRIVACY_REGIONS, fields)
        report = decode(payload, include_privacy=True)
        snapshot = report["settings"]
        self.assertEqual(snapshot, decode(self.payload(public, fields)))
        self.assertEqual(snapshot, decode(payload))
        self.assertNotIn("read_notes", snapshot)
        for key, raw in (("enhanced", enhanced), ("basic", basic)):
            self.assertEqual(base64.b64decode(report["usb_privacy"][key]), raw)
            self.assertNotIn(report["usb_privacy"][key], json.dumps(snapshot))
        sections = {section["id"]: section["rows"] for section in snapshot["cps_sections"]}
        self.assertEqual([row["fields"][0]["value"] for row in sections["messages"]], ["SMS 49", "SMS 50"])
        self.assertEqual([row["name"] for row in sections["emergency"]][1:],
                         ["№ 1 · Alarm 1", "№ 10 · Alarm 10", "№ 11 · Alarm 11"])
        with self.assertRaises(ValueError):
            decode(self.payload(public), include_privacy=True)
        with self.assertRaises(ValueError):
            decode(payload[:-1])

    def test_blank_codeplug_with_known_layout_can_contain_privacy(self):
        public = REGIONS + PUBLIC_REGIONS + SHARED_PUBLIC_REGIONS
        report = decode(self.payload(public + PRIVACY_REGIONS, has_channel=False), include_privacy=True)
        self.assertEqual(report["settings"]["channels"], [])
        self.assertEqual(base64.b64decode(report["usb_privacy"]["enhanced"]), bytes(128))
        with self.assertRaises(ValueError):
            decode(self.payload(REGIONS, has_channel=False))
        with self.assertRaises(ValueError):
            decode(self.payload(public, [(0x110020, b"\x01\x00")], has_channel=False))

    def test_channel_edit_preserves_every_other_bit(self):
        record = bytearray(64)
        record[0] = 2
        record[30] = 0xfe
        orig = channel_details(record)
        modified = [dict(f) for f in orig]
        next(f for f in modified if f["label"] == "Мощность")["value"] = "High"
        expected = bytearray(record)
        expected[30] = 0xff
        patch_channel_details(record, orig, modified)
        self.assertEqual(record, expected)
