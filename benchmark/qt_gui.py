"""Modern desktop workspace. All research still runs through the terminal backend."""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
from time import monotonic
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, QTimer, QUrl, QVariantAnimation
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence, QPalette, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTableView, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from .desktop import (STOP_LABELS, ProcessController, read_snapshot,
                      save_report_copy, sheet_names)
from .presentation import processing_label
from .limits import read_limits
from .ui_models import COLUMNS, CompanyFilter, CompanyModel, SiteDelegate, StatusDelegate, company_documents
from .ui_theme import icon, palette, stylesheet
from .runtime import runs_root
from . import __version__

AUTO_SHEET = "Определить автоматически"
FILTERS = [("Все", set()), ("Обрабатывается", {"running"}),
           ("Обработан", {"processed"}), ("Не обработан", {"unprocessed"})]


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


class CompanyCountInput(QLineEdit):
    """Plain keyboard/paste editing with a separate menu of suggested counts."""
    def __init__(self):
        super().__init__()
        self.setPlaceholderText("Вся очередь")
        self.setClearButtonEnabled(True)
        self.choices = QMenu(self)
        for text in ["Вся очередь", "10", "20", "50", "100"]:
            self.choices.addAction(text, lambda checked=False, value=text: self.choose(value))
        self.dropdown_action = self.addAction(icon("chevron-down"), QLineEdit.ActionPosition.TrailingPosition)
        self.dropdown_action.setToolTip("Выбрать число компаний")
        self.dropdown_action.triggered.connect(self.open_choices)

    def open_choices(self):
        self.choices.setMinimumWidth(self.width())
        self.choices.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def choose(self, value):
        self.setText(value); self.choices.close()
        QTimer.singleShot(0, lambda: (self.setFocus(), self.selectAll()))

    def setText(self, value):
        # Show the all-companies choice as a placeholder: typing works at once.
        super().setText("" if value.strip() == "Вся очередь" else value)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F4 or (event.key() == Qt.Key.Key_Down and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            self.open_choices(); event.accept(); return
        super().keyPressEvent(event)


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
        self.quit_on_close = False
        self.notice = None; self.selected_id = None; self.auth_ready = False
        self.login_url = ""
        self.limits_events = queue.Queue(); self.limits_inflight = False; self.limits_due = 0
        self.limits_generation = 0; self.limits_cancel = threading.Event()
        self.displayed_id = None
        self.elapsed_seconds = 0.0; self.elapsed_started = None
        self.copied_site = ""; self.copy_progress = 0.0
        self.copy_animation = QVariantAnimation(self)
        self.copy_animation.setDuration(1400); self.copy_animation.setStartValue(0.0); self.copy_animation.setEndValue(1.0)
        self.copy_animation.valueChanged.connect(self.animate_copy)
        self.copy_animation.finished.connect(self.finish_copy)
        self.theme = self.settings.value("theme", "light")
        self.theme = self.theme if self.theme in {"light", "dark"} else "light"
        self.compact_rows = True
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
        self.workspace = QWidget(); self.workspace.setObjectName("workspace")
        main = QVBoxLayout(self.workspace); main.setContentsMargins(26, 20, 26, 16); main.setSpacing(14)
        row.addWidget(self.workspace, 1)
        header = QHBoxLayout(); header.setSpacing(12)
        titles = QVBoxLayout(); titles.setSpacing(3)
        self.page_title = label("Компании", "pageTitle"); self.subtitle = label("Откройте исследование или добавьте Excel", "subtitle")
        for item in [self.page_title, self.subtitle]:
            item.setMinimumWidth(0); item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        titles.addWidget(self.page_title); titles.addWidget(self.subtitle); header.addLayout(titles, 1)
        self.processing_time = label("Время обработки · 00:00:00", "processingTime")
        self.processing_time.setToolTip("Время обработки в этом окне. Суммируется при продолжении; паузы не учитываются.")
        titles.addWidget(self.processing_time)
        self.limits_label = label("", "processingTime"); self.limits_label.hide(); titles.addWidget(self.limits_label)
        self.theme_button = tool("", self.toggle_theme, "iconButton"); self.theme_button.setToolTip("Переключить тему")
        header.addWidget(self.theme_button)
        self.login_button = QPushButton("Войти через ChatGPT")
        self._icon(self.login_button, "check"); self.login_button.clicked.connect(self.login)
        self.login_button.setToolTip("Вход через аккаунт ChatGPT")
        header.addWidget(self.login_button)
        self.open_run_button = tool("Открыть", self.choose_run); self._icon(self.open_run_button, "folder")
        header.addWidget(self.open_run_button)
        self.export_button = tool("Экспорт"); self._icon(self.export_button, "download")
        export_menu = QMenu(self.export_button)
        self.open_report_action = export_menu.addAction("Открыть отчёт Excel", self.open_report)
        self.save_report_action = export_menu.addAction("Сохранить копию…", self.save_report)
        export_menu.addSeparator(); self.open_folder_action = export_menu.addAction("Открыть папку исследования", self.open_folder)
        self.export_button.setMenu(export_menu); self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); header.addWidget(self.export_button)
        self.new_button = QPushButton("Новое исследование"); self.new_button.setObjectName("primary"); self._icon(self.new_button, "plus", white=True)
        self.new_button.clicked.connect(self.new_research); header.addWidget(self.new_button); main.addLayout(header)

        self.pages = QStackedWidget(); main.addWidget(self.pages, 1)
        self.welcome = QWidget(); welcome = QVBoxLayout(self.welcome)
        welcome.setContentsMargins(48, 48, 48, 48); welcome.setSpacing(16); welcome.addStretch()
        title = label("Загрузите файл Excel", "pageTitle"); title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message = label("Выберите список компаний, чтобы начать исследование.", "subtitle")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upload_button = QPushButton("Загрузить Excel"); self.upload_button.setObjectName("primary")
        self._icon(self.upload_button, "plus", white=True); self.upload_button.setMinimumWidth(220)
        self.upload_button.clicked.connect(self.new_research)
        welcome.addWidget(title); welcome.addWidget(message)
        welcome.addWidget(self.upload_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.resume_button = tool("Открыть сохранённое исследование", self.choose_run)
        welcome.addWidget(self.resume_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.welcome_status = label("", "muted"); self.welcome_status.setWordWrap(True)
        self.welcome_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.addWidget(self.welcome_status); welcome.addStretch()
        self.pages.addWidget(self.welcome)
        self.research_page = QWidget(); research = QVBoxLayout(self.research_page)
        research.setContentsMargins(0, 0, 0, 0); research.setSpacing(12)
        self.pages.addWidget(self.research_page)
        metrics = QHBoxLayout(); metrics.setSpacing(12); self.metrics = {}
        for title, note, states in [("Всего компаний", "в текущем исследовании", set()),
                                    ("Обработано", "результат сохранён", {"processed"}),
                                    ("Не обработано", "результат ещё не сохранён", {"unprocessed"})]:
            metric = Metric(title, note); metric.clicked.connect(lambda checked=False, s=states: self.show_filter(s))
            metrics.addWidget(metric, 1); self.metrics[title] = metric
        research.addLayout(metrics)

        self.splitter = QSplitter(Qt.Orientation.Horizontal); self.splitter.setChildrenCollapsible(False); self.splitter.setHandleWidth(12)
        research.addWidget(self.splitter, 1)
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
        self.table.setItemDelegateForColumn(3, SiteDelegate(lambda: self.theme, self.copy_site, self.table, feedback=self.copy_feedback))
        self.table.selectionModel().currentRowChanged.connect(self.selected_company)
        self.proxy.rowsInserted.connect(lambda *_: self.update_visible_count()); self.proxy.rowsRemoved.connect(lambda *_: self.update_visible_count())
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        table_box.addWidget(self.table, 1); self.splitter.addWidget(table_panel)
        self._build_inspector(); self.splitter.addWidget(self.inspector); self.splitter.setSizes([730, 380])
        self._build_runbar(research)
        for sequence, callback in [("Ctrl+F", lambda: self.search.setFocus()), ("Ctrl+Shift+D", self.toggle_theme), ("Ctrl+O", self.choose_run)]:
            shortcut = QAction(self); shortcut.setShortcut(QKeySequence(sequence)); shortcut.triggered.connect(callback); self.addAction(shortcut)

        self.quit_action = QAction("Выйти из Benchmark", self)
        self.quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.quit_action.triggered.connect(self.close)
        self.addAction(self.quit_action)
        if sys.platform == "darwin":
            self.menuBar().addMenu("Benchmark").addAction(self.quit_action)

    def _build_inspector(self):
        self.inspector = QFrame(); self.inspector.setObjectName("inspector"); self.inspector.setMinimumWidth(300)
        box = QVBoxLayout(self.inspector); box.setContentsMargins(18, 16, 18, 12); box.setSpacing(8)
        top = QHBoxLayout(); top.addWidget(label("КАРТОЧКА КОМПАНИИ", "eyebrow")); top.addStretch()
        self.retry_button = QPushButton("Повторить"); self.retry_button.setEnabled(False)
        self.retry_button.setToolTip("Заново исследовать только эту компанию. Прежняя карточка и история попыток сохранятся.")
        self.retry_button.clicked.connect(self.retry_selected); top.addWidget(self.retry_button)
        box.addLayout(top)
        self.company_title = label("Выберите компанию", "inspectorTitle"); self.company_title.setWordWrap(True); box.addWidget(self.company_title)
        self.company_meta = label("Сведения и источники", "muted"); self.company_meta.setWordWrap(True); box.addWidget(self.company_meta)
        site_row = QHBoxLayout(); site_row.addWidget(label("Сайт", "muted"))
        self.company_site = QLineEdit(); self.company_site.setReadOnly(True); self.company_site.setPlaceholderText("Нет данных")
        self.company_site.setAccessibleName("Сайт компании")
        self.copy_site_action = self.company_site.addAction(icon("copy"), QLineEdit.ActionPosition.TrailingPosition)
        self.copy_site_action.setText("Копировать сайт"); self.copy_site_action.setToolTip("Копировать сайт")
        self.copy_site_action.triggered.connect(lambda: self.copy_site(self.company_site.text()))
        site_row.addWidget(self.company_site, 1); box.addLayout(site_row)
        self.detail_tabs = QTabWidget(); self.detail_tabs.setDocumentMode(True)
        self.detail_tabs.tabBar().setDrawBase(False)
        self.details, self.sources = QTextBrowser(), QTextBrowser()
        for browser, title in [(self.details, "Сведения"), (self.sources, "Источники")]:
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
        self.batch = CompanyCountInput()
        count = max(0, min(100000, self.settings.value("company_limit", 0, type=int)))
        self.batch.setText(str(count) if count else "Вся очередь")
        self.batch.setAccessibleName("Компаний за запуск")
        self.batch.setToolTip("Введите число компаний или выберите из списка. Пустое поле или 0 — вся очередь. Во время обработки изменение применяется к следующему запуску.")
        self.batch.setFixedWidth(150); row.addWidget(self.batch)
        self.stop_button = QPushButton("Остановить"); self._icon(self.stop_button, "stop"); self.stop_button.clicked.connect(self.stop); row.addWidget(self.stop_button)
        self.start_button = QPushButton("Начать обработку"); self.start_button.setObjectName("primary"); self._icon(self.start_button, "play", white=True)
        self.start_button.clicked.connect(self.start_research); row.addWidget(self.start_button); parent.addWidget(runbar)

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
        self.set_density(True)

    def save_preferences(self):
        try:
            self.settings.setValue("company_limit", self.batch_size())
        except ValueError:
            pass  # Keep the previous value while an unfinished edit is in the field.
        for key, value in {"theme": self.theme,
                           "window_geometry": self.saveGeometry(), "splitter": self.splitter.saveState(),
                           "columns": self.table.horizontalHeader().saveState(),
                           "timeout": self.timeout, "token_budget": self.token_budget,
                           "model": self.model_name, "retry_failed": self.retry_failed}.items():
            self.settings.setValue(key, value)
        for obsolete in ["orientation", "show_card", "compact_rows", "sidebar_collapsed", "batch"]: self.settings.remove(obsolete)
        self.settings.remove("last_run")
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
        self.update_copy_icon()
        self.table.viewport().update(); self.show_selected()

    def toggle_theme(self):
        self.apply_theme("dark" if self.theme == "light" else "light")
        self.settings.setValue("theme", self.theme)

    def set_density(self, compact):
        self.compact_rows = bool(compact); self.table.verticalHeader().setDefaultSectionSize(36 if compact else 50)

    def reset_layout(self):
        self.splitter.setOrientation(Qt.Orientation.Horizontal); self.splitter.setSizes([730, 380])
        self.inspector.setVisible(True); self.set_density(True)
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
        title = next((text for text, states in FILTERS if states == self.proxy.states), "Все компании")
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
        self.company_meta.setText(f"ИНН {company['inn']} · {processing_label(company['has_card'], company['state'])}" if company else "Сведения и источники")
        self.company_site.setText((company or {}).get("site") or "")
        self.copy_site_action.setEnabled(bool(self.company_site.text()))
        self.update_copy_icon()
        for browser, html in zip([self.details, self.sources], company_documents(company, self.theme)):
            position = browser.verticalScrollBar().value() if keep_position else 0
            browser.setHtml(html); browser.verticalScrollBar().setValue(position)
        card = (company or {}).get("card") or {}
        self.detail_tabs.setTabText(1, f"Источники · {len(card.get('sources', []))}" if company else "Источники")

    def copy_site(self, site):
        if site:
            QApplication.clipboard().setText(site)
            self.copy_animation.stop(); self.copied_site = site
            self.animate_copy(0.0); self.copy_animation.start()

    def copy_feedback(self, site):
        return self.copy_progress if site and site == self.copied_site else None

    def animate_copy(self, progress):
        self.copy_progress = float(progress)
        self.update_copy_icon(); self.table.viewport().update()

    def finish_copy(self):
        self.copied_site = ""; self.animate_copy(0.0)

    def update_copy_icon(self):
        progress = self.copy_feedback(self.company_site.text())
        p = palette(self.theme)
        self.copy_site_action.setIcon(icon("copy", p["muted"]) if progress is None else icon("copied", p["green"], progress=progress))
        self.copy_site_action.setToolTip("Копировать сайт" if progress is None else "Скопировано")

    def processing_seconds(self):
        return self.elapsed_seconds + (max(0, monotonic() - self.elapsed_started) if self.elapsed_started is not None else 0)

    def update_processing_time(self):
        seconds = int(self.processing_seconds())
        hours, seconds = divmod(seconds, 3600); minutes, seconds = divmod(seconds, 60)
        self.processing_time.setText(f"Время обработки · {hours:02d}:{minutes:02d}:{seconds:02d}")

    def open_source(self, url):
        if url.scheme() in {"http", "https"}: QDesktopServices.openUrl(url)

    def load_run(self, path):
        try:
            snapshot = read_snapshot(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось открыть исследование", str(exc)); return False
        self.run_dir = snapshot.run_dir; self.set_notice(None); self.selected_id = None; self.search.clear()
        self.elapsed_seconds = 0.0; self.elapsed_started = None
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
        positions = [browser.verticalScrollBar().value() for browser in [self.details, self.sources]]
        changed = self.model.replace(s.companies)
        if changed and selected in self.model.rows_by_id:
            index = self.proxy.mapFromSource(self.model.index(self.model.rows_by_id[selected], 0))
            if index.isValid():
                self.table.setCurrentIndex(index)
                for browser, position in zip([self.details, self.sources], positions): browser.verticalScrollBar().setValue(position)
        elif changed:
            self.show_company(None)
        values = {"Всего компаний": s.total, "Обработано": s.saved, "Не обработано": s.total - s.saved}
        for title, value in values.items(): self.metrics[title].value.setText(str(value))
        self.progress.setRange(0, max(s.total, 1)); self.progress.setValue(s.saved)
        percent = round(s.saved / s.total * 100) if s.total else 0
        self.progress_text.setText(f"Обработано {s.saved} из {s.total} · {percent}%")
        if self.notice:
            self.status.setText(self.notice)
        elif self.stop_requested:
            self.status.setText("Останавливаем задания и сохраняем готовые результаты… Окно закроется автоматически." if self.close_when_finished else "Останавливаем задания и сохраняем готовые результаты…")
        elif self.action == "run":
            active = [c["name"] for c in s.companies if c["state"] == "running"]
            if active: self.status.setText("Сейчас: " + " · ".join(active))
        elif self.action is None:
            self.status.setText(self.notice or STOP_LABELS.get((s.last_stop or {}).get("reason"), "Готово к исследованию"))
        self.update_visible_count(); self.update_controls()

    def update_controls(self):
        idle = self.action is None; s = self.snapshot
        self.processing_time.setVisible(s is not None); self.update_processing_time()
        self.pages.setCurrentWidget(self.research_page if s else self.welcome)
        self.new_button.setText("Новое исследование" if s else "Загрузить Excel")
        self.export_button.setVisible(s is not None)
        self.upload_button.setEnabled(idle); self.resume_button.setEnabled(idle)
        for button in [self.new_button, self.open_run_button]: button.setEnabled(idle)
        self.login_button.setEnabled(idle or self.action == "login" and bool(self.login_url))
        can_run = s and (s.states.get("pending") or s.states.get("running") or (self.retry_failed and s.states.get("failed")))
        self.start_button.setEnabled(bool(idle and self.auth_ready and can_run))
        self.start_button.setText("Продолжить" if s and any(c["attempts"] for c in s.companies) else "Начать обработку")
        self.stop_button.setEnabled(self.action == "run" and not self.stop_requested)
        selected = next((c for c in s.companies if c["id"] == self.selected_id), None) if s else None
        self.retry_button.setEnabled(bool(idle and self.auth_ready and selected and (selected["has_card"] or selected["attempts"] > 0)))
        self.open_report_action.setEnabled(bool(idle and s and s.saved)); self.save_report_action.setEnabled(bool(idle and s and s.saved))
        self.open_folder_action.setEnabled(self.run_dir is not None)

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
        if action == "run": self.elapsed_started = monotonic()
        self.login_url = ""
        self.update_controls(); return True

    def check_codex(self): self.start_job("doctor", ["doctor", "--codex", self.binary], "Проверяем подключение Codex…")

    def login(self):
        if self.action == "login" and self.login_url:
            self.open_source(QUrl(self.login_url)); return
        if self.auth_ready:
            self.logout(); return
        self.start_job("login", ["login", "--codex", self.binary], "Завершите вход через ChatGPT в браузере.")

    def logout(self):
        if self.action is not None or not self.auth_ready: return
        self.clear_limits()
        self.start_job("logout", ["logout", "--codex", self.binary], "Выходим из ChatGPT в Codex CLI…")

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

    def batch_size(self):
        text = self.batch.text().strip()
        if text in {"", "Вся очередь"}: return 0
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
        self.welcome_status.setText(message or "")
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
                self.login_url = url.toString(); self.login_button.setText("Открыть страницу входа")
                self.update_controls()
                self.status.setText("Завершите вход в браузере. Если он не открылся, нажмите «Открыть страницу входа» сверху.")
                self.welcome_status.setText(self.status.text())
                break

    def tick(self):
        while True:
            try: event = self.controller.events.get_nowait()
            except queue.Empty: break
            if event["type"] == "log" and event["action"] == "login": self.login_progress(event["text"])
            elif event["type"] == "finished":
                self.finished(event)
                if self.closed: return
        while True:
            try: generation, result = self.limits_events.get_nowait()
            except queue.Empty: break
            if generation != self.limits_generation: continue
            self.limits_inflight = False
            self.show_limits(result)
        if self.stop_requested and self.action == "run": self._write_stop()
        if (self.auth_ready and not self.closed and not self.close_when_finished
                and self.action not in {"doctor", "login", "logout"}
                and not self.limits_inflight and monotonic() >= self.limits_due):
            self.limits_inflight = True; self.limits_due = monotonic() + 60
            generation, binary, cancel = self.limits_generation, self.binary, self.limits_cancel
            events = self.limits_events
            def fetch_limits():
                try: result = read_limits(binary, cancel_event=cancel)
                except Exception: result = None
                events.put((generation, result))
            threading.Thread(target=fetch_limits, daemon=True).start()
        self.refresh()

    def clear_limits(self):
        self.limits_cancel.set(); self.limits_cancel = threading.Event()
        self.limits_generation += 1; self.limits_inflight = False; self.limits_due = 0
        self.limits_label.clear(); self.limits_label.hide()

    def show_limits(self, result):
        if not result or not self.auth_ready:
            self.limits_label.clear(); self.limits_label.hide(); return
        windows = result["windows"]
        self.limits_label.setText("Осталось · " + " · ".join(f"{w['name']}: {w['remaining']:g}%" for w in windows))
        details = ["Остаток лимита Codex в аккаунте. Обновление раз в минуту; учитывает и другие запуски Codex."]
        for window in windows:
            if window.get("resets_at"):
                try: details.append(f"{window['name']}: сброс {datetime.fromtimestamp(window['resets_at']).strftime('%d.%m %H:%M')}")
                except (ValueError, OverflowError, OSError): pass
        self.limits_label.setToolTip("\n".join(details)); self.limits_label.show()

    def finished(self, event):
        action, code = event["action"], event["code"]; self.action = None
        if action == "run":
            self.elapsed_seconds = self.processing_seconds(); self.elapsed_started = None
        if action == "doctor":
            self.auth_ready = code == 0
            if not self.auth_ready: self.clear_limits()
            self.login_button.setToolTip("ChatGPT подключён. Выход удаляет сохранённую авторизацию Codex CLI на этом компьютере." if self.auth_ready else "Вход через аккаунт ChatGPT")
            self.login_button.setText("Выйти из ChatGPT" if self.auth_ready else "Войти через ChatGPT")
            if code:
                self.set_notice(self.error_message(event["output"], "Не удалось проверить Codex. Проверьте установку и войдите через ChatGPT."), error=True)
        elif action == "login":
            self.login_url = ""
            if code == 0 and not self.close_when_finished: self.check_codex()
            else:
                self.login_button.setToolTip("○  Вход не завершён")
                self.login_button.setText("Войти через ChatGPT")
                self.set_notice("Вход не завершён. Повторите вход через ChatGPT кнопкой сверху.", error=True)
        elif action == "logout":
            if code == 0:
                self.auth_ready = False; self.login_url = ""
                self.login_button.setText("Войти через ChatGPT")
                self.login_button.setToolTip("Вход через аккаунт ChatGPT")
                self.set_notice("Вы вышли из ChatGPT в Codex CLI.")
            else:
                self.set_notice(self.error_message(event["output"], "Не удалось выйти из Codex CLI. Повторите попытку."), error=True)
        elif action == "init":
            if code == 0: self.load_run(self.pending_run)
            elif not self.close_when_finished: QMessageBox.warning(self, "Не удалось создать очередь", event["output"][-2500:])
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
                            "Обработка остановлена после трёх ошибок. Необработанные компании можно запустить повторно."
                            if reason == "consecutive_errors" else "Ошибка запуска. Проверьте подключение Codex и параметры обработки.")
                self.set_notice(self.error_message(event["output"], fallback), error=True)
            if code == 75:
                self.refresh()
                reason = (self.snapshot.last_stop or {}).get("reason") if self.snapshot else None
                if reason == "authentication":
                    self.auth_ready = False; self.login_button.setToolTip("○  Требуется вход в ChatGPT")
                    self.login_button.setText("Войти через ChatGPT")
                else:
                    self.login_button.setToolTip("○  Лимит подписки — можно продолжить позже")
        if self.close_when_finished and self.action is None: self.close(); return
        self.refresh(); self.update_controls()

    def closeEvent(self, event):
        self.limits_cancel.set()
        if self.action:
            self.close_when_finished = True
            if self.action == "run": self.stop()
            elif self.action in {"doctor", "login"}: self.controller.cancel()
            event.ignore(); return
        self.closed = True; self.timer.stop(); self.batch.choices.close()
        self.save_preferences(); event.accept()
        if self.quit_on_close:
            QTimer.singleShot(0, QApplication.instance().quit)


def launch(run_dir=None, binary="codex"):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Benchmark"); app.setOrganizationName("Benchmark"); app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(True)
    app.setFont(QFont("Helvetica Neue" if sys.platform == "darwin" else "Segoe UI", 10))
    window = BenchmarkWindow(run_dir=run_dir, binary=binary); window.quit_on_close = True; window.show()
    return app.exec()
