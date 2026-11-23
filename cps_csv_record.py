"""TYT CPS V2.42 English channel CSV fields and their public 64-byte RDT record."""

from decimal import Decimal

# Column: (byte, low bit, bit mask, XOR after extraction), in the order
# used by CPS when translating its FM and digital controls into cells.
BITS = {
    "Channel Mode": (0, 0, 3, 0), "Band Width": (0, 2, 3, 0), "Scan List": (11, 0, 255, 0),
    "Squelch": (15, 0, 255, 0), "RX Ref Frequency": (3, 0, 3, 0), "TX Ref Frequency": (4, 0, 3, 0),
    "TOT[s]": (8, 0, 255, 0), "TOT Rekey Delay[s]": (9, 0, 255, 0),
    "Admit Criteria": (4, 6, 3, 0), "Auto Scan": (0, 4, 1, 0), "Rx Only": (1, 1, 1, 0),
    "Lone Worker": (0, 7, 1, 0), "VOX": (4, 4, 1, 0), "Allow Talkaround": (1, 0, 1, 0),
    "Send GPS Info": (31, 0, 1, 1), "Receive GPS Info": (31, 1, 1, 1),
    "Private Call Confirmed": (2, 6, 1, 0), "Emergency Alarm Ack": (3, 3, 1, 0),
    "Data Call Confirmed": (2, 7, 1, 0), "Allow Interrupt": (31, 2, 1, 1),
    "DCDM Switch": (31, 3, 1, 1), "Leader/MS": (31, 4, 1, 0),
    "Emergency System": (10, 0, 255, 0), "Group List": (12, 0, 255, 0),
    "Color Code": (1, 4, 15, 0), "In Call Criteria": (5, 4, 3, 0), "Privacy": (2, 4, 3, 0),
    "GPS System": (13, 0, 255, 0), "Rx Signaling System": (28, 0, 255, 0),
    "Tx Signaling System": (29, 0, 255, 0), "QT Reverse": (5, 0, 3, 0),
    "Display PTT ID": (3, 7, 1, 0), "Reverse Burst/Turn-off Code": (4, 2, 1, 0),
}
BITS.update({f"Decode {i + 1}": (14, i, 1, 0) for i in range(8)})
FREQUENCIES = {"RX Frequency(MHz)": 16, "TX Frequency(MHz)": 20}
TONES = {"CTCSS/DCS Dec": 24, "CTCSS/DCS Enc": 26}
POWER = (0, 2, 1, 2)  # CPS maps reserved 1 to High.
TURN_OFF = (0, 1, 2, 2)


def privacy_index(record):
    high = record[30] & 0xF0 if record[2] & 0x30 == 0x30 else 0
    return high | (record[2] & 15)


def channel_cells(record):
    from cps_csv import cps_tone
    values = {key: str(((record[byte] >> shift) & mask) ^ flip)
              for key, (byte, shift, mask, flip) in BITS.items()}
    for field, pos in FREQUENCIES.items():
        digits = record[pos:pos + 4][::-1].hex()
        if not digits.isdecimal():
            raise ValueError("В записи RDT частота не закодирована числом BCD.")
        values[field] = f"{Decimal(digits) / 100000:.5f}"
    for field, pos in TONES.items():
        value = int.from_bytes(record[pos:pos + 2], "little")
        values[field] = "None" if value >> 8 == 255 else ("0.0" if value == 0 else cps_tone(value))
    raw_name = record[32:64].decode("utf-16-le", errors="replace")
    values["Channel Name"] = raw_name.split("\0", 1)[0].rstrip("\uffff")
    values["Power"] = str(POWER[record[30] & 3])
    values["Non-QT/DQT Turn-off Freq"] = str(TURN_OFF[record[5] >> 6])
    values["Repeater Slot"] = "1" if record[1] & 12 == 8 else "0"
    contact = int.from_bytes(record[6:8], "little")
    values["Contact Name"] = str(contact if contact <= 10000 else 0)
    values["Privacy No."] = str(privacy_index(record))
    return values


def set_bits(record, pos, shift, mask, value):
    record[pos] = (record[pos] & ~(mask << shift)) | (value << shift)


def patch_channel_cells(record, old, new):
    """Retain unmodified and reserved bits. The caller only hands in a channel record."""
    from cps_csv import CPS_CHANNELS, hz, tone, unsigned
    if not isinstance(new, dict) or set(new) != set(CPS_CHANNELS):
        raise ValueError("Изменился набор колонок канала CPS.")
    for label in CPS_CHANNELS:
        value = new[label]
        if not isinstance(value, str):
            raise ValueError(f"В колонке «{label}» ожидается текст.")
        if old.get(label) == value and not (label == "Privacy No." and old.get("Privacy") != new["Privacy"]):
            continue
        if label in BITS:
            pos, shift, mask, flip = BITS[label]
            number = unsigned(value, mask)
            set_bits(record, pos, shift, mask, number ^ flip)
        elif label in FREQUENCIES:
            freq = hz(value)
            if freq % 10 or not 10 <= freq <= 999999990:
                raise ValueError("Частота CPS должна быть задана с шагом 10 Гц.")
            pos = FREQUENCIES[label]
            record[pos:pos + 4] = bytes.fromhex(f"{freq // 10:08d}")[::-1]
        elif label in TONES:
            number = tone(value)
            if number is None:
                raise ValueError(f"Неподдерживаемый тон в колонке {label}.")
            pos = TONES[label]
            record[pos:pos + 2] = number.to_bytes(2, "little")
        elif label == "Channel Name":
            encoded = value.encode("utf-16-le")
            if len(encoded) > 32 or not value.strip() or not value.isprintable():
                raise ValueError("Имя канала CPS должно занимать от 1 до 16 кодовых единиц UTF-16.")
            record[32:64] = encoded.ljust(32, b"\0")
        elif label == "Power":
            set_bits(record, 30, 0, 3, (0, 2, 3)[unsigned(value, 2)])
        elif label == "Non-QT/DQT Turn-off Freq":
            set_bits(record, 5, 6, 3, (0, 1, 3)[unsigned(value, 2)])
        elif label == "Repeater Slot":
            set_bits(record, 1, 2, 3, unsigned(value, 1) + 1)
        elif label == "Contact Name":
            record[6:8] = unsigned(value, 10000).to_bytes(2, "little")
        elif label == "Privacy No.":
            extended = record[2] & 0x30 == 0x30
            number = unsigned(value, 255 if extended else 15)
            set_bits(record, 2, 0, 15, number & 15)
            if extended:
                set_bits(record, 30, 4, 15, number >> 4)
