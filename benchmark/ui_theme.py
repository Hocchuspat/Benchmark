"""Shared Qt design tokens and small vector icons. No remote fonts or assets."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

PALETTES = {
    "light": dict(bg="#F4F6FA", panel="#FFFFFF", nav="#101C35", text="#18253C", muted="#7B879D",
                  border="#E5EAF2", accent="#4566ED", accent_soft="#EEF2FF", hover="#F6F8FC",
                  selected="#EBF0FF", green="#24856A", green_bg="#E7F5EF", amber="#A66A12",
                  amber_bg="#FFF3DD", red="#C04D59", red_bg="#FCECEF", track="#E9EDF5"),
    "dark": dict(bg="#101725", panel="#182235", nav="#0B1220", text="#E5ECF8", muted="#93A2BD",
                 border="#29354A", accent="#91A6FF", accent_soft="#263454", hover="#202D43",
                 selected="#293B60", green="#70D7B5", green_bg="#193D37", amber="#F2C575",
                 amber_bg="#443825", red="#F29AA4", red_bg="#432B39", track="#2B374B"),
}


def palette(theme):
    return PALETTES.get(theme, PALETTES["light"])


def stylesheet(theme):
    p = palette(theme)
    arrow = (Path(__file__).parent / "resources" / "chevron-down.svg").resolve().as_posix()
    return f"""
    QWidget {{ color:{p['text']}; font-size:13px; }}
    QMainWindow, QWidget#workspace {{ background:{p['bg']}; }}
    QFrame#sidebar {{ background:{p['nav']}; border:0; }}
    QLabel {{ background:transparent; }}
    QLabel#processingTime {{ color:{p['muted']}; font-size:11px; }}
    QLabel#brand {{ color:white; font-size:19px; font-weight:700; }}
    QLabel#brandMark {{ background:#526FF0; color:white; font-size:19px; font-weight:700; border-radius:9px; }}
    QLabel#navCaption {{ color:#657698; font-size:10px; font-weight:700; letter-spacing:1px; }}
    QLabel#navFooter {{ color:#8D9DBC; font-size:11px; }}
    QPushButton#navButton {{ background:transparent; border:0; border-radius:8px; text-align:left; }}
    QPushButton#navButton:hover {{ background:#1B2B48; }}
    QPushButton#navButton:checked {{ background:#263A62; }}
    QLabel#navText {{ color:#A6B4CE; font-size:13px; }}
    QPushButton#navButton:checked QLabel {{ color:white; }}
    QLabel#navCount {{ background:#22334F; color:#B4C1DB; border-radius:5px; padding:2px 6px; font-size:11px; }}
    QLabel#pageTitle {{ font-size:24px; font-weight:700; letter-spacing:-0.5px; }}
    QLabel#subtitle, QLabel#muted {{ color:{p['muted']}; font-size:12px; }}
    QLabel#muted[error="true"] {{ color:{p['red']}; }}
    QFrame#panel, QFrame#inspector {{ background:{p['panel']}; border:1px solid {p['border']}; border-radius:12px; }}
    QPushButton#metric {{ background:{p['panel']}; border:1px solid {p['border']}; border-radius:10px; text-align:left; }}
    QPushButton#metric:hover {{ border-color:{p['accent']}; }}
    QLabel#metricTitle {{ color:{p['muted']}; font-size:12px; }}
    QLabel#metricValue {{ color:{p['text']}; font-size:27px; font-weight:700; }}
    QLabel#metricNote {{ color:{p['muted']}; font-size:11px; }}
    QLabel#sectionTitle {{ font-size:14px; font-weight:600; }}
    QLabel#inspectorTitle {{ font-size:17px; font-weight:700; }}
    QLabel#eyebrow {{ color:{p['muted']}; font-size:10px; font-weight:700; letter-spacing:1px; }}
    QPushButton, QToolButton {{ background:{p['panel']}; border:1px solid {p['border']}; border-radius:7px; padding:8px 12px; }}
    QPushButton:hover, QToolButton:hover {{ background:{p['hover']}; border-color:#B2C0DF; }}
    QPushButton:disabled, QToolButton:disabled {{ color:{p['muted']}; border-color:{p['border']}; }}
    QPushButton#primary, QToolButton#primary {{ background:#4566ED; color:white; border:1px solid #4566ED; font-weight:600; }}
    QPushButton#primary:hover {{ background:#3556D9; }}
    QPushButton#primary:disabled {{ background:{p['track']}; border-color:{p['track']}; color:{p['muted']}; }}
    QToolButton#iconButton {{ border:0; background:transparent; padding:6px; border-radius:6px; }}
    QToolButton#iconButton:hover {{ background:{p['selected']}; }}
    QToolButton::menu-indicator {{ image:none; }}
    QPushButton#chip {{ padding:5px 10px; border:0; color:{p['muted']}; background:transparent; border-radius:6px; }}
    QPushButton#chip:checked {{ background:{p['accent_soft']}; color:{p['accent']}; font-weight:600; }}
    QPushButton#chip:hover {{ background:{p['hover']}; }}
    QLineEdit, QSpinBox, QComboBox {{ border:1px solid {p['border']}; background:{p['panel']}; border-radius:7px; padding:7px 10px; selection-background-color:{p['selected']}; }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color:{p['accent']}; }}
    QLineEdit#search {{ background:{p['hover']}; border-color:transparent; }}
    QComboBox::drop-down {{ border:0; width:24px; }}
    QComboBox::down-arrow {{ image:url("{arrow}"); width:12px; height:12px; }}
    QComboBox QLineEdit {{ border:0; background:transparent; padding:0; }}
    QComboBox QAbstractItemView {{ background:{p['panel']}; color:{p['text']}; border:1px solid {p['border']}; selection-background-color:{p['selected']}; selection-color:{p['text']}; outline:0; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width:17px; border:0; }}
    QCheckBox {{ spacing:8px; }}
    QCheckBox::indicator {{ width:16px; height:16px; }}
    QTableView {{ border:0; background:{p['panel']}; gridline-color:{p['border']}; selection-background-color:{p['selected']}; selection-color:{p['text']}; outline:0; }}
    QTableView::item {{ padding:0 12px; border-bottom:1px solid {p['border']}; }}
    QTableView::item:selected {{ background:{p['selected']}; }}
    QHeaderView {{ background:{p['panel']}; }}
    QHeaderView::section {{ background:{p['hover']}; color:{p['muted']}; border:0; border-bottom:1px solid {p['border']}; padding:11px 12px; font-size:11px; font-weight:600; text-align:left; }}
    QTableCornerButton::section {{ background:{p['panel']}; border:0; }}
    QScrollBar:vertical {{ background:transparent; width:8px; margin:3px; }}
    QScrollBar::handle:vertical {{ background:{p['border']}; border-radius:3px; min-height:30px; }}
    QScrollBar:horizontal {{ background:transparent; height:8px; margin:3px; }}
    QScrollBar::handle:horizontal {{ background:{p['border']}; border-radius:3px; min-width:30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
    QSplitter::handle {{ background:transparent; }}
    QSplitter::handle:hover {{ background:{p['border']}; }}
    QTextBrowser {{ background:{p['panel']}; border:0; padding:2px; selection-background-color:{p['selected']}; }}
    QTabWidget::pane {{ border:0; background:{p['panel']}; }}
    QTabBar::tab {{ color:{p['muted']}; background:transparent; border:0; border-bottom:2px solid transparent; padding:10px 9px; font-size:12px; }}
    QTabBar::tab:selected {{ color:{p['accent']}; border-bottom-color:{p['accent']}; font-weight:600; }}
    QProgressBar {{ background:{p['track']}; border:0; border-radius:3px; max-height:6px; min-height:6px; }}
    QProgressBar::chunk {{ background:#4566ED; border-radius:3px; }}
    QMenu {{ background:{p['panel']}; border:1px solid {p['border']}; padding:6px; }}
    QMenu::item {{ padding:7px 24px 7px 12px; border-radius:5px; }}
    QMenu::item:selected {{ background:{p['selected']}; color:{p['accent']}; }}
    QMenu::item:disabled {{ color:{p['muted']}; }}
    QMenu::separator {{ height:1px; background:{p['border']}; margin:5px; }}
    QDialog {{ background:{p['bg']}; }}
    QStatusBar {{ background:{p['bg']}; color:{p['muted']}; }}
    QToolTip {{ background:{p['panel']}; color:{p['text']}; border:1px solid {p['border']}; padding:5px; }}
    """


def icon(name, color="#7B879D", size=20, *, progress=1):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 20, size / 20)
    painter.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == "copied":
        # Draw the check in two strokes, hold it, then fade before restoring copy.
        reveal = min(1, progress / .2)
        painter.setOpacity(min(1, (1 - progress) / .15))
        tint = QColor(color); tint.setAlpha(25); painter.setBrush(tint)
        painter.setPen(Qt.PenStyle.NoPen); painter.drawEllipse(QRectF(1, 1, 18, 18))
        painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        first, second = min(1, reveal * 2), max(0, reveal * 2 - 1)
        painter.drawLine(QPointF(5, 10), QPointF(5 + 3.5 * first, 10 + 3.5 * first))
        if second:
            painter.drawLine(QPointF(8.5, 13.5), QPointF(8.5 + 6.5 * second, 13.5 - 7 * second))
    elif name == "grid":
        for x, y in [(3, 3), (11, 3), (3, 11), (11, 11)]:
            painter.drawRoundedRect(QRectF(x, y, 5.5, 5.5), 1, 1)
    elif name in {"check", "review"}:
        painter.drawEllipse(QRectF(2.5, 2.5, 15, 15))
        if name == "check":
            painter.drawLine(6, 10, 9, 13); painter.drawLine(9, 13, 14, 7)
        else:
            painter.drawLine(10, 6, 10, 10); painter.drawPoint(QPointF(10, 13.5))
    elif name == "log":
        painter.drawRoundedRect(QRectF(3, 2.5, 14, 15), 2, 2)
        for y in [6, 10, 14]:
            painter.drawLine(6, y, 14, y)
    elif name in {"plus", "close"}:
        if name == "plus":
            painter.drawLine(10, 4, 10, 16); painter.drawLine(4, 10, 16, 10)
        else:
            painter.drawLine(5, 5, 15, 15); painter.drawLine(5, 15, 15, 5)
    elif name == "play":
        path = QPainterPath(); path.moveTo(6, 3.5); path.lineTo(16, 10); path.lineTo(6, 16.5); path.closeSubpath()
        painter.drawPath(path)
    elif name == "stop":
        painter.drawRoundedRect(QRectF(5, 5, 10, 10), 1, 1)
    elif name == "download":
        painter.drawLine(10, 2, 10, 12); painter.drawLine(6, 8, 10, 12); painter.drawLine(14, 8, 10, 12)
        painter.drawLine(3, 13, 3, 17); painter.drawLine(3, 17, 17, 17); painter.drawLine(17, 13, 17, 17)
    elif name == "folder":
        path = QPainterPath(); path.moveTo(2, 5); path.lineTo(8, 5); path.lineTo(10, 7); path.lineTo(18, 7)
        path.lineTo(17, 16); path.lineTo(3, 16); path.closeSubpath(); painter.drawPath(path)
    elif name == "moon":
        path = QPainterPath(); path.moveTo(14, 3); path.cubicTo(3, 0, 1, 15, 11, 17)
        path.cubicTo(16, 18, 19, 13, 18, 10); path.cubicTo(11, 14, 8, 7, 14, 3); painter.drawPath(path)
    elif name == "sun":
        painter.drawEllipse(QRectF(6, 6, 8, 8))
        for a, b, c, d in [(10, 1, 10, 3), (10, 17, 10, 19), (1, 10, 3, 10), (17, 10, 19, 10), (3, 3, 5, 5), (15, 15, 17, 17)]:
            painter.drawLine(a, b, c, d)
    elif name == "panel":
        painter.drawRoundedRect(QRectF(2, 3, 16, 14), 2, 2); painter.drawLine(7, 3, 7, 17)
    elif name == "copy":
        painter.drawRoundedRect(QRectF(7, 7, 10, 11), 1.5, 1.5)
        painter.drawLine(12, 3, 3, 3); painter.drawLine(3, 3, 3, 13)
    elif name == "chevron-down":
        painter.drawLine(5, 8, 10, 13); painter.drawLine(10, 13, 15, 8)
    elif name == "refresh":
        painter.drawArc(QRectF(3, 3, 14, 14), 45 * 16, 290 * 16)
        painter.drawLine(15, 2, 16, 6); painter.drawLine(12, 6, 16, 6)
    elif name == "search":
        painter.drawEllipse(QRectF(3, 3, 10, 10)); painter.drawLine(12, 12, 17, 17)
    elif name == "settings":
        for y, x in [(5, 7), (10, 13), (15, 9)]:
            painter.drawLine(3, y, 17, y)
            painter.setBrush(QColor(color)); painter.drawEllipse(QPointF(x, y), 2, 2); painter.setBrush(Qt.BrushStyle.NoBrush)
    else:
        painter.drawRoundedRect(QRectF(4, 2, 12, 16), 2, 2)
    painter.end()
    return QIcon(pixmap)
