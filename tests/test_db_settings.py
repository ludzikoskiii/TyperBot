import json
import threading

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
    assert s.odds.bookmaker == "superbet" and s.odds.reference == "bookmaker"
    assert s.sync.csv_seasons == 10 and s.sync.odds_api_monthly_budget < 500 and s.sync.oddspapi_monthly_budget < 250
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


def test_secret_store_and_mask():
    store = MemorySecretStore()
    assert not store.has("the_odds_api")
    store.set("the_odds_api", "  abcdef123456  ")
    assert store.get("the_odds_api") == "abcdef123456"
    assert mask(store.get("the_odds_api")).endswith("3456")
    assert "abcdef" not in mask(store.get("the_odds_api"))
    store.delete("the_odds_api")
    assert store.get("the_odds_api") is None
