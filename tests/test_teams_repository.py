from datetime import datetime, timedelta, timezone

from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord, OddsQuote
from typerbot.data.repository import LeagueRepository, MatchRepository, merge_status, odds_view
from typerbot.data.teams import TeamMatcher, normalize, seed_group

KICK = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)


def rec(source, ext, home, away, league="EKS", kickoff=KICK, status=SCHEDULED, hg=None, ag=None, odds=None, **kw):
    return MatchRecord(source=source, external_id=ext, league_code=league, season=2026, kickoff=kickoff,
                       home=home, away=away, status=status, home_goals=hg, away_goals=ag, odds=odds or [], **kw)


def repo(db):
    LeagueRepository(db).ensure_defaults()
    return MatchRepository(db)


def resolve(db, matcher, source, league, name, **kw):
    with db.transaction() as conn:
        return matcher.resolve(conn, source, league, name, **kw)


# -- normalizacja ----------------------------------------------------------------------
def test_normalize_strips_accents_suffixes_and_numbers():
    assert normalize("Raków Częstochowa") == "rakow czestochowa"
    assert normalize("Widzew Łódź") == "widzew lodz"
    assert normalize("1. FC Köln") == "koln"
    assert normalize("Brighton & Hove Albion FC") == "brighton and hove albion"
    assert normalize("Nott'm Forest") == "nottm forest"
    assert normalize("FK Bodø/Glimt") == "bodo glimt"


def test_seed_groups_link_known_variants():
    assert seed_group("Man United") == seed_group("Manchester United FC")
    assert seed_group("Ath Madrid") == seed_group("Club Atlético de Madrid")
    assert seed_group("Lech") != seed_group("Lechia")
    assert seed_group("Paris FC") != seed_group("Paris SG")


# -- dopasowanie ---------------------------------------------------------------------------
def test_same_team_from_different_sources(db):
    LeagueRepository(db).ensure_defaults()
    m = TeamMatcher(db)
    a = resolve(db, m, "api_football", "EKS", "Rakow Czestochowa")
    b = resolve(db, m, "the_odds_api", "EKS", "Raków Częstochowa")
    c = resolve(db, m, "football_data_csv", "EKS", "Rakow")
    assert a.created and not b.created and not c.created
    assert a.team_id == b.team_id == c.team_id
    assert (b.method, c.method) == ("normalized", "seed")


def test_lech_and_lechia_stay_separate(db):
    m = TeamMatcher(db)
    lech = resolve(db, m, "api_football", "EKS", "Lech Poznan")
    lechia = resolve(db, m, "api_football", "EKS", "Lechia Gdansk")
    assert lech.team_id != lechia.team_id
    assert resolve(db, m, "the_odds_api", "EKS", "Lechia Gdańsk").team_id == lechia.team_id
    assert resolve(db, m, "the_odds_api", "EKS", "Lech Poznań").team_id == lech.team_id


def test_paris_fc_not_merged_with_psg(db):
    m = TeamMatcher(db)
    psg = resolve(db, m, "football_data_csv", "FL1", "Paris SG")
    pfc = resolve(db, m, "football_data_csv", "FL1", "Paris FC")
    assert psg.team_id != pfc.team_id
    assert resolve(db, m, "the_odds_api", "FL1", "Paris Saint Germain").team_id == psg.team_id
    assert resolve(db, m, "the_odds_api", "FL1", "Paris FC").team_id == pfc.team_id


def test_team_with_alias_from_same_source_is_not_candidate(db):
    m = TeamMatcher(db)
    a = resolve(db, m, "the_odds_api", "PL", "Sheffield United")
    b = resolve(db, m, "the_odds_api", "PL", "Sheffield United Reserves")  # inna nazwa z tego samego źródła
    assert a.team_id != b.team_id


def test_fuzzy_match_flags_review(db):
    m = TeamMatcher(db)
    a = resolve(db, m, "api_football", "PL", "Crystal Palace")
    b = resolve(db, m, "the_odds_api", "PL", "Crystal Palace London")
    assert b.team_id == a.team_id and b.method == "fuzzy"
    assert b.needs_review == (b.score < 95)


def test_cup_matches_domestic_team_by_exact_source_name(db):
    m = TeamMatcher(db)
    dom = resolve(db, m, "football_data_org", "PL", "Arsenal FC")
    cup = resolve(db, m, "football_data_org", "CL", "Arsenal FC", is_cup=True)
    assert cup.team_id == dom.team_id and cup.method == "exact"


def test_display_name_uses_highest_priority_source(db):
    m = TeamMatcher(db)
    t = resolve(db, m, "football_data_csv", "PL", "Man United")
    resolve(db, m, "football_data_org", "PL", "Manchester United FC")
    resolve(db, m, "api_football", "PL", "Manchester United")
    assert db.query_one("SELECT name FROM teams WHERE id = ?", (t.team_id,))["name"] == "Manchester United"


def test_manual_alias_correction(db):
    m = TeamMatcher(db)
    a = resolve(db, m, "api_football", "EKS", "Legia Warszawa")
    wrong = resolve(db, m, "oddspapi", "EKS", "KP Legia")
    assert wrong.team_id != a.team_id
    m.set_alias("oddspapi", "EKS", "KP Legia", a.team_id)
    assert resolve(db, m, "oddspapi", "EKS", "KP Legia").team_id == a.team_id


# -- repozytorium meczów -------------------------------------------------------------------
def test_same_match_from_two_sources_is_merged(db):
    r = repo(db)
    first = r.save_records([rec("api_football", "1", "Legia Warszawa", "Lech Poznan")])[0]
    second = r.save_records([rec("the_odds_api", "abc", "Legia Warsaw", "Lech Poznań",
                                 kickoff=KICK + timedelta(minutes=30))])[0]
    assert first.created and not second.created and first.match_id == second.match_id
    assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 1
    assert r.source_id(first.match_id, "the_odds_api")[0] == "abc"


def test_finished_status_is_not_downgraded(db):
    r = repo(db)
    mid = r.save_records([rec("football_data_org", "9", "Arsenal FC", "Chelsea FC", league="PL",
                              status=FINISHED, hg=2, ag=1)])[0].match_id
    r.save_records([rec("the_odds_api", "x", "Arsenal", "Chelsea", league="PL")])
    row = db.query_one("SELECT status, home_goals, away_goals FROM matches WHERE id = ?", (mid,))
    assert (row["status"], row["home_goals"], row["away_goals"]) == (FINISHED, 2, 1)


def test_merge_status_rules():
    assert merge_status("FINISHED", "SCHEDULED") == "FINISHED"
    assert merge_status("POSTPONED", "SCHEDULED") == "SCHEDULED"
    assert merge_status("SCHEDULED", "FINISHED") == "FINISHED"
    assert merge_status("LIVE", "SCHEDULED") == "LIVE"


def test_odds_upsert_and_view(db):
    r = repo(db)
    quotes = [OddsQuote("unibet_eu", "1X2", "H", 2.0), OddsQuote("pinnacle", "1X2", "H", 2.2),
              OddsQuote("unibet_eu", "OU", "O", 1.9, line=2.5)]
    mid = r.save_records([rec("the_odds_api", "e1", "Legia Warsaw", "Lech Poznań", odds=quotes)])[0].match_id
    r.save_records([rec("the_odds_api", "e1", "Legia Warsaw", "Lech Poznań",
                        odds=[OddsQuote("unibet_eu", "1X2", "H", 2.1)])])  # aktualizacja kursu
    r.save_records([rec("oddspapi", "f1", "Legia Warszawa", "Lech Poznan",
                        odds=[OddsQuote("superbet.pl", "1X2", "H", 1.95)])])
    view = odds_view(r.odds_for_match(mid), "superbet")
    assert view.bookmaker[("1X2", "H", 0.0)] == 1.95
    assert view.average[("1X2", "H", 0.0)] == round((2.1 + 2.2 + 1.95) / 3, 3)
    assert view.best[("1X2", "H", 0.0)] == 2.2
    ref = view.reference("bookmaker")
    assert ref[("1X2", "H", 0.0)] == 1.95          # Superbet, gdy jest
    assert ref[("OU", "O", 2.5)] == 1.9            # średnia, gdy Superbet nie ma kursu


def test_merge_teams_dedupes_matches(db):
    r = repo(db)
    a = r.save_records([rec("api_football", "1", "Legia Warszawa", "Lech Poznan")])[0].match_id
    b = r.save_records([rec("oddspapi", "2", "KP Legia", "Lech Poznan")])[0].match_id
    assert a != b  # nieznany wariant nazwy -> osobna drużyna i zdublowany mecz
    legia = db.query_one("SELECT home_team_id FROM matches WHERE id = ?", (a,))["home_team_id"]
    wrong = db.query_one("SELECT home_team_id FROM matches WHERE id = ?", (b,))["home_team_id"]
    assert r.merge_teams(legia, wrong) == 1
    assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 1
    assert r.source_id(a, "oddspapi")[0] == "2"
