"""Windows service editors for local CPS files and normal read-only USB reports."""

import base64
import binascii
import json
from pathlib import Path
import re
import secrets
import unicodedata


DEC_TYPES = ("Выкл.", "ARC4", "DES", "AES-128", "AES-256", "Enhance",
             "Proprietary algorithm", "MAES-512")
DEC_WIDTHS = (0, 5, 8, 16, 32, 9, 64, 64)
BANK_NAMES = ("Basic", "Enhanced · 128 бит", "Enhanced · 256 бит")
BANK_WIDTHS = (2, 16, 32)
USB_STATUSES = {"notApplicable": "Нет оценки", "zeros": "Нули · неизвестно",
                "ones": "FF · неизвестно", "dataPresent": "Есть данные"}


def read_limited(path, maximum):
    with Path(path).open("rb") as source:
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Размер файла превышает ожидаемый.")
    return data


def hex_bytes(text, size):
    if not re.fullmatch(rf"[0-9a-fA-F]{{{size * 2}}}", text):
        raise ValueError("Длина или формат ключа HEX не соответствует выбранному типу.")
    return bytes.fromhex(text)


class DecFile:
    size = 0x5000

    def __init__(self, data=None):
        if data is None:
            data = bytes(self.size)
        if len(data) != self.size:
            raise ValueError("Для файла Universal Privacy CPS 2.42 ожидается 20 480 байт.")
        self.data = bytearray(data)

    @staticmethod
    def layout(slot):
        if not 1 <= slot <= 255:
            raise ValueError("Укажите позицию от 1 до 255.")
        if slot <= 16:
            return (slot - 1) * 100, 64
        return 1600 + (slot - 17) * 68, 32

    def row(self, slot):
        offset, _ = self.layout(slot)
        raw = self.data[offset + 4:offset + 36]
        end = next((i for i in range(0, 32, 2) if raw[i:i + 2] in (b"\0\0", b"\xff\xff")), 32)
        return self.data[offset], raw[:end].decode("utf-16-le", "replace")

    def value(self, slot):
        off, capacity = self.layout(slot)
        kind = self.data[off]
        if kind >= len(DEC_WIDTHS) or not 0 < DEC_WIDTHS[kind] <= capacity:
            return None
        return self.data[off + 36:off + 36 + DEC_WIDTHS[kind]][::-1].hex().upper()

    def change(self, slot, kind, name, replacement=None, clear=False):
        offset, capacity = self.layout(slot)
        old_kind, old_name = self.row(slot)
        rename = clear or name != old_name
        if clear:
            kind, name = 0, ""
        if rename:
            if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") or c == "\uffff" for c in name):
                raise ValueError("Имя должно занимать до 16 кодовых единиц UTF-16 без управляющих символов.")
            encoded = name.encode("utf-16-le")
            if len(encoded) > 32:
                raise ValueError("Имя должно занимать до 16 кодовых единиц UTF-16 без управляющих символов.")
        key = None
        if clear or (kind == 0 and old_kind != 0):
            key = b""
        elif kind != old_kind or replacement is not None:
            if not 0 < kind < len(DEC_WIDTHS) or DEC_WIDTHS[kind] > capacity:
                raise ValueError("Этот тип ключа не помещается в выбранную позицию DEC.")
            if replacement is None:
                raise ValueError("Для нового типа введите ключ или нажмите «Сгенерировать».")
            key = hex_bytes(replacement, DEC_WIDTHS[kind])[::-1]
        self.data[offset] = kind
        if rename:
            self.data[offset + 4:offset + 36] = encoded.ljust(32, b"\0")
        if key is not None:
            self.data[offset + 36:offset + 36 + capacity] = key.ljust(capacity, b"\0")


class LegacyBank:
    file_size = 852533

    def __init__(self, data=None, usb=None):
        self.data = None
        self.usb = None
        if usb is not None:
            try:
                self.usb = tuple(base64.b64decode(usb[key], validate=True) for key in ("basic", "enhanced"))
                if tuple(map(len, self.usb)) != (32, 128):
                    raise ValueError()
            except (KeyError, TypeError, binascii.Error, ValueError):
                raise ValueError("Получена неполная таблица ключей Privacy.") from None
        else:
            if data is None or len(data) != self.file_size or data[0x125:0x12d] not in (b"MD-UV380", b"MD-UV390"):
                raise ValueError("Выберите штатный RDT для MD-UV380 или MD-UV390.")
            self.data = bytearray(data)

    @staticmethod
    def count(bank):
        return 16 if bank == 0 else 128 // BANK_WIDTHS[bank]

    def area(self, bank, slot):
        if bank not in (0, 1, 2) or not 1 <= slot <= self.count(bank):
            raise ValueError("Нет такой позиции ключа.")
        width = BANK_WIDTHS[bank]
        start = (slot - 1) * width
        if self.data is None:
            return self.usb[0 if bank == 0 else 1], slice(start, start + width)
        start += 0x59c0 + 0x225 + (0x90 if bank == 0 else 0)
        return self.data, slice(start, start + width)

    def value(self, bank, slot):
        data, region = self.area(bank, slot)
        value = data[region]
        if value == b"\x00" * len(value) or value == b"\xff" * len(value):
            return ""
        return value[::-1].hex().upper()

    def change(self, bank, slot, text):
        if self.data is None:
            raise ValueError("Данные USB доступны только для чтения.")
        data, region = self.area(bank, slot)
        if text:
            value = hex_bytes(text, BANK_WIDTHS[bank])
            if len(set(value)) == 1 and value[0] in (0, 255):
                raise ValueError("В этой таблице TYT значения из одних 00 или FF обозначают свободную позицию.")
            data[region] = value[::-1]
        elif self.value(bank, slot):
            data[region] = b"\xff" * BANK_WIDTHS[bank]


def calibration_fields():
    # Same numbered CPS 2.42 Test Mode rows used by the macOS profile format.
    rows = [(1, "A", "Частота приёма", 0x1b0, 9), (2, "A", "Частота передачи", 0x1b4, 9),
            (3, "B", "Частота приёма", 0x100, 5), (4, "B", "Частота передачи", 0x104, 5)]
    singles = ("VOX 1", "VOX 10", "Низкое напряжение приёма", "Полное напряжение приёма",
               "RSSI 1 (−120 дБм)", "RSSI 4 (−70 дБм)")
    rows.extend((i + 5, "A", title, i, 1) for i, title in enumerate(singles))
    rows.extend([(11, "A", "Подстройка частоты", 0x09, 1), (28, "B", "Подстройка частоты", 0x0c, 1)])
    pairs = (
        ("Мощность передачи · высокая", 12, 0x10, 29, 0x19),
        ("Мощность передачи · средняя", 13, 0x190, 30, 0x199),
        ("Мощность передачи · низкая", 14, 0x20, 31, 0x29),
        ("Чувствительность приёма", 15, 0x30, 32, 0x39),
        ("Открытие шумоподавителя 9", 16, 0x40, 41, 0xe0),
        ("Закрытие шумоподавителя 9", 17, 0x50, 42, 0xe5),
        ("Открытие шумоподавителя 1", 18, 0x60, 43, 0xea),
        ("Закрытие шумоподавителя 1", 19, 0x70, 44, 0xef),
        ("Модуляция CTCSS · 67 Гц", 20, 0x90, 33, 0x9b),
        ("Модуляция CTCSS · 151,4 Гц", 21, 0xa0, 34, 0xab),
        ("Модуляция CTCSS · 254,1 Гц", 22, 0xb0, 35, 0xbb),
        ("Модуляция DCS", 23, 0xd0, 36, 0xdb),
        ("DMR · амплитуда I", 24, 0x130, 37, 0x139),
        ("DMR · амплитуда Q", 25, 0x140, 38, 0x149),
        ("Аналог · амплитуда I", 26, 0x170, 39, 0x179),
        ("Аналог · амплитуда Q", 27, 0x180, 40, 0x189))
    for title, a, base_a, b, base_b in pairs:
        rows.extend([(a, "A", title, base_a, 9), (b, "B", title, base_b, 5)])
    rows.extend([(45, "A", "Модуляция аналогового DTMF", 0x1ae, 1),
                 (46, "B", "Модуляция аналогового DTMF", 0x1af, 1),
                 (47, "A", "Доля битовых ошибок (BER)", 0, 1),
                 (48, "B", "Доля битовых ошибок (BER)", 0, 1)])
    cells = {}
    for number, band, title, base, count in sorted(rows):
        points = [5] if count == 1 else range(1, count + 1)
        for i, point in enumerate(points):
            key = f"{number}.{point}"
            cells[key] = (band, title, point, base + i * (8 if number <= 4 else 1))
    return cells


CAL_CELLS = calibration_fields()


class Calibration:
    def __init__(self, template=None, values=None):
        if template is not None and len(template) != 8192:
            raise ValueError("Для формата .test CPS 2.42 ожидается файл размером 8192 байта.")
        self.template = template
        self.values = {} if values is None else values
        if not isinstance(self.values, dict) or not self.values.keys() <= CAL_CELLS.keys():
            raise ValueError("Профиль содержит неизвестный параметр или номер точки.")
        for key, val in self.values.items():
            self.encoded(key, val)

    @classmethod
    def open(cls, path):
        data = read_limited(path, 256 * 1024)
        if Path(path).suffix.lower() == ".test":
            return cls(data)
        try:
            doc = json.loads(data)
            if doc["format"] != "tyt-reader-calibration" or type(doc["version"]) is not int or doc["version"] != 1:
                raise ValueError
            template = doc.get("template")
            if template is not None:
                template = base64.b64decode(template, validate=True)
            if not isinstance(doc["values"], dict):
                raise ValueError
            return cls(template, doc["values"])
        except (ValueError, KeyError, TypeError):
            raise ValueError("Выберите JSON профиля калибровки TYT Reader.") from None

    @staticmethod
    def encoded(key, value):
        if key not in CAL_CELLS or not isinstance(value, str):
            raise ValueError("Профиль содержит неизвестный параметр или номер точки.")
        row = int(key.split(".")[0])
        value = value.strip().replace(",", ".")
        if row > 46:
            if not re.fullmatch(r"(?:0|[1-9][0-9]?|100)(?:\.[0-9]{1,8})?", value) or float(value) > 100:
                raise ValueError("Введите результат измерения от 0 до 100 %.")
            return b""
        if row > 4:
            if not re.fullmatch("[0-9]{1,3}", value) or int(value) > 255:
                raise ValueError("Введите целый коэффициент от 0 до 255.")
            return bytes([int(value)])
        if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,5})?", value):
            raise ValueError("Укажите частоту в МГц с точностью до 0,00001 МГц.")
        whole, _, fraction = value.partition(".")
        return bytes.fromhex(whole.zfill(3) + fraction.ljust(5, "0"))[::-1]

    def original(self, key):
        row = int(key.split(".")[0])
        if self.template is None or row > 46:
            return ""
        offset = CAL_CELLS[key][3]
        if row > 4:
            return str(self.template[offset])
        digits = self.template[offset:offset + 4][::-1].hex()
        return f"{int(digits[:3])}.{digits[3:]}" if digits.isdigit() else ""

    def native(self, source=None):
        image = Calibration(self.template if self.template is not None else source)
        if image.template is None:
            raise ValueError("Выберите исходный файл .test, чтобы сохранить заполненные поля и остальные байты оригинала.")
        data = bytearray(image.template)
        for key, text in self.values.items():
            encoded = self.encoded(key, text)
            off = CAL_CELLS[key][3]
            data[off:off + len(encoded)] = encoded
        return bytes(data)

    def json(self):
        doc = {"format": "tyt-reader-calibration", "version": 1, "values": self.values}
        if self.template is not None:
            doc["template"] = base64.b64encode(self.template).decode("ascii")
        return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")


def status_report(report, scope="Все"):
    rows = []
    for row in report["slots"]:
        if scope == "Активные" and row["type"] in (0, 255):
            continue
        if scope == "Слоты 1–4" and not 1 <= row["slot"] <= 4:
            continue
        if row["usbStatus"] not in USB_STATUSES:
            raise ValueError("Неизвестный статус USB.")
        rows.append({key: row[key] for key in ("slot", "name", "type", "typeName", "usbStatus")})
    return {"model": report["model"], "slots": rows}


def matching_dec_value(model, row):
    if "\ufffd" in row["name"] or model.row(row["slot"]) != (row["type"], row["name"]):
        raise ValueError("Имя или тип в этой позиции DEC отличается от данных рации.")
    value = model.value(row["slot"])
    if value is None:
        raise ValueError("Для этой позиции в DEC нет значения поддерживаемого типа.")
    if set(value) == {"0"}:
        raise ValueError("В файле DEC поле заполнено нулями. Такой файл мог быть сохранён после чтения со скрытыми ключами.")
    if set(value) == {"F"}:
        raise ValueError("В файле DEC поле заполнено байтами FF. Значение ключа в рации неизвестно.")
    return value


class ServiceDialog:
    def __init__(self, parent, tr, title, size="780x540"):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk = tk, ttk
        self.files, self.messages = filedialog, messagebox
        self.tr = tr
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title(tr(title))
        self.win.geometry(size)
        self.win.transient(parent)
        self.win.grab_set()
        self.body = ttk.Frame(self.win, padding=12)
        self.body.pack(fill="both", expand=True)
        self.top = ttk.Frame(self.body)
        self.top.pack(fill="x", pady=(0, 8))
        self.bottom = ttk.Frame(self.body)
        self.bottom.pack(fill="x", side="bottom", pady=(8, 0))
        self.dirty = False
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda _e: self.close())
        self.button(self.bottom, "Закрыть", self.close, "right")

    def discard(self):
        return not self.dirty or self.messages.askyesno(
            self.win.title(), self.tr("Отбросить несохранённые изменения?"), parent=self.win)

    def close(self):
        if self.discard():
            self.win.destroy()
            if self.parent.winfo_exists() and isinstance(self.parent, self.tk.Toplevel):
                self.parent.grab_set()

    def guarded(self, action):
        def run(*_):
            try:
                return action()
            except (ValueError, OSError, KeyError) as exc:
                detail = "Файл с таким именем уже существует. Выберите новое имя для копии." if isinstance(exc, FileExistsError) else str(exc)
                self.messages.showerror(self.win.title(), self.tr(detail), parent=self.win)
        return run

    def button(self, frame, text, action, side="left"):
        widget = self.ttk.Button(frame, text=self.tr(text), command=self.guarded(action))
        widget.pack(side=side, padx=3)
        return widget

    def table(self, columns):
        frame = self.ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        ids = tuple(range(len(columns)))
        tree = self.ttk.Treeview(frame, columns=ids, show="headings", selectmode="browse")
        scroll = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        for i, (label, width) in enumerate(columns):
            tree.heading(i, text=self.tr(label))
            tree.column(i, width=width, minwidth=36)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def save(self, data, ext, suggested):
        path = self.files.asksaveasfilename(parent=self.win, initialfile=suggested, defaultextension=ext,
                                            filetypes=[(ext.upper()[1:], "*" + ext)])
        if path:
            from win_document import save_new
            save_new(Path(path), data)
            return Path(path)
        return None


def _refresh(tree, rows):
    selected = tree.selection()
    tree.delete(*tree.get_children())
    for key, cells in rows:
        tree.insert("", "end", iid=str(key), values=cells)
    if selected and tree.exists(selected[0]):
        tree.selection_set(selected[0])
        tree.see(selected[0])


def _choice_bar(view, parent, labels, callback, initial=0):
    localized = [view.tr(s) for s in labels]
    combo = view.ttk.Combobox(parent, values=localized, state="readonly", width=max(map(len, localized)))
    combo.current(initial)
    combo.pack(side="left", padx=3)
    combo.bind("<<ComboboxSelected>>", view.guarded(callback))
    return combo


def _edit_slot(view, table, slot, apply, bank=None):
    dec = isinstance(table, DecFile)
    editable = dec or table.data is not None
    dialog = ServiceDialog(view.win, view.tr, "Значение ключа", "580x320")
    dialog.ttk.Label(dialog.top, text=view.tr("Слот") + " " + str(slot)).pack(side="left")
    fields = dialog.ttk.Frame(dialog.body)
    fields.pack(fill="x")
    fields.columnconfigure(1, weight=1)

    def field(row, title, widget):
        dialog.ttk.Label(fields, text=view.tr(title)).grid(row=row, column=0, sticky="w", padx=4, pady=3)
        widget.grid(row=row, column=1, sticky="ew", padx=4, pady=3)

    def new_entry(text=""):
        var = dialog.tk.StringVar(dialog.win, text)
        return var, dialog.ttk.Entry(fields, textvariable=var)

    if dec:
        original_type, original_name = table.row(slot)
        name, name_entry = new_entry(original_name)
        field(0, "Имя", name_entry)
        capacity = table.layout(slot)[1]
        options = [i for i, size in enumerate(DEC_WIDTHS) if size <= capacity]
        if original_type not in options:
            options.append(original_type)
        choices = [view.tr(DEC_TYPES[i]) if i < len(DEC_TYPES) else str(i) for i in options]
        kind = dialog.ttk.Combobox(fields, state="readonly", values=choices)
        kind.current(options.index(original_type))
        field(1, "Тип", kind)
    original = table.value(slot) if dec else table.value(bank, slot)
    material, raw = new_entry(original or "")
    raw.configure(show="•", state="normal" if editable else "readonly")
    field(2, "Ключ HEX", raw)
    show = dialog.tk.BooleanVar(dialog.win)
    dialog.ttk.Checkbutton(fields, text=view.tr("Показать значение"), variable=show,
                           command=lambda: raw.configure(show="" if show.get() else "•")).grid(row=3, column=1, sticky="w")
    if not editable:
        dialog.win.displayed_value = material
        return
    if dec:
        def reset_type(_):
            code = options[kind.current()]
            material.set((original or "") if code == original_type else "")
            show.set(False)
            raw.configure(show="•")
        kind.bind("<<ComboboxSelected>>", reset_type)

    def generate():
        if dec:
            code = options[kind.current()]
            if code >= len(DEC_WIDTHS) or not 0 < DEC_WIDTHS[code] <= capacity:
                raise ValueError("Сначала выберите тип ключа.")
            size = DEC_WIDTHS[code]
        else:
            size = BANK_WIDTHS[bank]
        key = secrets.token_bytes(size)
        while not dec and key in (bytes(size), b"\xff" * size):
            key = secrets.token_bytes(size)
        material.set(key.hex().upper())

    def commit(clear=False):
        if clear and not dialog.messages.askyesno(dialog.win.title(), view.tr("Очистить позицию") + "?", parent=dialog.win):
            return
        if dec:
            code = options[kind.current()]
            new = material.get()
            # Preserve bytes and padding of a name-only edit (including unknown types).
            replacement = None if code == original_type and new == (original or "") else (new or None)
            table.change(slot, code, name.get(), replacement, clear)
        elif clear or material.get() != (original or ""):
            table.change(bank, slot, "" if clear else material.get())
        apply()
        dialog.dirty = False
        dialog.close()
    material.trace_add("write", lambda *_: setattr(dialog, "dirty", True))
    if dec:
        name.trace_add("write", lambda *_: setattr(dialog, "dirty", True))
    dialog.button(dialog.bottom, "Сгенерировать", generate)
    dialog.button(dialog.bottom, "Очистить позицию", lambda: commit(True))
    dialog.button(dialog.bottom, "Применить к копии", commit)
    raw.focus_set()


def show_dec(parent, tr):
    view = ServiceDialog(parent, tr, "Ключи Universal Privacy")
    model = DecFile()
    filename = "Privacy"
    query = view.tk.StringVar(view.win)
    tree = view.table([("Слот", 60), ("Имя", 210), ("Тип", 200)])

    def refresh():
        items = []
        for slot in range(1, 256):
            code, name = model.row(slot)
            kind = tr(DEC_TYPES[code]) if code < len(DEC_TYPES) else str(code)
            if not query.get() or any(query.get().casefold() in s.casefold() for s in (str(slot), name, kind)):
                items.append((slot, (slot, name, kind)))
        _refresh(tree, items)

    def edit():
        if tree.selection():
            def changed():
                view.dirty = True
                refresh()
            _edit_slot(view, model, int(tree.selection()[0]), changed)

    def load(new=False):
        nonlocal model, filename
        if not view.discard():
            return
        if new:
            model, filename = DecFile(), "Privacy"
        else:
            path = view.files.askopenfilename(parent=view.win, filetypes=[("DEC", "*.dec")])
            if not path:
                return
            model, filename = DecFile(read_limited(path, DecFile.size)), Path(path).stem
        view.dirty = False
        query.set("")
        refresh()

    def save():
        if view.save(bytes(model.data), ".dec", f"{filename}-copy.dec"):
            view.dirty = False

    view.button(view.top, "Новая таблица", lambda: load(True))
    view.button(view.top, "Открыть DEC…", load)
    view.ttk.Label(view.top, text=tr("Номер, имя или тип")).pack(side="left", padx=4)
    view.ttk.Entry(view.top, textvariable=query).pack(side="left")
    query.trace_add("write", lambda *_: refresh())
    view.button(view.bottom, "Редактировать", edit)
    view.button(view.bottom, "Сохранить копию DEC…", save)
    tree.bind("<Double-1>", view.guarded(edit))
    tree.bind("<Return>", view.guarded(edit))
    refresh()
    return view.win


def _legacy_dialog(parent, tr, model, filename):
    usb = model.data is None
    view = ServiceDialog(parent, tr, "Ключи Privacy из рации" if usb else "Ключи Basic и Enhanced")
    tree = view.table([("Слот", 70), ("Значение ключа", 220)])

    def refresh():
        bank = combo.current()
        _refresh(tree, [(s, (s, tr("Задан" if model.value(bank, s) else "Свободен")))
                        for s in range(1, model.count(bank) + 1)])

    def edit():
        if tree.selection():
            def changed():
                view.dirty = True
                refresh()
            _edit_slot(view, model, int(tree.selection()[0]), changed, combo.current())

    def save():
        if view.save(bytes(model.data), ".rdt", f"{filename}-copy.rdt"):
            view.dirty = False

    combo = _choice_bar(view, view.top, BANK_NAMES, refresh)
    tree.bind("<Double-1>", view.guarded(edit))
    tree.bind("<Return>", view.guarded(edit))
    view.button(view.bottom, "Показать значение" if usb else "Редактировать", edit)
    if not usb:
        view.button(view.bottom, "Сохранить копию RDT…", save)
    refresh()
    return view.win


def show_rdt_privacy(parent, tr):
    from tkinter import filedialog, messagebox
    path = filedialog.askopenfilename(parent=parent, filetypes=[("TYT CPS RDT", "*.rdt")], title=tr("Открыть RDT…"))
    if path:
        try:
            model = LegacyBank(data=read_limited(path, LegacyBank.file_size))
        except (OSError, ValueError) as exc:
            messagebox.showerror(tr("Ключи Basic и Enhanced"), tr(str(exc)), parent=parent)
            return
        return _legacy_dialog(parent, tr, model, Path(path).stem)


def show_usb_privacy(parent, tr, bank):
    return _legacy_dialog(parent, tr, LegacyBank(usb=bank), "")


def show_universal(parent, tr, report):
    view = ServiceDialog(parent, tr, "Universal Privacy")
    view.ttk.Label(view.top, text=report["model"] + " · USB").pack(side="left", padx=4)
    local_dec = None
    source = view.tk.StringVar(view.win)
    scopes = ["Активные", "Слоты 1–4", "Все"]
    tree = view.table([("Слот", 55), ("Имя", 230), ("Тип", 150), ("Ответ USB", 185)])

    def current():
        return status_report(report, scopes[combo.current()])

    def refresh():
        _refresh(tree, [(r["slot"], (r["slot"], r["name"], tr(r["typeName"]), tr(USB_STATUSES[r["usbStatus"]])))
                        for r in current()["slots"]])

    def export():
        data = json.dumps(current(), ensure_ascii=False, indent=2).encode("utf-8")
        view.save(data, ".json", "TYT-Universal-Privacy-status.json")

    def load_dec():
        nonlocal local_dec
        path = view.files.askopenfilename(parent=view.win, filetypes=[("DEC", "*.dec")])
        if path:
            opened = DecFile(read_limited(path, DecFile.size))
            local_dec = opened
            source.set(Path(path).name)
            reveal.state(["!disabled"])

    def show_local_value():
        selected = tree.selection()
        if not selected or local_dec is None:
            return
        row = next(r for r in current()["slots"] if str(r["slot"]) == selected[0])
        value = matching_dec_value(local_dec, row)
        dialog = ServiceDialog(view.win, tr, "Значение из файла DEC", "620x180")
        dialog.ttk.Label(dialog.top, text=f"{source.get()} · {tr('Слот')} {row['slot']}").pack(side="left")
        key = dialog.tk.StringVar(dialog.win, value)
        # Retain the variable for as long as the child window is open.
        dialog.win.value = key
        dialog.ttk.Entry(dialog.body, textvariable=key, state="readonly").pack(fill="x")
        dialog.ttk.Label(dialog.body, text=tr("Совпали слот, имя и тип. Совпадение самих ключей не проверено."),
                         wraplength=550).pack(anchor="w", pady=4)

    combo = _choice_bar(view, view.top, scopes, refresh,
                        0 if status_report(report, "Активные")["slots"] else 2)
    view.button(view.top, "Открыть свой DEC…", load_dec)
    view.ttk.Label(view.top, textvariable=source).pack(side="left", padx=5)
    reveal = view.button(view.bottom, "Посмотреть значение из DEC", show_local_value)
    reveal.state(["disabled"])
    view.button(view.bottom, "Сохранить отчёт…", export)
    refresh()
    return view.win


def show_calibration(parent, tr):
    from tkinter import simpledialog
    view = ServiceDialog(parent, tr, "Сервисная калибровка")
    model, filename = Calibration(), "calibration"
    tree = view.table([("Параметр", 225), ("Точка", 55), ("Исходное значение", 145), ("Правка", 110)])

    def refresh():
        rows = []
        for key, (band, title, point, _offset) in CAL_CELLS.items():
            if band == ("A", "B")[combo.current()]:
                rows.append((key, (tr(title), point, model.original(key), model.values.get(key, ""))))
        _refresh(tree, rows)

    def edit():
        if not tree.selection():
            return
        key = tree.selection()[0]
        number = int(key.split(".")[0])
        _, title, point, _ = CAL_CELLS[key]
        unit = tr("МГц" if number <= 4 else "код CPS") if number < 47 else "%"
        value = simpledialog.askstring(tr(title), f"{tr('Точка')} {point} · {unit}",
                                       initialvalue=model.values.get(key, ""), parent=view.win)
        if value is None:
            return
        value = value.strip()
        if value:
            model.encoded(key, value)
            model.values[key] = value
        else:
            model.values.pop(key, None)
        view.dirty = True
        refresh()

    def load(new=False):
        nonlocal model, filename
        if not view.discard():
            return
        if new:
            model, filename = Calibration(), "calibration"
        else:
            path = view.files.askopenfilename(parent=view.win, filetypes=[("TYT CPS / JSON", "*.test *.json")])
            if not path:
                return
            model, filename = Calibration.open(path), Path(path).stem
        view.dirty = False
        refresh()

    def save(native):
        nonlocal model
        if not native:
            if view.save(model.json(), ".json", filename + "-profile.json"):
                view.dirty = False
            return
        source = model.template
        if source is None:
            path = view.files.askopenfilename(parent=view.win, title=tr("Выберите исходный .test для новой копии. В неё перейдут заполненные коэффициенты и частоты."),
                                              filetypes=[("TYT CPS", "*.test")])
            if not path:
                return
            source = read_limited(path, 8192)
        data = model.native(source)
        if view.save(data, ".test", filename + "-copy.test"):
            measurements = {k: v for k, v in model.values.items() if k in ("47.5", "48.5")}
            model = Calibration(data, measurements)
            view.dirty = bool(measurements)
            if measurements:
                view.messages.showinfo(view.win.title(), tr("Файл .test содержит таблицу CPS. Введённые измерения BER сохраняются отдельно в JSON."),
                                       parent=view.win)
            refresh()

    combo = _choice_bar(view, view.top, ["Сторона A", "Сторона B"], refresh)
    view.button(view.top, "Новый", lambda: load(True))
    view.button(view.top, "Открыть профиль…", load)
    view.button(view.bottom, "Редактировать", edit)
    view.button(view.bottom, "Сохранить профиль JSON…", lambda: save(False))
    view.button(view.bottom, "Сохранить копию .test…", lambda: save(True))
    tree.bind("<Double-1>", view.guarded(edit))
    tree.bind("<Return>", view.guarded(edit))
    refresh()
    return view.win
