"""Schemat bazy SQLite i migracje.

Każda migracja to skrypt SQL; numer wersji trzymany jest w PRAGMA user_version.
Nowe migracje dopisujemy na końcu listy – nigdy nie zmieniamy istniejących.
"""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # --- v1: dane meczowe, źródła, cache i limity ---------------------------
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE leagues (
        code            TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        country         TEXT NOT NULL DEFAULT '',
        is_cup          INTEGER NOT NULL DEFAULT 0,
        fd_org_code     TEXT,
        api_football_id INTEGER,
        odds_api_key    TEXT,
        oddspapi_id     INTEGER,
        fdcuk_code      TEXT,
        fdcuk_format    TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        sort_order      INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE teams (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        country       TEXT NOT NULL DEFAULT '',
        name_priority INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE team_aliases (
        source       TEXT NOT NULL,
        league_code  TEXT NOT NULL,
        name         TEXT NOT NULL,
        team_id      INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        method       TEXT NOT NULL,
        score        REAL,
        needs_review INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (source, league_code, name)
    );
    CREATE INDEX idx_alias_team ON team_aliases(team_id);
    CREATE INDEX idx_alias_league ON team_aliases(league_code);

    CREATE TABLE matches (
        id            INTEGER PRIMARY KEY,
        league_code   TEXT NOT NULL,
        season        INTEGER NOT NULL,
        kickoff       TEXT NOT NULL,
        home_team_id  INTEGER NOT NULL REFERENCES teams(id),
        away_team_id  INTEGER NOT NULL REFERENCES teams(id),
        status        TEXT NOT NULL,
        home_goals    INTEGER,
        away_goals    INTEGER,
        home_xg       REAL,
        away_xg       REAL,
        home_shots    INTEGER,
        away_shots    INTEGER,
        home_sot      INTEGER,
        away_sot      INTEGER,
        stats_checked INTEGER NOT NULL DEFAULT 0,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX idx_matches_league_kickoff ON matches(league_code, kickoff);
    CREATE INDEX idx_matches_home ON matches(home_team_id, kickoff);
    CREATE INDEX idx_matches_away ON matches(away_team_id, kickoff);
    CREATE INDEX idx_matches_status ON matches(status, kickoff);

    CREATE TABLE match_sources (
        source      TEXT NOT NULL,
        external_id TEXT NOT NULL,
        match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        extra       TEXT,
        PRIMARY KEY (source, external_id)
    );
    CREATE INDEX idx_match_sources_match ON match_sources(match_id);

    CREATE TABLE odds (
        match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        source     TEXT NOT NULL,
        bookmaker  TEXT NOT NULL,
        market     TEXT NOT NULL,
        selection  TEXT NOT NULL,
        line       REAL NOT NULL DEFAULT 0,
        kind       TEXT NOT NULL DEFAULT 'pre',
        price      REAL NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (match_id, source, bookmaker, market, selection, line, kind)
    );

    CREATE TABLE http_cache (
        key        TEXT PRIMARY KEY,
        source     TEXT NOT NULL,
        url        TEXT NOT NULL,
        status     INTEGER NOT NULL,
        body       BLOB NOT NULL,
        fetched_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );

    CREATE TABLE api_calls (
        id       INTEGER PRIMARY KEY,
        source   TEXT NOT NULL,
        ts       REAL NOT NULL,
        endpoint TEXT NOT NULL,
        status   INTEGER,
        cost     INTEGER NOT NULL DEFAULT 1,
        ok       INTEGER NOT NULL
    );
    CREATE INDEX idx_api_calls_source_ts ON api_calls(source, ts);

    CREATE TABLE api_quota (
        source      TEXT NOT NULL,
        period      TEXT NOT NULL,
        quota_limit INTEGER,
        remaining   INTEGER,
        used        INTEGER,
        updated_at  REAL NOT NULL,
        PRIMARY KEY (source, period)
    );

    CREATE TABLE source_status (
        source     TEXT PRIMARY KEY,
        state      TEXT NOT NULL,
        message    TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    );

    CREATE TABLE history_files (
        source      TEXT NOT NULL,
        league_code TEXT NOT NULL,
        season      INTEGER NOT NULL,
        fetched_at  REAL NOT NULL,
        rows        INTEGER NOT NULL,
        complete    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (source, league_code, season)
    );
    """,
    # --- v2: prognozy modelu i wyniki backtestów ---------------------------------
    """
    CREATE TABLE predictions (
        match_id      INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL,
        lam_home      REAL NOT NULL,
        lam_away      REAL NOT NULL,
        rho           REAL NOT NULL,
        probs         TEXT NOT NULL,
        low_data_home INTEGER NOT NULL,
        low_data_away INTEGER NOT NULL,
        new_home      INTEGER NOT NULL DEFAULT 0,
        new_away      INTEGER NOT NULL DEFAULT 0,
        cross_league  INTEGER NOT NULL DEFAULT 0,
        home_matches  INTEGER,
        away_matches  INTEGER
    );

    CREATE TABLE backtest_runs (
        id         INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        config     TEXT NOT NULL,
        summary    TEXT NOT NULL
    );
    """,
    # --- v3: rejestr postawionych kuponów -----------------------------------------
    """
    CREATE TABLE coupons (
        id                 INTEGER PRIMARY KEY,
        created_at         TEXT NOT NULL,
        placed_at          TEXT NOT NULL,
        bookmaker          TEXT NOT NULL DEFAULT '',
        stake              REAL NOT NULL,
        odds               REAL NOT NULL,
        bookmaker_pays_tax INTEGER NOT NULL DEFAULT 0,
        status             TEXT NOT NULL DEFAULT 'pending',
        payout             REAL,
        settled_at         TEXT,
        manual             INTEGER NOT NULL DEFAULT 0,
        probability        REAL,
        probability_model  REAL,
        probability_market REAL,
        ev                 REAL,
        note               TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_coupons_status ON coupons(status);
    CREATE INDEX idx_coupons_placed ON coupons(placed_at);

    CREATE TABLE coupon_legs (
        id          INTEGER PRIMARY KEY,
        coupon_id   INTEGER NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
        match_id    INTEGER NOT NULL REFERENCES matches(id),
        league_code TEXT NOT NULL,
        market      TEXT NOT NULL,
        selection   TEXT NOT NULL,
        line        REAL NOT NULL DEFAULT 0,
        odds        REAL NOT NULL,
        probability REAL,
        p_model     REAL,
        p_market    REAL,
        result      TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE INDEX idx_coupon_legs_coupon ON coupon_legs(coupon_id);
    CREATE INDEX idx_coupon_legs_match ON coupon_legs(match_id);
    """,
]


# --- v4: aplikacja korzysta wyłącznie z darmowych źródeł – usunięte API-Football -------------------
MIGRATIONS.append(
    """
    DELETE FROM source_status WHERE source = 'api_football';
    DELETE FROM api_quota WHERE source = 'api_football';
    DELETE FROM http_cache WHERE source = 'api_football';
    DELETE FROM history_files WHERE source = 'api_football';
    DELETE FROM settings WHERE key IN ('meta.api_football_seasons', 'meta.last_sync');
    """
)

# --- v5: historia kuponów bez kwot – wynik w jednostkach, kupony wygenerowane i skopiowane ---------------
MIGRATIONS.append(
    """
    ALTER TABLE coupons ADD COLUMN legs_key TEXT NOT NULL DEFAULT '';
    ALTER TABLE coupons ADD COLUMN copied INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE coupons ADD COLUMN target_odds REAL;
    ALTER TABLE coupons ADD COLUMN returned REAL;
    ALTER TABLE coupon_legs ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0;
    UPDATE coupons SET copied = 1;
    UPDATE coupons SET returned = payout / stake WHERE payout IS NOT NULL AND stake > 0;
    CREATE INDEX idx_coupons_legs_key ON coupons(legs_key);
    DELETE FROM settings WHERE key LIKE 'meta.budget%';
    """
)

# --- v6: wyłącznie źródła bez klucza (football-data.co.uk, openfootball, OpenLigaDB, reprezentacje) -------
MIGRATIONS.append(
    """
    ALTER TABLE leagues ADD COLUMN openfootball TEXT;
    ALTER TABLE leagues ADD COLUMN openligadb TEXT;
    ALTER TABLE leagues ADD COLUMN season_style TEXT NOT NULL DEFAULT 'split';
    ALTER TABLE leagues ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/London';
    ALTER TABLE leagues ADD COLUMN tier INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE leagues ADD COLUMN national INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE source_status ADD COLUMN last_ok REAL;
    ALTER TABLE matches ADD COLUMN kickoff_rank INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE matches ADD COLUMN neutral INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE predictions ADD COLUMN method TEXT NOT NULL DEFAULT '';
    UPDATE source_status SET last_ok = updated_at WHERE state = 'ok';

    CREATE TABLE club_names (
        country   TEXT NOT NULL,
        variant   TEXT NOT NULL,
        canonical TEXT NOT NULL,
        PRIMARY KEY (country, variant)
    );

    DELETE FROM source_status WHERE source IN ('football_data_org', 'the_odds_api', 'oddspapi');
    DELETE FROM api_quota WHERE source IN ('football_data_org', 'the_odds_api', 'oddspapi');
    DELETE FROM http_cache WHERE source IN ('football_data_org', 'the_odds_api', 'oddspapi');
    DELETE FROM history_files WHERE source IN ('football_data_org', 'the_odds_api', 'oddspapi');
    DELETE FROM api_calls WHERE source IN ('football_data_org', 'the_odds_api', 'oddspapi');
    DELETE FROM settings WHERE key = 'meta.last_sync' OR key LIKE 'meta.daily.%' OR key LIKE 'meta.oddspapi%'
        OR key LIKE 'meta.papi_fixtures%';
    """
)

# --- v7: porządki po usuniętych źródłach – terminarz, którego już nikt nie zaktualizuje -------------------
# Mecze nierozegrane znane tylko z usuniętych źródeł (np. terminarz Ligi Mistrzów z football-data.org) nie dostaną
# wyniku ani kursów; kursy z usuniętych źródeł dla nierozegranych meczów są nieaktualne. Mecze z kuponów zostają.
_REMOVED = "('football_data_org', 'the_odds_api', 'oddspapi', 'api_football')"
MIGRATIONS.append(
    f"""
    DELETE FROM odds WHERE source IN {_REMOVED}
        AND match_id IN (SELECT id FROM matches WHERE status NOT IN ('FINISHED', 'CANCELLED', 'AWARDED'));
    DELETE FROM matches WHERE status NOT IN ('FINISHED', 'CANCELLED', 'AWARDED')
        AND id NOT IN (SELECT match_id FROM match_sources WHERE source NOT IN {_REMOVED})
        AND id NOT IN (SELECT match_id FROM coupon_legs);
    """
)

SCHEMA_VERSION = len(MIGRATIONS)
