"""Dostęp do bazy SQLite.

Każdy wątek dostaje własne połączenie (SQLite nie pozwala współdzielić
połączeń między wątkami), a tryb WAL pozwala czytać w interfejsie,
gdy wątek w tle zapisuje dane.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from typerbot.data.schema import MIGRATIONS

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.migrate()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Blok zapisu: commit przy sukcesie, rollback przy wyjątku."""
        conn = self.conn()
        with conn:
            yield conn

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def migrate(self) -> None:
        conn = self.conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            log.info("Migracja bazy do wersji %d", index + 1)
            with conn:
                conn.executescript(MIGRATIONS[index])
                conn.execute(f"PRAGMA user_version = {index + 1}")

    def close(self) -> None:
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()
