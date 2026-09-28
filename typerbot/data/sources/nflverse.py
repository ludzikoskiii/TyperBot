"""NFL – plik games.csv projektu nflverse (dane Lee Sharpe'a) na GitHubie, bez klucza i rejestracji.

https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
Jeden plik: terminarz całego sezonu z wyprzedzeniem, wyniki od 1999 r. i kursy bukmacherskie
(zwycięzca, handicap – „spread” – i suma punktów) na najbliższą kolejkę, a po meczu – kursy zamknięcia.
Godziny rozpoczęcia są podane w czasie wschodnioamerykańskim.

Kursy amerykańskie (np. −150, +130) przeliczamy na dziesiętne. Handicap zapisujemy z perspektywy
gospodarzy: spread_line = 3 oznacza, że gospodarze są faworytem o 3 punkty – typ „1 (−3)”.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from typerbot.data.http import HttpResponse
from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord, OddsQuote
from typerbot.data.sources.base import ApiSource

LEAGUE = "NFL"
BOOKMAKER = "nflverse"          # kursy zbiorcze z pliku (jedna cena na typ)
EASTERN = ZoneInfo("America/New_York")

# Skróty drużyn → pełne nazwy; stare skróty przeniesionych klubów wskazują obecną nazwę (ta sama drużyna).
TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers", "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    "OAK": "Las Vegas Raiders", "SD": "Los Angeles Chargers", "STL": "Los Angeles Rams",
}


def american_to_decimal(value: str | float | None) -> float | None:
    """Kurs amerykański → dziesiętny: +150 → 2,50; −200 → 1,50."""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x == 0:
        return None
    return round(1 + x / 100 if x > 0 else 1 + 100 / abs(x), 3)


class Nflverse(ApiSource):
    name = "nflverse"
    label = "nflverse (NFL)"
    base_url = "https://raw.githubusercontent.com/nflverse/nfldata/master/data"
    requires_key = False
    per_minute = 10

    def decode(self, resp: HttpResponse) -> str:
        return resp.text()

    def games(self, since_season: int, *, ttl: float) -> list[MatchRecord]:
        return parse_games(self.request("/games.csv", ttl=ttl), since_season, self.name)


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _odds(row: dict, kind: str) -> list[OddsQuote]:
    out: list[OddsQuote] = []

    def add(market: str, sel: str, price: str | None, line: float = 0.0) -> None:
        dec = american_to_decimal(price)
        if dec and dec > 1.0:
            out.append(OddsQuote(BOOKMAKER, market, sel, dec, line, kind))

    add("ML", "H", row.get("home_moneyline"))
    add("ML", "A", row.get("away_moneyline"))
    try:
        spread = float(row.get("spread_line") or "")
        add("HCP", "H", row.get("home_spread_odds"), -spread)
        add("HCP", "A", row.get("away_spread_odds"), -spread)
    except ValueError:
        pass
    try:
        total = float(row.get("total_line") or "")
        add("OU", "O", row.get("over_odds"), total)
        add("OU", "U", row.get("under_odds"), total)
    except ValueError:
        pass
    return out


def parse_games(text: str, since_season: int, source: str = "nflverse") -> list[MatchRecord]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        season = _int(row.get("season"))
        home = TEAMS.get((row.get("home_team") or "").strip())
        away = TEAMS.get((row.get("away_team") or "").strip())
        day, clock = (row.get("gameday") or "").strip(), (row.get("gametime") or "").strip() or "13:00"
        if season is None or season < since_season or not home or not away or not day or not row.get("game_id"):
            continue
        try:
            kickoff = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=EASTERN)
        except ValueError:
            continue
        hs, as_ = _int(row.get("home_score")), _int(row.get("away_score"))
        finished = hs is not None and as_ is not None
        out.append(MatchRecord(
            source=source, external_id=row["game_id"].strip(), league_code=LEAGUE, season=season,
            kickoff=kickoff, home=home, away=away, status=FINISHED if finished else SCHEDULED,
            home_goals=hs, away_goals=as_, neutral=(row.get("location") or "").strip() == "Neutral",
            odds=_odds(row, "close" if finished else "pre"),
            extra={"week": row.get("week"), "type": row.get("game_type")},
        ))
    return out
