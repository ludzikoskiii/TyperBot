"""Mecze reprezentacji – zbiór „international_results” (domena publiczna, CC0) na GitHubie.

https://raw.githubusercontent.com/martj42/international_results/master/results.csv
Wyniki wszystkich meczów reprezentacji od 1872 r.; aktualizowany mniej więcej raz w miesiącu.
Służy do rankingu Elo reprezentacji. Terminarza zwykle nie ma – mecz bez wyniku (NA)
w pliku traktujemy jako zaplanowany (godzina nieznana).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timezone

from typerbot.data.http import HttpResponse
from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord
from typerbot.data.sources.base import ApiSource

LEAGUE = "INT"
DEFAULT_TIME = time(18, 0)       # godzina przybliżona (plik podaje tylko datę)


class InternationalResults(ApiSource):
    name = "international"
    label = "Reprezentacje"
    base_url = "https://raw.githubusercontent.com/martj42/international_results/master"
    requires_key = False
    per_minute = 10

    def decode(self, resp: HttpResponse) -> str:
        return resp.text()

    def results(self, since: date, *, ttl: float) -> list[MatchRecord]:
        return parse_results(self.request("/results.csv", ttl=ttl), since, self.name)


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_results(text: str, since: date, source: str = "international") -> list[MatchRecord]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            day = date.fromisoformat((row.get("date") or "").strip())
        except ValueError:
            continue
        home, away = (row.get("home_team") or "").strip(), (row.get("away_team") or "").strip()
        if day < since or not home or not away:
            continue
        hg, ag = _int(row.get("home_score", "")), _int(row.get("away_score", ""))
        finished = hg is not None and ag is not None
        kickoff = datetime.combine(day, DEFAULT_TIME, tzinfo=timezone.utc)
        out.append(MatchRecord(
            source=source, external_id=f"{LEAGUE}:{day.isoformat()}:{home}:{away}", league_code=LEAGUE,
            season=day.year, kickoff=kickoff, home=home, away=away, status=FINISHED if finished else SCHEDULED,
            home_goals=hg, away_goals=ag, kickoff_exact=False,
            neutral=(row.get("neutral") or "").strip().upper() == "TRUE",
            extra={"tournament": (row.get("tournament") or "").strip()},
        ))
    return out
