"""Qt table, filtering and safe presentation of already accepted research data."""
from html import escape
import sys

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSortFilterProxyModel, Qt, QRect, QRectF
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QToolTip

from .presentation import PROCESSING_LABELS, processing_state
from .ui_theme import icon, palette

COLUMNS = [("name", "Компания", 240), ("inn", "ИНН", 120), ("state", "Статус", 160),
           ("site", "Сайт", 190), ("sources", "Источники", 90), ("attempts", "Попытки", 80)]
FACT_LABELS = {"primary_activity": "Виды деятельности", "functions_summary": "Функции компании",
               "functional_profile": "Функциональный профиль", "products_services": "Продукты и услуги",
               "additional_activities": "Дополнительная деятельность", "product_service_type": "Тип деятельности",
               "legal_name": "Юридическое название", "inn": "ИНН", "ogrn": "ОГРН / ОГРНИП", "kpp": "КПП",
               "legal_address": "Юридический адрес", "actual_address": "Фактический адрес", "phone": "Телефон",
               "email": "Электронная почта", "okved": "ОКВЭД"}


class CompanyModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.companies = []
        self.rows_by_id = {}

    def replace(self, companies):
        if self.companies == companies:
            return False
        self.beginResetModel()
        self.companies = companies
        self.rows_by_id = {c["id"]: i for i, c in enumerate(companies)}
        self.endResetModel()
        return True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.companies)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        c = self.companies[index.row()]
        key = COLUMNS[index.column()][0]
        value = len((c.get("card") or {}).get("sources", [])) if key == "sources" else c.get(key, "")
        if key == "state":
            value = processing_state(c.get("has_card", bool(c.get("card"))), c.get("state"))
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            return PROCESSING_LABELS[value] if key == "state" else (value if value != "" else "—")
        if role == Qt.ItemDataRole.ToolTipRole:
            return PROCESSING_LABELS[value] if key == "state" else str(value)
        if role == Qt.ItemDataRole.FontRole and key == "name":
            font = QFont(); font.setWeight(QFont.Weight.DemiBold); return font
        return None


class CompanyFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.states = set()
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_filters(self, query=None, states=None):
        self.beginFilterChange()
        if query is not None:
            self.query = query.strip().casefold()
        if states is not None:
            self.states = set(states)
        self.endFilterChange()

    def filterAcceptsRow(self, row, parent):
        c = self.sourceModel().companies[row]
        state = processing_state(c.get("has_card", bool(c.get("card"))))
        matches = not self.states or state in self.states or c.get("state") == "running" and "running" in self.states
        return matches and (
            not self.query or any(self.query in str(c.get(k, "")).casefold() for k in ["name", "inn", "site"]))


class StatusDelegate(QStyledItemDelegate):
    def __init__(self, theme_getter, parent=None):
        super().__init__(parent)
        self.theme_getter = theme_getter

    def paint(self, painter, option, index):
        p = palette(self.theme_getter())
        state = index.data(Qt.ItemDataRole.UserRole)
        kind = {"processed": "green", "running": "accent"}.get(state, "muted")
        bg = p.get((kind or "") + "_bg", p["accent_soft"] if kind == "accent" else p["hover"])
        fg = p.get(kind, p["muted"])
        super().paint(painter, option, index)
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = PROCESSING_LABELS.get(state, state)
        width = min(option.rect.width() - 16, option.fontMetrics.horizontalAdvance(text) + 20)
        rect = QRectF(option.rect.x() + 9, option.rect.center().y() - 12, width, 24)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(bg)); painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor(fg)); painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.text = ""


class SiteDelegate(QStyledItemDelegate):
    """Keep site URLs selectable as rows, with an explicit copy target per cell."""
    def __init__(self, theme_getter, copy_site, parent=None, *, feedback=None):
        super().__init__(parent)
        self.theme_getter, self.copy_site = theme_getter, copy_site
        self.feedback = feedback or (lambda site: None)

    @staticmethod
    def copy_rect(rect):
        return QRect(rect.right() - 30, rect.center().y() - 14, 28, 28)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.data(Qt.ItemDataRole.UserRole):
            option.text = option.fontMetrics.elidedText(option.text, Qt.TextElideMode.ElideRight, max(0, option.rect.width() - 54))

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(Qt.ItemDataRole.UserRole):
            rect = self.copy_rect(option.rect).adjusted(6, 6, -6, -6)
            progress = self.feedback(index.data(Qt.ItemDataRole.UserRole))
            p = palette(self.theme_getter())
            graphic = icon("copy", p["accent"], 16) if progress is None else icon("copied", p["green"], 16, progress=progress)
            graphic.paint(painter, rect)

    def editorEvent(self, event, model, option, index):
        site = index.data(Qt.ItemDataRole.UserRole)
        if site and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and self.copy_rect(option.rect).contains(event.position().toPoint()):
            self.copy_site(site)
            return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(self, event, view, option, index):
        if index.data(Qt.ItemDataRole.UserRole) and self.copy_rect(option.rect).contains(event.pos()):
            QToolTip.showText(event.globalPos(), "Копировать сайт", view)
            return True
        return super().helpEvent(event, view, option, index)


def document(body, theme="light"):
    p = palette(theme)
    family = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
    return f"""<html><head><style>
    body {{ color:{p['text']}; font-family:'{family}'; font-size:13px; line-height:1.55; margin:4px; }}
    h3 {{ font-size:11px; color:{p['muted']}; margin-top:22px; margin-bottom:6px; font-weight:600; }}
    p {{ margin-top:0; margin-bottom:12px; }} a {{ color:{p['accent']}; text-decoration:none; }}
    .muted {{ color:{p['muted']}; font-size:11px; }}
    .value {{ font-size:14px; }} .empty {{ color:{p['muted']}; padding:12px; }}
    </style></head><body>{body}</body></html>"""


def company_documents(company, theme="light"):
    if company is None:
        empty = document('<h3>КАРТОЧКА КОМПАНИИ</h3><p class="empty">Выберите строку в таблице.<br>Здесь появятся сведения, источники и замечания.</p>', theme)
        return empty, empty, empty
    card = company.get("card")
    if not card:
        body = ('<h3>ВИДЫ ДЕЯТЕЛЬНОСТИ</h3><p class="muted">Исследование ещё не выполнено.</p>'
                '<h3>ФУНКЦИИ КОМПАНИИ</h3><p class="muted">Исследование ещё не выполнено.</p>')
        return document(body, theme), document('<p class="empty">Источники появятся после исследования.</p>', theme)
    facts = card["facts"]
    def fact_section(key, title, *, required=False):
        fact = facts.get(key) or {}
        if fact.get("value") is not None:
            value = escape(str(fact["value"])).replace("\n", "<br>")
            section = f'<h3>{escape(title.upper())}</h3><p class="value">{value}</p>'
            refs = ", ".join(fact.get("source_ids", []))
            return section + f'<p class="muted">Источники: {escape(refs)}</p>'
        if required:
            return f'<h3>{escape(title.upper())}</h3><p class="muted">Нет данных.</p>'
        return ""
    main_fields = ["primary_activity", "functions_summary"]
    body = "".join(fact_section(key, FACT_LABELS[key], required=True) for key in main_fields)
    body += "".join(fact_section(key, title) for key, title in FACT_LABELS.items() if key not in main_fields)
    sources = ""
    kinds = {"official": "Официальный сайт", "registry": "Реестр", "secondary": "Другой открытый источник"}
    for source in card["sources"]:
        url = escape(source["url"], quote=True)
        sources += (f'<h3>{escape(source["source_id"])} · {escape(kinds.get(source["kind"], source["kind"]))}</h3>'
                    f'<p class="value">{escape(source["title"])}</p><p><a href="{url}">{url}</a></p>'
                    f'<p>{escape(source["excerpt"])}</p><p class="muted">Дата доступа: {escape(source["accessed_at"])}</p>')
    return document(body, theme), document(sources or '<p class="empty">Источники не найдены.</p>', theme)
