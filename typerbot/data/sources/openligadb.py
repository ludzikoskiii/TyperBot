"""OpenLigaDB – terminarz i wyniki lig niemieckich na bieżąco (projekt społecznościowy).

Odczyt bez klucza i rejestracji: https://api.openligadb.de/getmatchdata/bl3/2026
(skrót ligi, rok rozpoczęcia sezonu). Limit serwisu to 1000 zapytań na godzinę z jednego
adresu – aplikacja wysyła kilka zapytań przy odświeżeniu (jedno na ligę), z cache.
Kursów nie ma – mecze tylko stąd dostają kurs szacunkowy.

Poza ligami z katalogu serwis ma ligi prowadzone przez społeczność – także innych dyscyplin
(piłka ręczna, hokej, piłka nożna kobiet, niższe ligi niemieckie). Listę bierzemy z
https://api.openligadb.de/getavailableleagues; które z nich trafiają do aplikacji (filtry jakości,
bez duplikatów) – decyduje synchronizacja (typerbot.services.sync).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from typerbot.config.leagues import League
from typerbot.config.sports import sport_of
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

    def available(self, *, ttl: float) -> list["AvailableLeague"]:
        return parse_available(self.request("/getavailableleagues", ttl=ttl))


@dataclass(frozen=True)
class AvailableLeague:
    shortcut: str
    name: str
    season: int
    sport: str                   # dyscyplina w aplikacji (typerbot.config.sports)


# Słowa w nazwie ligi, która nie jest prawdziwymi rozgrywkami (typowanie znajomych, testy, kopie).
JUNK_WORDS = ("tipp", "test", "demo", "probe", "kopie", "copy", "privat", "spielwiese", "sandbox", "übung")
CUP_WORDS = ("pokal", "cup", "coupe", "copa", "trophy")
COUNTRY_WORDS = (
    (("österreich", "oesterreich", "austria", "admiral"), "Austria"),
    (("schweiz", "swiss", "suisse"), "Szwajcaria"),
    (("england", "premier league", "english"), "Anglia"),
    (("spanien", "laliga", "la liga"), "Hiszpania"), (("italien", "serie a"), "Włochy"),
    (("frankreich", "ligue"), "Francja"), (("niederlande", "eredivisie"), "Holandia"),
    (("polen", "ekstraklasa"), "Polska"), (("dänemark", "daenemark"), "Dania"), (("schweden",), "Szwecja"),
    (("champions", "europa league", "conference", "euroleague", "ehf", "uefa", "europameister", "euro 20"), "Europa"),
    (("weltmeister", "world cup", " wm ", "wm 20", "olympia"), "Świat"),
    (("nhl", "nba", "nfl", "mlb", "mls"), "USA"),
)


def sport_from(sport_name: str, league_name: str = "") -> str | None:
    """Dyscyplina aplikacji z nazwy dyscypliny w OpenLigaDB (np. „Fußball”, „Handball”, „Eishockey”)."""
    s, n = sport_name.lower(), league_name.lower()
    if "american" in s:
        return "american_football"
    if "hand" in s:
        return "handball"
    if "eishockey" in s or "ice hockey" in s:
        return "hockey"
    if "basketball" in s:
        return "basketball"
    if any(w in s for w in ("fußball", "fussball", "football", "soccer")):
        women = any(w in s or w in n for w in ("frauen", "damen", "women", "female"))
        return "football_women" if women else "football"
    return None


def guess_country(name: str) -> str:
    text = f" {name.lower()} "
    for words, country in COUNTRY_WORDS:
        if any(w in text for w in words):
            return country
    return "Niemcy"            # OpenLigaDB to przede wszystkim ligi niemieckie


def is_junk(name: str) -> bool:
    text = name.lower()
    return any(w in text for w in JUNK_WORDS)


def is_cup(name: str) -> bool:
    text = name.lower()
    return any(w in text for w in CUP_WORDS)


def clean_name(name: str) -> str:
    """Nazwa ligi bez sezonu na końcu („Handball-Bundesliga 2026/2027” → „Handball-Bundesliga”)."""
    text = re.sub(r"\s*[\(\[]?\b(19|20)\d{2}(\s*[/-]\s*(19|20)?\d{2})?\b[\)\]]?\s*$", "", name.strip())
    return text.strip(" -–") or name.strip()


def parse_available(data: Any) -> list[AvailableLeague]:
    out = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict):
            continue
        shortcut = str(x.get("leagueShortcut") or "").strip()
        name = str(x.get("leagueName") or "").strip()
        sport_info = x.get("sport") if isinstance(x.get("sport"), dict) else {}
        sport = sport_from(str(sport_info.get("sportName") or ""), name)
        try:
            season = int(str(x.get("leagueSeason") or "").strip()[:4])
        except ValueError:
            continue
        if shortcut and name and sport:
            out.append(AvailableLeague(shortcut, name, season, sport))
    return out


def _final_score(m: dict, latest: bool = False) -> tuple[int, int] | None:
    """Wynik końcowy. `latest` – dyscypliny bez remisów: ostatni wynik (po dogrywce lub karnych, jeśli jest)."""
    results = [r for r in m.get("matchResults") or [] if isinstance(r, dict)]
    by_order = sorted(results, key=lambda r: r.get("resultOrderID") or 0)[-1:]
    final = (by_order if latest else []) or [r for r in results if r.get("resultTypeID") == FINAL_RESULT] or by_order
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
        score = _final_score(m, latest=not sport_of(league.sport).draws) if m.get("matchIsFinished") else None
        out.append(MatchRecord(
            source=source, external_id=str(m["matchID"]), league_code=league.code, season=season,
            kickoff=kickoff, home=home, away=away, status=FINISHED if score else SCHEDULED,
            home_goals=score[0] if score else None, away_goals=score[1] if score else None,
            home_hints=tuple(x for x in (t1.get("shortName"),) if x),
            away_hints=tuple(x for x in (t2.get("shortName"),) if x),
        ))
    return out
