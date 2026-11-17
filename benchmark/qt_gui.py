"""Modern desktop workspace. All research still runs through the terminal backend."""
from __future__ import annotations

import os
import queue
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QRegularExpression, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence, QPalette, QRegularExpressionValidator, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSplitter, QTableView, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from .desktop import (STATE_LABELS, STOP_LABELS, ProcessController, read_snapshot,
                      save_report_copy, sheet_names)
from .ui_models import COLUMNS, CompanyFilter, CompanyModel, StatusDelegate, company_documents
from .ui_theme import icon, palette, stylesheet
from .runtime import runs_root
from . import __version__

AUTO_SHEET = "Определить автоматически"
FILTERS = [("Все", set()), ("В очереди", {"pending"}), ("На проверку", {"needs_review"}),
           ("Ошибки", {"failed"}), ("Завершены", {"completed"})]


def label(text="", name=None, parent=None):
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def tool(text, callback=None, name=None):
    button = QToolButton()
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    if name:
        button.setObjectName(name)
    if callback:
        button.clicked.connect(callback)
    return button


class NavButton(QPushButton):
    def __init__(self, title, symbol):
        super().__init__()
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setFixedHeight(44)
        self.setToolTip(title)
        self.symbol = symbol
        row = QHBoxLayout(self); row.setContentsMargins(12, 8, 12, 8); row.setSpacing(11)
        self.picture, self.title, self.count = label(), label(title, "navText"), label("", "navCount")
        self.picture.setFixedSize(20, 20)
        for widget in [self.picture, self.title, self.count]:
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.picture); row.addWidget(self.title, 1); row.addWidget(self.count)
        self.count.hide()
        self.picture.setPixmap(icon(symbol, "#A6B4CE").pixmap(20, 20))

    def compact(self, enabled):
        self.title.setVisible(not enabled)
        self.count.setVisible(not enabled and bool(self.count.text()))


class Metric(QPushButton):
    def __init__(self, title, note):
        super().__init__()
        self.setObjectName("metric")
        self.setMinimumHeight(100)
        row = QVBoxLayout(self); row.setContentsMargins(18, 13, 18, 13); row.setSpacing(3)
        self.title, self.value, self.note = label(title, "metricTitle"), label("—", "metricValue"), label(note, "metricNote")
        for widget in [self.title, self.value, self.note]:
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(widget)


class NewResearchDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle("Новое исследование")
        self.resize(640, 390)
        self.run_dir = None
        box = QVBoxLayout(self); box.setContentsMargins(28, 26, 28, 24); box.setSpacing(16)
        box.addWidget(label("Добавить компании", "pageTitle"))
        box.addWidget(label("Выберите выгрузку Excel и папку для сохранения прогресса.", "subtitle"))
        form = QFormLayout(); form.setVerticalSpacing(16)
        self.source, self.destination = QLineEdit(), QLineEdit()
        self.source.setReadOnly(True); self.source.setPlaceholderText("Файл .xlsx")
        self.sheets = QComboBox(); self.sheets.addItem(AUTO_SHEET)
        source_row = QWidget(); sr = QHBoxLayout(source_row); sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(self.source); choose = QPushButton("Выбрать…"); choose.clicked.connect(self.choose_file); sr.addWidget(choose)
        dest_row = QWidget(); dr = QHBoxLayout(dest_row); dr.setContentsMargins(0, 0, 0, 0)
        dr.addWidget(self.destination); folder = tool("…", self.choose_folder); dr.addWidget(folder)
        form.addRow("Исходный Excel", source_row); form.addRow("Лист с компаниями", self.sheets); form.addRow("Папка исследования", dest_row)
        box.addLayout(form); box.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать очередь")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        self.buttons.rejected.connect(self.reject); self.buttons.accepted.connect(self.validate)
        box.addWidget(self.buttons)

    def choose_file(self):
        name, _ = QFileDialog.getOpenFileName(self, "Исходный Excel", "", "Excel (*.xlsx)")
        if not name:
            return
        try:
            names = sheet_names(Path(name))
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Не удалось открыть файл", str(exc)); return
        self.source.setText(name); self.sheets.clear(); self.sheets.addItems([AUTO_SHEET, *names])
        slug = re.sub(r"[^\w-]+", "-", Path(name).stem).strip("-")[:50] or "research"
        self.destination.setText(str(runs_root() / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"))

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка нового исследования")
        if folder:
            self.destination.setText(folder)

    def validate(self):
        if not self.source.text() or not self.destination.text().strip():
            QMessageBox.information(self, "Нужен файл и папка", "Выберите Excel и папку для результатов."); return
        path = Path(self.destination.text()).expanduser().resolve()
        if (path / "state.sqlite3").exists():
            QMessageBox.warning(self, "Исследование уже существует", "Откройте его для продолжения или выберите новую папку."); return
        self.run_dir = path
        self.accept()


class BenchmarkWindow(QMainWindow):
    def __init__(self, *, run_dir=None, binary="codex", python=None, settings=None, check_auth=True):
        super().__init__()
        self.settings = settings if settings is not None else QSettings("Benchmark", "Workspace")
        self.binary = binary
        self.controller = ProcessController(python=python)
        self.run_dir = None; self.snapshot = None; self.action = None; self.pending_run = None
        self.stop_requested = False; self.close_when_finished = False; self.closed = False
        self.notice = None; self.selected_id = None; self.auth_ready = False
        self.login_url = ""
        self.displayed_id = None
        self.theme = self.settings.value("theme", "light")
        self.theme = self.theme if self.theme in {"light", "dark"} else "light"
        self.compact_rows = self.settings.value("compact_rows", False, type=bool)
        self.sidebar_collapsed = self.settings.value("sidebar_collapsed", False, type=bool)
        self.timeout = self.settings.value("timeout", 420, type=int)
        self.token_budget = self.settings.value("token_budget", 0, type=int)
        self.model_name = self.settings.value("model", "")
        self.retry_failed = self.settings.value("retry_failed", False, type=bool)
        self._icons = []
        self.setWindowTitle(f"Benchmark {__version__} — рабочее пространство")
        self.resize(1440, 900); self.setMinimumSize(1060, 700)
        self._build()
        self.apply_theme(self.theme)
        self._restore_layout()
        if run_dir is None:
            last = self.settings.value("last_run", "")
            candidate = Path(last) if last else runs_root() / "pilot"
            run_dir = candidate if (candidate / "state.sqlite3").is_file() else None
        if run_dir:
            self.load_run(Path(run_dir))
        self.update_controls()
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(650)
        if check_auth:
            self.check_codex()

    def _icon(self, button, name, *, white=False):
        self._icons.append((button, name, white))
        button.setIcon(icon(name, "#FFFFFF" if white else palette(self.theme)["muted"]))
        button.setIconSize(QSize(18, 18))

    def _build(self):
        root = QWidget(); row = QHBoxLayout(root); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(0)
        self.setCentralWidget(root)
        self.sidebar = QFrame(); self.sidebar.setObjectName("sidebar"); self.sidebar.setFixedWidth(248)
        side = QVBoxLayout(self.sidebar); side.setContentsMargins(16, 24, 16, 20); side.setSpacing(6)
        brand = QHBoxLayout(); brand.setSpacing(10)
        mark = label("B", "brandMark"); mark.setAlignment(Qt.AlignmentFlag.AlignCenter); mark.setFixedSize(34, 34)
        self.brand_name = label("Benchmark", "brand"); brand.addWidget(mark); brand.addWidget(self.brand_name); brand.addStretch(); side.addLayout(brand)
        side.addSpacing(34); self.nav_caption = label("РАБОЧЕЕ ПРОСТРАНСТВО", "navCaption"); side.addWidget(self.nav_caption); side.addSpacing(8)
        self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True)
        self.nav = {}
        for key, title, symbol in [("companies", "Компании", "grid")]:
            button = NavButton(title, symbol); self.nav[key] = button; self.nav_group.addButton(button); side.addWidget(button)
            button.clicked.connect(lambda checked=False: self.show_filter(set()))
        self.nav["companies"].setChecked(True)
        side.addSpacing(20)
        self.open_run_button = NavButton("Открыть исследование", "folder"); self.open_run_button.setCheckable(False)
        self.open_run_button.clicked.connect(self.choose_run); side.addWidget(self.open_run_button)
        side.addStretch()
        self.auth_label = label("Проверка подключения…", "navFooter"); self.auth_label.setWordWrap(True); side.addWidget(self.auth_label)
        self.login_button = NavButton("Войти через ChatGPT", "check"); self.login_button.setCheckable(False)
        self.login_button.clicked.connect(self.login); side.addWidget(self.login_button)
        self.browser_login = NavButton("Открыть страницу входа", "folder"); self.browser_login.setCheckable(False)
        self.browser_login.clicked.connect(lambda: self.open_source(QUrl(self.login_url)))
        side.addWidget(self.browser_login); self.browser_login.hide()
        row.addWidget(self.sidebar)

        self.workspace = QWidget(); self.workspace.setObjectName("workspace")
        main = QVBoxLayout(self.workspace); main.setContentsMargins(26, 24, 26, 18); main.setSpacing(18)
        row.addWidget(self.workspace, 1)
        header = QHBoxLayout(); header.setSpacing(12)
        self.collapse_button = tool("", self.toggle_sidebar, "iconButton"); self.collapse_button.setToolTip("Свернуть / развернуть навигацию")
        self._icon(self.collapse_button, "panel"); header.addWidget(self.collapse_button)
        titles = QVBoxLayout(); titles.setSpacing(3)
        self.page_title = label("Компании", "pageTitle"); self.subtitle = label("Откройте исследование или добавьте Excel", "subtitle")
        for item in [self.page_title, self.subtitle]:
            item.setMinimumWidth(0); item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        titles.addWidget(self.page_title); titles.addWidget(self.subtitle); header.addLayout(titles, 1)
        self.theme_button = tool("", self.toggle_theme, "iconButton"); self.theme_button.setToolTip("Переключить тему")
        header.addWidget(self.theme_button)
        self.view_button = tool("Вид"); self._icon(self.view_button, "settings"); self.view_menu = QMenu(self.view_button)
        self.view_button.setMenu(self.view_menu); self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        header.addWidget(self.view_button)
        self.export_button = tool("Экспорт"); self._icon(self.export_button, "download")
        export_menu = QMenu(self.export_button)
        self.open_report_action = export_menu.addAction("Открыть отчёт Excel", self.open_report)
        self.save_report_action = export_menu.addAction("Сохранить копию…", self.save_report)
        export_menu.addSeparator(); self.open_folder_action = export_menu.addAction("Открыть папку исследования", self.open_folder)
        self.export_button.setMenu(export_menu); self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); header.addWidget(self.export_button)
        self.new_button = QPushButton("Новое исследование"); self.new_button.setObjectName("primary"); self._icon(self.new_button, "plus", white=True)
        self.new_button.clicked.connect(self.new_research); header.addWidget(self.new_button); main.addLayout(header)

        metrics = QHBoxLayout(); metrics.setSpacing(12); self.metrics = {}
        for title, note, states in [("Всего компаний", "в текущем исследовании", set()), ("Сохранено", "карточки с результатом", {"completed", "needs_review"}),
                                     ("Нужна проверка", "внимание аналитика", {"needs_review"}), ("Ошибки", "можно повторить", {"failed"})]:
            metric = Metric(title, note); metric.clicked.connect(lambda checked=False, s=states: self.show_filter(s))
            metrics.addWidget(metric, 1); self.metrics[title] = metric
        main.addLayout(metrics)

        self.splitter = QSplitter(Qt.Orientation.Horizontal); self.splitter.setChildrenCollapsible(False); self.splitter.setHandleWidth(12)
        main.addWidget(self.splitter, 1)
        table_panel = QFrame(); table_panel.setObjectName("panel"); table_panel.setMinimumWidth(400)
        table_box = QVBoxLayout(table_panel); table_box.setContentsMargins(0, 14, 0, 8); table_box.setSpacing(10)
        toolbar = QHBoxLayout(); toolbar.setContentsMargins(16, 0, 16, 0)
        self.table_title = label("Все компании", "sectionTitle"); self.visible_count = label("", "muted")
        toolbar.addWidget(self.table_title); toolbar.addWidget(self.visible_count); toolbar.addStretch()
        self.search = QLineEdit(); self.search.setObjectName("search"); self.search.setPlaceholderText("Поиск компании, ИНН или сайта")
        self.search.setClearButtonEnabled(True); self.search.setMinimumWidth(180); self.search.setMaximumWidth(300)
        self.search.addAction(icon("search", palette(self.theme)["muted"]), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda text: self.filter_changed()); toolbar.addWidget(self.search)
        self.columns_button = tool("", name="iconButton"); self._icon(self.columns_button, "panel"); self.columns_button.setToolTip("Показать / скрыть колонки")
        self.columns_menu = QMenu(self.columns_button); self.columns_button.setMenu(self.columns_menu)
        self.columns_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); toolbar.addWidget(self.columns_button); table_box.addLayout(toolbar)
        chips = QHBoxLayout(); chips.setContentsMargins(14, 0, 14, 0); chips.setSpacing(2)
        self.chip_group = QButtonGroup(self); self.chips = []
        for text, states in FILTERS:
            button = QPushButton(text); button.setObjectName("chip"); button.setCheckable(True)
            button.clicked.connect(lambda checked=False, s=states: self.show_filter(s)); self.chip_group.addButton(button); self.chips.append((button, states)); chips.addWidget(button)
        self.chips[0][0].setChecked(True); chips.addStretch(); table_box.addLayout(chips)
        self.model = CompanyModel(self); self.proxy = CompanyFilter(self); self.proxy.setSourceModel(self.model)
        self.table = QTableView(); self.table.setModel(self.proxy); self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.table.setShowGrid(False)
        self.table.verticalHeader().hide(); self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionsMovable(True); self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setMinimumSectionSize(70)
        for i, (_, title, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
            action = self.columns_menu.addAction(title); action.setCheckable(True); action.setChecked(i < 4); action.setEnabled(i != 0)
            action.toggled.connect(lambda visible, col=i: self.table.setColumnHidden(col, not visible))
            self.table.setColumnHidden(i, i >= 4)
        self.table.setItemDelegateForColumn(2, StatusDelegate(lambda: self.theme, self.table))
        self.table.selectionModel().currentRowChanged.connect(self.selected_company)
        self.proxy.rowsInserted.connect(lambda *_: self.update_visible_count()); self.proxy.rowsRemoved.connect(lambda *_: self.update_visible_count())
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        table_box.addWidget(self.table, 1); self.splitter.addWidget(table_panel)
        self._build_inspector(); self.splitter.addWidget(self.inspector); self.splitter.setSizes([730, 380])
        self._build_runbar(main)
        self._build_view_menu()
        for sequence, callback in [("Ctrl+F", lambda: self.search.setFocus()), ("Ctrl+Shift+D", self.toggle_theme), ("Ctrl+O", self.choose_run), ("Ctrl+Q", self.close)]:
            shortcut = QAction(self); shortcut.setShortcut(QKeySequence(sequence)); shortcut.triggered.connect(callback); self.addAction(shortcut)

    def _build_inspector(self):
        self.inspector = QFrame(); self.inspector.setObjectName("inspector"); self.inspector.setMinimumWidth(300)
        box = QVBoxLayout(self.inspector); box.setContentsMargins(18, 16, 18, 12); box.setSpacing(10)
        top = QHBoxLayout(); top.addWidget(label("КАРТОЧКА КОМПАНИИ", "eyebrow")); top.addStretch()
        self.retry_button = QPushButton("Повторить"); self.retry_button.setEnabled(False)
        self.retry_button.setToolTip("Заново исследовать только эту компанию. Прежняя карточка и история попыток сохранятся.")
        self.retry_button.clicked.connect(self.retry_selected); top.addWidget(self.retry_button)
        box.addLayout(top)
        self.company_title = label("Выберите компанию", "inspectorTitle"); self.company_title.setWordWrap(True); box.addWidget(self.company_title)
        self.company_meta = label("Сведения, источники и замечания", "muted"); self.company_meta.setWordWrap(True); box.addWidget(self.company_meta)
        self.detail_tabs = QTabWidget(); self.detail_tabs.setDocumentMode(True)
        self.detail_tabs.tabBar().setDrawBase(False)
        self.details, self.sources, self.notes = QTextBrowser(), QTextBrowser(), QTextBrowser()
        for browser, title in [(self.details, "Сведения"), (self.sources, "Источники"), (self.notes, "Замечания")]:
            browser.setOpenLinks(False); browser.anchorClicked.connect(self.open_source)
            options = browser.document().defaultTextOption(); options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            browser.document().setDefaultTextOption(options); self.detail_tabs.addTab(browser, title)
        box.addWidget(self.detail_tabs, 1)
        self.show_company(None)

    def _build_runbar(self, parent):
        runbar = QFrame(); runbar.setObjectName("panel"); row = QHBoxLayout(runbar); row.setContentsMargins(18, 12, 16, 12); row.setSpacing(12)
        progress = QVBoxLayout(); progress.setSpacing(7)
        self.progress_text = label("Выберите исследование", "sectionTitle"); self.status = label("Прогресс сохраняется после каждой компании", "muted")
        self.status.setWordWrap(True); self.status.setMinimumWidth(130)
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.setRange(0, 1)
        progress.addWidget(self.progress_text); progress.addWidget(self.progress); progress.addWidget(self.status); row.addLayout(progress, 1)
        self.batch = QComboBox(); self.batch.setEditable(True)
        self.batch.addItems(["10", "20", "50", "100", "Вся очередь"])
        self.batch.setInsertPolicy(QComboBox.InsertPolicy.NoInsert); self.batch.setCompleter(None)
        self.batch.setValidator(QRegularExpressionValidator(QRegularExpression(r"(?:[0-9]{1,5}|100000|Вся очередь)"), self.batch))
        count = max(0, min(100000, self.settings.value("batch", 10, type=int)))
        self.batch.setCurrentText(str(count) if count else "Вся очередь")
        self.batch.setAccessibleName("Компаний за запуск")
        self.batch.setToolTip("Компаний за запуск: выберите из списка или введите своё число. 0 — вся очередь.")
        self.batch.setFixedWidth(150); row.addWidget(self.batch)
        self.stop_button = QPushButton("Остановить"); self._icon(self.stop_button, "stop"); self.stop_button.clicked.connect(self.stop); row.addWidget(self.stop_button)
        self.start_button = QPushButton("Начать обработку"); self.start_button.setObjectName("primary"); self._icon(self.start_button, "play", white=True)
        self.start_button.clicked.connect(self.start_research); row.addWidget(self.start_button); parent.addWidget(runbar)

    def _build_view_menu(self):
        self.density_action = self.view_menu.addAction("Компактные строки")
        self.density_action.setCheckable(True); self.density_action.setChecked(self.compact_rows); self.density_action.toggled.connect(self.set_density)
        self.view_menu.addSeparator(); self.view_menu.addAction("Восстановить компоновку", self.reset_layout)
        self.view_menu.addSeparator()
        self.recheck_action = self.view_menu.addAction("Обновить проверки сохранённых ответов", self.recheck_saved)
        self.recheck_action.setToolTip("Без повторного исследования компаний и расхода подписки")

    def _restore_layout(self):
        geometry = self.settings.value("window_geometry")
        if isinstance(geometry, QByteArray): self.restoreGeometry(geometry)
        splitter = self.settings.value("splitter")
        legacy_below = self.settings.value("orientation", "side") == "below"
        legacy_hidden = not self.settings.value("show_card", True, type=bool)
        if isinstance(splitter, QByteArray) and not legacy_below and not legacy_hidden:
            self.splitter.restoreState(splitter)
        # Saved Qt splitter state includes its orientation; always override it.
        self.splitter.setOrientation(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.inspector.setVisible(True)
        if any(size <= 0 for size in self.splitter.sizes()): self.splitter.setSizes([730, 380])
        header = self.settings.value("columns")
        if isinstance(header, QByteArray): self.table.horizontalHeader().restoreState(header)
        self.table.setColumnHidden(0, False)
        for i, action in enumerate(self.columns_menu.actions()):
            action.blockSignals(True); action.setChecked(not self.table.isColumnHidden(i)); action.blockSignals(False)
        self.set_density(self.compact_rows); self.set_sidebar(self.sidebar_collapsed)

    def save_preferences(self):
        try:
            self.settings.setValue("batch", self.batch_size())
        except ValueError:
            pass  # Keep the previous value while an unfinished edit is in the field.
        for key, value in {"theme": self.theme, "compact_rows": self.compact_rows, "sidebar_collapsed": self.sidebar_collapsed,
                           "window_geometry": self.saveGeometry(), "splitter": self.splitter.saveState(),
                           "columns": self.table.horizontalHeader().saveState(),
                           "timeout": self.timeout, "token_budget": self.token_budget,
                           "model": self.model_name, "retry_failed": self.retry_failed}.items():
            self.settings.setValue(key, value)
        for obsolete in ["orientation", "show_card"]: self.settings.remove(obsolete)
        if self.run_dir: self.settings.setValue("last_run", str(self.run_dir))
        self.settings.sync()

    def apply_theme(self, theme):
        self.theme = theme; p = palette(theme); self.setStyleSheet(stylesheet(theme))
        pal = QPalette()
        for role, color in [(QPalette.ColorRole.Window, p["bg"]), (QPalette.ColorRole.Base, p["panel"]),
                            (QPalette.ColorRole.Text, p["text"]), (QPalette.ColorRole.WindowText, p["text"]),
                            (QPalette.ColorRole.Button, p["panel"]), (QPalette.ColorRole.ButtonText, p["text"]),
                            (QPalette.ColorRole.Highlight, p["selected"]), (QPalette.ColorRole.HighlightedText, p["text"])]:
            pal.setColor(role, QColor(color))
        self.setPalette(pal)
        for button, name, white in self._icons:
            button.setIcon(icon(name, "#FFFFFF" if white else p["muted"]))
        self.theme_button.setIcon(icon("sun" if theme == "dark" else "moon", p["muted"]))
        self.table.viewport().update(); self.show_selected()

    def toggle_theme(self):
        self.apply_theme("dark" if self.theme == "light" else "light")
        self.settings.setValue("theme", self.theme)

    def set_sidebar(self, collapsed):
        self.sidebar_collapsed = collapsed; self.sidebar.setFixedWidth(80 if collapsed else 248)
        self.brand_name.setVisible(not collapsed); self.nav_caption.setVisible(not collapsed); self.auth_label.setVisible(not collapsed)
        for button in [*self.nav.values(), self.open_run_button, self.login_button, self.browser_login]: button.compact(collapsed)

    def toggle_sidebar(self): self.set_sidebar(not self.sidebar_collapsed)

    def set_density(self, compact):
        self.compact_rows = bool(compact); self.table.verticalHeader().setDefaultSectionSize(36 if compact else 50)

    def reset_layout(self):
        self.splitter.setOrientation(Qt.Orientation.Horizontal); self.splitter.setSizes([730, 380])
        self.inspector.setVisible(True); self.set_sidebar(False)
        self.density_action.setChecked(False)
        header = self.table.horizontalHeader()
        for i, (_, _, width) in enumerate(COLUMNS):
            header.moveSection(header.visualIndex(i), i); self.table.setColumnWidth(i, width)
            self.columns_menu.actions()[i].setChecked(i < 4)

    def show_filter(self, states):
        self.set_filter(states)

    def set_filter(self, states):
        self.proxy.set_filters(states=states); self._sync_filter_chips(); self.update_visible_count()

    def _sync_filter_chips(self):
        self.chip_group.setExclusive(False)
        for button, states in self.chips: button.setChecked(states == self.proxy.states)
        self.chip_group.setExclusive(True)

    def filter_changed(self):
        self.proxy.set_filters(query=self.search.text()); self.update_visible_count()

    def update_visible_count(self):
        self.visible_count.setText(f"{self.proxy.rowCount()} из {self.model.rowCount()}")
        title = "Сохранённые карточки" if self.proxy.states == {"completed", "needs_review"} else next(
            (text for text, states in FILTERS if states == self.proxy.states), "Требуют внимания")
        self.table_title.setText(title if self.proxy.states else "Все компании")

    def selected_company(self, current, previous=None):
        source = self.proxy.mapToSource(current)
        company = self.model.companies[source.row()] if source.isValid() else None
        self.selected_id = company["id"] if company else None; self.show_company(company)
        self.update_controls()

    def show_selected(self):
        row = self.model.rows_by_id.get(self.selected_id)
        self.show_company(self.model.companies[row] if row is not None else None)

    def show_company(self, company):
        if not hasattr(self, "details"): return
        company_id = company["id"] if company else None
        keep_position = company_id == self.displayed_id
        self.displayed_id = company_id
        self.company_title.setText(company["name"] if company else "Выберите компанию")
        self.company_meta.setText(f"ИНН {company['inn']} · {STATE_LABELS[company['state']]}" if company else "Сведения, источники и замечания")
        for browser, html in zip([self.details, self.sources, self.notes], company_documents(company, self.theme)):
            position = browser.verticalScrollBar().value() if keep_position else 0
            browser.setHtml(html); browser.verticalScrollBar().setValue(position)
        card = (company or {}).get("card") or {}
        self.detail_tabs.setTabText(1, f"Источники · {len(card.get('sources', []))}" if company else "Источники")
        warnings = (company or {}).get("quality", {}).get("warnings", card.get("warnings", []))
        quality = (company or {}).get("quality", {})
        count = len(quality.get("review_reasons", warnings)) + len(quality.get("info_notes", []))
        self.detail_tabs.setTabText(2, f"Замечания · {count}" if company else "Замечания")

    def open_source(self, url):
        if url.scheme() in {"http", "https"}: QDesktopServices.openUrl(url)

    def load_run(self, path):
        try:
            snapshot = read_snapshot(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось открыть исследование", str(exc)); return False
        self.run_dir = snapshot.run_dir; self.set_notice(None); self.selected_id = None; self.search.clear()
        self.subtitle.setText(Path(snapshot.source.get("path", str(path))).name); self.subtitle.setToolTip(str(self.run_dir))
        self.set_filter(set()); self.refresh(); return True

    def refresh(self):
        if self.run_dir is None:
            self.update_controls(); return
        try:
            self.snapshot = read_snapshot(self.run_dir)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc)); return
        s = self.snapshot; selected = self.selected_id
        positions = [browser.verticalScrollBar().value() for browser in [self.details, self.sources, self.notes]]
        changed = self.model.replace(s.companies)
        if changed and selected in self.model.rows_by_id:
            index = self.proxy.mapFromSource(self.model.index(self.model.rows_by_id[selected], 0))
            if index.isValid():
                self.table.setCurrentIndex(index)
                for browser, position in zip([self.details, self.sources, self.notes], positions): browser.verticalScrollBar().setValue(position)
        elif changed:
            self.show_company(None)
        values = {"Всего компаний": s.total, "Сохранено": s.saved, "Нужна проверка": s.states.get("needs_review", 0), "Ошибки": s.states.get("failed", 0)}
        confirmed = sum(c["identity"].startswith("confirmed") for c in s.companies)
        self.metrics["Сохранено"].note.setText(f"Сайт подтверждён: {confirmed}")
        for title, value in values.items(): self.metrics[title].value.setText(str(value))
        for page, count in [("companies", s.total)]:
            self.nav[page].count.setText(str(count)); self.nav[page].count.setVisible(not self.sidebar_collapsed)
        self.progress.setRange(0, max(s.total, 1)); self.progress.setValue(s.saved)
        percent = round(s.saved / s.total * 100) if s.total else 0
        self.progress_text.setText(f"Сохранено {s.saved} из {s.total} · {percent}%")
        if self.notice:
            self.status.setText(self.notice)
        elif self.stop_requested:
            self.status.setText("Окно закроется после текущей компании…" if self.close_when_finished else "Завершаем текущую компанию и останавливаемся…")
        elif self.action == "run":
            active = next((c for c in s.companies if c["state"] == "running"), None)
            if active: self.status.setText("Сейчас: " + active["name"])
        elif self.action is None:
            self.status.setText(self.notice or STOP_LABELS.get((s.last_stop or {}).get("reason"), "Готово к исследованию"))
        self.update_visible_count(); self.update_controls()

    def update_controls(self):
        idle = self.action is None; s = self.snapshot
        for button in [self.new_button, self.open_run_button, self.login_button, self.batch]: button.setEnabled(idle)
        can_run = s and (s.states.get("pending") or s.states.get("running") or (self.retry_failed and s.states.get("failed")))
        self.start_button.setEnabled(bool(idle and self.auth_ready and can_run))
        self.start_button.setText("Продолжить" if s and s.saved else "Начать обработку")
        self.stop_button.setEnabled(self.action == "run" and not self.stop_requested)
        selected = next((c for c in s.companies if c["id"] == self.selected_id), None) if s else None
        self.retry_button.setEnabled(bool(idle and self.auth_ready and selected and (selected["has_card"] or selected["attempts"] > 0)))
        self.open_report_action.setEnabled(bool(idle and s and s.saved)); self.save_report_action.setEnabled(bool(idle and s and s.saved))
        self.open_folder_action.setEnabled(self.run_dir is not None)
        self.recheck_action.setEnabled(bool(idle and s and any(c["attempts"] for c in s.companies)))

    def new_research(self):
        dialog = NewResearchDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        self.pending_run = dialog.run_dir
        args = ["init", dialog.source.text(), "--run-dir", str(dialog.run_dir)]
        if dialog.sheets.currentText() != AUTO_SHEET: args += ["--sheet", dialog.sheets.currentText()]
        self.start_job("init", args, "Импортируем компании из Excel…")

    def choose_run(self):
        folder = runs_root()
        path = QFileDialog.getExistingDirectory(self, "Открыть папку исследования", str(folder if folder.exists() else Path.home()))
        if path: self.load_run(Path(path))

    def start_job(self, action, arguments, message):
        if self.action is not None: return False
        try:
            self.controller.start(action, arguments)
        except ValueError as exc:
            QMessageBox.warning(self, "Операция уже выполняется", str(exc)); return False
        self.action = action; self.set_notice(None); self.status.setText(message)
        self.login_url = ""; self.browser_login.hide()
        self.update_controls(); return True

    def check_codex(self): self.start_job("doctor", ["doctor", "--codex", self.binary], "Проверяем подключение Codex…")

    def login(self):
        if self.auth_ready:
            self.check_codex(); return
        self.start_job("login", ["login", "--codex", self.binary], "Завершите вход через ChatGPT в браузере.")

    def start_research(self):
        if self.run_dir is None or self.action is not None or not self.auth_ready: return
        try:
            count = self.batch_size()
        except ValueError as exc:
            self.set_notice(str(exc), error=True); self.batch.setFocus(); return
        self.stop_requested = False
        self.start_job("run", self.run_arguments(count), "Запускаем исследование…")

    def run_arguments(self, count, *, company_id=None):
        args = ["run", "--run-dir", str(self.run_dir), "--max-companies", str(count), "--codex", self.binary, "--timeout", str(self.timeout)]
        if company_id: args += ["--company-id", company_id]
        elif self.retry_failed: args.append("--retry-failed")
        if self.token_budget: args += ["--token-budget", str(self.token_budget)]
        if self.model_name: args += ["--model", self.model_name]
        return args

    def retry_selected(self):
        if not self.retry_button.isEnabled(): return
        self.stop_requested = False
        self.start_job("run", self.run_arguments(1, company_id=self.selected_id), "Повторно исследуем выбранную компанию…")

    def recheck_saved(self):
        if self.recheck_action.isEnabled():
            self.start_job("recheck", ["recheck", "--run-dir", str(self.run_dir)], "Обновляем проверки по сохранённым ответам, без запросов Codex…")

    def batch_size(self):
        text = self.batch.currentText().strip()
        if text == "Вся очередь": return 0
        if re.fullmatch(r"[0-9]{1,6}", text) and 0 <= int(text) <= 100000:
            return int(text)
        raise ValueError("Укажите число компаний от 1 до 100000 или выберите «Вся очередь».")

    def _write_stop(self):
        try: (self.run_dir / "stop.request").write_text(datetime.now().isoformat(), encoding="utf-8")
        except OSError as exc: self.set_notice("Не удалось передать остановку: " + str(exc), error=True)

    def stop(self):
        if self.action == "run" and self.run_dir:
            self.stop_requested = True; self._write_stop(); self.refresh()

    def open_folder(self):
        if self.run_dir: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir)))

    def open_report(self):
        self.after_export = ("open", None); self.start_job("export", ["export", "--run-dir", str(self.run_dir)], "Обновляем отчёт…")

    def save_report(self):
        name, _ = QFileDialog.getSaveFileName(self, "Сохранить отчёт", "Benchmark-отчёт.xlsx", "Excel (*.xlsx)")
        if name:
            self.after_export = ("save", Path(name)); self.start_job("export", ["export", "--run-dir", str(self.run_dir)], "Готовим отчёт…")

    def set_notice(self, message, *, error=False):
        self.notice = message
        self.status.setProperty("error", bool(error and message))
        self.status.style().unpolish(self.status); self.status.style().polish(self.status)
        self.status.setToolTip(message or "")
        if message: self.status.setText(message)

    @staticmethod
    def error_message(output, fallback):
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith("Ошибка:"):
                return line.removeprefix("Ошибка:").strip() or fallback
        return fallback

    def login_progress(self, text):
        if self.action != "login": return
        for value in re.findall(r'https://[^\s<>"\']+', text):
            url = QUrl(value.rstrip(".,;"))
            if url.host() == "auth.openai.com":
                self.login_url = url.toString(); self.browser_login.show()
                self.status.setText("Завершите вход в браузере. Если он не открылся, нажмите «Открыть страницу входа» слева.")
                break

    def tick(self):
        while True:
            try: event = self.controller.events.get_nowait()
            except queue.Empty: break
            if event["type"] == "log" and event["action"] == "login": self.login_progress(event["text"])
            elif event["type"] == "finished":
                self.finished(event)
                if self.closed: return
        if self.stop_requested and self.action == "run": self._write_stop()
        self.refresh()

    def finished(self, event):
        action, code = event["action"], event["code"]; self.action = None
        if action == "doctor":
            self.auth_ready = code == 0
            self.auth_label.setText("●  ChatGPT подключён" if self.auth_ready else "○  Войдите через ChatGPT")
            self.login_button.title.setText("Проверить подключение" if self.auth_ready else "Войти через ChatGPT")
            if code:
                self.set_notice(self.error_message(event["output"], "Не удалось проверить Codex. Проверьте установку и войдите через ChatGPT."), error=True)
        elif action == "login":
            self.login_url = ""; self.browser_login.hide()
            if code == 0 and not self.close_when_finished: self.check_codex()
            else:
                self.auth_label.setText("○  Вход не завершён")
                self.set_notice("Вход не завершён. Повторите вход через ChatGPT кнопкой слева.", error=True)
        elif action == "init":
            if code == 0: self.load_run(self.pending_run)
            elif not self.close_when_finished: QMessageBox.warning(self, "Не удалось создать очередь", event["output"][-2500:])
        elif action == "recheck":
            self.set_notice("Проверка сохранённых ответов завершена." if code == 0 else self.error_message(event["output"], "Не удалось обновить проверки."), error=code != 0)
        elif action == "export":
            if code == 0:
                mode, path = self.after_export
                try:
                    if mode == "open": QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir / "reports" / "report.xlsx")))
                    else:
                        save_report_copy(self.run_dir, path); self.set_notice("Отчёт сохранён: " + str(path))
                except (OSError, ValueError) as exc: QMessageBox.warning(self, "Не удалось сохранить отчёт", str(exc))
            else: QMessageBox.warning(self, "Отчёт", event["output"][-2000:])
        elif action == "run":
            self.stop_requested = False
            if code not in {0, 75, 130} and not event["cancelled"]:
                self.refresh()
                reason = (self.snapshot.last_stop or {}).get("reason") if self.snapshot else None
                fallback = (STOP_LABELS["selected_company_error"] if reason == "selected_company_error" else
                            "Обработка остановлена после трёх ошибок. Подробности — в карточках компаний."
                            if reason == "consecutive_errors" else "Ошибка запуска. Проверьте подключение Codex и параметры обработки.")
                self.set_notice(self.error_message(event["output"], fallback), error=True)
            if code == 75:
                self.refresh()
                reason = (self.snapshot.last_stop or {}).get("reason") if self.snapshot else None
                if reason == "authentication":
                    self.auth_ready = False; self.auth_label.setText("○  Требуется вход в ChatGPT")
                    self.login_button.title.setText("Войти через ChatGPT")
                else:
                    self.auth_label.setText("○  Лимит подписки — можно продолжить позже")
        if self.close_when_finished and self.action is None: self.close(); return
        self.refresh(); self.update_controls()

    def closeEvent(self, event):
        if self.action:
            self.close_when_finished = True
            if self.action == "run": self.stop()
            elif self.action in {"doctor", "login"}: self.controller.cancel()
            event.ignore(); return
        self.closed = True; self.timer.stop(); self.save_preferences(); event.accept()


def launch(run_dir=None, binary="codex"):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Benchmark"); app.setOrganizationName("Benchmark"); app.setStyle("Fusion")
    app.setFont(QFont("Helvetica Neue" if sys.platform == "darwin" else "Segoe UI", 10))
    window = BenchmarkWindow(run_dir=run_dir, binary=binary); window.show()
    return app.exec()
