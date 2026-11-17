"""Native Windows/macOS window over the terminal workflow."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from zipfile import BadZipFile

from .desktop import (IDENTITY_LABELS, PROJECT_ROOT, STATE_LABELS, STOP_LABELS,
                      ProcessController, read_snapshot, save_report_copy, sheet_names)

BG, WHITE, INK, MUTED, BLUE = "#F3F5F9", "#FFFFFF", "#17243B", "#64748B", "#2457DB"
AUTO_SHEET = "Определить автоматически"
FACT_LABELS = {"legal_name": "Название по источнику", "inn": "ИНН по источнику", "ogrn": "ОГРН/ОГРНИП", "kpp": "КПП",
               "legal_address": "Юридический адрес", "actual_address": "Фактический адрес", "phone": "Телефон", "email": "Email",
               "okved": "ОКВЭД", "primary_activity": "Основная деятельность", "additional_activities": "Дополнительная деятельность",
               "products_services": "Продукты и услуги", "product_service_type": "Тип", "functional_profile": "Функциональный профиль",
               "functions_summary": "Описание функций"}


def open_local(path: Path):
    if not path.exists():
        raise ValueError("Файл или папка ещё не созданы.")
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class NewRunDialog:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("Новое исследование")
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.resizable(True, False)
        self.window.geometry("740x410")
        frame = ttk.Frame(self.window, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        self.file = tk.StringVar()
        self.sheet = tk.StringVar(value=AUTO_SHEET)
        self.folder = tk.StringVar()
        ttk.Label(frame, text="Выберите исходный Excel", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))
        ttk.Label(frame, text="Файл .xlsx").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.file, state="readonly").grid(row=2, column=0, sticky="ew", pady=(5, 14))
        ttk.Button(frame, text="Выбрать…", command=self.choose_file).grid(row=2, column=1, padx=(10, 0), pady=(5, 14))
        ttk.Label(frame, text="Лист с компаниями").grid(row=3, column=0, sticky="w")
        self.sheets = ttk.Combobox(frame, textvariable=self.sheet, state="readonly", values=[AUTO_SHEET])
        self.sheets.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 14))
        ttk.Label(frame, text="Папка для прогресса и результатов").grid(row=5, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.folder).grid(row=6, column=0, sticky="ew", pady=(5, 14))
        ttk.Button(frame, text="Выбрать…", command=self.choose_folder).grid(row=6, column=1, padx=(10, 0), pady=(5, 14))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Отмена", command=self.window.destroy).pack(side="left", padx=(0, 10))
        ttk.Button(buttons, text="Создать очередь", style="Primary.TButton", command=self.create).pack(side="left")

    def choose_file(self):
        name = filedialog.askopenfilename(parent=self.window, title="Исходный Excel", filetypes=[("Excel", "*.xlsx")])
        if not name:
            return
        try:
            names = sheet_names(Path(name))
        except (OSError, ValueError, KeyError, BadZipFile) as exc:
            messagebox.showerror("Не удалось открыть Excel", str(exc), parent=self.window)
            return
        self.file.set(name)
        self.sheets.configure(values=[AUTO_SHEET, *names])
        self.sheet.set(AUTO_SHEET)
        slug = re.sub(r"[^\w-]+", "-", Path(name).stem, flags=re.UNICODE).strip("-")[:55] or "research"
        self.folder.set(str(self.app.runs_root / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"))

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.window, title="Папка нового исследования")
        if folder:
            self.folder.set(folder)

    def create(self):
        if not self.file.get() or not self.folder.get().strip():
            messagebox.showinfo("Новое исследование", "Выберите Excel и папку исследования.", parent=self.window)
            return
        folder = Path(self.folder.get()).expanduser().resolve()
        if (folder / "state.sqlite3").exists():
            messagebox.showerror("Очередь уже существует", "Откройте это исследование для продолжения или выберите новую папку.", parent=self.window)
            return
        args = ["init", self.file.get(), "--run-dir", str(folder)]
        if self.sheet.get() != AUTO_SHEET:
            args += ["--sheet", self.sheet.get()]
        self.app.pending_run = folder
        self.window.destroy()
        self.app.start_job("init", args, "Импортируем компании из Excel…")


class BenchmarkWindow:
    def __init__(self, root, *, run_dir=None, binary="codex", python=None, check_auth=True):
        self.root, self.binary = root, binary
        self.controller = ProcessController(python=python)
        self.runs_root = PROJECT_ROOT / "runs"
        self.run_dir = None
        self.snapshot = None
        self.action = None
        self.pending_run = None
        self.stop_requested = False
        self.close_when_finished = False
        self.render_signature = None
        self.notice = None
        self.company_by_id = {}
        self.auth_ready = False
        self.after_id = None
        self.closed = False
        self.root.title("Benchmark — исследование компаний")
        self.root.geometry("1260x900")
        self.root.minsize(1000, 760)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        if run_dir is None and (self.runs_root / "pilot" / "state.sqlite3").is_file():
            run_dir = self.runs_root / "pilot"
        if run_dir:
            self.load_run(Path(run_dir))
        self.update_controls()
        if check_auth:
            self.check_codex()
        self.after_id = self.root.after(150, self.tick)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Arial", 11))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Arial", 19, "bold"))
        style.configure("Brand.TLabel", font=("Arial", 27, "bold"))
        style.configure("Card.TFrame", background=WHITE)
        style.configure("Card.TLabel", background=WHITE, foreground=MUTED)
        style.configure("Metric.TLabel", background=WHITE, foreground=INK, font=("Arial", 26, "bold"))
        style.configure("TButton", padding=(12, 8), font=("Arial", 11), background=WHITE,
                        foreground=INK, borderwidth=1, relief="flat")
        style.configure("TCheckbutton", background=BG, foreground=INK, font=("Arial", 11))
        for name in ["TEntry", "TCombobox", "TSpinbox"]:
            style.configure(name, font=("Arial", 11))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#E8EDF5", foreground=MUTED,
                        font=("Arial", 11), padding=(14, 7))
        style.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", INK)])
        style.configure("Primary.TButton", background=BLUE, foreground=WHITE)
        style.map("Primary.TButton", background=[("active", "#1947BB"), ("disabled", "#C8D3EA")], foreground=[("disabled", "#65738C")])
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=INK, rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"), background="#E8EDF5", foreground=INK, padding=(8, 9))
        style.map("Treeview", background=[("selected", "#DBE7FF")], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#E0E6F0", thickness=8)

    def _build(self):
        content = ttk.Frame(self.root, padding=(24, 16))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(5, weight=1)
        header = ttk.Frame(content)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Benchmark", style="Brand.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Источники и досье компаний", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.open_button = ttk.Button(header, text="Открыть исследование", command=self.choose_run)
        self.open_button.grid(row=0, column=1, rowspan=2, padx=(12, 8))
        self.new_button = ttk.Button(header, text="Новое из Excel", style="Primary.TButton", command=lambda: NewRunDialog(self))
        self.new_button.grid(row=0, column=2, rowspan=2)

        info = ttk.Frame(content, padding=(0, 14, 0, 10))
        info.grid(row=1, column=0, sticky="ew")
        self.source_text = tk.StringVar(value="Выберите Excel или откройте папку существующего исследования")
        self.path_text = tk.StringVar(value="Исходный файл сохраняется без изменений")
        ttk.Label(info, textvariable=self.source_text, wraplength=1120).pack(anchor="w")
        ttk.Label(info, textvariable=self.path_text, style="Muted.TLabel", wraplength=1120).pack(anchor="w", pady=(4, 0))

        metrics = ttk.Frame(content)
        metrics.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.metrics = {}
        for i, label in enumerate(["Всего компаний", "Сохранено", "Ожидают", "На проверку", "Ошибки"]):
            metrics.columnconfigure(i, weight=1, uniform="metric")
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(16, 12))
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            value = tk.StringVar(value="—")
            ttk.Label(card, textvariable=value, style="Metric.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w", pady=(4, 0))
            self.metrics[label] = value

        controls = ttk.Frame(content)
        controls.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.start_button = ttk.Button(controls, text="Начать обработку", style="Primary.TButton", command=self.start_research)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Остановить", command=self.stop)
        self.stop_button.pack(side="left", padx=(8, 18))
        ttk.Label(controls, text="Компаний за запуск:").pack(side="left")
        self.batch = tk.StringVar(value="10")
        self.batch_input = ttk.Spinbox(controls, from_=0, to=100000, textvariable=self.batch, width=6)
        self.batch_input.pack(side="left", padx=(8, 8))
        ttk.Label(controls, text="0 — вся очередь", style="Muted.TLabel").pack(side="left")
        self.retry = tk.BooleanVar(value=False)
        self.retry_input = ttk.Checkbutton(controls, text="Повторить ошибки", variable=self.retry)
        self.retry_input.pack(side="right")

        progress = ttk.Frame(content)
        progress.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.progress = ttk.Progressbar(progress, maximum=1)
        self.progress.pack(fill="x")
        self.status_text = tk.StringVar(value="Готово к работе")
        ttk.Label(progress, textvariable=self.status_text, style="Muted.TLabel", wraplength=1120).pack(anchor="w", pady=(7, 0))

        notebook = ttk.Notebook(content)
        notebook.grid(row=5, column=0, sticky="nsew")
        companies_tab = ttk.Frame(notebook, padding=10)
        notebook.add(companies_tab, text=" Компании ")
        self._companies_tab(companies_tab)
        log_tab = ttk.Frame(notebook, padding=10)
        notebook.add(log_tab, text=" Журнал работы ")
        self.log = self._text_box(log_tab)
        self.notebook = notebook

        footer = ttk.Frame(content, padding=(0, 10, 0, 0))
        footer.grid(row=6, column=0, sticky="ew")
        self.auth_text = tk.StringVar(value="Проверка Codex…")
        ttk.Label(footer, textvariable=self.auth_text, style="Muted.TLabel").pack(side="left")
        self.login_button = ttk.Button(footer, text="Войти через ChatGPT", command=self.login)
        self.login_button.pack(side="left", padx=8)
        self.check_button = ttk.Button(footer, text="Проверить", command=self.check_codex)
        self.check_button.pack(side="left")
        self.save_button = ttk.Button(footer, text="Сохранить отчёт…", command=self.save_report)
        self.save_button.pack(side="right")
        self.report_button = ttk.Button(footer, text="Открыть отчёт", command=self.open_report)
        self.report_button.pack(side="right", padx=8)

    def _text_box(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", bg=WHITE, fg=INK, relief="flat", padx=14, pady=12,
                       font=("Arial", 11), state="disabled", cursor="arrow", height=6,
                       borderwidth=0, highlightthickness=1, highlightbackground="#D6DFEC", highlightcolor="#D6DFEC")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text

    def _companies_tab(self, frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        filters = ttk.Frame(frame)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        ttk.Label(filters, text="Поиск по названию или ИНН").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(filters, textvariable=self.search, width=30).pack(side="left", padx=10)
        self.filter = tk.StringVar(value="Все компании")
        ttk.Combobox(filters, textvariable=self.filter, state="readonly", width=22,
                     values=["Все компании", "Нужна проверка", "Ошибки", "В очереди"]).pack(side="right")
        self.search.trace_add("write", lambda *_: self.render_companies(force=True))
        self.filter.trace_add("write", lambda *_: self.render_companies(force=True))
        self.panes = ttk.Panedwindow(frame, orient="vertical")
        self.panes.grid(row=1, column=0, sticky="nsew")
        table_frame = ttk.Frame(self.panes)
        self.panes.add(table_frame, weight=1)
        columns = ("number", "name", "inn", "state", "site")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=7)
        for key, title, width in [("number", "№", 42), ("name", "Компания", 300), ("inn", "ИНН", 120),
                                  ("state", "Статус", 145), ("site", "Сайт / кандидат", 230)]:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=40, stretch=key in {"name", "site"})
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.tag_configure("failed", foreground="#A32932")
        self.table.tag_configure("needs_review", foreground="#865B19")
        self.table.tag_configure("running", foreground=BLUE)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.show_company())
        details_pane = ttk.Frame(self.panes)
        self.panes.add(details_pane, weight=1)
        ttk.Label(details_pane, text="Карточка компании", style="Muted.TLabel").pack(anchor="w", pady=(8, 5))
        self.details = self._text_box(details_pane)
        self.panes.bind("<Configure>", lambda event: self.panes.sashpos(0, event.height // 2) if event.height > 2 else None)
        self.set_text(self.details, "Выберите компанию в таблице, чтобы посмотреть сведения, источники и замечания.")

    @staticmethod
    def set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", value)
        widget.configure(state="disabled")

    def append_log(self, value):
        self.log.configure(state="normal")
        self.log.insert("end", value + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 2500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_controls(self):
        idle = self.action is None
        for widget in [self.open_button, self.new_button, self.login_button, self.check_button, self.batch_input, self.retry_input]:
            widget.configure(state="normal" if idle else "disabled")
        can_run = bool(self.snapshot and (self.snapshot.states.get("pending") or self.snapshot.states.get("running")
                                        or (self.retry.get() and self.snapshot.states.get("failed"))))
        self.start_button.configure(state="normal" if idle and can_run and self.auth_ready else "disabled")
        self.stop_button.configure(state="normal" if self.action == "run" and not self.stop_requested else "disabled")
        report_ready = self.snapshot and self.snapshot.saved > 0
        for button in [self.report_button, self.save_button]:
            button.configure(state="normal" if idle and report_ready else "disabled")
        self.start_button.configure(text="Продолжить обработку" if self.snapshot and self.snapshot.saved else "Начать обработку")

    def load_run(self, path):
        try:
            snapshot = read_snapshot(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Не удалось открыть исследование", str(exc), parent=self.root)
            return
        self.run_dir, self.snapshot = snapshot.run_dir, snapshot
        self.notice = None
        self.source_text.set("Исходный Excel: " + str(snapshot.source.get("path", "не указан")))
        self.path_text.set("Исследование: " + str(self.run_dir))
        self.render_signature = None
        self.search.set("")
        self.filter.set("Все компании")
        self.refresh()

    def choose_run(self):
        path = filedialog.askdirectory(parent=self.root, title="Папка исследования с файлом state.sqlite3",
                                       initialdir=str(self.runs_root if self.runs_root.exists() else PROJECT_ROOT))
        if path:
            self.load_run(Path(path))

    def refresh(self):
        if self.run_dir is None:
            self.update_controls()
            return
        try:
            self.snapshot = read_snapshot(self.run_dir)
        except (OSError, ValueError) as exc:
            self.status_text.set(str(exc))
            return
        snapshot = self.snapshot
        for label, value in [("Всего компаний", snapshot.total), ("Сохранено", snapshot.saved),
                             ("Ожидают", snapshot.states.get("pending", 0)), ("На проверку", snapshot.states.get("needs_review", 0)),
                             ("Ошибки", snapshot.states.get("failed", 0))]:
            self.metrics[label].set(str(value))
        self.progress.configure(maximum=max(snapshot.total, 1), value=snapshot.saved)
        if self.action == "run":
            active = next((c for c in snapshot.companies if c["state"] == "running"), None)
            if self.stop_requested:
                self.status_text.set("Окно закроется после сохранения текущей компании…" if self.close_when_finished
                                     else "Останавливаемся после сохранения текущей компании…")
            elif active:
                self.status_text.set(f"Обрабатывается: {active['name']} · Сохранено {snapshot.saved} из {snapshot.total}")
        elif self.action is None:
            reason = (snapshot.last_stop or {}).get("reason")
            text = STOP_LABELS.get(reason, "Готово к обработке")
            self.status_text.set(self.notice or f"{text} · Сохранено {snapshot.saved} из {snapshot.total}")
        self.render_companies()
        self.update_controls()

    def render_companies(self, force=False):
        if self.snapshot is None or not hasattr(self, "table"):
            return
        self.company_by_id = {c["id"]: c for c in self.snapshot.companies}
        query = self.search.get().strip().casefold()
        state = {"Нужна проверка": "needs_review", "Ошибки": "failed", "В очереди": "pending"}.get(self.filter.get())
        rows = [c for c in self.snapshot.companies if (not state or c["state"] == state)
                and (not query or query in c["name"].casefold() or query in c["inn"])]
        signature = [(c["id"], c["state"], c["attempts"], c["site"], c["quality"], c["error"]) for c in rows]
        if not force and signature == self.render_signature:
            return
        self.render_signature = signature
        selected = self.table.selection()
        position = self.table.yview()
        self.table.delete(*self.table.get_children())
        for company in rows:
            self.table.insert("", "end", iid=company["id"], values=(company["position"], company["name"], company["inn"],
                              STATE_LABELS.get(company["state"], company["state"]), company["site"]), tags=(company["state"],))
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])
            self.show_company()
        else:
            self.set_text(self.details, "Выберите компанию в таблице, чтобы посмотреть сведения, источники и замечания.")
        if position:
            self.table.yview_moveto(position[0])

    def show_company(self):
        selection = self.table.selection()
        if not selection or selection[0] not in self.company_by_id:
            return
        company = self.company_by_id[selection[0]]
        lines = [company["name"], f"Входной ИНН: {company['inn']} · {STATE_LABELS[company['state']]}"]
        card = company["card"]
        if card:
            identity = card["identity"]
            lines += ["", "Принадлежность сайта: " + IDENTITY_LABELS.get(identity["status"], identity["status"]), identity["reason"]]
            for key, fact in card["facts"].items():
                if fact["value"] is not None:
                    lines += ["", f"{FACT_LABELS.get(key, key)}: {fact['value']}", "Источники: " + ", ".join(fact["source_ids"])]
            if card["missing_information"]:
                lines += ["", "Не найдено: " + ", ".join(FACT_LABELS.get(k, k) for k in card["missing_information"])]
            warnings = company["quality"].get("warnings", card["warnings"])
            if warnings:
                lines += ["", "Замечания:", *["• " + w for w in warnings]]
            lines += ["", "Открытые источники:"]
            lines += [f"{s['source_id']} · {s['title']}\n{s['url']}\n{s['accessed_at']} · {s['excerpt']}" for s in card["sources"]]
        else:
            lines += ["", "Сохранённой карточки пока нет."]
        if company["error"]:
            lines += ["", "Ошибка последней попытки: " + company["error"]]
        self.set_text(self.details, "\n".join(lines))

    def start_job(self, action, arguments, message):
        try:
            self.controller.start(action, arguments)
        except ValueError as exc:
            messagebox.showerror("Операция уже выполняется", str(exc), parent=self.root)
            return
        self.action = action
        self.notice = None
        self.status_text.set(message)
        self.append_log("\n" + message)
        self.update_controls()

    def check_codex(self):
        self.start_job("doctor", ["doctor", "--codex", self.binary], "Проверяем Codex и вход через ChatGPT…")

    def login(self):
        self.notebook.select(1)
        self.start_job("login", ["login", "--codex", self.binary], "Откроется вход через ChatGPT. Завершите его в браузере.")

    def start_research(self):
        try:
            number = int(self.batch.get())
            if number < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Число компаний", "Укажите целое число от 0. Значение 0 означает всю очередь.", parent=self.root)
            return
        self.stop_requested = False
        args = ["run", "--run-dir", str(self.run_dir), "--max-companies", str(number), "--codex", self.binary]
        if self.retry.get():
            args.append("--retry-failed")
        self.start_job("run", args, "Запускаем исследование компаний…")

    def stop(self):
        if self.action != "run" or self.run_dir is None:
            return
        self.stop_requested = True
        self._write_stop()
        self.status_text.set("Останавливаемся после сохранения текущей компании…")
        self.update_controls()

    def _write_stop(self):
        try:
            (self.run_dir / "stop.request").write_text(datetime.now().isoformat(), encoding="utf-8")
        except OSError as exc:
            self.append_log("Не удалось передать остановку: " + str(exc))

    def open_report(self):
        self.after_export = ("open", None)
        self.start_job("export", ["export", "--run-dir", str(self.run_dir)], "Обновляем отчёт…")

    def save_report(self):
        name = filedialog.asksaveasfilename(parent=self.root, title="Сохранить отчёт", initialfile="Benchmark-отчёт.xlsx",
                                           defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if name:
            self.after_export = ("save", Path(name))
            self.start_job("export", ["export", "--run-dir", str(self.run_dir)], "Обновляем отчёт для сохранения…")

    def tick(self):
        self.after_id = None
        while True:
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if event["type"] == "log":
                if event["action"] != "doctor":
                    self.append_log(event["text"])
            elif event["type"] == "finished":
                self.finished(event)
                if self.closed:
                    return
        if self.stop_requested and self.action == "run":
            # A request made during CLI startup must survive its initial cleanup.
            self._write_stop()
        self.refresh()
        self.after_id = self.root.after(650, self.tick)

    def finished(self, event):
        action, code = event["action"], event["code"]
        self.action = None
        if action == "doctor":
            self.auth_ready = code == 0
            self.auth_text.set("Codex подключён · ChatGPT" if self.auth_ready else "Нужен вход в Codex или установка CLI")
            if code != 0:
                self.append_log(event["output"])
        elif action == "login":
            if code == 0 and not self.close_when_finished:
                self.check_codex()
            else:
                self.auth_ready = False
                self.auth_text.set("Вход не завершён. Можно повторить.")
        elif action == "init":
            if code == 0:
                self.load_run(self.pending_run)
            else:
                messagebox.showerror("Не удалось создать очередь", event["output"][-2500:], parent=self.root)
        elif action == "export":
            if code == 0:
                mode, path = self.after_export
                try:
                    if mode == "open":
                        open_local(self.run_dir / "reports" / "report.xlsx")
                    else:
                        save_report_copy(self.run_dir, path)
                        self.append_log("Отчёт сохранён: " + str(path))
                        messagebox.showinfo("Отчёт сохранён", str(path), parent=self.root)
                except (OSError, ValueError) as exc:
                    messagebox.showerror("Отчёт", str(exc), parent=self.root)
            else:
                messagebox.showerror("Не удалось обновить отчёт", event["output"][-2000:], parent=self.root)
        elif action == "run":
            self.stop_requested = False
            if code == 75:
                self.auth_text.set("Обработка приостановлена · проверьте лимит или вход")
            elif code not in {0, 130} and not event["cancelled"]:
                self.append_log("Запуск завершился с ошибкой. Результаты сохранены; подробности выше.")
                self.notice = "Не удалось завершить запуск. Подробности — во вкладке «Журнал работы»."
                self.notebook.select(1)
            self.refresh()
        if self.close_when_finished and self.action is None:
            self.destroy()
            return
        self.update_controls()

    def close(self):
        if self.action == "run":
            self.close_when_finished = True
            self.stop()
            self.status_text.set("Окно закроется после сохранения текущей компании…")
        elif self.action in {"login", "doctor"}:
            self.close_when_finished = True
            self.controller.cancel()
        elif self.action is not None:
            self.close_when_finished = True
            self.status_text.set("Завершаем операцию перед закрытием…")
        else:
            self.destroy()

    def destroy(self):
        self.closed = True
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.root.destroy()


def launch(run_dir=None, binary="codex"):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise ValueError("Не удалось открыть окно приложения. Запустите его в графическом сеансе Windows или macOS.") from exc
    BenchmarkWindow(root, run_dir=run_dir, binary=binary)
    root.mainloop()
    return 0
