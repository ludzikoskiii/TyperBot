"""Wspólne elementy interfejsu: paski prawdopodobieństwa, kafelki, komórki tabel."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QHeaderView, QLabel, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from typerbot.ui import theme

PROB_ROLE = Qt.UserRole + 1     # wartość 0–1 do narysowania paska
VALUE_ROLE = Qt.UserRole + 2    # True – typ value (zielony pasek)


class ProbabilityDelegate(QStyledItemDelegate):
    """Rysuje w komórce pasek prawdopodobieństwa z procentem."""

    def paint(self, painter: QPainter, option, index) -> None:
        p = index.data(PROB_ROLE)
        if p is None:
            super().paint(painter, option, index)
            return
        painter.save()
        try:
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, QColor("#2d3a5a"))
            r = option.rect.adjusted(4, 5, -4, -5)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.SURFACE_2))
            painter.drawRoundedRect(r, 3, 3)
            filled = QRect(r.x(), r.y(), int(r.width() * max(0.0, min(1.0, float(p)))), r.height())
            color = QColor(theme.POSITIVE if index.data(VALUE_ROLE) else theme.ACCENT)
            color.setAlpha(170)
            painter.setBrush(color)
            painter.drawRoundedRect(filled, 3, 3)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(r, Qt.AlignCenter, f"{100 * float(p):.0f}%")
        finally:
            painter.restore()


class NumItem(QTableWidgetItem):
    """Komórka sortowana liczbowo (tekst może być sformatowany po polsku)."""

    def __init__(self, text: str, value: float | None = None, align=Qt.AlignRight | Qt.AlignVCenter):
        super().__init__(text)
        self.setData(Qt.UserRole, value if value is not None else float("-inf"))
        self.setTextAlignment(align)

    def __lt__(self, other) -> bool:
        # Uwaga: nie wołamy super().__lt__ – w PySide6 wraca to do tej metody (rekurencja).
        a, b = self.data(Qt.UserRole), other.data(Qt.UserRole)
        try:
            return float(a) < float(b)
        except (TypeError, ValueError):
            return str(a if a is not None else self.text()) < str(b if b is not None else other.text())


def prob_item(p: float | None, value: bool = False) -> NumItem:
    item = NumItem("" if p is None else f"{100 * p:.0f}%", p)
    if p is not None:
        item.setData(PROB_ROLE, p)
        item.setData(VALUE_ROLE, value)
    return item


def text_item(text: str, color: str | None = None, bold: bool = False, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if color:
        item.setForeground(QBrush(QColor(color)))
    if bold:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def make_table(headers: list[str], stretch: int | None = None, sortable: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setSelectionMode(QTableWidget.SingleSelection)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(sortable)
    t.setShowGrid(False)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch is not None:
        h.setSectionResizeMode(stretch, QHeaderView.Stretch)
    t.verticalHeader().setDefaultSectionSize(28)
    return t


def fill_row_background(table: QTableWidget, row: int, color: str) -> None:
    for c in range(table.columnCount()):
        item = table.item(row, c)
        if item is not None:
            item.setBackground(QBrush(QColor(color)))


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def set_role(widget: QWidget, role: str | None) -> None:
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class KpiTile(QFrame):
    """Kafelek z wartością i opisem (np. „Bilans +123,45 zł”)."""

    def __init__(self, title: str):
        super().__init__()
        self.setProperty("role", "card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.title = label(title, "muted")
        self.value = label("–")
        f = self.value.font()
        f.setPointSize(16)
        f.setBold(True)
        self.value.setFont(f)
        self.sub = label("", "muted")
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, value: str, sub: str = "", role: str | None = None) -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        set_role(self.value, role)


def hbox(*widgets, stretch_last: bool = False, spacing: int = 8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    if stretch_last:
        lay.addStretch(1)
    return lay


def profit_role(value: float | None) -> str | None:
    if value is None or abs(value) < 1e-9:
        return None
    return "positive" if value > 0 else "negative"


def profit_color(value: float | None) -> str | None:
    role = profit_role(value)
    return {"positive": theme.POSITIVE, "negative": theme.NEGATIVE}.get(role or "")
