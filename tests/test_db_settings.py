import json
import threading
from datetime import datetime, timezone

from typerbot.config.secrets import MemorySecretStore, mask
from typerbot.config.settings import SETTINGS_VERSION, Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.schema import SCHEMA_VERSION


def test_migrations_create_schema(db):
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"matches", "teams", "team_aliases", "odds", "http_cache", "api_calls", "api_quota"} <= tables


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    Database(path).close()
    again = Database(path)
    assert again.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    again.close()


def test_each_thread_gets_own_connection(db):
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.conn()))
    t.start()
    t.join()
    assert seen[0] is not db.conn()


def test_settings_roundtrip_and_defaults(db):
    store = SettingsStore(db)
    s = store.load()
    assert s.tax.stake_tax == 0.12 and not s.tax.bookmaker_pays_tax and not hasattr(s, 'budget')
    assert s.odds.reference == "average" and s.odds.estimated_margin == 0.07 and not hasattr(s.odds, "bookmaker")
    assert s.sync.csv_seasons == 8 and s.sync.openfootball and s.sync.openligadb and s.sync.international
    assert s.coupon.estimated_odds == "fallback" and not hasattr(s.sync, "odds_api_monthly_budget")
    s.coupon.target_odds = 7.5
    s.model.last_matches = 12
    store.save(s)
    loaded = store.load()
    assert loaded.coupon.target_odds == 7.5 and loaded.model.last_matches == 12


def test_settings_ignore_unknown_and_bad_types():
    s = Settings.from_json('{"coupon": {"target_odds": "abc", "min_events": 3, "nieznane": 1}, "x": 5}')
    assert s.coupon.target_odds == 5.0  # zły typ -> wartość domyślna
    assert s.coupon.min_events == 3
    assert Settings.from_json("to nie jest json").coupon.target_odds == 5.0


def test_old_settings_get_calibrated_defaults_once(db):
    old = json.loads(Settings().to_json())
    del old["version"]
    old["model"].update(last_matches=20, half_life_days=180.0, regularization=10.0, model_weight=0.3)
    old["odds"]["margin_method"] = "proportional"
    old["coupon"].update(max_events=6, min_probability=0.6, target_odds=8.0)   # własne wartości zostają
    db.execute("INSERT INTO settings(key, value) VALUES('app', ?)", (json.dumps(old),))
    store = SettingsStore(db)
    s = store.load()
    assert (s.model.last_matches, s.model.half_life_days, s.model.regularization, s.model.model_weight) == (80, 365, 5, 0)
    assert s.odds.margin_method == "shin" and s.coupon.max_events == 4
    assert s.coupon.min_probability == 0.6 and s.coupon.target_odds == 8.0 and s.version == SETTINGS_VERSION
    s.model.last_matches = 20          # po migracji wybór użytkownika jest trwały
    store.save(s)
    assert store.load().model.last_matches == 20


def test_settings_v2_migrate_to_keyless_sources(db):
    old = json.loads(Settings().to_json())
    old["version"] = 2
    old["odds"].update(reference="bookmaker", bookmaker="superbet", region="eu")
    old["coupon"].pop("estimated_odds")
    old["coupon"]["allow_estimated_odds"] = True
    old["sync"].update(csv_seasons=10, odds_api_monthly_budget=400)
    db.execute("INSERT INTO settings(key, value) VALUES('app', ?)", (json.dumps(old),))
    s = SettingsStore(db).load()
    assert s.odds.reference == "average" and s.coupon.estimated_odds == "always" and s.sync.csv_seasons == 8


def test_settings_v3_get_more_coupons(db):
    old = json.loads(Settings().to_json())
    old["version"] = 3
    old["coupon"]["alternatives"] = 3
    db.execute("INSERT INTO settings(key, value) VALUES('app', ?)", (json.dumps(old),))
    assert SettingsStore(db).load().coupon.alternatives == 5


def test_migration_removes_fixtures_only_known_from_removed_sources(db):
    from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord, OddsQuote
    from typerbot.data.repository import LeagueRepository, MatchRepository
    from typerbot.data.schema import MIGRATIONS
    from typerbot.services.register import CouponRegister, LegInput

    LeagueRepository(db).ensure_defaults()
    repo = MatchRepository(db)
    k = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)

    def rec(source, ext, home, away, status=SCHEDULED, **kw):
        return MatchRecord(source=source, external_id=ext, league_code="CL", season=2026, kickoff=k, home=home,
                           away=away, status=status, **kw)

    stale = repo.save_records([rec("football_data_org", "1", "Arsenal", "Inter")])[0].match_id
    done = repo.save_records([rec("football_data_org", "2", "Lech", "Legia", FINISHED, home_goals=1,
                                  away_goals=0)])[0].match_id
    both = repo.save_records([rec("football_data_org", "3", "Bayern", "Porto")])[0].match_id
    repo.save_records([rec("openfootball", "x", "Bayern", "Porto",
                           odds=[OddsQuote("avg", "1X2", "H", 1.5)])])
    repo.save_records([rec("oddspapi", "p", "Bayern", "Porto", odds=[OddsQuote("superbet", "1X2", "H", 1.4)])])
    in_coupon = repo.save_records([rec("football_data_org", "4", "Ajax", "Celtic")])[0].match_id
    CouponRegister(db).save([LegInput(in_coupon, "CL", "1X2", "H", 0.0, 2.0, 0.5)], probability=0.5)
    db.conn().executescript(MIGRATIONS[6])
    ids = {r["id"] for r in db.query("SELECT id FROM matches")}
    assert stale not in ids and {done, both, in_coupon} <= ids   # historia, mecz z innym źródłem, mecz z kuponu
    assert {r["source"] for r in db.query("SELECT source FROM odds WHERE match_id = ?", (both,))} == {"openfootball"}


def test_secret_store_and_mask():
    store = MemorySecretStore()
    assert not store.has("the_odds_api")
    store.set("the_odds_api", "  abcdef123456  ")
    assert store.get("the_odds_api") == "abcdef123456"
    assert mask(store.get("the_odds_api")).endswith("3456")
    assert "abcdef" not in mask(store.get("the_odds_api"))
    store.delete("the_odds_api")
    assert store.get("the_odds_api") is None


def test_settings_v4_add_markets_of_other_sports(db):
    old = json.loads(Settings().to_json())
    old["version"] = 4
    old["coupon"]["markets"] = ["1X2", "OU"]
    old["markets_enabled"] = ["1X2", "DC", "OU", "BTTS"]
    old["coupon"].pop("sports", None)
    db.execute("INSERT INTO settings(key, value) VALUES('app', ?)", (json.dumps(old),))
    s = SettingsStore(db).load()
    assert s.coupon.markets == ["1X2", "ML", "HCP", "OU"]          # wybór użytkownika zostaje, nowe rynki dochodzą
    assert s.markets_enabled == ["1X2", "DC", "ML", "HCP", "OU", "BTTS"]
    assert s.coupon.sports == [] and s.sync.nflverse and s.sync.mlb and s.sync.openligadb_more


def test_leagues_have_sports(db):
    from typerbot.data.repository import LeagueRepository

    leagues = LeagueRepository(db)
    leagues.ensure_defaults()
    assert leagues.get("NFL").sport == "american_football" and leagues.get("NFL").feed == "nflverse"
    assert leagues.get("MLB").sport == "baseball" and leagues.get("PL").sport == "football"
