"""Wspólna logika modułów źródeł: klucz, limity, cache, obsługa błędów.

Każde źródło jest niezależne – wyjątek w jednym nie przerywa pracy innych
(łapie je serwis synchronizacji i zapisuje w statusie źródła).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from typerbot.config.secrets import SecretStore
from typerbot.data.errors import MissingKeyError, SourceError, SourceUnavailableError
from typerbot.data.http import HttpClient, HttpResponse
from typerbot.data.quota import QuotaInfo, QuotaTracker
from typerbot.data.ratelimit import RateLimiter

log = logging.getLogger(__name__)


class ApiSource:
    name: str = ""
    label: str = ""
    base_url: str = ""
    requires_key: bool = True
    per_minute: int | None = None
    secret_params: tuple[str, ...] = ()
    quota_period: str | None = None      # główny limit: 'day' / 'month' / None
    quota_limit: int | None = None       # domyślny limit planu darmowego

    def __init__(
        self,
        http: HttpClient,
        quota: QuotaTracker,
        secrets: SecretStore | None = None,
        *,
        limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.http = http
        self.quota = quota
        self.secrets = secrets
        self.limiter = limiter or RateLimiter(self.per_minute)
        self.clock = clock
        self.last_stale = False
        self.force_refresh = False   # True: pomiń odczyt z cache (przycisk „Odśwież”)

    # -- klucz i limity ------------------------------------------------------
    def api_key(self) -> str:
        key = self.secrets.get(self.name) if self.secrets else None
        if not key:
            raise MissingKeyError(f"Brak klucza API dla {self.label}", source=self.name)
        return key

    def has_key(self) -> bool:
        return not self.requires_key or bool(self.secrets and self.secrets.get(self.name))

    def quota_info(self) -> QuotaInfo | None:
        if not self.quota_period:
            return None
        return self.quota.get(self.name, self.quota_period, self.quota_limit)

    def check_budget(self, cost: int) -> None:
        """Nadpisywane przez źródła z limitem dziennym/miesięcznym."""

    # -- zapytania -----------------------------------------------------------
    def auth(self, key: str | None, params: dict[str, Any], headers: dict[str, str]) -> None:
        """Dodaje uwierzytelnienie do parametrów lub nagłówków."""

    def request(self, path: str, params: Mapping[str, Any] | None = None, *, ttl: float | None = None,
                cost: int = 1) -> Any:
        key = self.api_key() if self.requires_key else None
        url = self.base_url + path
        all_params: dict[str, Any] = dict(params or {})
        headers: dict[str, str] = {}
        self.auth(key, all_params, headers)
        cache_key = self.http.cache_key(self.name, url, all_params, self.secret_params)
        self.last_stale = False

        if ttl and not self.force_refresh:
            cached = self.http.cached(cache_key)
            if cached is not None:
                return self.decode(cached)

        self.check_budget(cost)
        try:
            resp = self.http.fetch(url, params=all_params, headers=headers, limiter=self.limiter)
            if resp.status >= 500:
                raise SourceUnavailableError(f"{self.label}: błąd serwera {resp.status}", source=self.name)
        except SourceUnavailableError as exc:
            stale = self.http.cached(cache_key, allow_expired=True)
            if stale is not None:
                log.warning("%s niedostępne (%s) – używam danych z cache", self.label, exc)
                self.last_stale = True
                return self.decode(stale)
            exc.source = self.name
            raise

        self.update_quota(resp)
        self.quota.record_call(self.name, path, resp.status, self.actual_cost(resp, cost), 200 <= resp.status < 300)
        data = self.check_response(resp)
        if ttl:
            self.http.store(cache_key, self.name, url, resp, ttl)
        return data

    def decode(self, resp: HttpResponse) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.label}: niepoprawna odpowiedź JSON", source=self.name) from exc

    def update_quota(self, resp: HttpResponse) -> None:
        """Odczyt limitów z nagłówków – implementacja w źródłach."""

    def actual_cost(self, resp: HttpResponse, expected: int) -> int:
        return expected

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status >= 400:
            raise SourceError(f"{self.label}: HTTP {resp.status}", source=self.name)
        return self.decode(resp)
