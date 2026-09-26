"""Lokalizacja plików aplikacji (baza, logi)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from typerbot import APP_NAME


def data_dir() -> Path:
    """Katalog danych użytkownika.

    Windows: %LOCALAPPDATA%\\TyperBot, pozostałe systemy: ~/.local/share/typerbot.
    Zmienna TYPERBOT_HOME nadpisuje lokalizację (przydatne w testach).
    """
    override = os.environ.get("TYPERBOT_HOME")
    if override:
        path = Path(override)
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / APP_NAME.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "typerbot.db"


def log_path() -> Path:
    return data_dir() / "typerbot.log"
