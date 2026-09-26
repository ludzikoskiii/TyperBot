"""Testy integracyjne synchronizacji na syntetycznym świecie (bez sieci)."""

from datetime import timedelta

import pytest

from tests.conftest import NOW
from typerbot.config.secrets import MemorySecretStore
from typerbot.demo.transport import DemoTransport
from typerbot.demo.world import DemoWorld


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


def test_full_sync_collects_all_sources(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    report = service.run_all()
    assert not report.errors, [(s.source, s.step, s.message) for s in report.errors]
    counts = service.matches.counts()
    pl_current = [m for m in world.league_matches("PL") if m.season == world.current_season]
    assert counts["aliases_to_review"] == 0
    assert counts["teams"] == len(world.teams["PL"]) + len(world.teams["EKS"])  # zero duplikatów drużyn
    # historia 2022–2024 z API-Football + bieżący sezon PL z football-data.org
    hist = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season BETWEEN 2022 AND 2024")[0]
    assert hist == 3 * 380
    cur = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season = ?",
                       (world.current_season,))[0]
    assert cur == len(pl_current)
    # wyniki zgodne z „prawdą” świata
    for m in [x for x in pl_current if world.is_finished(x)][:10]:
        row = db.query_one(
            "SELECT m.home_goals, m.away_goals FROM matches m JOIN match_sources s ON s.match_id = m.id "
            "WHERE s.source = 'football_data_org' AND s.external_id = ?", (str(m.fdorg_id),))
        assert (row["home_goals"], row["away_goals"]) == (m.home_goals, m.away_goals)
    assert counts["with_xg"] > 0 and counts["odds"] > 0


def test_superbet_and_market_odds_attached_to_same_match(db, secrets, world, clock):
    from typerbot.data.repository import odds_view

    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    upcoming = service.matches.matches_between(NOW, NOW + timedelta(days=7), statuses=["SCHEDULED"])
    with_both = 0
    for r in upcoming:
        view = odds_view(service.matches.odds_for_match(r["id"]), "superbet")
        if view.bookmaker and view.average:
            with_both += 1
            assert view.books[("1X2", "H", 0.0)] >= 5  # średnia z wielu bukmacherów
    assert with_both >= 5


def test_ekstraklasa_results_from_oddspapi_and_odds_api(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    finished = [m for m in world.league_matches("EKS") if m.season == world.current_season and world.is_finished(m)]
    rows = db.query("SELECT home_goals FROM matches WHERE league_code = 'EKS' AND season = ? AND status = 'FINISHED'",
                    (world.current_season,))
    with_score = sum(r["home_goals"] is not None for r in rows)
    assert len(rows) == len(finished)
    budget = service.settings().sync.oddspapi_scores_monthly
    assert with_score >= min(len(finished), budget)


def test_source_outage_does_not_stop_other_sources(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock, fail={"the_odds_api"})
    report = service.run_all()
    st = states(report)
    assert st[("the_odds_api", "odds", "PL")] == "offline"
    assert st[("football_data_org", "fixtures", "PL")] == "ok"
    assert st[("oddspapi", "odds", None)] == "ok"
    assert service.status.get("the_odds_api").state == "offline"
    assert service.status.get("football_data_org").state == "ok"


def test_missing_key_marks_source_and_continues(db, world, clock):
    secrets = MemorySecretStore({"football_data_org": "k", "oddspapi": "k"})
    service, transport = make_service(db, secrets, world, clock)
    report = service.run_all()
    st = states(report)
    assert st[("api_football", "history", "PL")] == "no_key"
    assert st[("football_data_org", "fixtures", "PL")] == "ok"
    assert not any(c.startswith("api_football") or c.startswith("the_odds_api") for c in transport.calls)
    rows = {r.source: r.state for r in service.quota_rows()}
    assert rows["api_football"] == "no_key" and rows["football_data_org"] == "ok"


def test_api_football_plan_range_is_detected(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.set_meta("api_football_seasons", [2021, 2025])  # błędne założenie o zakresie planu
    transport = service.http.transport
    report = service.run_all(odds=False, xg=False)
    assert service.apif_seasons() == [2022, 2023, 2024]   # zakres odczytany z komunikatu API
    assert not report.errors                              # sezon spoza planu pominięty, nie błąd
    seasons = {s.message for s in report.steps if s.source == "api_football" and s.step == "history" and s.records}
    assert seasons == {"sezon 2022/23", "sezon 2023/24", "sezon 2024/25"}
    before = len(transport.calls)
    service.run_all(odds=False, xg=False)
    assert not [c for c in transport.calls[before:] if c.startswith("api_football")]


def test_second_sync_uses_cache_and_history_once(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    service.run_all()
    first = len(transport.calls)
    report = service.run_all()
    new_calls = transport.calls[first:]
    assert not any("/fixtures" in c and c.startswith("api_football") for c in new_calls)  # historia pobrana raz
    assert not [s for s in report.steps if s.step == "history"]
    assert len(new_calls) < 10


def test_oddspapi_monthly_budget_is_respected(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    settings = service.settings()
    settings.sync.oddspapi_scores_monthly = 5
    service.settings_store.save(settings)
    service.run_all()
    scores = [c for c in transport.calls if c == "oddspapi:/v4/scores"]
    assert len(scores) <= 5


def test_xg_budget_is_respected(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    settings = service.settings()
    settings.sync.xg_daily_budget = 12
    service.settings_store.save(settings)
    service.run_all()
    stats_calls = [c for c in transport.calls if c.endswith("/fixtures/statistics")]
    history_calls = service.quota.calls_today("api_football") - len(stats_calls)
    assert len(stats_calls) <= max(0, 12 - history_calls) + 2


def test_event_markets_for_candidates(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    ids = [r["id"] for r in service.matches.matches_between(NOW, NOW + timedelta(days=3), statuses=["SCHEDULED"])][:2]
    service.run(lambda rep: service.sync_event_markets(rep, ids))
    markets = {r["market"] for r in db.query(
        f"SELECT DISTINCT market FROM odds WHERE source = 'the_odds_api' AND match_id IN ({','.join('?' * len(ids))})",
        tuple(ids))}
    assert {"BTTS", "DC"} <= markets
