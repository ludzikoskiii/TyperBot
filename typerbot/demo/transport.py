"""Transport HTTP trybu demo – odpowiada w formatach prawdziwych API.

Odwzorowuje ograniczenia planów darmowych: API-Football – tylko sezony
2022–2024, football-data.org – tylko bieżący sezon. Pozwala też zasymulować
awarię źródła (brak połączenia), żeby pokazać, że aplikacja działa dalej.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from typerbot.data.errors import SourceUnavailableError
from typerbot.data.http import HttpResponse
from typerbot.demo.world import CSV_BOOKS_1X2, ODDS_API_BOOKS, UK, DemoMatch, DemoWorld, price, true_probabilities

HOSTS = {
    "api.football-data.org": "football_data_org",
    "v3.football.api-sports.io": "api_football",
    "api.the-odds-api.com": "the_odds_api",
    "www.football-data.co.uk": "football_data_csv",
    "api.oddspapi.io": "oddspapi",
}
APIF_FREE_SEASONS = (2022, 2024)
PAPI_TOURNAMENTS = {17: ("PL", "england", "premier-league", "Premier League"),
                    202: ("EKS", "poland", "ekstraklasa", "Ekstraklasa")}
# Identyfikatory rynków w trybie demo (1X2 = 101 jak w prawdziwym API, pozostałe przykładowe).
PAPI_MARKETS = [
    {"marketId": 101, "marketName": "Full Time Result", "handicap": 0,
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
    {"marketId": 1010, "marketName": "Over Under Full Time", "handicap": 2.5,
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 104, "marketName": "Both Teams To Score", "handicap": 0,
     "outcomes": [{"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
    {"marketId": 10, "marketName": "Double Chance", "handicap": 0,
     "outcomes": [{"outcomeId": 10, "outcomeName": "1X"}, {"outcomeId": 11, "outcomeName": "12"},
                  {"outcomeId": 12, "outcomeName": "X2"}]},
    {"marketId": 1020, "marketName": "Over Under 1st Half", "handicap": 0.5,
     "outcomes": [{"outcomeId": 1020, "outcomeName": "Over"}, {"outcomeId": 1021, "outcomeName": "Under"}]},
]
SUPERBET_MARGIN = 0.07
FDORG_CODES = {"PL": "PL"}
APIF_LEAGUES = {39: "PL", 106: "EKS"}
ODDS_KEYS = {"soccer_epl": "PL", "soccer_poland_ekstraklasa": "EKS"}
CSV_MAIN = {"E0": "PL"}
CSV_EXTRA = {"POL": "EKS"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoTransport:
    def __init__(self, world: DemoWorld, *, fail: set[str] | None = None, apif_plan_restricted: bool = True,
                 apif_used_today: int = 3, odds_used: int = 42):
        self.world = world
        self.fail = set(fail or ())
        self.apif_plan_restricted = apif_plan_restricted
        self.apif_used = apif_used_today
        self.odds_used = odds_used
        self.calls: list[str] = []

    def __call__(self, url: str, params: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> HttpResponse:
        parsed = urlparse(url)
        source = HOSTS.get(parsed.netloc)
        self.calls.append(f"{source}:{parsed.path}")
        if source in self.fail:
            raise SourceUnavailableError("symulowana awaria – brak połączenia")
        handler = getattr(self, f"_{source}")
        return handler(parsed.path, dict(params), dict(headers))

    # -- football-data.org -----------------------------------------------------------
    def _football_data_org(self, path: str, params: dict, headers: dict) -> HttpResponse:
        hdr = {"x-requests-available-minute": "9", "x-requestcounter-reset": "60"}
        if not headers.get("X-Auth-Token"):
            return _json(403, {"message": "The resource you are looking for is restricted."}, hdr)
        m = re.match(r"/v4/competitions/(\w+)/matches", path)
        if not m or m.group(1) not in FDORG_CODES:
            return _json(200, {"matches": []}, hdr)
        league = FDORG_CODES[m.group(1)]
        matches = self.world.league_matches(league)
        if "season" in params:
            if int(params["season"]) != self.world.current_season:
                return _json(403, {"message": "The resource you are looking for is restricted. Please pass a valid "
                                              "API token and check your subscription for permission."}, hdr)
            matches = [x for x in matches if x.season == int(params["season"])]
        else:
            lo = date.fromisoformat(params["dateFrom"])
            hi = date.fromisoformat(params["dateTo"])
            matches = [x for x in matches if lo <= x.kickoff.date() <= hi]
        return _json(200, {"matches": [self._fdorg_match(x) for x in matches]}, hdr)

    def _fdorg_match(self, m: DemoMatch) -> dict:
        finished = self.world.is_finished(m)
        status = "FINISHED" if finished else ("IN_PLAY" if self.world.is_live(m) else "TIMED")
        return {
            "id": m.fdorg_id,
            "utcDate": _iso(m.kickoff),
            "status": status,
            "matchday": m.round,
            "season": {"startDate": f"{m.season}-08-15"},
            "homeTeam": {"id": m.home.fdorg_id, "name": m.home.fdorg, "shortName": m.home.key},
            "awayTeam": {"id": m.away.fdorg_id, "name": m.away.fdorg, "shortName": m.away.key},
            "score": {
                "duration": "REGULAR",
                "fullTime": {"home": m.home_goals if finished else None, "away": m.away_goals if finished else None},
            },
        }

    # -- API-Football -------------------------------------------------------------------
    def _api_football(self, path: str, params: dict, headers: dict) -> HttpResponse:
        if not headers.get("x-apisports-key"):
            return _json(200, {"errors": {"token": "Error/Missing application key."}, "response": []})
        if path != "/status":
            self.apif_used += 1
        hdr = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": str(100 - self.apif_used),
               "x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"}
        if path == "/status":
            return _json(200, {"errors": [], "response": {
                "account": {"firstname": "Demo"}, "subscription": {"plan": "Free", "active": True},
                "requests": {"current": self.apif_used, "limit_day": 100}}}, hdr)
        if path == "/fixtures":
            league = APIF_LEAGUES.get(int(params.get("league", 0)))
            season = int(params.get("season", 0))
            lo_s, hi_s = APIF_FREE_SEASONS
            if self.apif_plan_restricted and not lo_s <= season <= hi_s:
                return _json(200, {"errors": {"plan": f"Free plans do not have access to this season, "
                                                      f"try from {lo_s} to {hi_s}."}, "response": []}, hdr)
            lo = date.fromisoformat(params["from"]) if "from" in params else date(season, 7, 1)
            hi = date.fromisoformat(params["to"]) if "to" in params else date(season + 1, 6, 30)
            items = [self._apif_fixture(m) for m in self.world.league_matches(league or "")
                     if m.season == season and lo <= m.kickoff.date() <= hi]
            return _json(200, {"errors": [], "results": len(items), "response": items}, hdr)
        if path == "/fixtures/statistics":
            fid = int(params.get("fixture", 0))
            match = next((m for m in self.world.matches if m.apif_id == fid), None)
            if match is None or not self.world.is_finished(match):
                return _json(200, {"errors": [], "response": []}, hdr)
            return _json(200, {"errors": [], "response": [
                self._apif_stats(match.home, match.home_xg, match.home_shots, match.home_sot),
                self._apif_stats(match.away, match.away_xg, match.away_shots, match.away_sot),
            ]}, hdr)
        return _json(200, {"errors": {"endpoint": "unknown"}, "response": []}, hdr)

    def _apif_fixture(self, m: DemoMatch) -> dict:
        finished = self.world.is_finished(m)
        short = "FT" if finished else ("2H" if self.world.is_live(m) else "NS")
        goals = {"home": m.home_goals, "away": m.away_goals} if finished else {"home": None, "away": None}
        return {
            "fixture": {"id": m.apif_id, "date": m.kickoff.isoformat(), "status": {"short": short}},
            "league": {"id": 39 if m.league == "PL" else 106, "season": m.season, "round": f"Regular Season - {m.round}"},
            "teams": {"home": {"id": m.home.apif_id, "name": m.home.apif}, "away": {"id": m.away.apif_id, "name": m.away.apif}},
            "goals": goals,
            "score": {"fulltime": goals},
        }

    @staticmethod
    def _apif_stats(team, xg: float, shots: int, sot: int) -> dict:
        return {"team": {"id": team.apif_id, "name": team.apif}, "statistics": [
            {"type": "Shots on Goal", "value": sot},
            {"type": "Total Shots", "value": shots},
            {"type": "Ball Possession", "value": "50%"},
            {"type": "expected_goals", "value": f"{xg:.2f}"},
        ]}

    # -- The Odds API ---------------------------------------------------------------------
    def _the_odds_api(self, path: str, params: dict, headers: dict) -> HttpResponse:
        if not params.get("apiKey"):
            return _json(401, {"message": "API key is missing"})
        if path == "/v4/sports":
            return _json(200, [{"key": k, "group": "Soccer", "title": k, "active": True} for k in ODDS_KEYS],
                         self._odds_headers(0))
        m = re.match(r"/v4/sports/([\w]+)/(odds|scores|events/(\w+)/odds)", path)
        if not m or m.group(1) not in ODDS_KEYS:
            return _json(404, {"message": "Unknown sport"})
        league = ODDS_KEYS[m.group(1)]
        markets = [x for x in str(params.get("markets", "")).split(",") if x]
        regions = [x for x in str(params.get("regions", "")).split(",") if x]
        if m.group(2) == "odds":
            cost = len(markets) * len(regions)
            events = [self._odds_event(x, markets) for x in self.world.upcoming(league)]
            return _json(200, events, self._odds_headers(cost))
        if m.group(2) == "scores":
            since = self.world.now - timedelta(days=int(params.get("daysFrom", 1)))
            done = [x for x in self.world.league_matches(league) if since <= x.kickoff and self.world.is_finished(x)]
            return _json(200, [self._odds_score(x) for x in done], self._odds_headers(2))
        event = next((x for x in self.world.league_matches(league) if x.odds_id == m.group(3)), None)
        if event is None:
            return _json(404, {"message": "Event not found"})
        return _json(200, self._odds_event(event, markets), self._odds_headers(len(markets) * len(regions)))

    def _odds_headers(self, cost: int) -> dict:
        self.odds_used += cost
        return {"x-requests-remaining": str(500 - self.odds_used), "x-requests-used": str(self.odds_used),
                "x-requests-last": str(cost)}

    def _odds_event(self, m: DemoMatch, markets: list[str]) -> dict:
        books = []
        for key, margin in ODDS_API_BOOKS.items():
            o = self.world.odds_for(m, margin)
            mk = []
            if "h2h" in markets:
                mk.append({"key": "h2h", "outcomes": [
                    {"name": m.home.odds, "price": o["H"]}, {"name": m.away.odds, "price": o["A"]},
                    {"name": "Draw", "price": o["D"]}]})
            if "totals" in markets:
                mk.append({"key": "totals", "outcomes": [
                    {"name": "Over", "price": o["O"], "point": 2.5}, {"name": "Under", "price": o["U"], "point": 2.5}]})
            if "btts" in markets:
                mk.append({"key": "btts", "outcomes": [{"name": "Yes", "price": o["Y"]}, {"name": "No", "price": o["N"]}]})
            if "double_chance" in markets:
                p = true_probabilities(m.lam_home, m.lam_away)
                mk.append({"key": "double_chance", "outcomes": [
                    {"name": f"{m.home.odds}/Draw", "price": price(p["H"] + p["D"], margin)},
                    {"name": f"{m.home.odds}/{m.away.odds}", "price": price(p["H"] + p["A"], margin)},
                    {"name": f"Draw/{m.away.odds}", "price": price(p["D"] + p["A"], margin)}]})
            books.append({"key": key, "title": key, "last_update": _iso(self.world.now), "markets": mk})
        return {"id": m.odds_id, "sport_key": "soccer", "commence_time": _iso(m.kickoff),
                "home_team": m.home.odds, "away_team": m.away.odds, "bookmakers": books}

    def _odds_score(self, m: DemoMatch) -> dict:
        return {"id": m.odds_id, "commence_time": _iso(m.kickoff), "completed": True,
                "home_team": m.home.odds, "away_team": m.away.odds,
                "scores": [{"name": m.home.odds, "score": str(m.home_goals)},
                           {"name": m.away.odds, "score": str(m.away_goals)}]}

    # -- OddsPapi ------------------------------------------------------------------------
    def _oddspapi(self, path: str, params: dict, headers: dict) -> HttpResponse:
        if not params.get("apiKey"):
            return _json(401, {"message": "Invalid API key"})
        if path == "/v4/tournaments":
            return _json(200, [{"tournamentId": tid, "categorySlug": c, "tournamentSlug": t, "tournamentName": n}
                               for tid, (_, c, t, n) in PAPI_TOURNAMENTS.items()])
        if path == "/v4/bookmakers":
            return _json(200, [{"slug": s} for s in ("pinnacle", "bet365", "superbet", "betclic", "sts")])
        if path == "/v4/markets":
            return _json(200, PAPI_MARKETS)
        if path == "/v4/fixtures":
            league = PAPI_TOURNAMENTS.get(int(params.get("tournamentId", 0)), ("",))[0]
            lo, hi = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            rows = [self._papi_fixture(m) for m in self.world.league_matches(league) if lo <= m.kickoff.date() <= hi]
            return _json(200, rows)
        if path == "/v4/odds-by-tournaments":
            tids = [int(x) for x in str(params.get("tournamentIds", "")).split(",") if x]
            book = str(params.get("bookmaker"))
            items = []
            for tid in tids:
                league = PAPI_TOURNAMENTS.get(tid, ("",))[0]
                for m in self.world.upcoming(league):
                    items.append({"fixtureId": self._papi_id(m), "tournamentId": tid,
                                  "bookmakerOdds": {book: {"bookmakerIsActive": True,
                                                           "markets": self._papi_markets(m)}}})
            return _json(200, items)
        if path == "/v4/scores":
            fid = str(params.get("fixtureId"))
            m = next((x for x in self.world.matches if self._papi_id(x) == fid), None)
            if m is None or not self.world.is_finished(m):
                return _json(200, {"fixtureId": fid, "scores": {}})
            h1, a1 = m.home_goals // 2, m.away_goals // 2
            return _json(200, {"fixtureId": fid, "scores": {
                "1": {"participant1Score": h1, "participant2Score": a1},
                "2": {"participant1Score": m.home_goals - h1, "participant2Score": m.away_goals - a1}}})
        return _json(404, {"message": "Not found"})

    @staticmethod
    def _papi_id(m: DemoMatch) -> str:
        return f"id{1000000000 + m.seq}"

    def _papi_fixture(self, m: DemoMatch) -> dict:
        status = 2 if self.world.is_finished(m) else (1 if self.world.is_live(m) else 0)
        name = (lambda t: t.apif) if m.league == "PL" else (lambda t: t.odds)
        return {"fixtureId": self._papi_id(m), "tournamentId": 17 if m.league == "PL" else 202,
                "participant1Id": m.home.apif_id + 50000, "participant2Id": m.away.apif_id + 50000,
                "participant1Name": name(m.home), "participant2Name": name(m.away),
                "startTime": _iso(m.kickoff), "statusId": status}

    def _papi_markets(self, m: DemoMatch) -> dict:
        o = self.world.odds_for(m, SUPERBET_MARGIN)
        p = true_probabilities(m.lam_home, m.lam_away)

        def out(prices: dict[int, float]) -> dict:
            return {"outcomes": {str(k): {"players": {"0": {"price": v, "active": True}}} for k, v in prices.items()}}

        return {
            "101": out({101: o["H"], 102: o["D"], 103: o["A"]}),
            "1010": out({1010: o["O"], 1011: o["U"]}),
            "104": out({104: o["Y"], 105: o["N"]}),
            "10": out({10: price(p["H"] + p["D"], SUPERBET_MARGIN), 11: price(p["H"] + p["A"], SUPERBET_MARGIN),
                       12: price(p["D"] + p["A"], SUPERBET_MARGIN)}),
        }

    # -- football-data.co.uk --------------------------------------------------------------
    def _football_data_csv(self, path: str, params: dict, headers: dict) -> HttpResponse:
        m = re.match(r"/mmz4281/(\d{2})(\d{2})/(\w+)\.csv", path)
        if m and m.group(3) in CSV_MAIN:
            season = 2000 + int(m.group(1))
            rows = [x for x in self.world.league_matches(CSV_MAIN[m.group(3)]) if x.season == season and self._csv_ready(x)]
            if not rows:
                return HttpResponse(404, {}, b"Not Found")
            return HttpResponse(200, {"content-type": "text/csv"}, self._csv_main(rows).encode("utf-8"))
        m = re.match(r"/new/(\w+)\.csv", path)
        if m and m.group(1) in CSV_EXTRA:
            rows = [x for x in self.world.league_matches(CSV_EXTRA[m.group(1)]) if self._csv_ready(x)]
            return HttpResponse(200, {"content-type": "text/csv"}, self._csv_extra(rows).encode("utf-8"))
        return HttpResponse(404, {}, b"Not Found")

    def _csv_ready(self, m: DemoMatch) -> bool:
        return m.kickoff + timedelta(days=2) <= self.world.now  # pliki są aktualizowane z opóźnieniem

    def _csv_main(self, rows: list[DemoMatch]) -> str:
        buf = io.StringIO()
        cols = ["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "HS", "AS", "HST", "AST"]
        cols += [f"{b}{s}" for b in CSV_BOOKS_1X2 for s in "HDA"]
        cols += [f"{b}{s}2.5" for b in ("B365", "P", "Max", "Avg") for s in "><"]
        cols += [f"{b}C{s}" for b in CSV_BOOKS_1X2 for s in "HDA"]
        cols += [f"{b}C{s}2.5" for b in ("B365", "P", "Max", "Avg") for s in "><"]
        w = csv.writer(buf)
        w.writerow(cols)
        ou_books = {"B365": 0.065, "P": 0.025, "Max": 0.010, "Avg": 0.055}
        for m in rows:
            local = m.kickoff.astimezone(UK)
            res = "H" if m.home_goals > m.away_goals else ("A" if m.home_goals < m.away_goals else "D")
            row = [m.league, local.strftime("%d/%m/%Y"), local.strftime("%H:%M"), m.home.csv, m.away.csv,
                   m.home_goals, m.away_goals, res, m.home_shots, m.away_shots, m.home_sot, m.away_sot]
            for sharp in (1.0, 0.5):
                row += [self.world.odds_for(m, mg, sharp)[s] for mg in CSV_BOOKS_1X2.values() for s in "HDA"]
                row += [self.world.odds_for(m, mg, sharp)[s] for mg in ou_books.values() for s in "OU"]
            # kolejność kolumn: 1X2 pre, O/U pre, 1X2 close, O/U close
            w.writerow(row)
        return buf.getvalue()

    def _csv_extra(self, rows: list[DemoMatch]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Country", "League", "Season", "Date", "Time", "Home", "Away", "HG", "AG", "Res",
                    "PSCH", "PSCD", "PSCA", "MaxCH", "MaxCD", "MaxCA", "AvgCH", "AvgCD", "AvgCA",
                    "B365CH", "B365CD", "B365CA"])
        for m in rows:
            local = m.kickoff.astimezone(UK)
            res = "H" if m.home_goals > m.away_goals else ("A" if m.home_goals < m.away_goals else "D")
            row = ["Poland", "Ekstraklasa", f"{m.season}/{m.season + 1}", local.strftime("%d/%m/%Y"),
                   local.strftime("%H:%M"), m.home.csv, m.away.csv, m.home_goals, m.away_goals, res]
            for mg in (0.025, 0.010, 0.055, 0.065):
                o = self.world.odds_for(m, mg, 0.5)
                row += [o["H"], o["D"], o["A"]]
            w.writerow(row)
        return buf.getvalue()


def _json(status: int, payload: Any, headers: dict | None = None) -> HttpResponse:
    hdr = {"content-type": "application/json", **(headers or {})}
    return HttpResponse(status, hdr, json.dumps(payload).encode("utf-8"))
