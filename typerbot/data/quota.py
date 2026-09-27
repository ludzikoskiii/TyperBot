"""Śledzenie zużycia limitów API i statusu źródeł.

Prawdziwe wartości pozostałych zapytań odczytujemy z nagłówków odpowiedzi
(zapisywane w `api_quota`). Dodatkowo każde zapytanie trafia do `api_calls`,
co pozwala liczyć zużycie lokalnie, gdy nagłówków jeszcze nie ma.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from typerbot.data.db import Database


@dataclass
class QuotaInfo:
    source: str
    period: str               # 'minute' | 'day' | 'month'
    limit: int | None
    remaining: int | None
    used: int | None
    updated_at: float | None
    local_used: int = 0       # zapytania policzone lokalnie w bieżącym okresie
    from_headers: bool = False

    @property
    def used_fraction(self) -> float | None:
        if not self.limit:
            return None
        used = self.used if self.used is not None else self.limit - (self.remaining or 0)
        return max(0.0, min(1.0, used / self.limit))


def period_start(period: str, now: float) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if period == "day":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "minute":
        return now - 60.0
    return dt.timestamp()


class QuotaTracker:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock

    def record_call(self, source: str, endpoint: str, status: int | None, cost: int, ok: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_calls(source, ts, endpoint, status, cost, ok) VALUES (?, ?, ?, ?, ?, ?)",
                (source, self.clock(), endpoint, status, cost, int(ok)),
            )

    def update(
        self,
        source: str,
        period: str,
        *,
        limit: int | None = None,
        remaining: int | None = None,
        used: int | None = None,
    ) -> None:
        if limit is None and remaining is not None and used is not None:
            limit = remaining + used
        if used is None and limit is not None and remaining is not None:
            used = limit - remaining
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_quota(source, period, quota_limit, remaining, used, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, period) DO UPDATE SET "
                "quota_limit = COALESCE(excluded.quota_limit, quota_limit), "
                "remaining = excluded.remaining, used = excluded.used, updated_at = excluded.updated_at",
                (source, period, limit, remaining, used, self.clock()),
            )

    def local_used(self, source: str, period: str) -> int:
        start = period_start(period, self.clock())
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS used FROM api_calls WHERE source = ? AND ts >= ?",
            (source, start),
        )
        return int(row["used"]) if row else 0

    def get(self, source: str, period: str, default_limit: int | None = None) -> QuotaInfo:
        now = self.clock()
        local = self.local_used(source, period)
        row = self.db.query_one(
            "SELECT quota_limit, remaining, used, updated_at FROM api_quota WHERE source = ? AND period = ?",
            (source, period),
        )
        fresh = row is not None and row["updated_at"] >= period_start(period, now)
        if fresh:
            return QuotaInfo(
                source, period, row["quota_limit"] or default_limit, row["remaining"], row["used"],
                row["updated_at"], local, from_headers=True,
            )
        limit = (row["quota_limit"] if row else None) or default_limit
        remaining = None if limit is None else max(0, limit - local)
        return QuotaInfo(source, period, limit, remaining, local, row["updated_at"] if row else None, local)

    def calls_today(self, source: str) -> int:
        start = period_start("day", self.clock())
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM api_calls WHERE source = ? AND ts >= ?", (source, start)
        )
        return int(row["n"]) if row else 0


@dataclass
class SourceState:
    source: str
    state: str
    message: str
    updated_at: float
    last_ok: float | None = None      # ostatnia udana aktualizacja (dane w bazie są z tej chwili)


class StatusBoard:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock

    def set(self, source: str, state: str, message: str = "") -> None:
        now = self.clock()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO source_status(source, state, message, updated_at, last_ok) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET state = excluded.state, message = excluded.message, "
                "updated_at = excluded.updated_at, last_ok = COALESCE(excluded.last_ok, last_ok)",
                (source, state, message, now, now if state == "ok" else None),
            )

    @staticmethod
    def _state(r) -> SourceState:
        return SourceState(r["source"], r["state"], r["message"], r["updated_at"], r["last_ok"])

    def get(self, source: str) -> SourceState | None:
        row = self.db.query_one("SELECT * FROM source_status WHERE source = ?", (source,))
        return self._state(row) if row else None

    def all(self) -> dict[str, SourceState]:
        return {r["source"]: self._state(r) for r in self.db.query("SELECT * FROM source_status")}
