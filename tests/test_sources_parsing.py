from datetime import datetime, timezone

from typerbot.config.leagues import DEFAULT_LEAGUES
from typerbot.data.records import FINISHED, SCHEDULED
from typerbot.data.sources.football_data_csv import (
    parse_extra, parse_fixtures_extra, parse_fixtures_main, parse_main, parse_season_label, season_code,
)
from typerbot.data.sources.club_names import parse_clubs
from typerbot.data.sources.international import parse_results
from typerbot.data.sources.openfootball import parse_season, season_folder
from typerbot.data.sources.openligadb import parse_matches

LEAGUE = {lg.code: lg for lg in DEFAULT_LEAGUES}


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


FIXTURES_CSV = """Div,Date,Time,HomeTeam,AwayTeam,Referee,B365H,B365D,B365A,PSH,PSD,PSA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA,B365>2.5,B365<2.5,Avg>2.5,Avg<2.5
E0,27/09/2026,15:00,Arsenal,Chelsea,,1.90,3.60,4.20,1.95,3.70,4.30,2.00,3.80,4.40,1.92,3.60,4.10,1.80,2.00,1.82,1.98
SP1,27/09/2026,20:00,Barcelona,Getafe,,1.20,6.50,13.0,,,,,,,1.22,6.40,12.5,,,1.60,2.30
E0,20/09/2026,15:00,Everton,Fulham,,2.10,3.30,3.50,,,,,,,2.10,3.30,3.50,,,,
I1,27/09/2026,19:45,Inter,Milan,,1.80,3.70,4.50,,,,,,,1.80,3.70,4.50,,,,
"""
EXTRA_FIXTURES_CSV = """Country,League,Date,Time,Home,Away,PSH,PSD,PSA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA
Poland,Ekstraklasa,27/09/2026,17:30,Pogon Szczecin,Katowice,2.10,3.40,3.60,2.20,3.50,3.70,2.05,3.30,3.50
Denmark,Superliga,27/09/2026,14:00,FC Copenhagen,Brondby,1.80,3.80,4.20,1.90,3.90,4.30,1.78,3.70,4.10
"""
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_csv_fixtures_main_upcoming_with_odds():
    recs = parse_fixtures_main(FIXTURES_CSV, [LEAGUE["PL"], LEAGUE["PD"]], NOW)
    assert [(r.league_code, r.home) for r in recs] == [("PL", "Arsenal"), ("PD", "Barcelona")]  # stare i obce ligi pominięte
    ars = recs[0]
    assert ars.status == SCHEDULED and ars.home_goals is None and ars.season == 2026
    assert ars.kickoff == datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)     # 15:00 czasu UK (BST)
    got = {(q.bookmaker, q.market, q.selection, q.line, q.kind): q.price for q in ars.odds}
    assert got[("avg", "1X2", "H", 0.0, "pre")] == 1.92 and got[("pinnacle", "1X2", "A", 0.0, "pre")] == 4.30
    assert got[("avg", "OU", "O", 2.5, "pre")] == 1.82 and got[("bet365", "OU", "U", 2.5, "pre")] == 2.00
    # ten sam identyfikator co w pliku z wynikami – po meczu wynik trafi do tego samego wiersza
    assert ars.external_id.startswith("PL:2026:2026-09-27:Arsenal:Chelsea")


def test_csv_fixtures_extra_matches_country():
    recs = parse_fixtures_extra(EXTRA_FIXTURES_CSV, [LEAGUE["EKS"]], NOW)
    assert len(recs) == 1 and recs[0].league_code == "EKS" and recs[0].home == "Pogon Szczecin"
    got = {(q.bookmaker, q.selection): q.price for q in recs[0].odds}
    assert got[("avg", "H")] == 2.05 and got[("max", "A")] == 3.70 and got[("pinnacle", "D")] == 3.40
    assert all(q.market == "1X2" and q.kind == "pre" for q in recs[0].odds)


def test_csv_fixtures_resolver_adds_unknown_leagues():
    seen = []

    def resolve(div):
        seen.append(div)
        return LEAGUE["PL"] if div == "E0" else None

    recs = parse_fixtures_main(FIXTURES_CSV, resolve, NOW)
    assert {r.league_code for r in recs} == {"PL"} and "I1" in seen and "SP1" in seen


def test_calendar_season_for_fixtures():
    text = "Country,League,Date,Time,Home,Away,AvgH,AvgD,AvgA\nBrazil,Serie A,27/09/2026,21:30,Flamengo RJ,Santos,1.6,3.9,5.5\n"
    rec = parse_fixtures_extra(text, [LEAGUE["BRA"]], NOW)[0]
    assert rec.season == 2026 and rec.league_code == "BRA"


# -- openfootball ------------------------------------------------------------------------------
OPENFOOTBALL = {"name": "Premier League 2026/27", "matches": [
    {"round": "Matchday 1", "date": "2026-08-21", "time": "20:00", "team1": "Arsenal FC", "team2": "Coventry City FC",
     "score": {"ht": [2, 0], "ft": [3, 0]}},
    {"round": "Matchday 8", "date": "2026-10-10", "time": "12:30", "team1": "Chelsea FC", "team2": "Everton FC"},
    {"round": "Matchday 38", "date": "2027-05-30", "team1": "Fulham FC", "team2": "Liverpool FC"},
]}


def test_openfootball_season_parse():
    recs = parse_season(OPENFOOTBALL, LEAGUE["PL"], 2026)
    assert len(recs) == 3
    done, soon, later = recs
    assert done.status == FINISHED and (done.home_goals, done.away_goals) == (3, 0)
    assert done.kickoff == datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)      # 20:00 BST
    assert soon.status == SCHEDULED and soon.home_goals is None and soon.kickoff_exact
    assert soon.external_id == "PL:2026:Matchday 8:Chelsea FC:Everton FC"         # bez daty – przełożenie nie dubluje
    assert not later.kickoff_exact and later.kickoff.date().isoformat() == "2027-05-30"
    assert season_folder(LEAGUE["PL"], 2026) == "2026-27" and season_folder(LEAGUE["BRA"], 2026) == "2026"


def test_openfootball_old_rounds_format():
    data = {"rounds": [{"name": "1", "matches": [{"date": "2015-08-08", "team1": "A", "team2": "B",
                                                   "score1": 1, "score2": 0}]}]}
    rec = parse_season(data, LEAGUE["PL"], 2015)[0]
    assert rec.status == FINISHED and rec.home_goals == 1


# -- OpenLigaDB ---------------------------------------------------------------------------------
def test_openligadb_parse():
    data = [
        {"matchID": 1, "matchDateTimeUTC": "2026-09-19T17:00:00Z", "matchIsFinished": True,
         "team1": {"teamName": "TSV 1860 München", "shortName": "1860"}, "team2": {"teamName": "SC Verl"},
         "matchResults": [{"resultTypeID": 1, "pointsTeam1": 0, "pointsTeam2": 0},
                          {"resultTypeID": 2, "pointsTeam1": 2, "pointsTeam2": 1}]},
        {"matchID": 2, "matchDateTimeUTC": "2026-10-13T17:00:00Z", "matchIsFinished": False,
         "team1": {"teamName": "SC Verl"}, "team2": {"teamName": "FC Ingolstadt 04"}, "matchResults": []},
    ]
    done, upcoming = parse_matches(data, LEAGUE["BL3"], 2026)
    assert done.status == FINISHED and (done.home_goals, done.away_goals) == (2, 1)
    assert done.home_hints == ("1860",) and done.external_id == "1"
    assert upcoming.status == SCHEDULED and upcoming.kickoff.hour == 17


# -- reprezentacje i nazwy klubów -------------------------------------------------------------------
def test_international_results_parse():
    text = ("date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n"
            "2010-06-11,South Africa,Mexico,1,1,FIFA World Cup,Johannesburg,South Africa,FALSE\n"
            "2026-06-20,Poland,Brazil,0,2,FIFA World Cup,Dallas,United States,TRUE\n"
            "2026-10-10,Poland,Norway,NA,NA,UEFA Nations League,Warsaw,Poland,FALSE\n")
    recs = parse_results(text, datetime(2014, 1, 1).date())
    assert len(recs) == 2                                   # starsze niż data odcięcia pominięte
    played, planned = recs
    assert played.status == FINISHED and played.neutral and played.league_code == "INT"
    assert planned.status == SCHEDULED and planned.home_goals is None and not planned.kickoff_exact


def test_club_names_parse():
    text = """====================
=  England

Arsenal FC, 1886, @ Emirates Stadium, London   ## Greater London
  | Arsenal | FC Arsenal
  | Arsenal Football Club
Manchester United FC, 1878
  | Man United | Man Utd     # skróty
  @ Old Trafford
Newport County AFC
"""
    clubs = dict(parse_clubs(text))
    assert clubs["Arsenal FC"] == ["Arsenal", "FC Arsenal", "Arsenal Football Club"]
    assert clubs["Manchester United FC"] == ["Man United", "Man Utd"]
    assert clubs["Newport County AFC"] == []
