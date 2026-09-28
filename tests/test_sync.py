"""Testy integracyjne synchronizacji na syntetycznym świecie (bez sieci) – wyłącznie źródła bez klucza."""

from datetime import timedelta

import pytest

from tests.conftest import NOW
from typerbot.config.secrets import MemorySecretStore
from typerbot.config.settings import SettingsStore
from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord
from typerbot.demo.transport import DemoTransport
from typerbot.demo.world import LEAGUES, DemoWorld

ALL_SOURCES = {"football_data_csv", "openfootball", "openligadb", "international", "club_names", "nflverse", "mlb"}


@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


def make_service(db, secrets, world, clock, transport=None, now=NOW, **transport_kw):
    from typerbot.services.sync import SyncService

    transport = transport or DemoTransport(world, **transport_kw)
    service = SyncService(db, secrets, transport=transport, clock=clock, now=lambda: now, rate_limits=False)
    world.enable_leagues(service.leagues)
    return service, transport


def states(report):
    return {(s.source, s.step, s.league): s.state for s in report.steps}


def test_full_sync_collects_all_sources(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    report = service.run_all()
    assert not report.errors, [(s.source, s.step, s.message) for s in report.errors]
    assert set(service.sources) == ALL_SOURCES
    assert {s.source for s in report.steps} == ALL_SOURCES
    # zero duplikatów drużyn, choć źródła podają różne nazwy („Man United” / „Manchester United FC”)
    for spec in LEAGUES:
        n = db.query_one("SELECT COUNT(DISTINCT team_id) FROM team_aliases WHERE league_code = ?", (spec.code,))[0]
        assert n == len(world.teams[spec.code]), spec.code
    # bieżący sezon PL: pełny terminarz z openfootball połączony z kursami football-data.co.uk (bez duplikatów)
    season = world.current_season("PL")
    cur = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season = ?", (season,))[0]
    assert cur == len([m for m in world.league_matches("PL") if m.season == season])
    past = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'PL' AND season < ?", (season,))[0]
    assert past == 6 * 380
    # 3. Liga: OpenLigaDB + openfootball = jeden mecz na parę
    bl3 = [m for m in world.league_matches("BL3") if m.season == world.current_season("BL3")]
    assert db.query_one("SELECT COUNT(*) FROM matches WHERE league_code = 'BL3' AND season = ?",
                        (world.current_season("BL3"),))[0] == len(bl3)
    for m in [x for x in bl3 if world.is_finished(x)][:5]:
        row = db.query_one("SELECT m.home_goals, m.away_goals FROM matches m JOIN match_sources s ON "
                           "s.match_id = m.id WHERE s.source = 'openligadb' AND s.external_id = ?",
                           (str(900000 + m.seq),))
        assert (row["home_goals"], row["away_goals"]) == (m.home_goals, m.away_goals)
    # reprezentacje – historia do rankingu Elo, z terenem neutralnym
    intl = db.query_one("SELECT COUNT(*) AS n, SUM(neutral) AS neutral FROM matches WHERE league_code = 'INT'")
    assert intl["n"] > 100 and 0 < intl["neutral"] < intl["n"]
    assert service.matches.counts()["odds"] > 0
    assert all(row.last_ok for row in service.source_rows())


def test_works_offline_on_last_data(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    before = service.matches.counts()["matches"]
    good = service.status.get("football_data_csv").last_ok
    clock.advance(4 * 3600)
    service.http.transport = DemoTransport(world, fail=set(ALL_SOURCES))
    report = service.run_all(force=True)
    assert service.matches.counts()["matches"] == before              # dane w bazie zostają
    st = service.status.get("football_data_csv")
    assert st.state == "offline" and st.last_ok == good               # data ostatnich udanych danych
    assert any(s.stale for s in report.steps)                         # odpowiedzi z cache


def test_source_outage_does_not_stop_other_sources(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock, fail={"openfootball"})
    report = service.run_all()
    st = states(report)
    assert st[("football_data_csv", "fixtures", None)] == "ok"
    assert any(v == "offline" for (src, _, _), v in st.items() if src == "openfootball")
    assert all(v == "ok" for (src, _, _), v in st.items() if src == "openligadb")
    upcoming = db.query_one("SELECT COUNT(*) FROM matches WHERE league_code IN ('PL', 'E2') AND status = ?",
                            (SCHEDULED,))[0]
    assert upcoming > 0          # terminarz z kursami z football-data.co.uk jest mimo awarii openfootball


def test_second_sync_uses_cache_and_history_once(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    service.run_all()
    first = len(transport.calls)
    service.run_all()
    assert len(transport.calls) == first          # historia pobrana, terminarz w cache (ważny kilka godzin)
    clock.advance(4 * 3600)
    service.run_all()
    new = transport.calls[first:]
    # terminarz: pliki fixtures, OpenLigaDB, plik NFL (terminarz i wyniki w jednym), MLB – najbliższe dni
    # (historia sezonu MLB – od lutego – już pobrana)
    assert new and all("fixtures" in c or "/getmatchdata/" in c or c.endswith("/games.csv")
                       or (c.startswith("mlb:") and "-02-15" not in c) for c in new), new


def test_disabled_optional_source_is_not_called(db, secrets, world, clock):
    service, transport = make_service(db, secrets, world, clock)
    store = SettingsStore(db)
    s = store.load()
    s.sync.openligadb = False
    store.save(s)
    service.run_all()
    assert not [c for c in transport.calls if c.startswith("openligadb")]
    assert next(r for r in service.source_rows() if r.source == "openligadb").state == "disabled"


class ExtraLeagueTransport(DemoTransport):
    """Plik z terminarzem zawiera ligi spoza katalogu."""

    def _csv_fixtures_main(self) -> str:
        day = (NOW + timedelta(days=2)).strftime("%d/%m/%Y")
        return super()._csv_fixtures_main() + f"X9,{day},15:00,Alpha,Beta,," + ",".join(["2.1"] * 20) + "\n"

    def _csv_fixtures_extra(self) -> str:
        day = (NOW + timedelta(days=2)).strftime("%d/%m/%Y")
        return super()._csv_fixtures_extra() + f"Iceland,Besta deild,{day},19:00,Valur,KR," + ",".join(
            ["2.2", "3.4", "3.1"] * 4) + "\n"


def test_new_leagues_in_fixture_files_are_added_automatically(db, secrets, world, clock):
    transport = ExtraLeagueTransport(world)
    service, _ = make_service(db, secrets, world, clock, transport=transport)
    service.run_all()
    x9, iceland = service.leagues.get("X9"), service.leagues.get("XICEL")
    assert x9 is not None and x9.enabled and x9.fdcuk_code == "X9"
    assert iceland is not None and iceland.name == "Besta deild" and iceland.country == "Iceland"
    assert db.query_one("SELECT COUNT(*) FROM matches WHERE league_code IN ('X9', 'XICEL')")[0] == 2
    assert db.query_one("SELECT COUNT(*) FROM odds o JOIN matches m ON m.id = o.match_id "
                        "WHERE m.league_code = 'XICEL'")[0] > 0


def test_club_names_join_different_spellings(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    assert db.query_one("SELECT COUNT(*) FROM club_names WHERE country = 'Brazylia'")[0] > 0
    ids = {r["team_id"] for r in db.query(
        "SELECT team_id FROM team_aliases WHERE league_code = 'BRA' AND name IN ('Atletico-MG', 'CA Mineiro')")}
    assert len(ids) == 1                    # skrót z football-data.co.uk = pełna nazwa z openfootball


def test_removed_source_keys_are_deleted_once(db, world, clock):
    secrets = MemorySecretStore({"the_odds_api": "x", "oddspapi": "y", "football_data_org": "z", "api_football": "w"})
    service, _ = make_service(db, secrets, world, clock)
    assert all(secrets.get(s) is None for s in ("the_odds_api", "oddspapi", "football_data_org", "api_football"))
    assert service.meta("removed_keys_cleared") is True


def _rec(source, ext, kickoff, status=SCHEDULED, exact=True, **kw):
    return MatchRecord(source=source, external_id=ext, league_code="PL", season=2026, kickoff=kickoff,
                       home="Arsenal", away="Chelsea", status=status, kickoff_exact=exact, **kw)


def test_kickoff_from_more_reliable_source_wins(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    repo = service.matches
    k = NOW + timedelta(days=3)
    mid = repo.save_records([_rec("openfootball", "of1", k)])[0].match_id
    repo.save_records([_rec("football_data_csv", "csv1", k + timedelta(hours=2))])
    repo.save_records([_rec("openfootball", "of1", k)])            # terminarz openfootball nie nadpisuje
    row = db.query_one("SELECT kickoff FROM matches WHERE id = ?", (mid,))
    assert row["kickoff"] == (k + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.save_records([_rec("openfootball", "of1", k, status=FINISHED, home_goals=2, away_goals=1)])
    assert db.query_one("SELECT status, home_goals FROM matches WHERE id = ?", (mid,))["home_goals"] == 2


def test_fixture_removed_from_openfootball_is_pruned(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    k = NOW + timedelta(days=5)
    keep = service.matches.save_records([_rec("openfootball", "PL:2026:Matchday 9:Arsenal:Chelsea", k)])[0].match_id
    gone = service.matches.save_records([MatchRecord(
        source="openfootball", external_id="PL:2026:Matchday 9:Everton:Fulham", league_code="PL", season=2026,
        kickoff=k, home="Everton", away="Fulham")])[0].match_id
    service._prune_stale("openfootball", "PL", 2026, {"PL:2026:Matchday 9:Arsenal:Chelsea"})
    assert db.query_one("SELECT COUNT(*) FROM matches WHERE id = ?", (keep,))[0] == 1
    assert db.query_one("SELECT COUNT(*) FROM matches WHERE id = ?", (gone,))[0] == 0
