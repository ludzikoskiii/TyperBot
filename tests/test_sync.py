"""Testy integracyjne synchronizacji na syntetycznym świecie (bez sieci) – wyłącznie darmowe źródła."""

from datetime import timedelta

import pytest

from tests.conftest import NOW
from typerbot.config.secrets import MemorySecretStore
from typerbot.data.records import to_iso
from typerbot.demo.transport import DemoTransport
from typerbot.demo.world import DemoWorld
from typerbot.services.sync import Budget


@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


def make_service(db, secrets, world, clock, **transport_kw):
    from typerbot.services.sync import SyncService

    transport = DemoTransport(world, **transport_kw)
    service = SyncService(db, secrets, transport=transport, clock=clock, now=lambda: NOW, rate_limits=False)
    for league in service.leagues.all():
        service.leagues.set_enabled(league.code, league.code in ("PL", "EKS"))
    return service, transport


def states(report):
    return {(s.source, s.step, s.league): s.state for s in report.steps}


def odds_calls(transport, source):
    return [c for c in transport.calls if c.startswith(source) and ("/odds" in c)]


def test_full_sync_collects_all_sources(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    report = service.run_all()
    assert not report.errors, [(s.source, s.step, s.message) for s in report.errors]
    counts = service.matches.counts()
    pl_current = [m for m in world.league_matches("PL") if m.season == world.current_season]
    assert counts["aliases_to_review"] == 0
    assert counts["teams"] == len(world.teams["PL"]) + len(world.teams["EKS"])  # zero duplikatów drużyn
    # historia z football-data.co.uk (wszystkie zakończone sezony świata) + bieżący sezon z football-data.org
    hist = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season < ?",
                        (world.current_season,))[0]
    assert hist == (len(world.seasons) - 1) * 380
    cur = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season = ?",
                       (world.current_season,))[0]
    assert cur == len(pl_current)
    for m in [x for x in pl_current if world.is_finished(x)][:10]:
        row = db.query_one(
            "SELECT m.home_goals, m.away_goals FROM matches m JOIN match_sources s ON s.match_id = m.id "
            "WHERE s.source = 'football_data_org' AND s.external_id = ?", (str(m.fdorg_id),))
        assert (row["home_goals"], row["away_goals"]) == (m.home_goals, m.away_goals)
    assert counts["odds"] > 0 and counts["with_xg"] == 0
    assert set(service.sources) == {"football_data_csv", "football_data_org", "the_odds_api", "oddspapi"}


def test_works_without_any_key_from_football_data_co_uk(db, world, clock):
    """Bez żadnego klucza: historia, nadchodzące mecze i kursy 1X2 (+ powyżej/poniżej w lidze 'main')."""
    from typerbot.data.repository import odds_view

    service, transport = make_service(db, MemorySecretStore({}), world, clock)
    service.run_all()
    assert all(c.startswith("football_data_csv") for c in transport.calls)
    upcoming = service.matches.matches_between(NOW, NOW + timedelta(days=5), statuses=["SCHEDULED"])
    by_league = {"PL": 0, "EKS": 0}
    for r in upcoming:
        view = odds_view(service.matches.odds_for_match(r["id"]))
        assert ("1X2", "H", 0.0) in view.average
        if r["league_code"] == "PL":
            assert ("OU", "O", 2.5) in view.average
        by_league[r["league_code"]] += 1
    assert by_league["PL"] > 0 and by_league["EKS"] > 0


def test_supplements_only_missing_markets_once_per_day(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    service.run_all()
    papi = [c for c in transport.calls if c == "oddspapi:/v4/odds-by-tournaments"]
    assert len(papi) == 1                                   # obie ligi w jednym zapytaniu
    assert odds_calls(transport, "the_odds_api") == []      # OddsPapi uzupełnił 1X2/OU – The Odds API niepotrzebne
    gaps = service.odds_gaps(NOW, NOW + timedelta(days=3))
    assert not any(m in markets for markets in gaps.values() for m in ("1X2", "OU"))
    before = len(transport.calls)
    service.run_all(force=True)                             # „Odśwież dane” tego samego dnia
    assert "oddspapi:/v4/odds-by-tournaments" not in transport.calls[before:]


def test_same_league_asked_at_most_once_per_day(db, world, clock):
    """Mecz, którego źródło nie zna, zostawia lukę – mimo to nie pytamy drugi raz tego samego dnia."""
    from typerbot.data.records import MatchRecord

    secrets = MemorySecretStore({"football_data_org": "k", "the_odds_api": "k"})
    service, transport = make_service(db, secrets, world, clock)
    service.matches.save_records([MatchRecord("football_data_org", "x-1", "EKS", world.current_season,
                                              NOW + timedelta(days=1), "Legia Warszawa", "Lech Poznań")])
    service.run_all()
    assert len(odds_calls(transport, "the_odds_api")) == 1
    assert "EKS" in service.odds_gaps(NOW, NOW + timedelta(days=3))       # luka została
    report = service.run_all(force=True)
    assert len(odds_calls(transport, "the_odds_api")) == 1
    assert any(s.source == "the_odds_api" and s.state == "skipped" and "już dziś" in s.message
               for s in report.steps)
    clock.advance(24 * 3600)                                               # następnego dnia – można znowu
    service.run_all()
    assert len(odds_calls(transport, "the_odds_api")) == 2


def test_odds_api_fills_only_1x2_and_ou_gaps(db, world, clock):
    """Bez OddsPapi: The Odds API tylko dla Ekstraklasy (brak powyżej/poniżej w pliku 'extra'), 1 kredyt."""
    secrets = MemorySecretStore({"football_data_org": "k", "the_odds_api": "k"})
    service, transport = make_service(db, secrets, world, clock)
    report = service.run_all()
    st = states(report)
    assert st[("the_odds_api", "odds", "EKS")] == "ok"
    assert ("the_odds_api", "odds", "PL") not in st            # plik co.uk ma 1X2 i powyżej/poniżej
    costs = db.query("SELECT endpoint, cost FROM api_calls WHERE source = 'the_odds_api' AND endpoint LIKE '%/odds'")
    assert [r["cost"] for r in costs] == [1]                   # tylko rynek totals
    events = db.query("SELECT cost FROM api_calls WHERE source = 'the_odds_api' AND endpoint LIKE '%/events'")
    assert events and all(r["cost"] == 0 for r in events)      # lista meczów bezpłatna


def test_budget_math():
    b = Budget("the_odds_api", plan_limit=500, app_limit=400, used_month=100, used_today=0, days_left=30)
    assert b.month_left == 300 and b.daily_allowance == 10 and b.allows(10) and not b.allows(11)
    spent = Budget("the_odds_api", plan_limit=500, app_limit=400, used_month=110, used_today=10, days_left=30)
    assert spent.daily_allowance == 10 and spent.today_left == 0 and not spent.allows(1)
    last_day = Budget("the_odds_api", plan_limit=500, app_limit=400, used_month=395, used_today=0, days_left=1)
    assert last_day.today_left == 5
    over = Budget("oddspapi", plan_limit=250, app_limit=300, used_month=250, used_today=0, days_left=5)
    assert over.month_left == 0 and not over.allows(1)     # budżet aplikacji nigdy ponad limit planu


def test_budget_blocks_calls_and_never_exceeds_plan(db, world, clock):
    secrets = MemorySecretStore({"football_data_org": "k", "the_odds_api": "k"})
    service, transport = make_service(db, secrets, world, clock)      # demo: 42 kredyty już zużyte
    settings = service.settings()
    settings.sync.odds_api_monthly_budget = 42
    service.settings_store.save(settings)
    report = service.run_all()
    assert odds_calls(transport, "the_odds_api") == []
    skipped = [s for s in report.steps if s.source == "the_odds_api" and s.state == "skipped"]
    assert skipped and "budżet" in skipped[0].message


def test_source_outage_does_not_stop_other_sources(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock, fail={"oddspapi"})
    report = service.run_all()
    st = states(report)
    assert st[("oddspapi", "odds", None)] == "offline"
    assert st[("the_odds_api", "odds", "EKS")] == "ok"          # uzupełnia to, czego nie dał OddsPapi
    assert st[("football_data_org", "fixtures", "PL")] == "ok"
    assert service.status.get("oddspapi").state == "offline"
    assert service.status.get("football_data_csv").state == "ok"
    # brak połączenia nie „zużywa” dnia – przy kolejnym odświeżeniu spróbujemy ponownie
    transport.fail.clear()
    before = len(transport.calls)
    service.run_all()
    assert "oddspapi:/v4/odds-by-tournaments" in transport.calls[before:]


def test_missing_key_marks_source_and_continues(db, world, clock):
    secrets = MemorySecretStore({"football_data_org": "k"})
    service, transport = make_service(db, secrets, world, clock)
    report = service.run_all()
    st = states(report)
    assert st[("football_data_org", "fixtures", "PL")] == "ok"
    assert not any(c.startswith("the_odds_api") or c.startswith("oddspapi") for c in transport.calls)
    rows = {r.source: r.state for r in service.quota_rows()}
    assert rows["the_odds_api"] == "no_key" and rows["oddspapi"] == "no_key" and rows["football_data_org"] == "ok"


def test_removed_api_football_key_is_deleted(db, world, clock):
    secrets = MemorySecretStore({"api_football": "stary-klucz", "football_data_org": "k"})
    make_service(db, secrets, world, clock)
    assert secrets.get("api_football") is None and secrets.get("football_data_org") == "k"


def test_second_sync_uses_cache_and_history_once(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    service.run_all()
    first = len(transport.calls)
    report = service.run_all()
    new_calls = transport.calls[first:]
    assert not [s for s in report.steps if s.step == "history"]
    assert len(new_calls) < 5


def test_missing_results_only_for_pending_coupon_legs(db, secrets, world, clock):
    """Ekstraklasa: plik CSV z wynikami ma opóźnienie – wynik meczu z kuponu w grze bierzemy z The Odds API
    (2 kredyty), najwyżej raz dziennie; bez kuponów w grze nie wydajemy kredytów."""
    service, transport = make_service(db, secrets, world, clock)
    service.run_all()
    assert not [c for c in transport.calls if c.endswith("/scores")]
    done = [m for m in world.league_matches("EKS") if world.is_finished(m) and m.kickoff > NOW - timedelta(days=2)]
    assert done, "w świecie demo powinien być mecz zakończony w ostatnich dniach"
    m = done[-1]
    row = db.query_one("SELECT m.id, m.status FROM matches m JOIN match_sources s ON s.match_id = m.id "
                       "WHERE s.source = 'the_odds_api' AND s.external_id = ?", (m.odds_id,))
    if row is None:  # mecz spoza listy nadchodzących – zapisujemy go jak z terminarza
        from typerbot.data.records import MatchRecord

        rec = MatchRecord("the_odds_api", m.odds_id, "EKS", m.season, m.kickoff, m.home.odds, m.away.odds)
        service.matches.save_records([rec])
        row = db.query_one("SELECT m.id, m.status FROM matches m JOIN match_sources s ON s.match_id = m.id "
                           "WHERE s.source = 'the_odds_api' AND s.external_id = ?", (m.odds_id,))
    assert row["status"] == "SCHEDULED"
    with db.transaction() as conn:
        cid = conn.execute("INSERT INTO coupons(created_at, placed_at, stake, odds) VALUES (?, ?, 1, 2.0)",
                           (to_iso(NOW), to_iso(NOW))).lastrowid
        conn.execute("INSERT INTO coupon_legs(coupon_id, match_id, league_code, market, selection, odds) "
                     "VALUES (?, ?, 'EKS', '1X2', 'H', 2.0)", (cid, row["id"]))
    service.run_all()
    assert len([c for c in transport.calls if c.endswith("/scores")]) == 1
    after = db.query_one("SELECT status, home_goals, away_goals FROM matches WHERE id = ?", (row["id"],))
    assert (after["status"], after["home_goals"], after["away_goals"]) == ("FINISHED", m.home_goals, m.away_goals)
    service.run_all(force=True)
    assert len([c for c in transport.calls if c.endswith("/scores")]) == 1


def test_usage_estimates(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    est = {e.source: e for e in service.usage_estimates()}
    assert set(est) == {"the_odds_api", "oddspapi"}
    for e in est.values():
        assert e.used_month <= e.projected <= e.app_limit <= e.plan_limit
        assert e.daily_allowance > 0 and e.rule
    assert est["oddspapi"].used_month >= 1
