"""Baseball – oficjalne MLB Stats API (bez klucza i rejestracji).

https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-28&endDate=2026-10-10
Terminarz (także play-off) i wyniki na bieżąco; kursów nie ma – mecze dostają kurs szacunkowy.
Warunki MLB: dozwolony użytek indywidualny, niekomercyjny, bez masowego pobierania – aplikacja
pyta kilka razy dziennie o najbliższe dni, a historię sezonu pobiera raz.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from typerbot.config.leagues import League
from typerbot.data.records import CANCELLED, FINISHED, LIVE, POSTPONED, SCHEDULED, MatchRecord, parse_iso
from typerbot.data.sources.base import ApiSource

LEAGUE = "MLB"
SKIP_TYPES = frozenset({"S", "E", "A"})     # przygotowania, pokazowe, Mecz Gwiazd


class MlbStatsApi(ApiSource):
    name = "mlb"
    label = "MLB Stats API"
    base_url = "https://statsapi.mlb.com/api/v1"
    requires_key = False
    per_minute = 20

    def schedule(self, start: date, end: date, *, ttl: float, league: str = LEAGUE) -> list[MatchRecord]:
        data = self.request("/schedule", {"sportId": 1, "startDate": start.isoformat(), "endDate": end.isoformat()},
                            ttl=ttl)
        return parse_schedule(data, league, self.name)

    def season(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        """Cały sezon (od przygotowań do World Series) – do historii modelu."""
        return self.schedule(date(season, 2, 15), date(season, 11, 30), ttl=ttl, league=league.code)


def _status(game: dict) -> str:
    st = game.get("status") or {}
    detailed = str(st.get("detailedState") or "")
    if detailed.startswith("Postponed") or detailed == "Suspended":
        return POSTPONED
    if detailed.startswith("Cancelled"):
        return CANCELLED
    abstract = st.get("abstractGameState")
    if abstract == "Final":
        return FINISHED
    if abstract == "Live":
        return LIVE
    return SCHEDULED


def parse_schedule(data: Any, league: str = LEAGUE, source: str = "mlb") -> list[MatchRecord]:
    out = []
    days = data.get("dates") if isinstance(data, dict) else None
    for day in days if isinstance(days, list) else []:
        for g in day.get("games") or [] if isinstance(day, dict) else []:
            if not isinstance(g, dict) or g.get("gameType") in SKIP_TYPES or g.get("gamePk") is None:
                continue
            teams = g.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            h_name = str((home.get("team") or {}).get("name") or "").strip()
            a_name = str((away.get("team") or {}).get("name") or "").strip()
            if not h_name or not a_name or not g.get("gameDate"):
                continue
            try:
                kickoff = parse_iso(str(g["gameDate"]))
            except ValueError:
                continue
            status = _status(g)
            hs, as_ = home.get("score"), away.get("score")
            if status == FINISHED and not (isinstance(hs, int) and isinstance(as_, int)):
                status = SCHEDULED
            final = status == FINISHED
            try:
                season = int(g.get("season") or kickoff.year)
            except (TypeError, ValueError):
                season = kickoff.year
            out.append(MatchRecord(
                source=source, external_id=str(g["gamePk"]), league_code=league, season=season, kickoff=kickoff,
                home=h_name, away=a_name, status=status, home_goals=hs if final else None,
                away_goals=as_ if final else None, extra={"type": g.get("gameType")},
            ))
    return out
