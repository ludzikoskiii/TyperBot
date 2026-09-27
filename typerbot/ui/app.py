"""Uruchomienie interfejsu graficznego."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from typerbot.logging_setup import setup_logging
from typerbot.ui.context import demo_context, real_context
from typerbot.ui.main_window import MainWindow
from typerbot.ui.theme import apply_theme


def run(demo: bool = False) -> int:
    setup_logging(logging.INFO)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("TyperBot")
    apply_theme(app)
    ctx = demo_context() if demo else real_context()
    window = MainWindow(ctx)
    window.show()
    window.start()
    return app.exec()
