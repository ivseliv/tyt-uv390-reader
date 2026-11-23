"""Ordered local codeplug reference picker for the Windows GUI."""


def choose_members(parent, tr, candidates, initial, multiple=True):
    """Return a new list of slot IDs, or None on cancellation; keep unresolved references."""
    import tkinter as tk
    from tkinter import ttk

    names = dict(candidates)
    selected = list(initial) if multiple else list(initial)[:1]
    visible = []
    result = None
    previous_grab = parent.grab_current()
    previous_focus = parent.focus_get()
    window = tk.Toplevel(parent)
    window.title(tr("В порядке следования" if multiple else "Назначение"))
    window.transient(parent.winfo_toplevel())
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure((0, 1), weight=1, uniform="lists")
    frame.rowconfigure(2, weight=1)
    ttk.Label(frame, text=tr("Найти номер или название")).grid(row=0, column=0, sticky="w")
    ttk.Label(frame, text=tr("В порядке следования" if multiple else "Назначение")).grid(row=0, column=1, sticky="w")
    search = tk.StringVar(window)
    entry = ttk.Entry(frame, textvariable=search)
    entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(3, 6))

    def listbox(column, mode):
        box = ttk.Frame(frame)
        box.grid(row=2, column=column, sticky="nsew", padx=(0, 8) if not column else 0)
        items = tk.Listbox(box, selectmode=mode, exportselection=False, width=32, height=14)
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=items.yview)
        items.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        items.pack(side="left", fill="both", expand=True)
        return items

    available = listbox(0, tk.EXTENDED if multiple else tk.BROWSE)
    assigned = listbox(1, tk.BROWSE)

    def buttons(*_):
        add_btn.configure(state="normal" if available.curselection() else "disabled")
        pos = assigned.curselection()
        remove_btn.configure(state="normal" if pos else "disabled")
        if multiple:
            up.configure(state="normal" if pos and pos[0] > 0 else "disabled")
            down.configure(state="normal" if pos and pos[0] + 1 < len(selected) else "disabled")

    def refresh_available(*_):
        term = search.get().strip().casefold()
        visible[:] = [key for key, name in names.items() if term in name.casefold() or term in str(key)]
        available.delete(0, "end")
        if visible:
            available.insert("end", *(f"{key} · {names[key]}" for key in visible))
        buttons()

    def refresh_selected(position=None):
        assigned.delete(0, "end")
        for key in selected:
            assigned.insert("end", f"{key} · {names.get(key, tr('Запись отсутствует'))}")
        if position is not None and selected:
            position = min(position, len(selected) - 1)
            assigned.selection_set(position)
            assigned.see(position)
        buttons()

    def add(*_):
        for index in available.curselection():
            key = visible[index]
            if not multiple:
                selected[:] = [key]
            elif key not in selected:
                selected.append(key)
        refresh_selected(len(selected) - 1 if selected else None)
        return "break"

    def remove(*_):
        positions = assigned.curselection()
        if positions:
            del selected[positions[0]]
            refresh_selected(positions[0])
        return "break"

    def move(delta):
        positions = assigned.curselection()
        if positions:
            i = positions[0]
            j = i + delta
            if 0 <= j < len(selected):
                selected[i], selected[j] = selected[j], selected[i]
                refresh_selected(j)

    def accept():
        nonlocal result
        result = list(selected)
        window.destroy()

    add_btn = ttk.Button(frame, text=tr("Добавить →" if multiple else "Назначить →"), command=add)
    add_btn.grid(row=3, column=0, sticky="w", pady=6)
    actions = ttk.Frame(frame)
    actions.grid(row=3, column=1, sticky="w", pady=6)
    remove_btn = ttk.Button(actions, text=tr("Убрать"), command=remove)
    remove_btn.pack(side="left")
    if multiple:
        up = ttk.Button(actions, text=tr("Выше"), command=lambda: move(-1))
        down = ttk.Button(actions, text=tr("Ниже"), command=lambda: move(1))
        up.pack(side="left", padx=3)
        down.pack(side="left")
    footer = ttk.Frame(frame)
    footer.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(footer, text=tr("Отмена"), command=window.destroy).pack(side="left", padx=4)
    ttk.Button(footer, text=tr("Применить"), command=accept).pack(side="left")
    search.trace_add("write", refresh_available)
    available.bind("<<ListboxSelect>>", buttons)
    assigned.bind("<<ListboxSelect>>", buttons)
    available.bind("<Double-Button-1>", add)
    available.bind("<Return>", add)
    assigned.bind("<Delete>", remove)
    window.bind("<Escape>", lambda _e: window.destroy())
    refresh_available()
    refresh_selected()
    window.grab_set()
    entry.focus_set()
    try:
        parent.wait_window(window)
    finally:
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        if previous_focus is not None and previous_focus.winfo_exists():
            previous_focus.focus_set()
    return result
