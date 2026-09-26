"""football-data.org (API v4) – terminarze i wyniki lig top-5 oraz Ligi Mistrzów.

Plan darmowy: 10 zapytań na minutę, bez limitu dziennego.
Dokumentacja: https://docs.football-data.org/general/v4/index.html
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from typerbot.config.leagues import League
from typerbot.data.errors import AuthError, PlanRestrictionError, QuotaExceededError, SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import (
    AWARDED, CANCELLED, FINISHED, LIVE, POSTPONED, SCHEDULED, MatchRecord, parse_iso,
)
from typerbot.data.sources.base import ApiSource

STATUS_MAP = {
    "SCHEDULED": SCHEDULED, "TIMED": SCHEDULED,
    "IN_PLAY": LIVE, "PAUSED": LIVE, "LIVE": LIVE, "EXTRA_TIME": LIVE, "PENALTY_SHOOTOUT": LIVE,
    "FINISHED": FINISHED, "AWARDED": AWARDED,
    "POSTPONED": POSTPONED, "SUSPENDED": POSTPONED,
    "CANCELLED": CANCELLED,
}

MAX_RANGE_DAYS = 10  # API ogranicza zakres dat w jednym zapytaniu


class FootballDataOrg(ApiSource):
    name = "football_data_org"
    label = "football-data.org"
    base_url = "https://api.football-data.org/v4"
    per_minute = 10
    quota_period = "minute"
    quota_limit = 10

    def auth(self, key, params, headers) -> None:
        headers["X-Auth-Token"] = key or ""

    def update_quota(self, resp: HttpResponse) -> None:
        remaining = resp.header_int("x-requests-available-minute")
        if remaining is not None:
            self.quota.update(self.name, "minute", limit=self.quota_limit, remaining=remaining)
            if remaining <= 0:
                self.limiter.block_for(resp.header_int("x-requestcounter-reset") or 60)

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status < 400:
            return self.decode(resp)
        try:
            message = str(resp.json().get("message", ""))
        except ValueError:
            message = ""
        if resp.status == 429:
            self.limiter.block_for(resp.header_int("x-requestcounter-reset") or 60)
            raise QuotaExceededError(f"{self.label}: limit 10 zapytań/min", source=self.name)
        if resp.status in (400, 401) and "token" in message.lower():
            raise AuthError(f"{self.label}: nieprawidłowy klucz API", source=self.name)
        if resp.status == 403:
            raise PlanRestrictionError(f"{self.label}: {message or 'brak dostępu w planie darmowym'}",
                                       source=self.name)
        raise SourceError(f"{self.label}: HTTP {resp.status} {message}".strip(), source=self.name)

    # -- dane ------------------------------------------------------------------
    def matches(self, league: League, date_from: date, date_to: date, *, ttl: float) -> list[MatchRecord]:
        """Mecze ligi w zakresie dat (dzielone na okna po 10 dni)."""
        if not league.fd_org_code:
            return []
        out: list[MatchRecord] = []
        start = date_from
        while start <= date_to:
            end = min(date_to, start + timedelta(days=MAX_RANGE_DAYS - 1))
            data = self.request(
                f"/competitions/{league.fd_org_code}/matches",
                {"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
                ttl=ttl,
            )
            out.extend(self.parse_matches(data, league))
            start = end + timedelta(days=1)
        return out

    def season_matches(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        if not league.fd_org_code:
            return []
        data = self.request(f"/competitions/{league.fd_org_code}/matches", {"season": season}, ttl=ttl)
        return self.parse_matches(data, league)

    def parse_matches(self, data: dict, league: League) -> list[MatchRecord]:
        out = []
        for m in data.get("matches") or []:
            try:
                out.append(self._parse_match(m, league))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _parse_match(self, m: dict, league: League) -> MatchRecord:
        home, away = m["homeTeam"], m["awayTeam"]
        if not home.get("name") or not away.get("name"):
            raise ValueError("mecz bez ustalonych drużyn")
        status = STATUS_MAP.get(m.get("status", ""), SCHEDULED)
        hg, ag = regular_time_score(m.get("score") or {})
        season_info = m.get("season") or {}
        kickoff = parse_iso(m["utcDate"])
        season = int(season_info["startDate"][:4]) if season_info.get("startDate") else kickoff.year
        return MatchRecord(
            source=self.name,
            external_id=str(m["id"]),
            league_code=league.code,
            season=season,
            kickoff=kickoff,
            home=home["name"],
            away=away["name"],
            status=status,
            home_goals=hg if status == FINISHED else None,
            away_goals=ag if status == FINISHED else None,
            home_hints=tuple(x for x in (home.get("shortName"),) if x),
            away_hints=tuple(x for x in (away.get("shortName"),) if x),
        )


def regular_time_score(score: dict) -> tuple[int | None, int | None]:
    """Wynik po 90 minutach – według niego rozliczane są zakłady.

    Dla meczów z dogrywką/karnymi pole fullTime zawiera wynik końcowy,
    więc korzystamy z regularTime albo odejmujemy dogrywkę i karne.
    """
    full = score.get("fullTime") or {}
    duration = score.get("duration") or "REGULAR"
    if duration == "REGULAR":
        return full.get("home"), full.get("away")
    regular = score.get("regularTime") or {}
    if regular.get("home") is not None and regular.get("away") is not None:
        return regular["home"], regular["away"]
    if full.get("home") is None or full.get("away") is None:
        return None, None
    extra = score.get("extraTime") or {}
    pens = score.get("penalties") or {}
    home = full["home"] - (extra.get("home") or 0) - (pens.get("home") or 0)
    away = full["away"] - (extra.get("away") or 0) - (pens.get("away") or 0)
    return home, away
