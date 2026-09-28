"""Transport HTTP trybu demo – odpowiada w formatach prawdziwych źródeł (bez sieci).

  * football-data.co.uk – pliki sezonów (z opóźnieniem 2 dni), fixtures.csv i new_league_fixtures.csv
    z kursami na najbliższe dni,
  * openfootball – football.json (terminarz całego sezonu, wyniki z opóźnieniem) i warianty nazw klubów,
  * OpenLigaDB – JSON meczów ligi,
  * international_results – results.csv reprezentacji,
  * nflverse – games.csv (terminarz, wyniki, kursy NFL na najbliższy tydzień i kursy zamknięcia),
  * MLB Stats API – /schedule (terminarz i wyniki),
  * OpenLigaDB – lista lig (/getavailableleagues) z ligami innych dyscyplin i „śmieciowymi” ligami
    (typowanie znajomych, kopia 3. Ligi, liga zagraniczna, stara liga), które aplikacja ma odrzucić.
Pozwala zasymulować awarię źródła (brak połączenia), żeby pokazać, że aplikacja działa dalej.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import date, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from typerbot.data.errors import SourceUnavailableError
from typerbot.data.http import HttpResponse
from typerbot.demo.sports import SPORT_BY_CODE, SPORT_BY_SHORTCUT, SportSpec, american, nfl_lines
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
        if host == "statsapi.mlb.com":
            return "mlb"
        if host == "raw.githubusercontent.com":
            if path.startswith("/nflverse/nfldata/"):
                return "nflverse"
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
        query = f"?{params.get('startDate')}..{params.get('endDate')}" if source == "mlb" else ""
        self.calls.append(f"{source}:{parsed.path}{query}")
        if source is None:
            return NOT_FOUND
        if source in self.fail:
            raise SourceUnavailableError("symulowana awaria – brak połączenia")
        if source == "mlb":
            return self._mlb(parsed.path, params)
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
    def _available_leagues(self) -> list[dict]:
        year = self.world.now.year if self.world.now.month >= 7 else self.world.now.year - 1

        def item(shortcut: str, name: str, season: int, sport: str, sport_id: int) -> dict:
            return {"leagueId": abs(hash((shortcut, season))) % 100000, "leagueName": name, "leagueShortcut": shortcut,
                    "leagueSeason": str(season), "sport": {"sportId": sport_id, "sportName": sport}}

        return [
            item("bl3", "3. Liga", year, "Fußball", 1),
            item("hbl", "Handball-Bundesliga 2025/2026", year - 1, "Handball", 3),
            item("hbl", f"Handball-Bundesliga {year}/{year + 1}", year, "Handball", 3),
            item("del", f"DEL {year}/{year + 1}", year, "Eishockey", 4),
            item("tippbl", f"Tippspiel Bundesliga {year}", year, "Fußball", 1),          # typowanie znajomych
            item("3lfan", f"3. Liga Fanliga {year}/{year + 1}", year, "Fußball", 1),    # kopia 3. Ligi
            item("epl", f"Premier League {year}/{year + 1}", year, "Fußball", 1),       # liga zagraniczna
            item("hallen", f"Hallenmasters {year}", year, "Fußball", 1),                # za mało meczów
            item("oldhc", "Hockey Cup 2019", 2019, "Eishockey", 4),                     # stary sezon
            item("xyz", "Jakaś liga", year, "Curling", 9),                              # nieznana dyscyplina
        ]

    def _openligadb(self, path: str) -> HttpResponse:
        if path.rstrip("/") == "/getavailableleagues":
            return _json(200, self._available_leagues())
        m = re.match(r"/getmatchdata/(\w+)/(\d{4})", path)
        if m and m.group(1) == "3lfan":           # kopia 3. Ligi – te same drużyny i mecze
            return self._openligadb(f"/getmatchdata/bl3/{m.group(2)}")
        if m and m.group(1) in SPORT_BY_SHORTCUT:
            return self._sport_matchdata(SPORT_BY_SHORTCUT[m.group(1)], int(m.group(2)))
        if m and m.group(1) == "hallen":
            return _json(200, [])
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

    def _sport_matchdata(self, spec: SportSpec, season: int) -> HttpResponse:
        out = []
        for x in self.world.league_matches(spec.code):
            if x.season != season:
                continue
            done = self.world.is_finished(x)
            out.append({
                "matchID": 800000 + x.seq, "leagueShortcut": spec.shortcut, "leagueSeason": season,
                "matchDateTimeUTC": x.kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "team1": {"teamName": x.home.full, "shortName": ""}, "team2": {"teamName": x.away.full, "shortName": ""},
                "matchIsFinished": done,
                "matchResults": [{"resultTypeID": 2, "resultName": "Endergebnis", "resultOrderID": 2,
                                  "pointsTeam1": x.home_goals, "pointsTeam2": x.away_goals}] if done else [],
            })
        return _json(200, out)

    # -- nflverse ---------------------------------------------------------------------------------
    def _nflverse(self, path: str) -> HttpResponse:
        if not path.endswith("/data/games.csv"):
            return NOT_FOUND
        spec = SPORT_BY_CODE["NFL"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["game_id", "season", "game_type", "week", "gameday", "weekday", "gametime", "away_team",
                    "away_score", "home_team", "home_score", "location", "result", "total", "overtime",
                    "away_moneyline", "home_moneyline", "spread_line", "away_spread_odds", "home_spread_odds",
                    "total_line", "under_odds", "over_odds"])
        tz = ZoneInfo(spec.tz)
        for x in self.world.league_matches("NFL"):
            local = x.kickoff.astimezone(tz)
            done = self.world.is_finished(x)
            priced = done or x.kickoff <= self.world.now + timedelta(days=self.fixtures_days)
            odds = [""] * 8
            if priced:
                ln = nfl_lines(x.lam_home, x.lam_away, spec.sd)
                ph = min(max(ln["H"] * (1 + x.noise["H"]), 0.02), 0.98)
                cover = min(max(ln["cover"] * (1 + x.noise["S"]), 0.05), 0.95)
                over = min(max(ln["over"] * (1 + x.noise["O"]), 0.05), 0.95)
                odds = [american(1 - ph, 0.025), american(ph, 0.025), ln["spread"], american(1 - cover, 0.024),
                        american(cover, 0.024), ln["total"], american(1 - over, 0.024), american(over, 0.024)]
            w.writerow([f"{x.season}_{x.round:02d}_{x.away.key}_{x.home.key}", x.season, "REG", x.round,
                        local.date().isoformat(), local.strftime("%A"), local.strftime("%H:%M"), x.away.key,
                        x.away_goals if done else "", x.home.key, x.home_goals if done else "", "Home",
                        x.home_goals - x.away_goals if done else "", x.home_goals + x.away_goals if done else "",
                        "", *odds])
        return _csv(buf.getvalue())

    # -- MLB ----------------------------------------------------------------------------------------
    def _mlb(self, path: str, params: Mapping[str, Any]) -> HttpResponse:
        if not path.endswith("/schedule"):
            return NOT_FOUND
        start = date.fromisoformat(str(params.get("startDate")))
        end = date.fromisoformat(str(params.get("endDate")))
        tz = ZoneInfo(SPORT_BY_CODE["MLB"].tz)
        by_day: dict[str, list[dict]] = {}
        for x in self.world.league_matches("MLB"):
            day = x.kickoff.astimezone(tz).date()
            if not start <= day <= end:
                continue
            done = self.world.is_finished(x)
            post = x.round > 60
            by_day.setdefault(day.isoformat(), []).append({
                "gamePk": 700000 + x.seq, "gameType": "D" if post else "R", "season": str(x.season),
                "gameDate": x.kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": {"abstractGameState": "Final" if done else "Preview",
                           "detailedState": "Final" if done else "Scheduled"},
                "teams": {"home": {"team": {"id": 1, "name": x.home.full}, **({"score": x.home_goals} if done else {})},
                          "away": {"team": {"id": 2, "name": x.away.full}, **({"score": x.away_goals} if done else {})}},
            })
        return _json(200, {"dates": [{"date": d, "games": g} for d, g in sorted(by_day.items())]})

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
