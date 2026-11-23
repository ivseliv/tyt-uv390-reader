import base64
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import write_settings as writer
from rdt_editor import EMPTY_CHANNEL


MODEL = "MD-UV390"
BINDING = bytes(range(12))


def baseline():
    blocks = {a: bytes(1024) for a in writer.ADDRESSES}
    guard = bytearray(1024)
    guard[0x84:0x87] = (42).to_bytes(3, "little")
    blocks[0x2000] = bytes(guard)
    return MODEL, BINDING, blocks


def report(**updates):
    result = {table: [] for table in writer.TABLES}
    result.update(usb_model=MODEL, dmr_id=42)
    result.update(updates)
    return result


def actual(proposal):
    model, binding, before, after = writer.decode_plan(base64.b64decode(proposal["plan"]))
    return writer._decode_image(writer._make_image(after)), (model, binding, after)


def octets(original):
    model, binding, blocks = original
    header = writer.HEADER.pack(writer.BASE_MAGIC, model.encode(), binding, len(blocks))
    return header + b"".join(struct.pack("<I", addr) + blocks[addr] for addr in writer.ADDRESSES)


class WritePlanTests(unittest.TestCase):
    def test_add_roundtrip_all_tables_and_zone_extension(self):
        contacts = [{"index": 10000, "name": "中文", "type": "group", "dmr_id": 17, "ring": True}]
        channels = [{"index": number, "name": f"Тест{number}", "rx_hz": 430000000, "tx_hz": 430000000,
                     "mode": "DMR", "contact_index": 10000} for number in range(1, 19)]
        zones = [{"index": 250, "name":"Тест", "A":[{"index": n} for n in range(1, 19)],
                  "B":[{"index": 17}]}]
        groups = [{"index":54, "name":"Group", "contacts":[10000]}]
        scans = [{"index":250, "name":"Scan", "channels":[18], "primary":0}]
        proposal = writer.plan_settings(report(channels=channels, contacts=contacts, zones=zones,
                                               rx_groups=groups, scan_lists=scans), baseline())
        self.assertEqual(proposal["changes"], {"channels":18,"contacts":1,"zones":1,"rx_groups":1,"scan_lists":1,"dmr_id":0})
        decoded, connected = actual(proposal)
        self.assertEqual(decoded["contacts"][0]["name"], "中文")
        self.assertEqual(decoded["channels"][0]["contact_index"], 10000)
        self.assertEqual(decoded["zones"][0]["A"][-1]["index"], 18)
        self.assertEqual(decoded["zones"][0]["B"][0]["index"], 17)
        repeated = writer.plan_settings(dict(decoded, file_model=MODEL), connected)
        self.assertEqual(repeated["blocks"], 0)
        self.assertEqual(repeated["sectors"], 0)
        self.assertTrue(all(count == 0 for count in repeated["changes"].values()))

    def test_rename_preserves_other_device_slots_and_reserved_bytes(self):
        model, binding, blocks = baseline()
        channel = bytearray(EMPTY_CHANNEL)
        channel[21:24] = b"\x00\x00\x40"
        channel[32:34] = "A".encode("utf-16le")
        channel[7] = 0x39  # retained unused high byte for the analogue channel's digital contact
        channel[29] = 0x03
        block = bytearray(blocks[0x110000])
        block[:64] = channel
        block[64:128] = channel
        block[96:98] = b"B\0"
        blocks[0x110000] = bytes(block)
        doc = writer._decode_image(writer._make_image(blocks))
        doc["usb_model"] = model
        doc["channels"] = [doc["channels"][0]]
        doc["channels"][0]["name"] = "C"
        p = writer.plan_settings(doc, (model,binding,blocks))
        decoded, (_, _, after) = actual(p)
        self.assertEqual([c["name"] for c in decoded["channels"]], ["C","B"])
        self.assertEqual(after[0x110000], bytes(block[:32]) + b"C" + bytes(block[33:]))
        self.assertEqual(p["changes"]["channels"], 1)
        self.assertEqual((p["blocks"],p["sectors"]),(1,1))

    def test_edit_only_named_channel_detail_and_reject_invalid_reference(self):
        ch = {"index":1,"name":"Test","mode":"analog","rx_hz":400000000,"tx_hz":400000000}
        p = writer.plan_settings(report(channels=[ch]),baseline())
        doc, connected = actual(p)
        doc["file_model"] = MODEL
        detail = next(f for f in doc["channels"][0]["details"] if f["label"] == "Только приём")
        detail["value"] = "Вкл."
        changed, (_, _, after) = actual(writer.plan_settings(doc, connected))
        self.assertEqual(changed["channels"][0]["details"],doc["channels"][0]["details"])
        old = connected[2][0x110000]
        self.assertEqual([i for i,(x,y) in enumerate(zip(old,after[0x110000])) if x != y], [1])
        self.assertEqual(old[1] ^ after[0x110000][1], 2)
        doc["channels"][0]["scan_list_index"] = 1
        with self.assertRaises(ValueError):
            writer.plan_settings(doc, connected)

    def test_malformed_partial_protected_and_wrong_model_rejected(self):
        model,binding,blocks = baseline()
        # Visibly unrecognized entry occupies the first slot we can otherwise write.
        occupied = bytearray(blocks[0x10000])
        occupied[:2] = b"\x01\0"
        blocks[0x10000] = bytes(occupied)
        bad_inputs = [report(dmr_id=43), report(file_model="MD-UV380"),
                      report(usb_model="unknown"), report(contacts={}), report(csv_source={}),
                      report(rx_groups=[{"index":53,"name":"Reserved","contacts":[]}]),
                      report(rx_groups=[{"index":54,"name":"Damaged","contacts":[]}]),
                      report(unrecognized_rx_group_slots=[55], rx_groups=[{"index":55,"name":"Unseen","contacts":[]}]),
                      report(contacts=[{"index":1,"name":"Private","type":"private","dmr_id":1}],
                             rx_groups=[{"index":55,"name":"Invalid","contacts":[1]}])]
        missing = report()
        del missing["scan_lists"]
        bad_inputs.append(missing)
        for doc in bad_inputs:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                writer.plan_settings(doc,(model,binding,blocks))
        # A partial snapshot which avoids the unreadable record is safe.
        self.assertEqual(writer.plan_settings(report(unrecognized_rx_group_slots=[54]),(model,binding,blocks))["blocks"], 0)

    def test_decode_masks_guards_and_requires_exact_shape(self):
        model,binding,blocks = baseline()
        binary = octets((model,binding,blocks))
        self.assertEqual(writer.decode_baseline(binary),(model,binding,blocks))
        for data in (binary[1:],binary+b"x",bytes([0])+binary[1:],binary[:32]+bytes(4)+binary[36:]):
            with self.assertRaises(ValueError):
                writer.decode_baseline(data)
        blocks[0x2000] = bytes([3]) + bytes(1023)
        with self.assertRaises(ValueError):
            writer.decode_baseline(octets((model,binding,blocks)))

    def test_plan_rejects_changes_outside_core(self):
        model,binding,blocks = baseline()
        for addr, offset in ((0x2000,0x84),(0xEC00,31),(0x13FC00,10),(0x3EC00,0x40),(0x19FC00,0x3ff)):
            after = dict(blocks)
            block = bytearray(after[addr])
            block[offset] ^= 3
            after[addr] = bytes(block)
            with self.assertRaises(ValueError):
                writer.proposal_from_blocks(model,binding,blocks,after)

    def test_apply_uses_exact_plan_only_after_backup_and_disallows_empty(self):
        proposal = writer.plan_settings(report(contacts=[{"index":1,"name":"A","type":"group","dmr_id":12}]),baseline())
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/"state.tytbackup"
            def verify_call(mode,*,helper,data):
                self.assertTrue(out.is_file())
                self.assertEqual(mode,"--write-settings")
                self.assertEqual(data,base64.b64decode(proposal["plan"]))
                return subprocess.CompletedProcess([], 0, b"",
                    b"WRITE_PREFLIGHT\nWRITE_START 0x140000\nWRITE_VERIFIED 0x140000\nWRITE_COMPLETE 1\n")
            with patch.object(writer,"_run_helper",side_effect=verify_call) as run:
                writer.apply(proposal,backup=out)
                run.assert_called_once()
                run.reset_mock()
                with self.assertRaises((OSError,ValueError)):
                    writer.apply(proposal,backup=out)
                for altered in [{"blocks":0},{"blocks":True},{"sectors":1.0},{"sectors":2},
                                {"model":"MD-UV380"},{"dmr_id":0},{"plan":"!"}]:
                    with self.assertRaises(ValueError):
                        writer.apply(dict(proposal,**altered),backup=Path(d)/"unused")
                with self.assertRaises(ValueError):
                    writer.apply(writer.plan_settings(report(),baseline()),backup=Path(d)/"unused")
                run.assert_not_called()

    def test_confirmation_requires_ordered_native_markers_and_checked_sector_count(self):
        expected = [0x10000, 0x30000]
        events = ["WRITE_PREFLIGHT", "WRITE_START 0x010000", "WRITE_VERIFIED 0x010000",
                  "WRITE_START 0x030000", "WRITE_VERIFIED 0x030000", "WRITE_COMPLETE 2"]
        backup = Path("prior.tytbackup")
        def result(code, lines):
            return subprocess.CompletedProcess([], code, b"", "\n".join(lines).encode())
        writer._check_write_result(result(0, ["USB connected", *events]), expected, backup)
        for code, lines, message in (
                (1, ["USB absent", events[0]], "до стирания"),
                (0, [], "неизвестно"), (-9, [events[0]], "неизвестно"),
                (1, events[:2], "0x010000 мог записаться частично"),
                (1, events[:3], "1 из 2. Запись остановилась между секторами"),
                (1, events[:4], "0x030000 мог записаться частично"),
                (1, events[:5], "Все секторы (2) сверены"),
                (1, events, "Все секторы (2) сверены"),
                (0, [*events[:5], "WRITE_COMPLETE 1"], "неизвестно"),
                (0, [*events, events[-1]], "неизвестно"),
                (0, [events[0], events[3], *events[2:]], "неизвестно")):
            with self.subTest(code=code, lines=lines), self.assertRaises(ValueError) as exc:
                writer._check_write_result(result(code, lines), expected, backup)
            self.assertIn(message, str(exc.exception))
            self.assertIn(str(backup), str(exc.exception))
