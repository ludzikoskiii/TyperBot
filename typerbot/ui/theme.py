"""Ciemny motyw aplikacji (paleta Qt + arkusz stylów)."""

from __future__ import annotations

from PySide6.QtCore import QLocale
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#17181c"
SURFACE = "#1f2127"
SURFACE_2 = "#272a31"
BORDER = "#343842"
TEXT = "#e7e9ee"
MUTED = "#9aa1ad"
ACCENT = "#5b8cff"
POSITIVE = "#3ecf8e"     # value, zysk, wygrany
NEGATIVE = "#f05d5e"     # strata, przegrany
WARNING = "#f2b33d"      # mało danych, poza zakresem
VALUE_BG = "#173628"     # tło wiersza z typem value

QSS = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; background: {BG}; }}
QTabBar::tab {{ background: {SURFACE}; color: {MUTED}; padding: 8px 16px; border: 1px solid {BORDER};
               border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 10px 8px 8px 8px;
            background: {SURFACE}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="title"] {{ font-size: 13pt; font-weight: 600; }}
QLabel[role="positive"] {{ color: {POSITIVE}; }}
QLabel[role="negative"] {{ color: {NEGATIVE}; }}
QLabel[role="warning"] {{ color: {WARNING}; }}
QPushButton {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 5px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton[role="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #6f9bff; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 6px;
    selection-background-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QTableView, QTreeView, QListWidget {{ background: {SURFACE}; alternate-background-color: #22252b;
    border: 1px solid {BORDER}; border-radius: 4px; gridline-color: {BORDER};
    selection-background-color: #2d3a5a; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE_2}; color: {MUTED}; padding: 5px 6px; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
QCheckBox::indicator, QTableView::indicator, QListView::indicator {{ width: 14px; height: 14px;
    border: 1px solid {MUTED}; border-radius: 3px; background: {SURFACE_2}; }}
QCheckBox::indicator:checked, QTableView::indicator:checked, QListView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: {BG}; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 6px; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; }}
QFrame[role="card"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame[role="banner"] {{ background: #2b2616; border: 1px solid #5a4a1c; border-radius: 6px; }}
"""


def apply_theme(app: QApplication) -> None:
    QLocale.setDefault(QLocale(QLocale.Polish, QLocale.Poland))   # przecinek dziesiętny w polach liczbowych
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, SURFACE),
        (QPalette.AlternateBase, "#22252b"), (QPalette.Text, TEXT), (QPalette.Button, SURFACE_2),
        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, SURFACE_2), (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED),
    ):
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(MUTED))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
