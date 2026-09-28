"""Inne dyscypliny: źródła (nflverse, MLB, OpenLigaDB), model wyników, rynki, synchronizacja i kupony."""

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from tests.conftest import NOW
from tests.test_sync import make_service
from typerbot.betting.evaluation import evaluate_match
from typerbot.betting.settlement import VOID, settle
from typerbot.config.settings import CouponSettings, Settings
from typerbot.config.sports import SPORTS
from typerbot.data.records import FINISHED, POSTPONED, SCHEDULED
from typerbot.data.sources.mlb import parse_schedule
from typerbot.data.sources.nflverse import american_to_decimal, parse_games
from typerbot.data.sources.openligadb import (
    clean_name, guess_country, is_junk, parse_available, parse_matches, sport_from,
)
from typerbot.demo.world import DemoWorld
from typerbot.model.data import MatchTable
from typerbot.model.markets import label, sport_market_probabilities
from typerbot.model.scores import ScoreModel, resolve_draws
from typerbot.services.coupons import CouponService

NFL_CSV = """game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,home_score,location,\
result,total,overtime,away_moneyline,home_moneyline,spread_line,away_spread_odds,home_spread_odds,total_line,under_odds,\
over_odds
2019_01_OAK_SD,2019,REG,1,2019-09-08,Sunday,16:05,OAK,20,SD,24,Home,4,44,0,120,-140,2.5,-110,-110,44.5,-110,-110
2026_03_PHI_CHI,2026,REG,3,2026-09-28,Monday,20:15,PHI,,CHI,,Home,,,,-198,164,-3.5,-112,-108,41.5,-110,-110
2026_04_MIN_PIT,2026,REG,4,2026-10-04,Sunday,09:30,MIN,,PIT,,Neutral,,,,,,,,,,,
"""


# -- źródła ----------------------------------------------------------------------------------------------
def test_nflverse_parsing_odds_and_teams():
    recs = parse_games(NFL_CSV, 2019)
    old, mon, london = recs
    assert (old.home, old.away) == ("Los Angeles Chargers", "Las Vegas Raiders")   # stare skróty klubów
    assert old.status == FINISHED and (old.home_goals, old.away_goals) == (24, 20)
    assert {q.kind for q in old.odds} == {"close"}
    assert mon.status == SCHEDULED and mon.kickoff == datetime(2026, 9, 29, 0, 15, tzinfo=timezone.utc)
    q = {(o.market, o.selection, o.line): o.price for o in mon.odds}
    assert q[("ML", "A", 0.0)] == pytest.approx(1 + 100 / 198, abs=1e-3)
    assert q[("ML", "H", 0.0)] == pytest.approx(2.64)
    assert q[("HCP", "H", 3.5)] and q[("HCP", "A", 3.5)]     # gospodarze +3,5 (goście faworytem o 3,5)
    assert q[("OU", "O", 41.5)] == pytest.approx(1 + 100 / 110, abs=1e-3)
    assert london.neutral and london.odds == []
    assert american_to_decimal("+150") == 2.5 and american_to_decimal("-200") == 1.5
    assert american_to_decimal("") is None
    assert parse_games(NFL_CSV, 2026)[0].season == 2026                              # filtr sezonów


def test_mlb_schedule_parsing():
    game = {"gamePk": 1, "gameType": "R", "season": "2026", "gameDate": "2026-09-28T23:05:00Z",
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
            "teams": {"home": {"team": {"name": "New York Yankees"}, "score": 5},
                      "away": {"team": {"name": "Boston Red Sox"}, "score": 3}}}
    later = {**game, "gamePk": 2, "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
             "teams": {"home": {"team": {"name": "New York Yankees"}}, "away": {"team": {"name": "Boston Red Sox"}}}}
    rain = {**later, "gamePk": 3, "status": {"abstractGameState": "Final", "detailedState": "Postponed"}}
    spring = {**game, "gamePk": 4, "gameType": "S"}
    recs = parse_schedule({"dates": [{"date": "2026-09-28", "games": [game, later, rain, spring]}]})
    assert [r.external_id for r in recs] == ["1", "2", "3"]
    assert (recs[0].status, recs[0].home_goals, recs[0].away_goals) == (FINISHED, 5, 3)
    assert recs[1].status == SCHEDULED and recs[1].home_goals is None
    assert recs[2].status == POSTPONED
    assert parse_schedule({"oops": 1}) == [] and parse_schedule(None) == []


def test_openligadb_league_list_filters():
    data = [
        {"leagueShortcut": "hbl", "leagueName": "Handball-Bundesliga 2026/2027", "leagueSeason": "2026",
         "sport": {"sportId": 3, "sportName": "Handball"}},
        {"leagueShortcut": "del", "leagueName": "DEL 2026/27", "leagueSeason": 2026,
         "sport": {"sportId": 4, "sportName": "Eishockey"}},
        {"leagueShortcut": "fbl", "leagueName": "Frauen-Bundesliga", "leagueSeason": "2026",
         "sport": {"sportId": 1, "sportName": "Fußball"}},
        {"leagueShortcut": "xx", "leagueName": "Curling", "leagueSeason": "2026",
         "sport": {"sportId": 9, "sportName": "Curling"}},
        {"leagueShortcut": "bad", "leagueName": "Bez sezonu", "leagueSeason": "?",
         "sport": {"sportName": "Fußball"}},
    ]
    got = {a.shortcut: a for a in parse_available(data)}
    assert set(got) == {"hbl", "del", "fbl"}
    assert (got["hbl"].sport, got["del"].sport, got["fbl"].sport) == ("handball", "hockey", "football_women")
    assert sport_from("American Football") == "american_football" and sport_from("Basketball") == "basketball"
    assert clean_name("Handball-Bundesliga 2026/2027") == "Handball-Bundesliga"
    assert guess_country("Admiral Bundesliga 2026/27") == "Austria" and guess_country("Regionalliga West") == "Niemcy"
    assert is_junk("Tippspiel Bundesliga") and not is_junk("DEL")


def test_openligadb_final_score_after_overtime():
    from typerbot.config.leagues import League

    match = {"matchID": 1, "matchDateTimeUTC": "2026-10-02T17:30:00Z", "matchIsFinished": True,
             "team1": {"teamName": "Eisbären Berlin"}, "team2": {"teamName": "Kölner Haie"},
             "matchResults": [{"resultTypeID": 2, "resultOrderID": 2, "pointsTeam1": 2, "pointsTeam2": 2},
                              {"resultTypeID": 3, "resultOrderID": 3, "pointsTeam1": 3, "pointsTeam2": 2}]}
    hockey = League("OL-DEL", "DEL", "Niemcy", openligadb="del", sport="hockey")
    handball = League("OL-HBL", "HBL", "Niemcy", openligadb="hbl", sport="handball")
    assert (parse_matches([match], hockey, 2026)[0].home_goals, parse_matches([match], hockey, 2026)[0].away_goals) \
        == (3, 2)                                      # hokej – wynik po dogrywce
    assert parse_matches([match], handball, 2026)[0].home_goals == 2    # wynik końcowy (remis możliwy)


# -- rynki i rozliczanie ------------------------------------------------------------------------------------
def test_handicap_and_total_markets_from_matrix():
    rng = np.random.default_rng(1)
    m = rng.random((30, 30))
    m /= m.sum()
    p = sport_market_probabilities(m, False, ou_lines=[20.5, 21.0], hcp_lines=[-3.5, 2.0])
    assert p[("ML", "H", 0.0)] + p[("ML", "A", 0.0)] == pytest.approx(1 - np.trace(m))
    assert p[("HCP", "H", -3.5)] + p[("HCP", "A", -3.5)] == pytest.approx(1.0)        # linia połówkowa
    push = sum(m[i, j] for i in range(30) for j in range(30) if i - j == -2)
    assert p[("HCP", "H", 2.0)] + p[("HCP", "A", 2.0)] == pytest.approx(1 - push)     # linia całkowita – zwrot
    assert p[("OU", "O", 20.5)] >= p[("OU", "O", 21.0)]
    with_draw = sport_market_probabilities(m, True)
    assert sum(with_draw[("1X2", s, 0.0)] for s in "HDA") == pytest.approx(1.0)
    assert ("ML", "H", 0.0) not in with_draw


def test_labels_and_settlement_of_new_markets():
    assert label(("HCP", "H", -3.5)) == "1 (−3,5)" and label(("HCP", "A", -3.5)) == "2 (+3,5)"
    assert label(("ML", "A", 0.0)) == "2 (z dogrywką)" and label(("OU", "O", 44.5)) == "Powyżej 44,5"
    assert settle("ML", "H", 0.0, 24, 20) == 1 and settle("ML", "A", 0.0, 24, 20) == 0
    assert settle("ML", "H", 0.0, 20, 20) is VOID                         # remis w NFL – zwrot
    assert settle("HCP", "H", -3.5, 24, 20) == 1 and settle("HCP", "A", -3.5, 24, 20) == 0
    assert settle("HCP", "H", -4.0, 24, 20) is VOID
    assert settle("HCP", "A", 1.5, 3, 4) == 0                             # goście −1,5 wygrali tylko różnicą 1
    assert settle("HCP", "A", 1.5, 2, 4) == 1
    assert settle("OU", "O", 44.5, 24, 21) == 1


def test_resolve_draws_moves_regulation_ties():
    m = np.array([[0.1, 0.1], [0.3, 0.5]])
    out = resolve_draws(m / m.sum(), 1)
    assert out.shape == (3, 3) and np.trace(out) == pytest.approx(0.0)
    assert out.sum() == pytest.approx(1.0)


# -- model wyników -------------------------------------------------------------------------------------------
def synthetic_table(sport: str, n_teams: int = 12, seasons: int = 3, seed: int = 3):
    """Mecze każdy z każdym z ukrytą siłą drużyn (normalny: punkty; count: Poisson)."""
    rng = np.random.default_rng(seed)
    strength = rng.normal(0, 4.0 if sport != "hockey" else 0.25, n_teams)
    rows = []
    t0 = datetime(2023, 9, 1, tzinfo=timezone.utc).timestamp() / 86400
    day = 0
    for _ in range(seasons):
        for h in range(n_teams):
            for a in range(n_teams):
                if h == a:
                    continue
                if sport == "hockey":
                    hg = rng.poisson(3.0 * math.exp(0.1 + strength[h] - strength[a] / 2))
                    ag = rng.poisson(3.0 * math.exp(strength[a] - strength[h] / 2))
                else:
                    hg = max(0, round(rng.normal(22 + 1 + strength[h] - strength[a] / 2, 9.5)))
                    ag = max(0, round(rng.normal(22 - 1 + strength[a] - strength[h] / 2, 9.5)))
                rows.append((t0 + day, h + 1, a + 1, hg, ag))
                day += 0.5
    t, home, away, hg, ag = (np.array(x) for x in zip(*rows))
    n = len(rows)
    table = MatchTable(np.arange(n), t.astype(float), np.full(n, 2024), home, away, hg, ag,
                       np.full(n, np.nan), np.full(n, np.nan), np.array(["L"] * n, dtype=object), np.zeros(n, bool),
                       neutral=np.zeros(n, bool), sport=np.array([sport] * n, dtype=object))
    return table, strength


@pytest.mark.parametrize("sport", ["american_football", "hockey"])
def test_score_model_recovers_strength_and_is_calibrated(sport):
    table, strength = synthetic_table(sport)
    cut = float(table.t[-1]) + 1
    model = ScoreModel.fit(table, cut, SPORTS[sport])
    assert model is not None
    best, worst = int(np.argmax(strength)) + 1, int(np.argmin(strength)) + 1
    p = model.predict(best, worst, "L")
    assert p.probs[("ML", "H", 0.0)] > 0.75 and p.lam_home > p.lam_away
    assert sum(v for k, v in p.probs.items() if k[0] == "ML") == pytest.approx(1.0, abs=1e-6)   # bez remisów
    assert any(k[0] == "OU" for k in p.probs) and any(k[0] == "HCP" for k in p.probs)
    # kalibracja „w próbie”: średnia prognoza zwycięstwa gospodarzy ≈ odsetek zwycięstw
    preds = [model.predict(int(h), int(a), "L").probs[("ML", "H", 0.0)] for h, a in zip(table.home_id, table.away_id)]
    wins = [(hg > ag) + 0.5 * (hg == ag) for hg, ag in zip(table.hg, table.ag)]
    assert np.mean(preds) == pytest.approx(np.mean(wins), abs=0.04)
    assert model.predict(best, 999, "L").low_data_away                    # nieznana drużyna – mało danych
    if sport == "american_football":
        assert 11 < model.sd_margin < 17


def test_handball_keeps_draws_and_three_way_market():
    table, _ = synthetic_table("american_football")
    table.sport = np.array(["handball"] * len(table), dtype=object)
    model = ScoreModel.fit(table, float(table.t[-1]) + 1, SPORTS["handball"])
    p = model.predict(1, 2, "L")
    assert ("1X2", "D", 0.0) in p.probs and ("ML", "H", 0.0) not in p.probs
    assert 0.0 < p.probs[("1X2", "D", 0.0)] < 0.2


# -- ocena typów z kursami NFL -------------------------------------------------------------------------------
def test_evaluation_uses_real_handicap_and_total_odds():
    model = {("ML", "H", 0.0): 0.62, ("ML", "A", 0.0): 0.38, ("HCP", "H", -3.5): 0.5, ("HCP", "A", -3.5): 0.5,
             ("OU", "O", 44.5): 0.52, ("OU", "U", 44.5): 0.48, ("OU", "O", 41.5): 0.6, ("OU", "U", 41.5): 0.4}
    odds = [{"bookmaker": "nflverse", "market": m, "selection": s, "line": line, "price": p}
            for m, s, line, p in (("ML", "H", 0.0, 1.6), ("ML", "A", 0.0, 2.45), ("HCP", "H", -3.5, 1.91),
                                  ("HCP", "A", -3.5, 1.91), ("OU", "O", 44.5, 1.95), ("OU", "U", 44.5, 1.87))]
    s = Settings()
    ev = {e.key: e for e in evaluate_match(1, model, odds, s, sport="american_football")}
    assert ev[("ML", "H", 0.0)].odds == 1.6 and ev[("ML", "H", 0.0)].p_market == pytest.approx(
        (1 / 1.6) / (1 / 1.6 + 1 / 2.45), abs=0.02)
    assert ev[("HCP", "A", -3.5)].p_market == pytest.approx(0.5, abs=1e-6)
    assert ev[("OU", "O", 41.5)].odds is None                   # linia bez kursu – nie trafi na kupon
    assert ev[("OU", "U", 44.5)].odds_source == "average"


# -- synchronizacja i kupony na świecie demo -----------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


@pytest.fixture
def synced(db, world, clock):
    sync, _ = make_service(db, None, world, clock)
    report = sync.run_all()
    assert not report.errors, [(s.source, s.step, s.message) for s in report.errors]
    return sync


def test_sync_adds_other_sports_and_rejects_junk_leagues(synced, db, world):
    leagues = {lg.code: lg for lg in synced.leagues.all()}
    assert leagues["NFL"].sport == "american_football" and leagues["MLB"].sport == "baseball"
    assert leagues["OL-HBL"].sport == "handball" and leagues["OL-HBL"].name == "Handball-Bundesliga"
    assert leagues["OL-DEL"].sport == "hockey" and leagues["OL-DEL"].country == "Niemcy"
    # kopia 3. Ligi, liga zagraniczna, za mało meczów, typowanie znajomych, stara liga – nie trafiają do aplikacji
    assert not {"OL-3LFAN", "OL-EPL", "OL-HALLEN", "OL-TIPPBL", "OL-OLDHC", "OL-XYZ"} & set(leagues)
    assert set(synced.meta("openligadb.rejected")) == {"3lfan", "epl", "hallen"}
    for code in ("NFL", "MLB", "OL-HBL", "OL-DEL"):
        n = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = ?", (code,))[0]
        assert n == len([m for m in world.league_matches(code) if m.season >= (2020 if code == "NFL" else 2024)]), code
        teams = db.query_one("SELECT COUNT(DISTINCT team_id) FROM team_aliases WHERE league_code = ?", (code,))[0]
        assert teams == len(world.teams[code]), code
    # MLB: serie meczów tych samych drużyn dzień po dniu to osobne mecze
    pairs = db.query_one("SELECT COUNT(*) FROM (SELECT home_team_id, away_team_id, season, COUNT(*) AS n FROM matches "
                         "WHERE league_code = 'MLB' GROUP BY 1, 2, 3 HAVING n > 1)")[0]
    assert pairs > 0
    # drużyna nie ma aliasów w dwóch dyscyplinach
    mixed = db.query_one("SELECT COUNT(*) FROM (SELECT a.team_id FROM team_aliases a JOIN leagues l ON "
                         "l.code = a.league_code GROUP BY a.team_id HAVING COUNT(DISTINCT l.sport) > 1)")[0]
    assert mixed == 0
    # kursy NFL: na najbliższy tydzień (przedmeczowe) i kursy zamknięcia rozegranych meczów
    kinds = {r["kind"] for r in db.query("SELECT DISTINCT o.kind FROM odds o JOIN matches m ON m.id = o.match_id "
                                         "WHERE m.league_code = 'NFL'")}
    assert kinds == {"pre", "close"}
    # druga synchronizacja nie pyta ponownie o odrzucone ligi i nie pobiera zakończonych sezonów NFL
    report = synced.run_all()
    assert not [s for s in report.steps if s.step == "discover"]


def test_coupons_for_chosen_sports(synced, db):
    service = CouponService(db, now=lambda: NOW)
    base = CouponSettings(target_odds=3.0, days_ahead=3, min_events=1, max_events=4, estimated_odds="always")
    for sport in ("american_football", "handball", "hockey"):
        result = service.run(CouponSettings(**{**base.__dict__, "sports": [sport]}))
        assert result.coupons, (sport, result.diagnosis.reasons)
        assert {leg.match.sport for c in result.coupons for leg in c.legs} == {sport}
        markets = {leg.selection.key[0] for c in result.coupons for leg in c.legs}
        assert markets <= set(SPORTS[sport].markets)
    # NFL: typy z prawdziwymi kursami (nflverse), nie szacunkowymi
    nfl = service.run(CouponSettings(**{**base.__dict__, "sports": ["american_football"], "estimated_odds": "never"}))
    assert nfl.coupons and all(not leg.selection.estimated for c in nfl.coupons for leg in c.legs)
    # wszystkie dyscypliny razem – kupon może łączyć piłkę nożną z innymi
    mixed = service.run(CouponSettings(**{**base.__dict__, "alternatives": 10}))
    assert len({leg.match.sport for c in mixed.coupons for leg in c.legs}) > 1
    # dyscyplina bez meczów w zakresie – konkretny powód
    none = service.run(CouponSettings(**{**base.__dict__, "sports": ["basketball"]}))
    assert not none.coupons and "W wybranych dyscyplinach i ligach" in none.diagnosis.reasons[0]


def test_coupon_legs_of_other_sports_settle(synced, db, world):
    from typerbot.services.register import CouponRegister, LegInput

    finished = [m for m in world.league_matches("OL-DEL") if world.is_finished(m)][-1]
    row = db.query_one("SELECT m.id FROM matches m JOIN match_sources s ON s.match_id = m.id "
                       "WHERE s.source = 'openligadb' AND s.external_id = ?", (str(800000 + finished.seq),))
    reg = CouponRegister(db, now=lambda: NOW)
    won = "H" if finished.home_goals > finished.away_goals else "A"
    cid = reg.save([LegInput(row["id"], "OL-DEL", "ML", won, 0.0, 1.9)], probability=0.5)
    reg.settle_pending()
    assert reg.get(cid).status == "won"


def test_sport_backtest_runs_on_demo(synced, db):
    from typerbot.model.backtest_sports import run_sport_backtest

    res = run_sport_backtest(db, "american_football", [2024, 2025])
    assert res is not None and res.matches > 200
    model, naive, market = res.model["ML"].summary(), res.naive.summary(), res.market["ML"].summary()
    assert model["log_loss"] < naive["log_loss"]           # model lepszy od prognozy naiwnej
    assert market["n"] > 200 and res.model["OU"].n > 200


def test_mlb_window_and_history(synced, db):
    seasons = {r["season"] for r in db.query("SELECT DISTINCT season FROM matches WHERE league_code = 'MLB'")}
    assert seasons == {2024, 2025, 2026}          # bieżący sezon i dwa poprzednie
    upcoming = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'MLB' AND status = 'SCHEDULED'")[0]
    assert upcoming > 0
