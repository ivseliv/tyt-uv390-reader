"""TYT Reader for Windows. USB programming is a separate, explicitly confirmed action."""
import argparse
import copy
import queue
import tempfile
import threading
import weakref
from pathlib import Path

import win_program
import write_settings
from write_backup import prepare_restore
from cps_dtmf import sequence_section
from cps_sections import TITLES
from win_cps_actions import add_slot, assignments, delete_slot, save_dtmf, set_assignment, slot_note, slot_number, slot_sections
from win_document import Document, read_universal_usb
from win_language import LANGUAGES, Language, resource_root
from win_records import (FREE_TEXT, TABLES, Field, apply_field, field_text, fields_for, mhz,
                         new_record, option_label, parse_field)


COLUMNS = {
    "channels": (("№", 60), ("Имя", 180), ("Режим", 100), ("RX, МГц", 115), ("TX, МГц", 115)),
    "contacts": (("№", 60), ("Имя", 180), ("Тип вызова", 140), ("Номер / позывной", 160)),
    "zones": (("№", 60), ("Имя", 220), ("Сторона A", 110), ("Сторона B", 110)),
    "rx_groups": (("№", 60), ("Имя", 220), ("Контакты", 120)),
    "scan_lists": (("№", 60), ("Имя", 220), ("Каналы", 110)),
}

ASSIGNMENTS = {"quick": "Быстрый доступ (1–6)", "numberKeys": "Цифровые клавиши (10 позиций)"}
TARGETS = {"contact_index": "contacts", "contacts": "contacts", "rx_group_index": "rx_groups",
           "scan_list_index": "scan_lists", "primary": "channels", "secondary": "channels", "revert": "channels",
           "A": "channels", "B": "channels", "channels": "channels"}


def assignment_fields(table, row):
    fields = [Field("contact_index", "№ контакта", row["contact_index"], "number")]
    if table == "quick":
        fields = [Field("quick_type", "Действие", row["quick_type"], "choice",
                        ("Не назначено", "Вызов", "Сообщение", "DTMF")), *fields,
                  Field("message_slot", "№ шаблона", row["message_slot"], "number"),
                  Field("profile", "Профиль", row["profile"], "choice", tuple(f"DTMF-{i}" for i in range(1, 5))),
                  Field("encode_slot", "Номер Encode (0–32)", row["encode_slot"], "number")]
    return fields


def preview(table, row, tr):
    if table not in TABLES:
        return (tr.generated(row.get("name", "")),)
    values = [row["index"], row.get("name", "")]
    if table == "channels":
        values += [option_label("mode", row.get("mode"), tr), mhz(row.get("rx_hz")), mhz(row.get("tx_hz"))]
    elif table == "contacts":
        address = {"dtmf": "dtmf_number", "m17": "callsign"}.get(row.get("type"), "dmr_id")
        values += [option_label("type", row.get("type"), tr), row.get(address, "")]
    elif table == "zones":
        values += [len(row.get(side, [])) for side in ("A", "B")]
    else:
        values.append(len(row.get("contacts" if table == "rx_groups" else "channels", [])))
    return values


def offline_check():
    """Exercise the packaged file backend, catalogs and reversible local edits."""
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        doc = Document.new()
        data = copy.deepcopy(doc.report)
        data["channels"] = [{"index": 1, "name": "Дом / 家", "mode": "analog", "rx_hz": 433125000, "tx_hz": 433125000}]
        data["zones"] = [{"index": 1, "name": "A", "A": [{"index": 1}], "B": []}]
        doc.apply(data)
        path = folder / "测试.json"
        doc.save_json(path)
        reopened = Document.open(path)
        assert reopened.report["zones"][0]["A"][0]["name"] == "Дом / 家"
        for lang in LANGUAGES:
            tr = Language(lang)
            assert preview("channels", reopened.report["channels"][0], tr)[1] == "Дом / 家"
        assert Language("en")("Считать настройки") == "Read Settings"
        assert Language("zh-Hans")("Считать настройки") == "读取设置"
        # Exercise the bundled programming backend offline, with an empty simulated radio.
        target = dict.fromkeys(write_settings.ADDRESSES, bytes(write_settings.BLOCK))
        report = dict(reopened.report, usb_model="MD-UV390", dmr_id=0)
        assert win_program.eligible(report)
        planned = write_settings.plan_settings(report, ("MD-UV390", bytes(12), target))
        assert planned["changes"]["channels"] == 1 and planned["changes"]["zones"] == 1


def open_gui(smoke=False, initial=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import win_services

    class ReaderWindow:
        def __init__(self, root):
            self.root = root
            self.tr = Language("en" if smoke else None)
            self.doc = None
            self.dirty = False
            self.busy = False
            self.activity = "Чтение по USB…"
            self.section = "channels"
            self.events = queue.Queue()
            self.query = tk.StringVar()
            self.status = tk.StringVar()
            self.rows = {}
            self.sections = {}
            self.clip = None
            self.print_folder = None
            self.query.trace_add("write", lambda *_: self.refresh_rows())
            root.title("TYT Reader")
            root.geometry("1100x690")
            root.minsize(800, 490)
            try:
                self.icon = tk.PhotoImage(file=resource_root() / "macos/AppIcon.png").subsample(12)
                root.iconphoto(True, self.icon)
            except tk.TclError:
                pass
            self.build()
            root.protocol("WM_DELETE_WINDOW", self.quit)
            for shortcut, fn in (("<Control-o>", self.open_file), ("<Control-s>", self.save),
                                 ("<Control-n>", self.new), ("<Control-e>", self.edit)):
                root.bind(shortcut, lambda event, f=fn: self.run(f))
            root.after(80, self.results)

        def run(self, fn):
            if self.busy:
                return
            try:
                fn()
            except Exception as exc:
                messagebox.showerror("TYT Reader", self.tr.generated(str(exc)), parent=self.root)

        def build(self):
            tr = self.tr
            for child in self.root.winfo_children():
                child.destroy()
            self.menu = tk.Menu(self.root)
            self.root.config(menu=self.menu)
            def menu(title):
                item = tk.Menu(self.menu, tearoff=False)
                self.menu.add_cascade(label=title, menu=item)
                return item
            def actions(target, entries):
                for text, fn in entries:
                    target.add_command(label=tr(text), command=lambda fn=fn: self.run(fn))
            file = menu(tr("Файл"))
            actions(file, [("Новый кодплаг…", self.new), ("Новый из шаблона RDT…", self.template),
                           ("Открыть файл…", self.open_file), ("Сохранить JSON…", self.save),
                           ("Сохранить копию RDT…", self.save_rdt), ("RDT по шаблону…", self.transfer),
                           ("Импорт CSV…", self.merge_csv), ("Печать…", self.print_preview)])
            export = tk.Menu(file, tearoff=False)
            file.add_cascade(label=tr("Экспорт"), menu=export)
            actions(export, [("Каналы в CSV…", lambda: self.export("channels")),
                             ("Контакты в CSV…", lambda: self.export("contacts")),
                             ("YAML для qdmr…", lambda: self.export("yaml"))])
            file.add_separator()
            actions(file, [("Выход", self.quit)])
            actions(menu(tr("Правка")), [("Добавить", lambda: self.edit("add")),
                    ("Изменить", self.edit), ("Дублировать", lambda: self.edit("duplicate")),
                    ("Удалить", self.delete), ("Копировать запись", self.copy_row), ("Вставить запись", lambda: self.edit("paste"))])
            services = menu(tr("Сервис"))
            self.service_menu = services
            actions(services, [("Считать настройки", self.read), ("Считать Universal Privacy по USB…", self.universal),
                 ("Basic и Enhanced (USB)…", self.basic), ("Ключи Universal (.dec)…", lambda: win_services.show_dec(self.root, tr)),
                 ("Ключи Basic и Enhanced (.rdt)…", lambda: win_services.show_rdt_privacy(self.root, tr)),
                 ("Сервисная калибровка…", lambda: win_services.show_calibration(self.root, tr)),
                 ("Последовательности DTMF…", self.dtmf), ("Сохранить DTMF в копию RDT…", self.save_sequences)])
            services.add_separator()
            actions(services, [("Записать настройки в рацию…", self.write_radio),
                               ("Восстановить настройки из резервной копии…", self.restore_radio)])
            langs = menu("Язык / Language / 语言")
            self.active_language = tk.StringVar(value=tr.choice)
            for code, name in {"system": tr("Системный язык"), **LANGUAGES}.items():
                langs.add_radiobutton(label=name, value=code, variable=self.active_language,
                                      command=lambda code=code: self.run(lambda: self.change_language(code)))

            frame = ttk.Frame(self.root, padding=12)
            frame.pack(fill="both", expand=True)
            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(0, 10))
            ttk.Label(bar, text="TYT MD-UV390", font=("Segoe UI", 16, "bold")).pack(side="left", padx=(0, 18))
            self.buttons = {}
            for key, fn in [("Считать настройки", self.read), ("Записать в рацию…", self.write_radio), ("Открыть файл…", self.open_file),
                            ("Сохранить JSON…", self.save)]:
                b = ttk.Button(bar, text=tr(key), command=lambda fn=fn: self.run(fn))
                b.pack(side="left", padx=(0, 5))
                self.buttons[key] = b
            body = ttk.Panedwindow(frame, orient="horizontal")
            body.pack(fill="both", expand=True)
            side = ttk.Frame(body, padding=(0, 0, 7, 0))
            right = ttk.Frame(body, padding=(8, 0, 0, 0))
            body.add(side, weight=1)
            body.add(right, weight=4)
            self.nav = ttk.Treeview(side, show="tree", selectmode="browse", height=15)
            self.nav.column("#0", width=196, minwidth=120)
            self.nav.pack(fill="both", expand=True)
            self.nav.bind("<<TreeviewSelect>>", self.pick_section)
            tools = ttk.Frame(right)
            tools.pack(fill="x", pady=(0, 6))
            self.section_title = ttk.Label(tools, font=("Segoe UI", 12, "bold"))
            self.section_title.pack(side="left")
            ttk.Entry(tools, textvariable=self.query, width=22).pack(side="right")
            ttk.Label(tools, text=tr("Поиск")).pack(side="right", padx=5)
            tables = ttk.Panedwindow(right, orient="vertical")
            tables.pack(fill="both", expand=True)
            self.table = self.grid(tables, (), 10)
            self.detail = self.grid(tables, (("Параметр", 220), ("Значение", 350)), 7)
            self.table.bind("<<TreeviewSelect>>", lambda _: self.refresh_detail())
            self.table.bind("<Double-1>", lambda _: self.run(self.edit))
            self.table.bind("<Return>", lambda _: self.run(self.edit))
            bottom = ttk.Frame(right)
            bottom.pack(fill="x", pady=(8, 0))
            for key, fn in [("Добавить", lambda: self.edit("add")), ("Изменить", self.edit),
                            ("Дублировать", lambda: self.edit("duplicate")), ("Удалить", self.delete)]:
                b = ttk.Button(bottom, text=tr(key), command=lambda fn=fn: self.run(fn))
                b.pack(side="left", padx=(0, 5))
                self.buttons[key] = b
            ttk.Label(frame, textvariable=self.status, wraplength=950).pack(anchor="w", pady=(9, 0))
            self.rebuild_nav()

        def grid(self, parent, columns, height):
            frame = ttk.Frame(parent)
            parent.add(frame, weight=1)
            tree = ttk.Treeview(frame, show="headings", height=height, selectmode="browse")
            y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
            tree.grid(row=0, column=0, sticky="nsew")
            y.grid(row=0, column=1, sticky="ns")
            x.grid(row=1, column=0, sticky="ew")
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            self.columns(tree, columns)
            return tree

        def columns(self, tree, columns):
            tree.configure(columns=list(range(len(columns))))
            for i, (label, width) in enumerate(columns):
                tree.heading(i, text=self.tr(label))
                tree.column(i, width=width, minwidth=min(55, width), stretch=(i == 1 or len(columns) == 1))

        def rebuild_nav(self):
            self.sections = {}
            labels = dict(TABLES)
            if self.doc:
                labels["radio"] = "Сведения о рации"
                if (self.doc.report.get("file_model") or self.doc.report.get("usb_model")) in ("MD-UV380", "MD-UV390"):
                    labels.update(ASSIGNMENTS)
                for kind in slot_sections(self.doc.report):
                    labels["cps:" + kind] = TITLES[kind]
                for sec in self.doc.report.get("cps_sections", []):
                    key = "cps:" + sec["id"]
                    self.sections[key] = sec
                    labels[key] = sec["title"]
            self.nav.delete(*self.nav.get_children())
            for key, title in labels.items():
                self.nav.insert("", "end", iid=key, text=self.tr(title))
            if self.section not in labels:
                self.section = "channels"
            self.nav.selection_set(self.section)
            self.nav.see(self.section)
            self.refresh_rows()
            self.update_status()

        def pick_section(self, _):
            sel = self.nav.selection()
            if sel and self.section != sel[0]:
                self.section = sel[0]
                self.query.set("")
                self.refresh_rows()
                self.update_status()

        def refresh_rows(self, select=None):
            key = self.section
            self.table.delete(*self.table.get_children())
            self.detail.delete(*self.detail.get_children())
            self.rows = {}
            self.columns(self.table, COLUMNS.get(key, (("Параметры записи", 440),)))
            self.section_title.config(text=self.nav.item(key, "text") if self.nav.exists(key) else "")
            editable = self.doc is not None
            if key in TABLES:
                rows = self.doc.report.get(key, []) if self.doc else []
            elif key in ASSIGNMENTS:
                rows = assignments(self.doc.report, key)
            elif key == "radio":
                rows = [{"name":"DMR ID", "fields":[{"label":"DMR ID", "value":str(self.doc.report.get("dmr_id") or 0)}]}]
                rows += [{"name":"Примечания", "fields":[{"label":"Данные","value":n} for n in self.doc.report["read_notes"]]}] if self.doc.report.get("read_notes") else []
            else:
                rows = self.sections.get(key, {}).get("rows", [])
            self.buttons["Сохранить JSON…"].state(["!disabled" if editable else "disabled"])
            self.update_write_controls()
            slot_table = editable and key.startswith("cps:") and key[4:] in slot_sections(self.doc.report)
            for label in ("Добавить", "Дублировать", "Удалить"):
                allowed = editable and (key in TABLES or (slot_table and label != "Дублировать"))
                self.buttons[label].state(["!disabled" if allowed else "disabled"])
            self.buttons["Изменить"].state(["!disabled" if editable and rows and key != "cps:basic" else "disabled"])
            query = self.query.get().casefold()
            for i, row in enumerate(rows):
                values = preview(key, row, self.tr)
                if query and not any(query in str(v).casefold() for v in values):
                    continue
                rid = str(row["index"]) if key in TABLES else str(i)
                self.rows[rid] = row
                self.table.insert("", "end", iid=rid, values=values)
            if self.rows:
                selected = str(select) if str(select) in self.rows else next(iter(self.rows))
                self.table.selection_set(selected)
                self.table.see(selected)
                self.refresh_detail()

        def refresh_detail(self):
            self.detail.delete(*self.detail.get_children())
            ids = self.table.selection()
            if not ids or ids[0] not in self.rows:
                return
            row = self.rows[ids[0]]
            fields = assignment_fields(self.section, row) if self.section in ASSIGNMENTS else fields_for(self.section.removeprefix("cps:"), row)
            for f in fields:
                if f.kind == "members":
                    target = "contacts" if f.key == "contacts" else "channels"
                    names = self.ref_names[target]
                    indices = [m["index"] if isinstance(m, dict) else m for m in f.value or []]
                    value = ", ".join(f"{n} · {names[n]}" if n in names else str(n) for n in indices)
                else:
                    value = field_text(f, self.tr)
                    if f.kind == "field" and f.label not in FREE_TEXT:
                        value = self.tr.generated(value)
                self.detail.insert("", "end", values=(self.tr.generated(f.label), value))

        def change_language(self, code):
            self.tr.select(code)
            self.build()

        def update_status(self):
            if self.busy:
                msg = self.tr(self.activity)
            elif self.doc:
                msg = str(self.doc.source or self.tr("Временный документ"))
                if self.dirty:
                    msg += "   ·   " + self.tr("Локальные изменения")
                note = slot_note(self.doc.report, self.section.removeprefix("cps:"))
                if note:
                    msg += "   ·   " + self.tr(note)
            else:
                msg = self.tr("Подключите рацию по USB или откройте RDT, YAML, JSON или CSV.")
            self.status.set(msg)

        def set_doc(self, doc, dirty=False):
            self.doc, self.dirty = doc, dirty
            self.reindex()
            self.query.set("")
            self.rebuild_nav()

        def reindex(self):
            self.ref_names = {table:{row["index"]: row.get("name", "") for row in self.doc.report.get(table, [])}
                              for table in ("channels","contacts")}

        def discard(self):
            return not self.dirty or messagebox.askyesno("TYT Reader", self.tr("Отбросить изменения в текущем документе?"), parent=self.root)

        def quit(self):
            if not self.busy and self.discard():
                if self.print_folder:
                    self.print_folder.cleanup()
                self.root.destroy()

        def new(self):
            if self.discard():
                self.set_doc(Document.new())

        def choose(self, extensions):
            return filedialog.askopenfilename(parent=self.root,
                filetypes=[(self.tr("Файл настроек"), " ".join("*." + ext for ext in extensions)), (self.tr("Все файлы"), "*.*")])

        def open_file(self):
            path = self.choose(("json", "rdt", "yaml", "yml", "csv"))
            if path and self.discard():
                self.set_doc(Document.open(path))

        def template(self):
            path = self.choose(("rdt",))
            if path and self.discard():
                self.set_doc(Document.from_template(path), dirty=True)

        def need_doc(self):
            if not self.doc:
                raise ValueError("Сначала откройте файл или считайте настройки.")
            return self.doc

        def output(self, ext):
            source = self.doc.source if self.doc else None
            stem = Path(source).stem if source else "TYT-Settings"
            return filedialog.asksaveasfilename(parent=self.root, defaultextension=ext,
                    filetypes=[(ext.upper()[1:], "*" + ext)], initialfile=f"{stem}-copy{ext}")

        def save(self):
            doc = self.need_doc()
            path = self.output(".json")
            if path:
                doc.save_json(path)
                self.dirty = False
                self.status.set(self.tr("Скопировано в файл %@", Path(path).name))

        def save_rdt(self):
            doc = self.need_doc()
            if not doc.source or Path(doc.source).suffix.lower() != ".rdt":
                self.transfer()
                return
            path = self.output(".rdt")
            if path:
                doc.save_rdt(path)
                self.dirty = False
                self.status.set(self.tr("Скопировано в файл %@", Path(path).name))

        def transfer(self):
            doc = self.need_doc()
            template = self.choose(("rdt",))
            if template:
                path = self.output(".rdt")
                if path:
                    doc.save_rdt(path, template=template)
                    self.status.set(self.tr("Скопировано в файл %@", Path(path).name))

        def merge_csv(self):
            doc = self.need_doc()
            path = self.choose(("csv",))
            if path:
                doc.merge_csv(path)
                self.set_doc(doc, dirty=True)

        def export(self, kind):
            doc = self.need_doc()
            path = self.output(".yaml" if kind == "yaml" else ".csv")
            if path:
                if kind == "yaml":
                    doc.save_yaml(path)
                else:
                    doc.save_csv(path, kind)
                self.status.set(self.tr("Скопировано в файл %@", Path(path).name))

        def update_write_controls(self):
            ready = not self.busy and self.doc is not None and win_program.eligible(self.doc.report)
            self.buttons["Записать в рацию…"].state(["!disabled" if ready else "disabled"])
            self.service_menu.entryconfigure(self.tr("Записать настройки в рацию…"), state="normal" if ready else "disabled")
            self.service_menu.entryconfigure(self.tr("Восстановить настройки из резервной копии…"), state="disabled" if self.busy else "normal")

        def asynchronous(self, job, success, activity="Чтение по USB…", failure=None):
            self.busy = True
            self.activity = activity
            self.update_write_controls()
            self.update_status()
            def worker():
                try:
                    value = job()
                except Exception as exc:
                    self.events.put((failure, str(exc)))
                else:
                    self.events.put((success, value))
            threading.Thread(target=worker, daemon=True).start()

        def results(self):
            try:
                callback, value = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                self.busy = False
                self.update_write_controls()
                self.update_status()
                if callback:
                    self.run(lambda: callback(value))
                else:
                    messagebox.showerror("TYT Reader", self.tr.generated(value), parent=self.root)
            self.root.after(80, self.results)

        def read(self):
            if self.discard():
                self.asynchronous(Document.read_usb, self.set_doc)

        def write_radio(self):
            doc = self.need_doc()
            report = doc.draft()
            source = str(doc.source or self.tr("Открытая копия в редакторе"))
            self.asynchronous(lambda: write_settings.prepare(report),
                              lambda p: self.review_write(p, source), "Сравниваю настройки с рацией…")

        def restore_radio(self):
            path = filedialog.askopenfilename(parent=self.root, title=self.tr("Восстановить настройки из резервной копии…"),
                                               filetypes=[("TYT Backup", "*.tytbackup")])
            if path:
                self.asynchronous(lambda: prepare_restore(path),
                                  lambda p: self.review_write(p, path, True), "Сравниваю настройки с рацией…")

        def review_write(self, proposal, source, restore=False):
            backup = win_program.review(self.root, self.tr, proposal, source, restore)
            if backup is None:
                return
            def verified(_):
                text = self.tr("Запись проверена повторным чтением. Резервная копия: %@", backup)
                self.status.set(text)
                messagebox.showinfo("TYT Reader", text, parent=self.root)
            def failed(reason):
                message = "\n".join(self.tr.generated(line) for line in reason.splitlines())
                messagebox.showerror("TYT Reader", message, parent=self.root)
            self.asynchronous(lambda: write_settings.apply(proposal, backup=backup), verified,
                              "Записываю и проверяю настройки…", failed)

        def universal(self):
            self.asynchronous(read_universal_usb, lambda report: win_services.show_universal(self.root, self.tr, report))

        def basic(self):
            doc = self.need_doc()
            if not doc.usb_privacy:
                raise ValueError("Эти данные доступны после чтения по USB.")
            win_services.show_usb_privacy(self.root, self.tr, doc.usb_privacy)

        def dtmf(self):
            doc = self.doc or Document.new()
            key = "dtmf_sequences"
            if not any(section["id"] == key for section in doc.report.get("cps_sections", [])):
                draft = copy.deepcopy(doc.report)
                draft.setdefault("cps_sections", []).append(sequence_section())
                doc.apply(draft)
                self.dirty = True
            self.section = "cps:" + key
            self.set_doc(doc, self.dirty)

        def save_sequences(self):
            doc = self.need_doc()
            template = self.choose(("rdt",))
            if template:
                path = self.output(".rdt")
                if path:
                    save_dtmf(doc, template, path)
                    self.status.set(self.tr("Скопировано в файл %@", Path(path).name))

        def print_preview(self):
            from html import escape
            import webbrowser
            doc = self.need_doc()
            title = Path(doc.source).name if doc.source else self.tr("Новый документ")
            html = ['<!doctype html><meta charset="utf-8"><title>' + escape(title) + '</title>',
                    '<style>body{font:14px Segoe UI,sans-serif;margin:2em}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.3em;border-bottom:1px solid #ddd}h2{break-after:avoid}@media print{button{display:none}}</style>',
                    '<button onclick="window.print()">' + escape(self.tr("Печать…")) + '</button><h1>' + escape(title) + '</h1>']
            def cells(tag, values):
                return "<tr>" + "".join("<" + tag + ">" + escape(str(v)) + "</" + tag + ">" for v in values) + "</tr>"
            for table, label in TABLES.items():
                html.append("<h2>" + escape(self.tr(label)) + "</h2><table>")
                html.append(cells("th", [self.tr(col) for col, _ in COLUMNS[table]]))
                for row in doc.report.get(table, []):
                    html.append(cells("td", preview(table, row, self.tr)))
                html.append("</table>")
            for sec in doc.report.get("cps_sections", []):
                html.append("<h2>" + escape(self.tr(sec["title"])) + "</h2><table>")
                for row in sec["rows"]:
                    html.append(cells("th", [self.tr.generated(row["name"]), ""]))
                    for f in row["fields"]:
                        value = f["value"] if f["label"] in FREE_TEXT else self.tr.generated(f["value"])
                        html.append(cells("td", [self.tr.generated(f["label"]), value]))
                html.append("</table>")
            if self.print_folder is None:
                self.print_folder = tempfile.TemporaryDirectory(prefix="tyt-print-")
            with tempfile.NamedTemporaryFile(dir=self.print_folder.name, suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
                f.write("\n".join(html))
            if not webbrowser.open(Path(f.name).as_uri()):
                raise OSError(self.tr("Не удалось открыть окно печати."))

        def selected(self):
            selected = self.table.selection()
            if not selected or selected[0] not in self.rows:
                raise ValueError("Сначала выберите запись.")
            return selected[0], self.rows[selected[0]]

        def apply(self, draft, selected=None):
            self.doc.apply(draft)
            self.dirty = True
            self.reindex()
            self.rebuild_nav()
            self.refresh_rows(selected)

        def copy_row(self):
            if self.section in TABLES:
                self.clip = self.section, copy.deepcopy(self.selected()[1]), weakref.ref(self.doc)

        def delete(self):
            doc = self.need_doc()
            kind = self.section.removeprefix("cps:")
            if self.section not in TABLES and kind not in slot_sections(doc.report):
                return
            _, row = self.selected()
            if messagebox.askyesno("TYT Reader", self.tr("Удалить выбранную запись из локальной копии?"), parent=self.root):
                if self.section not in TABLES:
                    delete_slot(doc, kind, row["name"])
                    self.set_doc(doc, dirty=True)
                    return
                draft = copy.deepcopy(doc.report)
                draft[self.section] = [r for r in draft[self.section] if r["index"] != row["index"]]
                self.apply(draft)

        def edit(self, action="edit"):
            doc, table = self.need_doc(), self.section
            native = bool(doc.source and Path(doc.source).suffix.lower() == ".rdt")
            section_id = table.removeprefix("cps:")
            if table.startswith("cps:") and section_id in slot_sections(doc.report) and action == "add":
                name = add_slot(doc, section_id, text=self.tr("Новое сообщение"))
                self.set_doc(doc, dirty=True)
                pos = next(i for i, r in enumerate(self.sections[table]["rows"]) if r["name"] == name)
                self.refresh_rows(pos)
                self.edit()
                return
            if action != "edit" and table not in TABLES:
                return
            before = self.selected() if action in ("edit", "duplicate") else None
            if table in TABLES:
                if action == "paste" and (not self.clip or self.clip[0] != table):
                    return
                if action == "edit":
                    row = copy.deepcopy(before[1])
                else:
                    duplicate = before[1] if action == "duplicate" else self.clip[1] if action == "paste" else None
                    same = action != "paste" or self.clip[2]() is doc
                    row = new_record(table, doc.report, self.tr("Новая запись"), duplicate, native and same)
                    if not same:
                        row.pop("rdt_template_index", None)
                fields = list(fields_for(table, row, native))
            elif action != "edit" or table == "cps:basic" or (table == "radio" and before[0] != "0"):
                return
            elif table == "radio":
                fields = [Field("dmr_id", "DMR ID", doc.report.get("dmr_id"), "number")]
            elif table in ASSIGNMENTS:
                row = before[1]
                fields = assignment_fields(table, row)
            else:
                row = copy.deepcopy(before[1])
                fields = list(fields_for(table.removeprefix("cps:"), row))
                if section_id == "emergency" and slot_number(section_id, row["name"]):
                    fields.insert(0, Field("emergency_name", "Название системы", row["name"].partition(" · ")[2]))
            def accept(updated):
                if table in ASSIGNMENTS:
                    assignment = dict(row)
                    assignment.update({f.key: value for f, value in updated})
                    set_assignment(doc, table, assignment)
                    self.set_doc(doc, dirty=True)
                    self.refresh_rows(before[0])
                    return
                draft = copy.deepcopy(doc.report)
                selected = before[0] if before else None
                if table == "radio":
                    if updated:
                        draft["dmr_id"] = updated[0][1]
                else:
                    edited_row = copy.deepcopy(row)
                    for f, value in updated:
                        if f.key == "emergency_name":
                            if not value.strip():
                                raise ValueError(self.tr("Неверное значение для поля «%@».", self.tr(f.label)))
                            edited_row["name"] = row["name"].partition(" · ")[0] + " · " + value
                        else:
                            apply_field(edited_row, f, value)
                    if table in TABLES:
                        changed = {f.key for f, value in updated}
                        if table == "channels" and changed.intersection(("privacy_type_code", "privacy_index_code")):
                            limit = 255 if edited_row.get("privacy_type_code") == 3 else 15
                            if (edited_row.get("privacy_index_code") or 0) > limit:
                                raise ValueError(self.tr("Неверное значение для поля «%@».", self.tr("Индекс Privacy (с нуля)")))
                        selected = row["index"]
                        records = [r for r in draft.get(table, []) if r["index"] != selected]
                        draft[table] = sorted(records + [edited_row], key=lambda r: r["index"])
                    else:
                        sec = next(s for s in draft["cps_sections"] if "cps:" + s["id"] == table)
                        sec["rows"][int(selected)] = edited_row
                self.apply(draft, selected)
            self.form(fields, accept)

        def form(self, fields, accept):
            tr = self.tr
            dialog = tk.Toplevel(self.root)
            dialog.title(tr("Изменить"))
            dialog.geometry("595x490")
            dialog.transient(self.root)
            dialog.grab_set()
            outer = ttk.Frame(dialog, padding=12)
            outer.pack(fill="both", expand=True)
            canvas = tk.Canvas(outer, highlightthickness=0)
            scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            canvas.config(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            form = ttk.Frame(canvas)
            wid = canvas.create_window(0, 0, anchor="nw", window=form)
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(wid, width=e.width))
            form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            form.columnconfigure(1, weight=1)
            inputs = []
            for i, field in enumerate(fields):
                label = tr.generated(field.label)
                ttk.Label(form, text=label, wraplength=200).grid(row=i, column=0, sticky="w", padx=4, pady=3)
                start = field_text(field, tr)
                v = tk.StringVar(value=start)
                fixed = field.value == "Служебный профиль CPS" and self.section == "cps:dtmf_sequences"
                if field.options:
                    options = [option_label(field.key, item, tr) for item in field.options]
                    widget = ttk.Combobox(form, textvariable=v, state="readonly",
                                          values=list(dict.fromkeys([start, *options])))
                else:
                    widget = ttk.Entry(form, textvariable=v, state="readonly" if fixed else "normal")
                widget.grid(row=i, column=1, sticky="ew", padx=5, pady=3)
                if field.key in TARGETS:
                    def pick(field=field, var=v):
                        from win_members import choose_members
                        candidates = [(r["index"], r["name"]) for r in self.doc.report.get(TARGETS[field.key], [])]
                        if self.section == "scan_lists":
                            candidates.insert(0, (0, tr("Выбранный на рации")))
                        try:
                            initial = [int(x.strip()) for x in var.get().split(",") if x.strip()]
                        except ValueError:
                            initial = []
                        result = choose_members(dialog, tr, candidates, initial, multiple=field.kind == "members")
                        if result is not None:
                            var.set(", ".join(map(str, result)))
                    ttk.Button(form, text=tr("Выбрать…"), command=pick).grid(row=i, column=2)
                inputs.append((field, start, v))
            def save():
                try:
                    changes = [(f,parse_field(f, v.get(), tr)) for f, old, v in inputs if old != v.get()]
                    accept(changes)
                except Exception as exc:
                    messagebox.showerror("TYT Reader", tr.generated(str(exc)), parent=dialog)
                else:
                    dialog.destroy()
            buttons = ttk.Frame(dialog, padding=(12, 0, 12, 10))
            buttons.pack(fill="x")
            if any(f.kind == "members" for f in fields):
                ttk.Label(buttons, text=tr("Индексы через запятую, в порядке следования"), wraplength=240).pack(side="left")
            ttk.Button(buttons, text=tr("Применить"), command=save).pack(side="right", padx=3)
            ttk.Button(buttons, text=tr("Отмена"), command=dialog.destroy).pack(side="right")
            dialog.bind("<Escape>", lambda e: dialog.destroy())

    root = tk.Tk()
    if smoke:
        root.withdraw()
    app = ReaderWindow(root)
    if smoke:
        app.new()
        assert app.buttons["Записать в рацию…"].instate(["disabled"])
        app.dtmf()
        root.update_idletasks()
        assert app.table.get_children() and app.section == "cps:dtmf_sequences"
        root.destroy()
    else:
        if initial:
            app.run(lambda: app.set_doc(Document.open(initial)))
        root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TYT Reader for Windows")
    parser.add_argument("file", nargs="?")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--smoke-ui", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        offline_check()
    else:
        open_gui(args.smoke_ui, args.file)
