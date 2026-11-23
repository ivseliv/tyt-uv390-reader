"""Public TYT CPS file information, using absolute offsets in the RDT layout."""

from datetime import datetime


def basic_section(read_at, model):
    fields = []
    if model:
        fields.append({"label": "Модель", "value": model})

    limits = read_at(0x139, 8)
    if limits and len(limits) == 8:
        for side, start in (("A", 0), ("B", 4)):
            low = limits[start:start + 2][::-1].hex()
            high = limits[start + 2:start + 4][::-1].hex()
            if low.isdecimal() and high.isdecimal() and 0 < int(low) < int(high):
                fields.append({"label": f"Диапазон {side} (МГц)",
                               "value": f"{int(low) / 10:g}–{int(high) / 10:g}"})

    stamp = read_at(0x2226, 11)
    if stamp and len(stamp) == 11:
        date = stamp[:7].hex()
        if date.isdecimal():
            try:
                timestamp = datetime(int(date[:4]), *(int(date[i:i + 2]) for i in range(4, 14, 2)))
            except ValueError:
                pass
            else:
                fields.append({"label": "Последнее программирование по кодплагу",
                               "value": timestamp.strftime("%d.%m.%Y %H:%M:%S")})
        a, b, c, d = stamp[7:]
        if max(a, b, c, d) <= 9 and any((a, b, c, d)):
            fields.append({"label": "Версия CPS в кодплаге", "value": f"{10 * a + b}.{c}{d}"})

    if fields:
        return {"id": "basic", "title": "Сведения о рации",
                "rows": [{"name": "Сведения", "fields": fields}]}
    return None
