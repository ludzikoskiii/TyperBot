import threading

from typerbot.config.secrets import MemorySecretStore, mask
from typerbot.config.settings import Settings, SettingsStore
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
    assert s.tax.stake_tax == 0.12 and s.tax.win_tax_threshold == 2280.0
    assert s.odds.bookmaker == "superbet" and s.odds.reference == "bookmaker"
    assert s.sync.csv_import is False
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


def test_secret_store_and_mask():
    store = MemorySecretStore()
    assert not store.has("api_football")
    store.set("api_football", "  abcdef123456  ")
    assert store.get("api_football") == "abcdef123456"
    assert mask(store.get("api_football")).endswith("3456")
    assert "abcdef" not in mask(store.get("api_football"))
    store.delete("api_football")
    assert store.get("api_football") is None
