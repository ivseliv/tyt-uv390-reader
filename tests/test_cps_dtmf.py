import copy
import io
import unittest

from cps_dtmf import (
    BASE, SERVICE, STRIDE, UNKNOWN, dtmf_section, merge_sequence_edits, patch_dtmf,
    patch_sequences, sequence_section, validate_sequence_edit,
)


class SignalingTimers(unittest.TestCase):
    def setUp(self):
        self.image = bytearray(b"\x85" * (BASE + 4 * STRIDE))
        self.allowed = set()
        for i in range(4):
            base = BASE + i * STRIDE
            self.image[base:base + 5] = bytes([40 + i, 10, 10, 10, 10])
            self.image[base + 6:base + 9] = bytes([20, 10, 0xAA | (i % 2)])
            self.allowed.add((base, 9))

    def section(self):
        def read_at(offset, size):
            self.assertIn((offset, size), self.allowed)
            return self.image[offset:offset + size]
        return dtmf_section(read_at)

    def test_selective_read_and_patch(self):
        before = self.section()
        self.assertEqual([row["fields"][0]["value"] for row in before["rows"]], ["400", "410", "420", "430"])
        after = copy.deepcopy(before)
        after["rows"][2]["fields"][0]["value"] = "500"
        after["rows"][2]["fields"][-1]["value"] = "Вкл."
        target = io.BytesIO(self.image)
        patch_dtmf(target, before, after)
        output = target.getvalue()
        changed = {i for i, (old, new) in enumerate(zip(self.image, output)) if old != new}
        base = BASE + 2 * STRIDE
        self.assertEqual(changed, {base, base + 8})
        self.assertEqual(output[base], 50)
        self.assertEqual(output[base + 8], 0xAB)
        self.assertIsNone(dtmf_section(lambda *_: None))

    def test_invalid_later_edit_preserves_staged_file(self):
        before = self.section()
        after = copy.deepcopy(before)
        after["rows"][0]["fields"][0]["value"] = "150"
        after["rows"][3]["fields"][1]["value"] = "27"
        target = io.BytesIO(self.image)
        with self.assertRaisesRegex(ValueError, "шагом 10"):
            patch_dtmf(target, before, after)
        self.assertEqual(target.getvalue(), self.image)

    def test_ptt_mode_group_and_side_tone_share_flags_without_overwriting(self):
        before = self.section()
        after = copy.deepcopy(before)
        fields = {f["label"]: f for f in after["rows"][0]["fields"]}
        fields["Длительность клавиши D, мс"]["value"] = "500"
        fields["Режим PTT ID"]["value"] = "Pre & Post"
        fields["Групповой символ"]["value"] = "#"
        fields["Самопрослушивание DTMF"]["value"] = "Вкл."
        dest = io.BytesIO(self.image)
        patch_dtmf(dest, before, after)
        image = dest.getvalue()
        self.assertEqual(image[BASE + 8], 0x6f)
        self.assertEqual(image[BASE + 5], 50)
        self.assertEqual(after, dtmf_section(lambda pos, n: image[pos:pos+n]))


class SignalingSequences(unittest.TestCase):
    # Offsets derived separately from the CPS V2.42 PTT/encode/decode accessors.
    UP1, DOWN4, TX1, TX32, RX1, RX8 = 0x30235, 0x302D5, 0x302E5, 0x304D5, 0x304E5, 0x3058D

    def setUp(self):
        self.image = bytearray(0x30600)
        self.image[self.UP1:self.UP1 + 5] = bytes.fromhex("05 10 ba ec aa")
        self.image[self.DOWN4:self.DOWN4 + 3] = bytes.fromhex("02 fd 51")
        self.image[self.TX1:self.TX1 + 2] = bytes.fromhex("01 71")
        self.image[self.TX32:self.TX32 + 4] = bytes.fromhex("03 76 a5 01")
        self.image[self.RX1:self.RX1 + 3] = bytes.fromhex("02 21 41")
        self.image[self.RX8 + 16] = 3  # CPS service response; its ID is outside the normal decoder editor.
        self.reads = []
        self.before = sequence_section(self.read)

    def read(self, pos, size):
        self.reads.append((pos, size))
        return self.image[pos:pos + size]

    @staticmethod
    def field(section, name, label=None):
        row = next(row for row in section["rows"] if row["name"] == name)
        return next(f for f in row["fields"] if label is None or f["label"] == label)

    def test_import_packed_sequences_and_skip_service_ids(self):
        self.assertEqual(len(self.before["rows"]), 44)
        for name, field, expected in (
            ("Система 1", "KeyUp Encode", "01ABC"), ("Система 4", "KeyDown Encode", "D#"),
            ("Передача 1", None, "1"), ("Передача 32", None, "675"), ("Декодер 1", None, "12"),
            ("Декодер 8", None, SERVICE),
        ):
            self.assertEqual(self.field(self.before, name, field)["value"], expected)
        self.assertFalse(any(pos < self.RX8 + 16 and pos + n > self.RX8 for pos, n in self.reads))

    def test_patch_round_trip_all_symbols_and_preserve_unused_nibbles(self):
        after = copy.deepcopy(self.before)
        changes = [("Система 1", "KeyUp Encode", "#"), ("Система 4", "KeyDown Encode", "0"),
                   ("Передача 32", None, "0123456789abcd*#0123456789ABCD"),
                   ("Декодер 1", None, "")]
        target = io.BytesIO(self.image)
        for name, label, value in changes:
            self.field(after, name, label)["value"] = value
        patch_sequences(target, self.before, after)
        result = target.getvalue()
        self.assertEqual(result[self.UP1:self.UP1 + 5], bytes.fromhex("01 1f ba ec aa"))
        self.assertEqual(result[self.DOWN4:self.DOWN4 + 3], bytes.fromhex("01 f0 51"))
        self.assertEqual(result[self.RX1:self.RX1 + 3], bytes.fromhex("00 21 41"))
        self.assertEqual(result[self.TX32:self.TX32 + 16], bytes.fromhex("1e 10 32 54 76 98 ba dc fe 10 32 54 76 98 ba dc"))
        changed = {i for i, (a, b) in enumerate(zip(result, self.image)) if a != b}
        writable = {self.UP1, self.UP1 + 1, self.DOWN4, self.DOWN4 + 1, self.RX1} | set(range(self.TX32, self.TX32+16))
        self.assertTrue(changed <= writable)
        reread = sequence_section(lambda pos, n: result[pos:pos+n])
        self.assertEqual(validate_sequence_edit(self.before, after), reread)
        unchanged = io.BytesIO(self.image)
        patch_sequences(unchanged, self.before, self.before)
        self.assertEqual(unchanged.getvalue(), self.image)

    def test_sparse_snapshot_inherits_only_unspecified_slots(self):
        partial = sequence_section()
        self.assertEqual(self.field(partial, "Передача 2")["value"], UNKNOWN)
        self.field(partial, "Передача 32")["value"] = ""
        self.field(partial, "Система 4", "KeyUp Encode")["value"] = "a0"
        normal = validate_sequence_edit(None, partial)
        merged = merge_sequence_edits(self.before, normal)
        self.assertEqual(self.field(merged, "Передача 1")["value"], "1")
        self.assertEqual(self.field(merged, "Передача 32")["value"], "")
        self.assertEqual(self.field(merged, "Система 4", "KeyUp Encode")["value"], "A0")
        self.assertEqual(self.field(merged, "Декодер 8")["value"], SERVICE)
        self.assertEqual(self.field(partial, "Система 4", "KeyUp Encode")["value"], "a0")

    def test_invalid_edits_never_modify_the_file(self):
        for name, value in (("Передача 32", "A"*31), ("Передача 32", "A1G"), ("Передача 1", ""),
                            ("Декодер 8", "1"), ("Передача 2", UNKNOWN), ("Передача 2", "１２")):
            with self.subTest(name=name, value=value):
                edited = copy.deepcopy(self.before)
                self.field(edited, "Система 1", "KeyUp Encode")["value"] = "1"
                self.field(edited, name)["value"] = value
                target = io.BytesIO(self.image)
                with self.assertRaises(ValueError):
                    patch_sequences(target, self.before, edited)
                self.assertEqual(target.getvalue(), self.image)
        bad = copy.deepcopy(self.before)
        bad["rows"][1]["name"] = "Система 1"
        with self.assertRaises(ValueError):
            validate_sequence_edit(self.before, bad)
        partial = sequence_section()
        self.field(partial, "Декодер 8")["value"] = "5"
        with self.assertRaisesRegex(ValueError, "служебного"):
            merge_sequence_edits(self.before, partial)
