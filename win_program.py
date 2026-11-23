"""Review a stock radio write and select a new backup on Windows."""
from datetime import datetime
from pathlib import Path

from win_records import TABLES
from write_settings import MODELS


def eligible(report):
    if not isinstance(report, dict) or "csv_source" in report:
        return False
    models = [report[k] for k in ("usb_model", "file_model") if k in report]
    return bool(models and models[0] in MODELS and all(m == models[0] for m in models)
                and type(report.get("dmr_id")) is int and 0 <= report["dmr_id"] <= 0xffffff
                and all(isinstance(report.get(key), list) for key in TABLES))


def review(parent, tr, proposal, source, restore=False):
    from tkinter import filedialog, messagebox

    if not proposal["blocks"]:
        messagebox.showinfo("TYT Reader", tr("В доступных для записи полях изменений нет."), parent=parent)
        return None
    name = f'{proposal["model"]}-{proposal["dmr_id"]}-{datetime.now():%Y%m%d-%H%M%S}.tytbackup'
    backup = filedialog.asksaveasfilename(parent=parent, title=tr("Резервная копия перед записью"),
                initialfile=name, defaultextension=".tytbackup", filetypes=[("TYT Backup", "*.tytbackup")])
    if not backup:
        return None
    counts = [f'{tr(label)} — {proposal["changes"][table]}' for table, label in TABLES.items()]
    summary = "\n".join([tr("Источник: %@", source), f'DMR ID {proposal["dmr_id"]}', *counts,
                         tr("Секторов флеш-памяти для обновления: %ld.", proposal["sectors"]),
                         tr("Резервная копия перед записью: %@", backup)])
    notes = [tr("Будут восстановлены изменённые позиции из резервной копии.") if restore else
             tr("Переносятся заполненные позиции поддерживаемых таблиц. Отсутствующие в документе позиции остаются в рации."),
             tr("DMR ID, группы приёма 1–53, DTMF, ключи Privacy и калибровка исключены из записи."),
             tr("Запись из TYT Reader на физическую модель 10 Вт с прошивкой D219.043 ещё не проверена."),
             tr("Рация стирает выбранные секторы перед их перезаписью. При обрыве связи или питания сектор может остаться записанным лишь частично."),
             tr("Оставьте рацию подключённой и включённой до окончания проверки.")]
    approved = messagebox.askyesno(tr("Записать настройки в %@?", proposal["model"]),
                                   summary + "\n\n" + "\n\n".join(notes), parent=parent,
                                   icon=messagebox.WARNING, default=messagebox.NO)
    return Path(backup) if approved else None
