"""OpenLigaDB – terminarz i wyniki lig niemieckich na bieżąco (projekt społecznościowy).

Odczyt bez klucza i rejestracji: https://api.openligadb.de/getmatchdata/bl3/2026
(skrót ligi, rok rozpoczęcia sezonu). Limit serwisu to 1000 zapytań na godzinę z jednego
adresu – aplikacja wysyła kilka zapytań przy odświeżeniu (jedno na ligę), z cache.
Kursów nie ma – mecze tylko stąd dostają kurs szacunkowy.
"""

from __future__ import annotations

from typing import Any

from typerbot.config.leagues import League
from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord, parse_iso
from typerbot.data.sources.base import ApiSource

FINAL_RESULT = 2        # resultTypeID wyniku końcowego („Endergebnis”); 1 = do przerwy


class OpenLigaDb(ApiSource):
    name = "openligadb"
    label = "OpenLigaDB"
    base_url = "https://api.openligadb.de"
    requires_key = False
    per_minute = 30

    def season(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        if not league.openligadb:
            return []
        data = self.request(f"/getmatchdata/{league.openligadb}/{season}", ttl=ttl)
        return parse_matches(data, league, season, self.name)


def _final_score(m: dict) -> tuple[int, int] | None:
    results = [r for r in m.get("matchResults") or [] if isinstance(r, dict)]
    final = [r for r in results if r.get("resultTypeID") == FINAL_RESULT] or sorted(
        results, key=lambda r: r.get("resultOrderID") or 0)[-1:]
    if not final:
        return None
    a, b = final[0].get("pointsTeam1"), final[0].get("pointsTeam2")
    return (a, b) if isinstance(a, int) and isinstance(b, int) else None


def parse_matches(data: Any, league: League, season: int, source: str = "openligadb") -> list[MatchRecord]:
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        t1, t2 = m.get("team1") or {}, m.get("team2") or {}
        home, away = str(t1.get("teamName") or "").strip(), str(t2.get("teamName") or "").strip()
        when = m.get("matchDateTimeUTC") or m.get("matchDateTime")
        if not home or not away or not when or m.get("matchID") is None:
            continue
        try:
            kickoff = parse_iso(str(when))
        except ValueError:
            continue
        score = _final_score(m) if m.get("matchIsFinished") else None
        out.append(MatchRecord(
            source=source, external_id=str(m["matchID"]), league_code=league.code, season=season,
            kickoff=kickoff, home=home, away=away, status=FINISHED if score else SCHEDULED,
            home_goals=score[0] if score else None, away_goals=score[1] if score else None,
            home_hints=tuple(x for x in (t1.get("shortName"),) if x),
            away_hints=tuple(x for x in (t2.get("shortName"),) if x),
        ))
    return out
