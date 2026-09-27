"""football-data.co.uk – główne, darmowe źródło: wyniki, kursy i nadchodzące mecze (pliki CSV).

Nie wymaga klucza ani rejestracji i nie ma limitu zapytań. Pliki:
  * 'main'  – jeden plik na sezon: /mmz4281/2425/E0.csv (ligi top-5 i inne),
              kursy przedmeczowe i zamknięcia dla 1X2 oraz powyżej/poniżej 2,5,
              strzały (HS/AS) i celne (HST/AST);
  * 'extra' – jeden plik ze wszystkimi sezonami: /new/POL.csv (np. Ekstraklasa),
              tylko kursy zamknięcia 1X2;
  * /fixtures.csv – nadchodzące mecze lig 'main' z kursami (1X2, powyżej/poniżej 2,5),
  * /new_league_fixtures.csv – nadchodzące mecze lig 'extra' z kursami 1X2;
    oba pliki są aktualizowane w piątek po południu (mecze weekendowe) i we wtorek po południu
    (mecze w środku tygodnia) – wcześniej terminarz dają openfootball i OpenLigaDB.
Ligi z plików z terminarzem, których nie ma w katalogu, są dopisywane automatycznie.
Opis kolumn: https://www.football-data.co.uk/notes.txt
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from collections.abc import Callable

from typerbot.config.leagues import League
from typerbot.data.errors import SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import FINISHED, MARKET_1X2, MARKET_OU, SCHEDULED, MatchRecord, OddsQuote
from typerbot.data.sources.base import ApiSource

UK = ZoneInfo("Europe/London")

# (bookmaker, rynek, typ, rodzaj, linia) -> kandydaci na nazwę kolumny (pierwsza niepusta wygrywa)
_1X2 = {"H": "H", "D": "D", "A": "A"}
_BOOKS_PRE = {
    "avg": ("Avg{s}", "BbAv{s}"),
    "max": ("Max{s}", "BbMx{s}"),
    "pinnacle": ("PS{s}",),
    "bet365": ("B365{s}",),
}
_BOOKS_CLOSE = {
    "avg": ("AvgC{s}",),
    "max": ("MaxC{s}",),
    "pinnacle": ("PSC{s}",),
    "bet365": ("B365C{s}",),
}
_OU_PRE = {
    "avg": ("Avg{s}2.5", "BbAv{s}2.5"),
    "max": ("Max{s}2.5", "BbMx{s}2.5"),
    "pinnacle": ("P{s}2.5",),
    "bet365": ("B365{s}2.5",),
}
_OU_CLOSE = {
    "avg": ("AvgC{s}2.5",),
    "max": ("MaxC{s}2.5",),
    "pinnacle": ("PC{s}2.5",),
    "bet365": ("B365C{s}2.5",),
}
# Plik z nadchodzącymi meczami lig 'extra' – nazwy kolumn bywają z literą C lub bez niej.
_EXTRA_FIXTURE = {
    "avg": ("Avg{s}", "AvgC{s}", "BbAv{s}"),
    "max": ("Max{s}", "MaxC{s}", "BbMx{s}"),
    "pinnacle": ("PS{s}", "P{s}", "PSC{s}"),
    "bet365": ("B365{s}", "B365C{s}"),
}
# Kraj w plikach 'extra' dla kodów lig (kolumna Country).
EXTRA_COUNTRIES = {
    "POL": "Poland", "ARG": "Argentina", "AUT": "Austria", "BRA": "Brazil", "CHN": "China", "DNK": "Denmark",
    "FIN": "Finland", "IRL": "Ireland", "JPN": "Japan", "MEX": "Mexico", "NOR": "Norway", "ROU": "Romania",
    "RUS": "Russia", "SWE": "Sweden", "SWZ": "Switzerland", "USA": "USA",
}
FIXTURES_PAST_TOLERANCE = timedelta(hours=3)   # starsze wiersze pliku z terminarzem pomijamy (plik bywa nieaktualny)

# Starsze pliki 'extra' miały kursy zamknięcia bez litery C.
_EXTRA_CLOSE_FALLBACK = {
    "avg": ("AvgC{s}", "Avg{s}"),
    "max": ("MaxC{s}", "Max{s}"),
    "pinnacle": ("PSC{s}", "P{s}"),
    "bet365": ("B365C{s}", "B365{s}"),
}


def season_code(season: int) -> str:
    return f"{season % 100:02d}{(season + 1) % 100:02d}"


class FootballDataCsv(ApiSource):
    name = "football_data_csv"
    label = "football-data.co.uk"
    base_url = "https://www.football-data.co.uk"
    requires_key = False
    per_minute = 30

    def decode(self, resp: HttpResponse) -> str:
        return resp.text()

    def check_response(self, resp: HttpResponse) -> str:
        if resp.status == 404:
            raise SourceError(f"{self.label}: brak pliku (404)", source=self.name)
        if resp.status >= 400:
            raise SourceError(f"{self.label}: HTTP {resp.status}", source=self.name)
        text = self.decode(resp)
        if "<html" in text[:500].lower():
            raise SourceError(f"{self.label}: zamiast CSV otrzymano stronę HTML", source=self.name)
        return text

    # -- pobieranie ----------------------------------------------------------------
    def season(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        """Mecze jednego sezonu dla lig w formacie 'main'."""
        if league.fdcuk_format != "main" or not league.fdcuk_code:
            return []
        text = self.request(f"/mmz4281/{season_code(season)}/{league.fdcuk_code}.csv", ttl=ttl)
        return parse_main(text, league, season, self.name)

    def extra(self, league: League, seasons: set[int] | None, *, ttl: float) -> list[MatchRecord]:
        """Wszystkie (lub wybrane) sezony z pliku w formacie 'extra'."""
        if league.fdcuk_format != "extra" or not league.fdcuk_code:
            return []
        text = self.request(f"/new/{league.fdcuk_code}.csv", ttl=ttl)
        return parse_extra(text, league, seasons, self.name)

    def upcoming_main(self, resolve: Callable[[str], League | None], now: datetime, *,
                      ttl: float) -> list[MatchRecord]:
        """Nadchodzące mecze lig 'main' z kursami (jeden plik dla wszystkich lig).
        `resolve(kod pliku)` zwraca ligę (także nową, dopisaną automatycznie) albo None (pomiń)."""
        return parse_fixtures_main(self.request("/fixtures.csv", ttl=ttl), resolve, now, self.name)

    def upcoming_extra(self, resolve: Callable[[str, str], League | None], now: datetime, *,
                       ttl: float) -> list[MatchRecord]:
        """Nadchodzące mecze lig 'extra' (np. Ekstraklasa) z kursami 1X2; `resolve(kraj, liga)`."""
        return parse_fixtures_extra(self.request("/new_league_fixtures.csv", ttl=ttl), resolve, now, self.name)


# -- parsowanie -------------------------------------------------------------------
def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        if any(clean.values()):
            rows.append(clean)
    return rows


def _float(row: dict[str, str], *columns: str) -> float | None:
    for col in columns:
        value = row.get(col, "")
        if value:
            try:
                number = float(value)
            except ValueError:
                continue
            if number > 1.0:
                return number
    return None


def _int(row: dict[str, str], col: str) -> int | None:
    value = row.get(col, "")
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def _parse_date(value: str, clock: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            day = datetime.strptime(value, fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    try:
        hh, mm = (int(x) for x in clock.split(":")[:2]) if clock else (15, 0)
    except ValueError:
        hh, mm = 15, 0
    return datetime.combine(day, time(hh, mm), tzinfo=UK).astimezone(timezone.utc)


def _odds(row: dict[str, str], table: dict[str, tuple[str, ...]], market: str,
          selections: dict[str, str], kind: str, line: float = 0.0) -> list[OddsQuote]:
    out = []
    for book, patterns in table.items():
        for sel, suffix in selections.items():
            price = _float(row, *(p.format(s=suffix) for p in patterns))
            if price is not None:
                out.append(OddsQuote(bookmaker=book, market=market, selection=sel, price=price, line=line, kind=kind))
    return out


def _external_id(league: League, season: int, kickoff: datetime, home: str, away: str) -> str:
    return f"{league.code}:{season}:{kickoff.date().isoformat()}:{home}:{away}"


def parse_main(text: str, league: League, season: int, source: str = "football_data_csv") -> list[MatchRecord]:
    out = []
    for row in _rows(text):
        home, away = row.get("HomeTeam", ""), row.get("AwayTeam", "")
        kickoff = _parse_date(row.get("Date", ""), row.get("Time", ""))
        hg, ag = _int(row, "FTHG"), _int(row, "FTAG")
        if not home or not away or kickoff is None or hg is None or ag is None:
            continue
        odds = (
            _odds(row, _BOOKS_PRE, MARKET_1X2, _1X2, "pre")
            + _odds(row, _BOOKS_CLOSE, MARKET_1X2, _1X2, "close")
            + _odds(row, _OU_PRE, MARKET_OU, {"O": ">", "U": "<"}, "pre", 2.5)
            + _odds(row, _OU_CLOSE, MARKET_OU, {"O": ">", "U": "<"}, "close", 2.5)
        )
        out.append(MatchRecord(
            source=source,
            external_id=_external_id(league, season, kickoff, home, away),
            league_code=league.code,
            season=season,
            kickoff=kickoff,
            home=home,
            away=away,
            status=FINISHED,
            home_goals=hg,
            away_goals=ag,
            home_shots=_int(row, "HS"),
            away_shots=_int(row, "AS"),
            home_sot=_int(row, "HST"),
            away_sot=_int(row, "AST"),
            odds=odds,
        ))
    return out


def parse_season_label(label: str) -> int | None:
    """'2023/2024' -> 2023, '2023' -> 2023."""
    head = label.strip().split("/")[0]
    return int(head) if head.isdigit() else None


def parse_extra(text: str, league: League, seasons: set[int] | None,
                source: str = "football_data_csv") -> list[MatchRecord]:
    out = []
    for row in _rows(text):
        season = parse_season_label(row.get("Season", ""))
        if season is None or (seasons is not None and season not in seasons):
            continue
        home, away = row.get("Home", ""), row.get("Away", "")
        kickoff = _parse_date(row.get("Date", ""), row.get("Time", ""))
        hg, ag = _int(row, "HG"), _int(row, "AG")
        if not home or not away or kickoff is None or hg is None or ag is None:
            continue
        out.append(MatchRecord(
            source=source,
            external_id=_external_id(league, season, kickoff, home, away),
            league_code=league.code,
            season=season,
            kickoff=kickoff,
            home=home,
            away=away,
            status=FINISHED,
            home_goals=hg,
            away_goals=ag,
            odds=_odds(row, _EXTRA_CLOSE_FALLBACK, MARKET_1X2, _1X2, "close"),
        ))
    return out


# -- nadchodzące mecze ------------------------------------------------------------------
def _fixture_record(league: League, kickoff: datetime, home: str, away: str, odds: list[OddsQuote],
                    source: str) -> MatchRecord:
    season = league.season_at(kickoff)
    return MatchRecord(
        source=source,
        external_id=_external_id(league, season, kickoff, home, away),   # ten sam id co w pliku z wynikami
        league_code=league.code,
        season=season,
        kickoff=kickoff,
        home=home,
        away=away,
        status=SCHEDULED,
        odds=odds,
    )


def _resolver(leagues: list[League] | Callable, key: Callable[[League], str]) -> Callable:
    if callable(leagues):
        return leagues
    index = {key(lg).lower(): lg for lg in leagues}
    return lambda *names: next((index[n.lower()] for n in names if n and n.lower() in index), None)


def parse_fixtures_main(text: str, leagues: list[League] | Callable[[str], League | None], now: datetime,
                        source: str = "football_data_csv") -> list[MatchRecord]:
    resolve = _resolver(leagues, lambda lg: lg.fdcuk_code or "")
    out = []
    for row in _rows(text):
        div = row.get("Div", "")
        league = resolve(div) if div else None
        home, away = row.get("HomeTeam", ""), row.get("AwayTeam", "")
        kickoff = _parse_date(row.get("Date", ""), row.get("Time", ""))
        if league is None or not home or not away or kickoff is None or kickoff < now - FIXTURES_PAST_TOLERANCE:
            continue
        odds = (_odds(row, _BOOKS_PRE, MARKET_1X2, _1X2, "pre")
                + _odds(row, _OU_PRE, MARKET_OU, {"O": ">", "U": "<"}, "pre", 2.5))
        out.append(_fixture_record(league, kickoff, home, away, odds, source))
    return out


def parse_fixtures_extra(text: str, leagues: list[League] | Callable[[str, str], League | None], now: datetime,
                         source: str = "football_data_csv") -> list[MatchRecord]:
    resolve = _resolver(leagues, lambda lg: EXTRA_COUNTRIES.get(lg.fdcuk_code or "", lg.fdcuk_code or ""))
    out = []
    for row in _rows(text):
        country = row.get("Country", "").strip()
        league = resolve(country, row.get("League", "").strip()) if country else None
        home, away = row.get("Home", "") or row.get("HomeTeam", ""), row.get("Away", "") or row.get("AwayTeam", "")
        kickoff = _parse_date(row.get("Date", ""), row.get("Time", ""))
        if league is None or not home or not away or kickoff is None or kickoff < now - FIXTURES_PAST_TOLERANCE:
            continue
        out.append(_fixture_record(league, kickoff, home, away,
                                   _odds(row, _EXTRA_FIXTURE, MARKET_1X2, _1X2, "pre"), source))
    return out
