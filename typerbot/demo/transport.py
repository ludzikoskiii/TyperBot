"""Transport HTTP trybu demo – odpowiada w formatach prawdziwych źródeł (bez sieci).

  * football-data.co.uk – pliki sezonów (z opóźnieniem 2 dni), fixtures.csv i new_league_fixtures.csv
    z kursami na najbliższe dni,
  * openfootball – football.json (terminarz całego sezonu, wyniki z opóźnieniem) i warianty nazw klubów,
  * OpenLigaDB – JSON meczów ligi,
  * international_results – results.csv reprezentacji.
Pozwala zasymulować awarię źródła (brak połączenia), żeby pokazać, że aplikacja działa dalej.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from typerbot.data.errors import SourceUnavailableError
from typerbot.data.http import HttpResponse
from typerbot.demo.world import (
    CSV_BOOKS_1X2, LEAGUE_BY_CODE, LEAGUES, OU_BOOKS, UK, DemoMatch, DemoWorld, LeagueSpec,
)

CSV_MAIN = {spec.csv_main: spec for spec in LEAGUES if spec.csv_main}
CSV_EXTRA = {spec.csv_extra: spec for spec in LEAGUES if spec.csv_extra}
OPENFOOTBALL = {spec.openfootball: spec for spec in LEAGUES if spec.openfootball}
OPENLIGADB = {spec.openligadb: spec for spec in LEAGUES if spec.openligadb}
CLUB_FILES = {"europe/england/eng": "England", "south-america/brazil/br": "Brazil", "asia/japan/jp": "Japan"}


def _json(status: int, payload: Any) -> HttpResponse:
    return HttpResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))


def _csv(text: str) -> HttpResponse:
    return HttpResponse(200, {"content-type": "text/csv"}, text.encode("utf-8"))


NOT_FOUND = HttpResponse(404, {}, b"404: Not Found")


class DemoTransport:
    def __init__(self, world: DemoWorld, *, fail: set[str] | None = None, fixtures_days: int = 7,
                 extra_fixtures: bool = True, result_lag_days: int = 2):
        self.world = world
        self.fail = set(fail or ())
        self.fixtures_days = fixtures_days       # ile dni naprzód obejmują pliki z terminarzem i kursami
        self.extra_fixtures = extra_fixtures     # False – plik lig 'extra' nieaktualny (pusty)
        self.result_lag_days = result_lag_days   # pliki z wynikami są aktualizowane z opóźnieniem
        self.calls: list[str] = []

    def _source(self, host: str, path: str) -> str | None:
        if host == "www.football-data.co.uk":
            return "football_data_csv"
        if host == "api.openligadb.de":
            return "openligadb"
        if host == "raw.githubusercontent.com":
            if path.startswith("/openfootball/football.json/"):
                return "openfootball"
            if path.startswith("/openfootball/clubs/"):
                return "club_names"
            if path.startswith("/martj42/international_results/"):
                return "international"
        return None

    def __call__(self, url: str, params: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> HttpResponse:
        parsed = urlparse(url)
        source = self._source(parsed.netloc, parsed.path)
        self.calls.append(f"{source}:{parsed.path}")
        if source is None:
            return NOT_FOUND
        if source in self.fail:
            raise SourceUnavailableError("symulowana awaria – brak połączenia")
        return getattr(self, f"_{source}")(parsed.path)

    def _ready(self, m: DemoMatch) -> bool:
        return m.kickoff + timedelta(days=self.result_lag_days) <= self.world.now

    def _upcoming(self, spec: LeagueSpec) -> list[DemoMatch]:
        end = self.world.now + timedelta(days=self.fixtures_days)
        return [m for m in self.world.league_matches(spec.code) if self.world.now < m.kickoff <= end]

    # -- football-data.co.uk --------------------------------------------------------------
    def _football_data_csv(self, path: str) -> HttpResponse:
        m = re.match(r"/mmz4281/(\d{2})(\d{2})/(\w+)\.csv", path)
        if m and m.group(3) in CSV_MAIN:
            spec, season = CSV_MAIN[m.group(3)], 2000 + int(m.group(1))
            rows = [x for x in self.world.league_matches(spec.code) if x.season == season and self._ready(x)]
            return _csv(self._csv_main(spec, rows)) if rows else NOT_FOUND
        if path == "/fixtures.csv":
            return _csv(self._csv_fixtures_main())
        if path == "/new_league_fixtures.csv":
            return _csv(self._csv_fixtures_extra())
        m = re.match(r"/new/(\w+)\.csv", path)
        if m and m.group(1) in CSV_EXTRA:
            spec = CSV_EXTRA[m.group(1)]
            return _csv(self._csv_extra(spec, [x for x in self.world.league_matches(spec.code) if self._ready(x)]))
        return NOT_FOUND

    def _csv_fixtures_main(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Div", "Date", "Time", "HomeTeam", "AwayTeam", "Referee"]
                   + [f"{b}{s}" for b in CSV_BOOKS_1X2 for s in "HDA"]
                   + [f"{b}{s}2.5" for b in OU_BOOKS for s in "><"])
        for code, spec in CSV_MAIN.items():
            for m in self._upcoming(spec):
                local = m.kickoff.astimezone(UK)
                row = [code, local.strftime("%d/%m/%Y"), local.strftime("%H:%M"), m.home.csv, m.away.csv, ""]
                row += [self.world.odds_for(m, mg)[s] for mg in CSV_BOOKS_1X2.values() for s in "HDA"]
                row += [self.world.odds_for(m, mg)[s] for mg in OU_BOOKS.values() for s in "OU"]
                w.writerow(row)
        return buf.getvalue()

    def _csv_fixtures_extra(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Country", "League", "Date", "Time", "Home", "Away", "PSH", "PSD", "PSA", "MaxH", "MaxD", "MaxA",
                    "AvgH", "AvgD", "AvgA", "B365H", "B365D", "B365A"])
        if not self.extra_fixtures:
            return buf.getvalue()
        for spec in CSV_EXTRA.values():
            for m in self._upcoming(spec):
                local = m.kickoff.astimezone(UK)
                row = [spec.country, spec.name, local.strftime("%d/%m/%Y"), local.strftime("%H:%M"),
                       m.home.csv, m.away.csv]
                for mg in (0.025, 0.010, 0.055, 0.065):
                    o = self.world.odds_for(m, mg)
                    row += [o["H"], o["D"], o["A"]]
                w.writerow(row)
        return buf.getvalue()

    def _csv_main(self, spec: LeagueSpec, rows: list[DemoMatch]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "HS", "AS", "HST", "AST"]
                   + [f"{b}{s}" for b in CSV_BOOKS_1X2 for s in "HDA"]
                   + [f"{b}{s}2.5" for b in OU_BOOKS for s in "><"]
                   + [f"{b}C{s}" for b in CSV_BOOKS_1X2 for s in "HDA"]
                   + [f"{b}C{s}2.5" for b in OU_BOOKS for s in "><"])
        for m in rows:
            local = m.kickoff.astimezone(UK)
            res = "H" if m.home_goals > m.away_goals else ("A" if m.home_goals < m.away_goals else "D")
            row = [spec.csv_main, local.strftime("%d/%m/%Y"), local.strftime("%H:%M"), m.home.csv, m.away.csv,
                   m.home_goals, m.away_goals, res, m.home_shots, m.away_shots, m.home_sot, m.away_sot]
            for sharp in (1.0, 0.5):      # kursy przedmeczowe, potem zamknięcia
                row += [self.world.odds_for(m, mg, sharp)[s] for mg in CSV_BOOKS_1X2.values() for s in "HDA"]
                row += [self.world.odds_for(m, mg, sharp)[s] for mg in OU_BOOKS.values() for s in "OU"]
            w.writerow(row)
        return buf.getvalue()

    def _csv_extra(self, spec: LeagueSpec, rows: list[DemoMatch]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Country", "League", "Season", "Date", "Time", "Home", "Away", "HG", "AG", "Res",
                    "PSCH", "PSCD", "PSCA", "MaxCH", "MaxCD", "MaxCA", "AvgCH", "AvgCD", "AvgCA",
                    "B365CH", "B365CD", "B365CA"])
        for m in rows:
            local = m.kickoff.astimezone(UK)
            res = "H" if m.home_goals > m.away_goals else ("A" if m.home_goals < m.away_goals else "D")
            label = str(m.season) if spec.style == "calendar" else f"{m.season}/{m.season + 1}"
            row = [spec.country, spec.name, label, local.strftime("%d/%m/%Y"), local.strftime("%H:%M"),
                   m.home.csv, m.away.csv, m.home_goals, m.away_goals, res]
            for mg in (0.025, 0.010, 0.055, 0.065):
                o = self.world.odds_for(m, mg, 0.5)
                row += [o["H"], o["D"], o["A"]]
            w.writerow(row)
        return buf.getvalue()

    # -- openfootball --------------------------------------------------------------------------
    def _openfootball(self, path: str) -> HttpResponse:
        m = re.match(r"/openfootball/football\.json/master/(\d{4})(?:-(\d{2}))?/([\w.]+)\.json", path)
        if not m or m.group(3) not in OPENFOOTBALL:
            return NOT_FOUND
        spec = OPENFOOTBALL[m.group(3)]
        season = int(m.group(1))
        rows = [x for x in self.world.league_matches(spec.code) if x.season == season]
        if not rows:
            return NOT_FOUND
        tz = ZoneInfo(spec.tz)
        matches = []
        for x in rows:
            local = x.kickoff.astimezone(tz)
            item = {"round": f"Matchday {x.round}", "date": local.date().isoformat(),
                    "time": local.strftime("%H:%M"), "team1": x.home.full, "team2": x.away.full}
            if self._ready(x):
                item["score"] = {"ft": [x.home_goals, x.away_goals]}
            matches.append(item)
        return _json(200, {"name": f"{spec.name} {season}", "matches": matches})

    def _club_names(self, path: str) -> HttpResponse:
        m = re.match(r"/openfootball/clubs/master/(.+)\.clubs\.txt", path)
        text = self.world.clubs_txt(CLUB_FILES.get(m.group(1), "")) if m else None
        return HttpResponse(200, {"content-type": "text/plain"}, text.encode("utf-8")) if text else NOT_FOUND

    # -- OpenLigaDB ------------------------------------------------------------------------------
    def _openligadb(self, path: str) -> HttpResponse:
        m = re.match(r"/getmatchdata/(\w+)/(\d{4})", path)
        if not m or m.group(1) not in OPENLIGADB:
            return _json(200, [])
        spec, season = OPENLIGADB[m.group(1)], int(m.group(2))
        out = []
        for x in self.world.league_matches(spec.code):
            if x.season != season:
                continue
            done = self.world.is_finished(x)
            out.append({
                "matchID": 900000 + x.seq, "leagueShortcut": spec.openligadb, "leagueSeason": season,
                "matchDateTimeUTC": x.kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "team1": {"teamName": x.home.csv, "shortName": x.home.key},
                "team2": {"teamName": x.away.csv, "shortName": x.away.key},
                "matchIsFinished": done,
                "matchResults": [{"resultTypeID": 2, "resultName": "Endergebnis", "pointsTeam1": x.home_goals,
                                  "pointsTeam2": x.away_goals}] if done else [],
            })
        return _json(200, out)

    # -- reprezentacje -------------------------------------------------------------------------
    def _international(self, path: str) -> HttpResponse:
        if not path.endswith("/results.csv"):
            return NOT_FOUND
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "home_team", "away_team", "home_score", "away_score", "tournament", "city", "country",
                    "neutral"])
        for x in self.world.league_matches("INT"):
            ready = self._ready(x)
            if not ready and not self.world.international_fixtures:
                continue
            w.writerow([x.kickoff.date().isoformat(), x.home.csv, x.away.csv,
                        x.home_goals if ready else "NA", x.away_goals if ready else "NA", "Friendly", "",
                        x.home.csv, "TRUE" if x.neutral else "FALSE"])
        return _csv(buf.getvalue())


__all__ = ["DemoTransport", "LEAGUE_BY_CODE"]
