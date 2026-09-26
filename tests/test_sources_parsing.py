from datetime import timezone

from typerbot.config.leagues import DEFAULT_LEAGUES
from typerbot.data.records import FINISHED, POSTPONED, SCHEDULED
from typerbot.data.sources.api_football import ApiFootball, parse_statistics, plan_seasons
from typerbot.data.sources.football_data_csv import parse_extra, parse_main, parse_season_label, season_code
from typerbot.data.sources.football_data_org import FootballDataOrg, regular_time_score
from typerbot.data.sources.oddspapi import (
    OddsPapi, parse_bookmaker_odds, parse_historical, parse_market_definition, parse_score,
)
from typerbot.data.sources.the_odds_api import TheOddsApi, parse_double_chance

LEAGUE = {lg.code: lg for lg in DEFAULT_LEAGUES}


def _source(cls):
    return cls.__new__(cls)  # parsowanie nie potrzebuje klienta HTTP


# -- football-data.org -----------------------------------------------------------------
def test_fdorg_regular_time_score_excludes_extra_time_and_penalties():
    assert regular_time_score({"duration": "REGULAR", "fullTime": {"home": 2, "away": 1}}) == (2, 1)
    assert regular_time_score({"duration": "EXTRA_TIME", "fullTime": {"home": 2, "away": 1},
                               "regularTime": {"home": 1, "away": 1}}) == (1, 1)
    assert regular_time_score({"duration": "PENALTY_SHOOTOUT", "fullTime": {"home": 6, "away": 5},
                               "extraTime": {"home": 0, "away": 0}, "penalties": {"home": 5, "away": 4}}) == (1, 1)


def test_fdorg_parse_matches():
    src = _source(FootballDataOrg)
    src.name = "football_data_org"
    data = {"matches": [
        {"id": 1, "utcDate": "2026-09-27T14:00:00Z", "status": "FINISHED", "season": {"startDate": "2026-08-15"},
         "homeTeam": {"name": "Arsenal FC", "shortName": "Arsenal"}, "awayTeam": {"name": "Chelsea FC"},
         "score": {"duration": "REGULAR", "fullTime": {"home": 3, "away": 0}}},
        {"id": 2, "utcDate": "2026-10-04T14:00:00Z", "status": "POSTPONED", "season": {"startDate": "2026-08-15"},
         "homeTeam": {"name": "Everton FC"}, "awayTeam": {"name": "Fulham FC"}, "score": {"fullTime": {}}},
        {"id": 3, "utcDate": "2026-10-04T14:00:00Z", "status": "TIMED",
         "homeTeam": {"name": None}, "awayTeam": {"name": "X"}},  # drużyna nieustalona – pomijamy
    ]}
    recs = src.parse_matches(data, LEAGUE["PL"])
    assert len(recs) == 2
    assert (recs[0].status, recs[0].home_goals, recs[0].season, recs[0].home_hints) == (FINISHED, 3, 2026, ("Arsenal",))
    assert recs[1].status == POSTPONED and recs[1].home_goals is None
    assert recs[0].kickoff.tzinfo == timezone.utc


# -- API-Football ---------------------------------------------------------------------
def test_apif_parse_fixture_uses_90_minute_score():
    src = _source(ApiFootball)
    src.name = "api_football"
    data = {"response": [{
        "fixture": {"id": 7, "date": "2024-05-01T20:00:00+02:00", "status": {"short": "AET"}},
        "league": {"season": 2023},
        "teams": {"home": {"id": 10, "name": "Legia Warszawa"}, "away": {"id": 11, "name": "Lech Poznan"}},
        "goals": {"home": 2, "away": 1}, "score": {"fulltime": {"home": 1, "away": 1}}}]}
    rec = src.parse_fixtures(data, LEAGUE["EKS"])[0]
    assert (rec.home_goals, rec.away_goals, rec.status, rec.season) == (1, 1, FINISHED, 2023)
    assert rec.kickoff.hour == 18 and rec.extra == {"home_id": 10, "away_id": 11}


def test_apif_statistics_order_and_xg():
    data = {"response": [
        {"team": {"id": 11}, "statistics": [{"type": "expected_goals", "value": "0.80"},
                                            {"type": "Total Shots", "value": 7}, {"type": "Shots on Goal", "value": 2}]},
        {"team": {"id": 10}, "statistics": [{"type": "expected_goals", "value": "1.95"},
                                            {"type": "Total Shots", "value": 15}, {"type": "Shots on Goal", "value": None}]},
    ]}
    stats = parse_statistics(data, home_team_id=10)
    assert (stats.home_xg, stats.away_xg, stats.home_shots, stats.away_sot, stats.home_sot) == (1.95, 0.8, 15, 2, None)
    assert parse_statistics({"response": []}) is None


def test_apif_plan_seasons_parsing():
    assert plan_seasons("Free plans do not have access to this season, try from 2022 to 2024.") == (2022, 2024)
    assert plan_seasons("inny komunikat") is None


# -- The Odds API -----------------------------------------------------------------------
def test_odds_api_parse_event_markets():
    src = _source(TheOddsApi)
    src.name = "the_odds_api"
    event = {"id": "ev1", "commence_time": "2026-09-27T16:00:00Z", "home_team": "Legia Warsaw",
             "away_team": "Lech Poznań", "bookmakers": [{"key": "unibet_eu", "markets": [
                 {"key": "h2h", "outcomes": [{"name": "Legia Warsaw", "price": 2.1}, {"name": "Draw", "price": 3.4},
                                             {"name": "Lech Poznań", "price": 3.3}]},
                 {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5},
                                                {"name": "Under", "price": 1.9, "point": 2.5}]},
                 {"key": "btts", "outcomes": [{"name": "Yes", "price": 1.8}, {"name": "No", "price": 2.0}]},
                 {"key": "double_chance", "outcomes": [{"name": "Legia Warsaw/Draw", "price": 1.3},
                                                       {"name": "Legia Warsaw/Lech Poznań", "price": 1.3},
                                                       {"name": "Draw/Lech Poznań", "price": 1.6}]},
                 {"key": "spreads", "outcomes": [{"name": "Legia Warsaw", "price": 1.9, "point": -0.5}]},
             ]}]}
    rec = src.parse_event(event, LEAGUE["EKS"])
    got = {(q.market, q.selection, q.line): q.price for q in rec.odds}
    assert got[("1X2", "H", 0.0)] == 2.1 and got[("1X2", "D", 0.0)] == 3.4 and got[("1X2", "A", 0.0)] == 3.3
    assert got[("OU", "O", 2.5)] == 1.9 and got[("BTTS", "Y", 0.0)] == 1.8
    assert got[("DC", "1X", 0.0)] == 1.3 and got[("DC", "12", 0.0)] == 1.3 and got[("DC", "X2", 0.0)] == 1.6
    assert len(rec.odds) == 10  # 3 + 2 + 2 + 3, spreads pominięte
    assert rec.status == SCHEDULED and rec.season == 2026


def test_double_chance_name_variants():
    assert parse_double_chance("Home/Draw", "A", "B") == "1X"
    assert parse_double_chance("Draw or Away", "A", "B") == "X2"
    assert parse_double_chance("Widzew Łódź/Legia", "Widzew Lodz", "Legia") == "12"
    assert parse_double_chance("Something", "A", "B") is None


# -- OddsPapi --------------------------------------------------------------------------------
MARKETS = [
    {"marketId": 1010, "marketName": "Over Under Full Time", "handicap": 2.5,
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 104, "marketName": "Both Teams To Score", "outcomes": [
        {"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
    {"marketId": 10, "marketName": "Double Chance", "outcomes": [
        {"outcomeId": 10, "outcomeName": "1X"}, {"outcomeId": 11, "outcomeName": "12"}, {"outcomeId": 12, "outcomeName": "X2"}]},
    {"marketId": 1020, "marketName": "Over Under 1st Half", "handicap": 0.5, "outcomes": [
        {"outcomeId": 1020, "outcomeName": "Over"}, {"outcomeId": 1021, "outcomeName": "Under"}]},
]


def market_map():
    mapping = {"101": ("1X2", 0.0, {"101": "H", "102": "D", "103": "A"})}
    for m in MARKETS:
        parsed = parse_market_definition(m)
        if parsed:
            mapping[str(m["marketId"])] = parsed
    return mapping


def test_oddspapi_market_definitions():
    mm = market_map()
    assert mm["1010"] == ("OU", 2.5, {"1010": "O", "1011": "U"})
    assert mm["104"][0] == "BTTS" and mm["10"][2] == {"10": "1X", "11": "12", "12": "X2"}
    assert "1020" not in mm  # rynek na 1. połowę pomijamy


def test_oddspapi_bookmaker_odds():
    price = lambda p, active=True: {"players": {"0": {"price": p, "active": active}}}  # noqa: E731
    odds = {"superbet": {"markets": {
        "101": {"outcomes": {"101": price(2.05), "102": price(3.5), "103": price(3.6, active=False)}},
        "1010": {"outcomes": {"1010": price(1.85), "1011": price(1.95)}},
        "999": {"outcomes": {"1": price(1.5)}},
    }}}
    quotes = parse_bookmaker_odds(odds, market_map())
    got = {(q.market, q.selection, q.line): q.price for q in quotes}
    assert got == {("1X2", "H", 0.0): 2.05, ("1X2", "D", 0.0): 3.5, ("OU", "O", 2.5): 1.85, ("OU", "U", 2.5): 1.95}


def test_oddspapi_historical_open_and_close():
    books = {"pinnacle": {"markets": {"101": {"outcomes": {"101": {"players": {"0": [
        {"price": 2.3, "createdAt": "2026-09-20T10:00:00Z"},
        {"price": 2.1, "createdAt": "2026-09-27T13:55:00Z"},
        {"price": 2.2, "createdAt": "2026-09-24T10:00:00Z"},
    ]}}}}}}}
    quotes = parse_historical(books, market_map())
    assert {(q.kind, q.price) for q in quotes} == {("pre", 2.3), ("close", 2.1)}


def test_oddspapi_score_regular_time():
    halves = {"fixtureId": "x", "scores": {"1": {"participant1Score": 1, "participant2Score": 0},
                                           "2": {"participant1Score": 1, "participant2Score": 2}}}
    assert parse_score(halves) == (2, 2)
    assert parse_score({"scores": {"0": {"participant1Score": 3, "participant2Score": 1}}}) == (3, 1)
    assert parse_score({"scores": {}}) is None


def test_oddspapi_fixture_parse():
    src = _source(OddsPapi)
    src.name = "oddspapi"
    rec = src.parse_fixture({"fixtureId": "id1", "participant1Name": "Legia Warszawa", "participant2Name": "Lech Poznań",
                             "startTime": "2026-09-27T16:00:00Z", "statusId": 2}, LEAGUE["EKS"])
    assert rec.status == FINISHED and rec.home_goals is None and rec.external_id == "id1"
    assert src.parse_fixture({"fixtureId": "id2"}, LEAGUE["EKS"]) is None


# -- CSV (opcjonalny import) ------------------------------------------------------------------
MAIN_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST,B365H,B365D,B365A,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5,PSCH,PSCD,PSCA,AvgC>2.5,AvgC<2.5
E0,16/08/2024,20:00,Man United,Fulham,1,0,H,14,10,5,2,1.60,4.20,5.25,1.62,4.15,5.10,1.80,2.00,1.65,4.00,5.20,1.85,1.95
E0,17/08/24,,Ipswich,Liverpool,0,2,A,7,18,2,5,,,,,,,,,,,,,
E0,,,,,,,,,,,,,,,,,,,,,,,,
"""


def test_csv_main_format():
    recs = parse_main(MAIN_CSV, LEAGUE["PL"], 2024)
    assert len(recs) == 2
    first = recs[0]
    assert (first.home, first.home_goals, first.home_shots, first.away_sot) == ("Man United", 1, 14, 2)
    assert first.kickoff.hour == 19  # 20:00 czasu brytyjskiego (BST) = 19:00 UTC
    got = {(q.bookmaker, q.market, q.selection, q.kind): q.price for q in first.odds}
    assert got[("bet365", "1X2", "H", "pre")] == 1.60
    assert got[("avg", "OU", "O", "pre")] == 1.80
    assert got[("pinnacle", "1X2", "A", "close")] == 5.20
    assert got[("avg", "OU", "U", "close")] == 1.95
    assert recs[1].kickoff.hour == 14 and recs[1].odds == []  # brak godziny -> 15:00 czasu UK


def test_csv_extra_format_filters_seasons():
    text = ("Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA\n"
            "Poland,Ekstraklasa,2023/2024,21/07/2023,18:00,Legia,Lech,2,2,D,2.1,3.4,3.5,2.0,3.3,3.4\n"
            "Poland,Ekstraklasa,2024/2025,19/07/2024,18:00,Rakow,Lechia,1,0,H,1.5,4.0,6.0,1.45,4.1,6.2\n")
    recs = parse_extra(text, LEAGUE["EKS"], {2024})
    assert len(recs) == 1 and recs[0].season == 2024
    got = {(q.bookmaker, q.selection): q.price for q in recs[0].odds}
    assert got[("pinnacle", "H")] == 1.5 and got[("avg", "A")] == 6.2
    assert all(q.kind == "close" for q in recs[0].odds)


def test_season_helpers():
    assert season_code(2024) == "2425" and season_code(1999) == "9900"
    assert parse_season_label("2023/2024") == 2023 and parse_season_label("2023") == 2023
