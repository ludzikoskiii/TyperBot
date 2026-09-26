"""API-Football (api-sports.io, v3) – Ekstraklasa oraz xG ze statystyk meczów.

Plan darmowy: 100 zapytań dziennie (reset o północy UTC). Uwaga: API zwraca
błędy z kodem HTTP 200 w polu "errors", więc sprawdzamy treść odpowiedzi.
Dokumentacja: https://www.api-football.com/documentation-v3
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from typerbot.config.leagues import League
from typerbot.data.errors import AuthError, PlanRestrictionError, QuotaExceededError, SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import (
    AWARDED, CANCELLED, FINISHED, LIVE, POSTPONED, SCHEDULED, MatchRecord, MatchStats, parse_iso,
)
from typerbot.data.sources.base import ApiSource

STATUS_MAP = {
    "TBD": SCHEDULED, "NS": SCHEDULED,
    "1H": LIVE, "HT": LIVE, "2H": LIVE, "ET": LIVE, "BT": LIVE, "P": LIVE, "LIVE": LIVE, "INT": LIVE,
    "FT": FINISHED, "AET": FINISHED, "PEN": FINISHED,
    "PST": POSTPONED, "SUSP": POSTPONED,
    "CANC": CANCELLED, "ABD": CANCELLED,
    "AWD": AWARDED, "WO": AWARDED,
}


class ApiFootball(ApiSource):
    name = "api_football"
    label = "API-Football"
    base_url = "https://v3.football.api-sports.io"
    per_minute = 10
    quota_period = "day"
    quota_limit = 100

    def auth(self, key, params, headers) -> None:
        headers["x-apisports-key"] = key or ""

    def check_budget(self, cost: int) -> None:
        info = self.quota_info()
        if info and info.remaining is not None and info.remaining < cost:
            raise QuotaExceededError(f"{self.label}: wykorzystano dzienny limit zapytań", source=self.name)

    def update_quota(self, resp: HttpResponse) -> None:
        limit = resp.header_int("x-ratelimit-requests-limit")
        remaining = resp.header_int("x-ratelimit-requests-remaining")
        if remaining is not None:
            self.quota.update(self.name, "day", limit=limit or self.quota_limit, remaining=remaining)
        minute_left = resp.header_int("x-ratelimit-remaining")
        if minute_left is not None and minute_left <= 0:
            self.limiter.block_for(60)

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status in (401, 403):
            raise AuthError(f"{self.label}: nieprawidłowy klucz API", source=self.name)
        if resp.status == 429:
            self.limiter.block_for(60)
            raise QuotaExceededError(f"{self.label}: zbyt wiele zapytań", source=self.name)
        if resp.status >= 400:
            raise SourceError(f"{self.label}: HTTP {resp.status}", source=self.name)
        data = self.decode(resp)
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            items = errors.items() if isinstance(errors, dict) else [("", e) for e in errors]
            for key, message in items:
                text = f"{self.label}: {message}"
                key = str(key).lower()
                if key == "token":
                    raise AuthError(text, source=self.name)
                if key in ("requests", "ratelimit"):
                    if key == "ratelimit":
                        self.limiter.block_for(60)
                    raise QuotaExceededError(text, source=self.name)
                if key == "plan":
                    raise PlanRestrictionError(text, source=self.name)
            raise SourceError(f"{self.label}: {errors}", source=self.name)
        return data

    # -- dane --------------------------------------------------------------------
    def account_status(self) -> dict:
        """Stan konta i zużycie dzienne (to zapytanie nie jest liczone do limitu)."""
        data = self.request("/status", cost=0)
        resp = data.get("response") or {}
        requests = resp.get("requests") if isinstance(resp, dict) else None
        if requests:
            limit = requests.get("limit_day")
            used = requests.get("current")
            if limit is not None and used is not None:
                self.quota.update(self.name, "day", limit=int(limit), remaining=int(limit) - int(used))
        return resp if isinstance(resp, dict) else {}

    def fixtures(self, league: League, season: int, date_from: date, date_to: date, *, ttl: float) -> list[MatchRecord]:
        if not league.api_football_id:
            return []
        data = self.request(
            "/fixtures",
            {"league": league.api_football_id, "season": season,
             "from": date_from.isoformat(), "to": date_to.isoformat()},
            ttl=ttl,
        )
        return self.parse_fixtures(data, league)

    def season_fixtures(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        """Cały sezon jednym zapytaniem (historia do modelu i backtestu)."""
        if not league.api_football_id:
            return []
        data = self.request("/fixtures", {"league": league.api_football_id, "season": season}, ttl=ttl)
        return self.parse_fixtures(data, league)

    def parse_fixtures(self, data: dict, league: League) -> list[MatchRecord]:
        out = []
        for item in data.get("response") or []:
            try:
                out.append(self._parse_fixture(item, league))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _parse_fixture(self, item: dict, league: League) -> MatchRecord:
        fixture, teams = item["fixture"], item["teams"]
        status = STATUS_MAP.get((fixture.get("status") or {}).get("short", ""), SCHEDULED)
        fulltime = (item.get("score") or {}).get("fulltime") or {}
        goals = item.get("goals") or {}
        hg, ag = fulltime.get("home"), fulltime.get("away")
        if hg is None and status == FINISHED:
            hg, ag = goals.get("home"), goals.get("away")
        return MatchRecord(
            source=self.name,
            external_id=str(fixture["id"]),
            league_code=league.code,
            season=int((item.get("league") or {}).get("season") or 0),
            kickoff=parse_iso(fixture["date"]),
            home=teams["home"]["name"],
            away=teams["away"]["name"],
            status=status,
            home_goals=hg if status == FINISHED else None,
            away_goals=ag if status == FINISHED else None,
            extra={"home_id": teams["home"].get("id"), "away_id": teams["away"].get("id")},
        )

    def fixture_statistics(self, fixture_id: str, home_team_id: int | None = None) -> MatchStats | None:
        """xG i strzały z meczu. Zwraca None, gdy API nie ma statystyk dla meczu."""
        data = self.request("/fixtures/statistics", {"fixture": fixture_id}, ttl=365 * 86400)
        return parse_statistics(data, home_team_id)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", ""))
    except ValueError:
        return None


def parse_statistics(data: dict, home_team_id: int | None = None) -> MatchStats | None:
    entries = data.get("response") or []
    if len(entries) != 2:
        return None
    if home_team_id is not None and (entries[1].get("team") or {}).get("id") == home_team_id:
        entries = [entries[1], entries[0]]
    parsed = []
    for entry in entries:
        stats = {s.get("type"): s.get("value") for s in entry.get("statistics") or []}
        parsed.append(stats)
    h, a = parsed

    def as_int(v: Any) -> int | None:
        n = _num(v)
        return int(n) if n is not None else None

    stats = MatchStats(
        home_xg=_num(h.get("expected_goals")),
        away_xg=_num(a.get("expected_goals")),
        home_shots=as_int(h.get("Total Shots")),
        away_shots=as_int(a.get("Total Shots")),
        home_sot=as_int(h.get("Shots on Goal")),
        away_sot=as_int(a.get("Shots on Goal")),
    )
    if all(v is None for v in vars(stats).values()):
        return None
    return stats


def plan_seasons(message: str) -> tuple[int, int] | None:
    """Z komunikatu 'Free plans do not have access to this season, try from 2022 to 2024.'
    odczytuje zakres sezonów dostępnych w planie darmowym."""
    found = re.search(r"from (\d{4}) to (\d{4})", message)
    return (int(found.group(1)), int(found.group(2))) if found else None
