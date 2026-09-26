"""Klient HTTP z cache w SQLite.

Transport jest wstrzykiwany, więc testy i tryb demo działają bez sieci.
Cache przechowuje tylko odpowiedzi zatwierdzone przez źródło (np. API-Football
zwraca błędy z kodem 200 – takich odpowiedzi nie zapisujemy).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from typerbot import __version__
from typerbot.data.db import Database
from typerbot.data.errors import SourceUnavailableError
from typerbot.data.ratelimit import RateLimiter

log = logging.getLogger(__name__)


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    from_cache: bool = False
    stale: bool = False

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def header_int(self, name: str) -> int | None:
        value = self.header(name)
        try:
            return int(float(value)) if value is not None else None
        except ValueError:
            return None

    def json(self) -> Any:
        return json.loads(self.text())

    def text(self) -> str:
        try:
            return self.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self.body.decode("latin-1")


class Transport(Protocol):
    def __call__(
        self, url: str, params: Mapping[str, Any], headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


class RequestsTransport:
    def __init__(self) -> None:
        import requests

        self._requests = requests
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"TyperBot/{__version__}"

    def __call__(self, url, params, headers, timeout) -> HttpResponse:
        try:
            resp = self.session.get(url, params=dict(params), headers=dict(headers), timeout=timeout)
        except self._requests.RequestException as exc:
            raise SourceUnavailableError(f"Brak połączenia: {exc.__class__.__name__}") from exc
        return HttpResponse(
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            body=resp.content,
        )


class HttpClient:
    def __init__(
        self,
        db: Database,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.transport = transport or RequestsTransport()
        self.clock = clock
        self.cache_hits = 0
        self.network_calls = 0

    @staticmethod
    def cache_key(source: str, url: str, params: Mapping[str, Any] | None, secret_params=()) -> str:
        visible = {k: v for k, v in (params or {}).items() if k not in secret_params}
        raw = f"{source}|{url}|{urlencode(sorted((k, str(v)) for k, v in visible.items()))}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def cached(self, key: str, *, allow_expired: bool = False) -> HttpResponse | None:
        row = self.db.query_one("SELECT status, body, expires_at FROM http_cache WHERE key = ?", (key,))
        if row is None:
            return None
        expired = row["expires_at"] <= self.clock()
        if expired and not allow_expired:
            return None
        self.cache_hits += 1
        return HttpResponse(status=row["status"], body=bytes(row["body"]), from_cache=True, stale=expired)

    def store(self, key: str, source: str, url: str, response: HttpResponse, ttl: float) -> None:
        now = self.clock()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO http_cache(key, source, url, status, body, fetched_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET status = excluded.status, "
                "body = excluded.body, fetched_at = excluded.fetched_at, expires_at = excluded.expires_at",
                (key, source, url, response.status, response.body, now, now + ttl),
            )

    def fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        limiter: RateLimiter | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        if limiter is not None:
            limiter.acquire()
        self.network_calls += 1
        return self.transport(url, params or {}, headers or {}, timeout)

    def purge(self, older_than_days: float = 30.0) -> int:
        """Usuwa przeterminowane wpisy starsze niż podana liczba dni."""
        cutoff = self.clock() - older_than_days * 86400
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM http_cache WHERE expires_at < ?", (cutoff,))
            return cur.rowcount
