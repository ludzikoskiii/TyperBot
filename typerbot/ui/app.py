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


def self_test() -> int:
    """Sprawdzenie zbudowanej aplikacji: tryb demo, prognozy i kupony, potem wyjście (kod 0 = OK)."""
    from typerbot.ui import workers

    setup_logging(logging.INFO)
    workers.SYNCHRONOUS = True
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow(demo_context())
    window.show()
    window.start()
    app.processEvents()
    window.generator.days.setValue(7)
    window.generator.generate()
    app.processEvents()
    matches, coupons = len(window.matches.evaluated), len(window.generator.cards())
    print(f"TyperBot self-test: {matches} meczów z prognozami, {coupons} kupony")
    window.close()
    return 0 if matches and coupons else 1


def main() -> int:
    """Punkt wejścia pliku .exe: bez argumentów – aplikacja, --demo – tryb demo, --self-test – test."""
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    return run(demo="--demo" in args)
