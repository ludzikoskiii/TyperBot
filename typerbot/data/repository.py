"""Zapis i odczyt meczów oraz kursów.

Ten sam mecz może przyjść z kilku źródeł (np. terminarz z football-data.org,
kursy z The Odds API, historia z football-data.co.uk). Łączymy je w jeden
wiersz `matches`, a identyfikatory ze źródeł trzymamy w `match_sources`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from typerbot.config.leagues import DEFAULT_LEAGUES, League
from typerbot.data.db import Database
from typerbot.data.records import (
    FINAL_STATUSES, FINISHED, LIVE, SCHEDULED,
    MatchRecord, MatchStats, OddsQuote, parse_iso, to_iso,
)
from typerbot.data.teams import TeamMatcher

MATCH_WINDOW = timedelta(hours=36)
# Źródła, których godzina rozpoczęcia jest przybliżona i nie nadpisuje innych.
APPROXIMATE_KICKOFF_SOURCES = frozenset({"football_data_csv"})


@dataclass
class UpsertResult:
    match_id: int
    created: bool


_LEAGUE_COLUMNS = ("code", "name", "country", "is_cup", "fd_org_code", "api_football_id", "odds_api_key",
                   "oddspapi_id", "fdcuk_code", "fdcuk_format", "enabled", "sort_order")


class LeagueRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _values(lg: League) -> tuple:
        return tuple(int(v) if isinstance(v, bool) else v for v in (getattr(lg, c) for c in _LEAGUE_COLUMNS))

    def ensure_defaults(self, leagues: Iterable[League] = DEFAULT_LEAGUES) -> None:
        cols = ", ".join(_LEAGUE_COLUMNS)
        marks = ", ".join("?" * len(_LEAGUE_COLUMNS))
        with self.db.transaction() as conn:
            conn.executemany(f"INSERT OR IGNORE INTO leagues({cols}) VALUES ({marks})",
                             [self._values(lg) for lg in leagues])

    def all(self, enabled_only: bool = False) -> list[League]:
        sql = "SELECT * FROM leagues" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY sort_order, code"
        return [self._to_league(r) for r in self.db.query(sql)]

    def get(self, code: str) -> League | None:
        row = self.db.query_one("SELECT * FROM leagues WHERE code = ?", (code,))
        return self._to_league(row) if row else None

    def save(self, league: League) -> None:
        cols = ", ".join(_LEAGUE_COLUMNS)
        marks = ", ".join("?" * len(_LEAGUE_COLUMNS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in _LEAGUE_COLUMNS[1:])
        with self.db.transaction() as conn:
            conn.execute(f"INSERT INTO leagues({cols}) VALUES ({marks}) ON CONFLICT(code) DO UPDATE SET {updates}",
                         self._values(league))

    def set_enabled(self, code: str, enabled: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE leagues SET enabled = ? WHERE code = ?", (int(enabled), code))

    def set_oddspapi_id(self, code: str, tournament_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE leagues SET oddspapi_id = ? WHERE code = ?", (tournament_id, code))

    @staticmethod
    def _to_league(r: sqlite3.Row) -> League:
        slug = next((lg.oddspapi_slug for lg in DEFAULT_LEAGUES if lg.code == r["code"]), "")
        return League(**{c: (bool(r[c]) if c in ("is_cup", "enabled") else r[c]) for c in _LEAGUE_COLUMNS},
                      oddspapi_slug=slug)


class MatchRepository:
    def __init__(self, db: Database, matcher: TeamMatcher | None = None):
        self.db = db
        self.matcher = matcher or TeamMatcher(db)
        self.leagues = LeagueRepository(db)
        # Zapisy z kilku wątków są serializowane, żeby nie tworzyć duplikatów drużyn.
        self.write_lock = threading.RLock()

    # -- zapis -------------------------------------------------------------------
    def save_records(self, records: Iterable[MatchRecord], *, fetched_at: datetime | None = None) -> list[UpsertResult]:
        fetched = to_iso(fetched_at or datetime.now(timezone.utc))
        results: list[UpsertResult] = []
        league_cache: dict[str, League | None] = {}
        with self.write_lock:
            try:
                with self.db.transaction() as conn:
                    for rec in records:
                        if rec.league_code not in league_cache:
                            league_cache[rec.league_code] = self.leagues.get(rec.league_code)
                        league = league_cache[rec.league_code]
                        res = self._upsert(conn, rec, league)
                        if rec.odds:
                            self._save_odds(conn, res.match_id, rec.source, rec.odds, fetched)
                        results.append(res)
            except Exception:
                self.matcher.clear_cache()  # cache mógł zapamiętać wycofane drużyny
                raise
        return results

    def save_stats(self, match_id: int, stats: MatchStats | None) -> None:
        with self.write_lock, self.db.transaction() as conn:
            if stats is None:
                conn.execute("UPDATE matches SET stats_checked = 1 WHERE id = ?", (match_id,))
                return
            conn.execute(
                "UPDATE matches SET home_xg = COALESCE(?, home_xg), away_xg = COALESCE(?, away_xg), "
                "home_shots = COALESCE(?, home_shots), away_shots = COALESCE(?, away_shots), "
                "home_sot = COALESCE(?, home_sot), away_sot = COALESCE(?, away_sot), stats_checked = 1, "
                "updated_at = ? WHERE id = ?",
                (stats.home_xg, stats.away_xg, stats.home_shots, stats.away_shots, stats.home_sot,
                 stats.away_sot, to_iso(datetime.now(timezone.utc)), match_id),
            )

    def set_score(self, match_id: int, home_goals: int, away_goals: int) -> None:
        with self.write_lock, self.db.transaction() as conn:
            conn.execute(
                "UPDATE matches SET status = ?, home_goals = ?, away_goals = ?, updated_at = ? WHERE id = ?",
                (FINISHED, home_goals, away_goals, to_iso(datetime.now(timezone.utc)), match_id),
            )

    def finished_without_score(self, league_code: str, since: datetime) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM matches WHERE league_code = ? AND status = ? AND home_goals IS NULL AND kickoff >= ? "
            "ORDER BY kickoff DESC", (league_code, FINISHED, to_iso(since)),
        )

    def link_source(self, match_id: int, source: str, external_id: str, extra: dict | None = None) -> None:
        with self.write_lock, self.db.transaction() as conn:
            self._link(conn, match_id, source, external_id, extra)

    def _upsert(self, conn: sqlite3.Connection, rec: MatchRecord, league: League | None) -> UpsertResult:
        is_cup = bool(league and league.is_cup)
        country = league.country if league and not is_cup else ""
        home = self.matcher.resolve(conn, rec.source, rec.league_code, rec.home, hints=rec.home_hints,
                                    is_cup=is_cup, country=country).team_id
        away = self.matcher.resolve(conn, rec.source, rec.league_code, rec.away, hints=rec.away_hints,
                                    is_cup=is_cup, country=country).team_id
        now = to_iso(datetime.now(timezone.utc))

        row = conn.execute(
            "SELECT m.* FROM match_sources s JOIN matches m ON m.id = s.match_id "
            "WHERE s.source = ? AND s.external_id = ?",
            (rec.source, rec.external_id),
        ).fetchone()
        if row is None:
            lo, hi = to_iso(rec.kickoff - MATCH_WINDOW), to_iso(rec.kickoff + MATCH_WINDOW)
            row = conn.execute(
                "SELECT * FROM matches WHERE league_code = ? AND home_team_id = ? AND away_team_id = ? "
                "AND kickoff BETWEEN ? AND ? ORDER BY ABS(julianday(kickoff) - julianday(?)) LIMIT 1",
                (rec.league_code, home, away, lo, hi, rec.kickoff_iso),
            ).fetchone()

        if row is None:
            cur = conn.execute(
                "INSERT INTO matches(league_code, season, kickoff, home_team_id, away_team_id, status, "
                "home_goals, away_goals, home_xg, away_xg, home_shots, away_shots, home_sot, away_sot, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.league_code, rec.season, rec.kickoff_iso, home, away, rec.status, rec.home_goals,
                 rec.away_goals, rec.home_xg, rec.away_xg, rec.home_shots, rec.away_shots, rec.home_sot,
                 rec.away_sot, now),
            )
            match_id = int(cur.lastrowid)
            self._link(conn, match_id, rec.source, rec.external_id, rec.extra)
            return UpsertResult(match_id, True)

        match_id = int(row["id"])
        status = merge_status(row["status"], rec.status)
        kickoff = row["kickoff"]
        if rec.source not in APPROXIMATE_KICKOFF_SOURCES and status not in FINAL_STATUSES | {LIVE}:
            kickoff = rec.kickoff_iso
        goals_known = rec.home_goals is not None and rec.away_goals is not None
        conn.execute(
            "UPDATE matches SET status = ?, kickoff = ?, "
            "home_goals = CASE WHEN ? THEN ? ELSE home_goals END, "
            "away_goals = CASE WHEN ? THEN ? ELSE away_goals END, "
            "home_xg = COALESCE(?, home_xg), away_xg = COALESCE(?, away_xg), "
            "home_shots = COALESCE(?, home_shots), away_shots = COALESCE(?, away_shots), "
            "home_sot = COALESCE(?, home_sot), away_sot = COALESCE(?, away_sot), updated_at = ? WHERE id = ?",
            (status, kickoff, goals_known, rec.home_goals, goals_known, rec.away_goals, rec.home_xg,
             rec.away_xg, rec.home_shots, rec.away_shots, rec.home_sot, rec.away_sot, now, match_id),
        )
        self._link(conn, match_id, rec.source, rec.external_id, rec.extra)
        return UpsertResult(match_id, False)

    @staticmethod
    def _link(conn: sqlite3.Connection, match_id: int, source: str, external_id: str, extra: dict | None) -> None:
        conn.execute(
            "INSERT INTO match_sources(source, external_id, match_id, extra) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, external_id) DO UPDATE SET match_id = excluded.match_id, "
            "extra = COALESCE(excluded.extra, extra)",
            (source, external_id, match_id, json.dumps(extra) if extra else None),
        )

    @staticmethod
    def _save_odds(conn: sqlite3.Connection, match_id: int, source: str, quotes: Iterable[OddsQuote],
                   fetched: str) -> None:
        conn.executemany(
            "INSERT INTO odds(match_id, source, bookmaker, market, selection, line, kind, price, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(match_id, source, bookmaker, market, selection, line, kind) "
            "DO UPDATE SET price = excluded.price, fetched_at = excluded.fetched_at",
            [(match_id, source, q.bookmaker, q.market, q.selection, q.line, q.kind, q.price, fetched)
             for q in quotes if q.price and q.price > 1.0],
        )

    # -- odczyt ------------------------------------------------------------------
    def source_id(self, match_id: int, source: str) -> tuple[str, dict] | None:
        row = self.db.query_one(
            "SELECT external_id, extra FROM match_sources WHERE match_id = ? AND source = ?", (match_id, source)
        )
        if row is None:
            return None
        return row["external_id"], json.loads(row["extra"]) if row["extra"] else {}

    def matches_between(self, start: datetime, end: datetime, leagues: Iterable[str] | None = None,
                        statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT m.*, h.name AS home_name, a.name AS away_name FROM matches m "
            "JOIN teams h ON h.id = m.home_team_id JOIN teams a ON a.id = m.away_team_id "
            "WHERE m.kickoff >= ? AND m.kickoff < ?"
        )
        params: list = [to_iso(start), to_iso(end)]
        if leagues is not None:
            codes = list(leagues)
            sql += f" AND m.league_code IN ({','.join('?' * len(codes))})"
            params += codes
        if statuses is not None:
            sts = list(statuses)
            sql += f" AND m.status IN ({','.join('?' * len(sts))})"
            params += sts
        return self.db.query(sql + " ORDER BY m.kickoff, m.id", tuple(params))

    def matches_needing_stats(self, league_code: str, since: datetime, limit: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM matches WHERE league_code = ? AND status = ? AND stats_checked = 0 AND kickoff >= ? "
            "AND home_goals IS NOT NULL ORDER BY kickoff DESC LIMIT ?",
            (league_code, FINISHED, to_iso(since), limit),
        )

    def unfinished_before(self, moment: datetime) -> list[sqlite3.Row]:
        """Mecze, które powinny już mieć wynik (do odświeżenia i rozliczenia kuponów)."""
        return self.db.query(
            "SELECT * FROM matches WHERE status IN (?, ?) AND kickoff < ? ORDER BY kickoff",
            (SCHEDULED, LIVE, to_iso(moment)),
        )

    def odds_for_match(self, match_id: int, kind: str = "pre") -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM odds WHERE match_id = ? AND kind = ? ORDER BY market, selection, bookmaker",
            (match_id, kind),
        )

    def counts(self) -> dict[str, int]:
        one = lambda sql: int(self.db.query_one(sql)[0])  # noqa: E731
        return {
            "leagues": one("SELECT COUNT(*) FROM leagues WHERE enabled = 1"),
            "teams": one("SELECT COUNT(*) FROM teams"),
            "matches": one("SELECT COUNT(*) FROM matches"),
            "finished": one(f"SELECT COUNT(*) FROM matches WHERE status = '{FINISHED}'"),
            "upcoming": one(f"SELECT COUNT(*) FROM matches WHERE status = '{SCHEDULED}'"),
            "with_xg": one("SELECT COUNT(*) FROM matches WHERE home_xg IS NOT NULL"),
            "odds": one("SELECT COUNT(*) FROM odds"),
            "aliases_to_review": one("SELECT COUNT(*) FROM team_aliases WHERE needs_review = 1"),
        }

    # -- korekty -----------------------------------------------------------------
    def merge_teams(self, keep_id: int, remove_id: int) -> int:
        """Łączy dwie drużyny (błędnie rozdzielone) i scala zdublowane mecze."""
        if keep_id == remove_id:
            return 0
        with self.write_lock, self.db.transaction() as conn:
            conn.execute("UPDATE team_aliases SET team_id = ?, needs_review = 0 WHERE team_id = ?",
                         (keep_id, remove_id))
            conn.execute("UPDATE matches SET home_team_id = ? WHERE home_team_id = ?", (keep_id, remove_id))
            conn.execute("UPDATE matches SET away_team_id = ? WHERE away_team_id = ?", (keep_id, remove_id))
            merged = self._dedupe_matches(conn, keep_id)
            conn.execute("DELETE FROM teams WHERE id = ?", (remove_id,))
        self.matcher.clear_cache()
        return merged

    @staticmethod
    def _dedupe_matches(conn: sqlite3.Connection, team_id: int) -> int:
        rows = conn.execute(
            "SELECT * FROM matches WHERE home_team_id = ? OR away_team_id = ? ORDER BY kickoff, id",
            (team_id, team_id),
        ).fetchall()
        merged = 0
        removed: set[int] = set()
        for i, a in enumerate(rows):
            if a["id"] in removed:
                continue
            for b in rows[i + 1:]:
                if b["id"] in removed:
                    continue
                same = (a["league_code"], a["home_team_id"], a["away_team_id"]) == (
                    b["league_code"], b["home_team_id"], b["away_team_id"])
                if not same or abs(parse_iso(a["kickoff"]) - parse_iso(b["kickoff"])) > MATCH_WINDOW:
                    continue
                conn.execute("UPDATE OR IGNORE match_sources SET match_id = ? WHERE match_id = ?", (a["id"], b["id"]))
                conn.execute("UPDATE OR IGNORE odds SET match_id = ? WHERE match_id = ?", (a["id"], b["id"]))
                conn.execute(
                    "UPDATE matches SET home_goals = COALESCE(home_goals, ?), away_goals = COALESCE(away_goals, ?), "
                    "home_xg = COALESCE(home_xg, ?), away_xg = COALESCE(away_xg, ?) WHERE id = ?",
                    (b["home_goals"], b["away_goals"], b["home_xg"], b["away_xg"], a["id"]),
                )
                conn.execute("DELETE FROM matches WHERE id = ?", (b["id"],))
                removed.add(b["id"])
                merged += 1
        return merged


def merge_status(current: str, incoming: str) -> str:
    """Status końcowy (np. FINISHED) nie jest nadpisywany starszym stanem
    z innego źródła; przełożony mecz może wrócić do SCHEDULED."""
    if current in FINAL_STATUSES and incoming not in FINAL_STATUSES:
        return current
    if current == LIVE and incoming in (SCHEDULED,):
        return current
    return incoming


OddsKey = tuple[str, str, float]   # (rynek, typ, linia)
PSEUDO_BOOKMAKERS = frozenset({"avg", "max"})   # kolumny zbiorcze z plików CSV


@dataclass
class OddsView:
    """Kursy meczu w trzech ujęciach: średnia rynkowa, najwyższy kurs, wybrany bukmacher."""
    average: dict[OddsKey, float]
    best: dict[OddsKey, float]
    bookmaker: dict[OddsKey, float]
    books: dict[OddsKey, int]

    def reference(self, mode: str) -> dict[OddsKey, float]:
        """Kurs do liczenia EV. Tryb 'bookmaker' (np. Superbet) uzupełnia braki średnią."""
        if mode == "best":
            return dict(self.best)
        if mode == "bookmaker":
            return {**self.average, **self.bookmaker}
        return dict(self.average)


def odds_view(rows: Iterable[sqlite3.Row], bookmaker: str = "") -> OddsView:
    grouped: dict[OddsKey, list[tuple[str, float]]] = {}
    for r in rows:
        grouped.setdefault((r["market"], r["selection"], float(r["line"])), []).append((r["bookmaker"], r["price"]))
    wanted = bookmaker.lower()
    average: dict[OddsKey, float] = {}
    best: dict[OddsKey, float] = {}
    chosen: dict[OddsKey, float] = {}
    books: dict[OddsKey, int] = {}
    for key, quotes in grouped.items():
        real = [(b, p) for b, p in quotes if b not in PSEUDO_BOOKMAKERS]
        summary = dict((b, p) for b, p in quotes if b in PSEUDO_BOOKMAKERS)
        prices = [p for _, p in real]
        if "avg" in summary:          # średnia z ok. 40 bukmacherów z plików CSV – dokładniejsza
            average[key] = summary["avg"]
        elif prices:
            average[key] = round(sum(prices) / len(prices), 3)
        if prices:
            best[key] = max(prices)
        if "max" in summary:
            best[key] = max(best.get(key, 0.0), summary["max"])
        books[key] = len(real)
        if wanted:
            match = [p for b, p in real if b.lower() == wanted or b.lower().startswith(wanted + ".")]
            if match:
                chosen[key] = match[0]
    return OddsView(average, best, chosen, books)
