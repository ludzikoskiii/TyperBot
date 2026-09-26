"""Konfiguracja logowania do pliku i konsoli."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from typerbot.paths import log_path


def setup_logging(level: int = logging.INFO, console: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_typerbot_configured", False):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    root._typerbot_configured = True  # type: ignore[attr-defined]
