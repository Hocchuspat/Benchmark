"""Qt table, filtering and safe presentation of already accepted research data."""
from html import escape
import sys

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyledItemDelegate

from .desktop import IDENTITY_LABELS, STATE_LABELS
from .ui_theme import palette

COLUMNS = [("name", "Компания", 240), ("inn", "ИНН", 120), ("state", "Проверка карточки", 160),
           ("site", "Сайт / кандидат", 190), ("sources", "Источники", 90), ("attempts", "Попытки", 80),
           ("identity", "Подтверждение сайта", 230)]
FACT_LABELS = {"functions_summary": "Описание функций", "primary_activity": "Основная деятельность",
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
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "identity": return IDENTITY_LABELS.get(value, value or "—")
            return STATE_LABELS.get(value, value) if key == "state" else (value if value != "" else "—")
        if role == Qt.ItemDataRole.ToolTipRole:
            return c.get("error") or str(value) if key == "state" else str(value)
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
        return (not self.states or c["state"] in self.states) and (
            not self.query or any(self.query in str(c.get(k, "")).casefold() for k in ["name", "inn", "site"]))


class StatusDelegate(QStyledItemDelegate):
    def __init__(self, theme_getter, parent=None):
        super().__init__(parent)
        self.theme_getter = theme_getter

    def paint(self, painter, option, index):
        p = palette(self.theme_getter())
        state = index.data(Qt.ItemDataRole.UserRole)
        kind = {"completed": "green", "needs_review": "amber", "failed": "red", "running": "accent"}.get(state)
        bg = p.get((kind or "") + "_bg", p["accent_soft"] if kind == "accent" else p["hover"])
        fg = p.get(kind, p["muted"])
        super().paint(painter, option, index)
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = STATE_LABELS.get(state, state)
        width = min(option.rect.width() - 16, option.fontMetrics.horizontalAdvance(text) + 20)
        rect = QRectF(option.rect.x() + 9, option.rect.center().y() - 12, width, 24)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(bg)); painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor(fg)); painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.text = ""


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
        body = '<h3>ОЖИДАЕТ ИССЛЕДОВАНИЯ</h3><p>Для этой компании ещё нет сохранённой карточки.</p>'
        error = escape(company.get("error", ""))
        return document(body, theme), document('<p class="empty">Источники появятся после исследования.</p>', theme), document(f"<p>{error or 'Замечаний пока нет.'}</p>", theme)
    facts = card["facts"]
    identity = card["identity"]
    body = f'<h3>ПРИНАДЛЕЖНОСТЬ САЙТА</h3><p>{escape(IDENTITY_LABELS.get(identity["status"], identity["status"]))}</p><p class="muted">{escape(identity["reason"])}</p>'
    for key, label in FACT_LABELS.items():
        fact = facts.get(key) or {}
        if fact.get("value") is not None:
            value = escape(str(fact["value"])).replace("\n", "<br>")
            body += f'<h3>{escape(label.upper())}</h3><p class="value">{value}</p>'
            refs = ", ".join(fact.get("source_ids", []))
            body += f'<p class="muted">Источники: {escape(refs)}</p>'
    sources = ""
    kinds = {"official": "Официальный сайт", "registry": "Реестр", "secondary": "Другой открытый источник"}
    for source in card["sources"]:
        url = escape(source["url"], quote=True)
        sources += (f'<h3>{escape(source["source_id"])} · {escape(kinds.get(source["kind"], source["kind"]))}</h3>'
                    f'<p class="value">{escape(source["title"])}</p><p><a href="{url}">{url}</a></p>'
                    f'<p>{escape(source["excerpt"])}</p><p class="muted">Дата доступа: {escape(source["accessed_at"])}</p>')
    warnings = company.get("quality", {}).get("warnings", card.get("warnings", []))
    quality = company.get("quality", {})
    review = quality.get("review_reasons", warnings)
    info = quality.get("info_notes", [])
    notes = "".join(f'<h3>ТРЕБУЕТ ПРОВЕРКИ {i}</h3><p>{escape(w)}</p>' for i, w in enumerate(review, 1))
    notes += "".join(f'<h3>ИНФОРМАЦИЯ {i}</h3><p>{escape(w)}</p>' for i, w in enumerate(info, 1))
    if card["missing_information"]:
        fields = ", ".join(FACT_LABELS.get(k, k) for k in card["missing_information"])
        notes += f'<h3>НЕДОСТАЮЩИЕ СВЕДЕНИЯ</h3><p>{escape(fields)}</p>'
    if company.get("error"):
        notes += f'<h3>ОШИБКА ПОСЛЕДНЕЙ ПОПЫТКИ</h3><p>{escape(company["error"])}</p>'
    return (document(body, theme), document(sources or '<p class="empty">Нет принятых источников. Посмотрите замечания.</p>', theme),
            document(notes or '<p class="empty">Дополнительных замечаний нет.</p>', theme))
