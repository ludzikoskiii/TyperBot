"""Synchronizacja danych ze wszystkich źródeł.

Każdy krok (liga × źródło) jest uruchamiany osobno: błąd jednego źródła trafia
do raportu i statusu źródła, ale nie przerywa pozostałych kroków.
Serwis jest niezależny od Qt – w interfejsie uruchamiamy go w wątku w tle.

Podział ról źródeł (plany darmowe):
  * API-Football      – historia sezonów dostępnych w planie (wyniki, xG),
  * football-data.org – bieżący sezon lig top-5 i Ligi Mistrzów,
  * OddsPapi          – kursy Superbet, terminarz i wyniki Ekstraklasy,
  * The Odds API      – kursy wielu bukmacherów (średnia rynkowa), zapasowe wyniki,
  * football-data.co.uk – opcjonalny import CSV (domyślnie wyłączony).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from typerbot.config.leagues import League, season_of
from typerbot.config.secrets import SecretStore
from typerbot.config.settings import Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.errors import STATE_LABELS, PlanRestrictionError, QuotaExceededError, SourceError
from typerbot.data.http import HttpClient, Transport
from typerbot.data.quota import QuotaInfo, QuotaTracker, StatusBoard, period_start
from typerbot.data.ratelimit import RateLimiter
from typerbot.data.records import MatchRecord, to_iso
from typerbot.data.repository import LeagueRepository, MatchRepository
from typerbot.data.sources import ApiFootball, ApiSource, FootballDataCsv, FootballDataOrg, OddsPapi, TheOddsApi
from typerbot.data.sources.api_football import plan_seasons

log = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 24 * HOUR
CURRENT_SEASON_REFRESH = 12 * HOUR
DEFAULT_APIF_SEASONS = (2022, 2024)   # zakres planu darmowego API-Football (aktualizowany automatycznie)
_SEVERITY = {"ok": 0, "skipped": 0, "offline": 2, "plan": 3, "error": 4, "quota": 5, "no_key": 6, "auth": 7}


@dataclass
class StepResult:
    source: str
    step: str
    league: str | None
    state: str
    message: str = ""
    records: int = 0
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.state in ("ok", "skipped")


@dataclass
class SyncReport:
    steps: list[StepResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def add(self, step: StepResult) -> None:
        self.steps.append(step)

    def by_source(self) -> dict[str, list[StepResult]]:
        out: dict[str, list[StepResult]] = {}
        for s in self.steps:
            out.setdefault(s.source, []).append(s)
        return out

    @property
    def errors(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok]


@dataclass
class QuotaRow:
    source: str
    label: str
    period: str
    used: int | None
    limit: int | None
    remaining: int | None
    from_headers: bool
    calls_today: int
    state: str
    message: str


class SyncService:
    def __init__(
        self,
        db: Database,
        secrets: SecretStore,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] | None = None,
        rate_limits: bool = True,
    ):
        self.db = db
        self.secrets = secrets
        self.clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.settings_store = SettingsStore(db)
        self.http = HttpClient(db, transport, clock)
        self.quota = QuotaTracker(db, clock)
        self.status = StatusBoard(db, clock)
        self.leagues = LeagueRepository(db)
        self.leagues.ensure_defaults()
        self.matches = MatchRepository(db)
        self.fd_org = FootballDataOrg(self.http, self.quota, secrets, clock=clock)
        self.api_football = ApiFootball(self.http, self.quota, secrets, clock=clock)
        self.odds_api = TheOddsApi(self.http, self.quota, secrets, clock=clock)
        self.oddspapi = OddsPapi(self.http, self.quota, secrets, clock=clock)
        self.csv = FootballDataCsv(self.http, self.quota, None, clock=clock)
        self.sources: dict[str, ApiSource] = {
            s.name: s for s in (self.fd_org, self.api_football, self.oddspapi, self.odds_api, self.csv)
        }
        if not rate_limits:  # transport lokalny (demo, testy) – bez czekania między zapytaniami
            for src in self.sources.values():
                src.limiter = RateLimiter(None)
        self._run_lock = threading.Lock()

    # -- pomocnicze ------------------------------------------------------------
    def now(self) -> datetime:
        return self._now()

    def settings(self) -> Settings:
        return self.settings_store.load()

    def current_season(self) -> int:
        n = self.now()
        return season_of(n.year, n.month)

    def meta(self, key: str, default=None):
        row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (f"meta.{key}",))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value = excluded.value", (f"meta.{key}", json.dumps(value)))

    def apif_seasons(self) -> list[int]:
        lo, hi = self.meta("api_football_seasons", list(DEFAULT_APIF_SEASONS))
        return list(range(int(lo), int(hi) + 1))

    def _step(self, report: SyncReport, source: ApiSource, step: str, league: League | None,
              fn: Callable[[], int], detail: str = "") -> int | None:
        code = league.code if league else None
        if not source.has_key():
            report.add(StepResult(source.name, step, code, "no_key", STATE_LABELS["no_key"]))
            return None
        try:
            count = fn()
        except SourceError as exc:
            log.warning("%s/%s/%s: %s", source.name, step, code, exc.message)
            report.add(StepResult(source.name, step, code, exc.state, exc.message))
            return None
        except Exception as exc:  # błąd programu nie może zatrzymać reszty synchronizacji
            log.exception("Nieoczekiwany błąd w %s/%s/%s", source.name, step, code)
            report.add(StepResult(source.name, step, code, "error", f"{exc.__class__.__name__}: {exc}"))
            return None
        state = "offline" if source.last_stale else "ok"
        msg = "dane z cache (źródło niedostępne)" if source.last_stale else detail
        report.add(StepResult(source.name, step, code, state, msg, records=count, stale=source.last_stale))
        return count

    def _save(self, records: Iterable[MatchRecord]) -> int:
        return len(self.matches.save_records(list(records)))

    def _finish(self, report: SyncReport) -> SyncReport:
        report.finished = self.clock()
        for name, steps in report.by_source().items():
            worst = max(steps, key=lambda s: _SEVERITY.get(s.state, 4))
            if worst.state == "skipped":
                continue
            if worst.state == "ok":
                self.status.set(name, "ok", f"{sum(s.records for s in steps)} rekordów")
            else:
                messages = list(dict.fromkeys(s.message for s in steps if s.state == worst.state and s.message))
                self.status.set(name, worst.state, "; ".join(messages[:2]))
        return report

    @contextmanager
    def _forced(self, force: bool):
        for src in self.sources.values():
            src.force_refresh = force
        try:
            yield
        finally:
            for src in self.sources.values():
                src.force_refresh = False

    def _history_fresh(self, source: str, code: str, season: int, past: bool) -> bool:
        row = self.db.query_one(
            "SELECT fetched_at, complete FROM history_files WHERE source = ? AND league_code = ? AND season = ?",
            (source, code, season),
        )
        if row is None:
            return False
        if past:
            return bool(row["complete"])
        return self.clock() - row["fetched_at"] < CURRENT_SEASON_REFRESH

    def _mark_history(self, source: str, code: str, season: int, rows: int, complete: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO history_files(source, league_code, season, fetched_at, rows, complete) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, league_code, season) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, rows = excluded.rows, complete = excluded.complete",
                (source, code, season, self.clock(), rows, int(complete)),
            )

    def _calls_this_month(self, source: str, endpoint_like: str = "%") -> int:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS n FROM api_calls WHERE source = ? AND endpoint LIKE ? AND ts >= ?",
            (source, endpoint_like, period_start("month", self.clock())),
        )
        return int(row["n"]) if row else 0

    def _oddspapi_left(self, settings: Settings) -> int:
        return settings.sync.oddspapi_monthly_budget - self._calls_this_month(self.oddspapi.name)

    # -- historia --------------------------------------------------------------------
    def sync_history(self, report: SyncReport) -> None:
        """Zakończone sezony z API-Football (raz na zawsze) i opcjonalnie pliki CSV."""
        leagues = self.leagues.all(enabled_only=True)
        current = self.current_season()
        for league in leagues:
            if not league.api_football_id:
                continue
            for season in self.apif_seasons():
                if season >= current or self._history_fresh(self.api_football.name, league.code, season, True):
                    continue
                result = self._step(report, self.api_football, "history", league,
                                    lambda lg=league, s=season: self._history_apif(lg, s),
                                    detail=f"sezon {season}/{(season + 1) % 100:02d}")
                if result is None and report.steps[-1].state in ("quota", "auth", "no_key"):
                    break
        if self.settings().sync.csv_import:
            self._sync_csv(report, leagues, current)

    def _history_apif(self, league: League, season: int) -> int:
        try:
            records = self.api_football.season_fixtures(league, season, ttl=0)
        except PlanRestrictionError as exc:
            allowed = plan_seasons(exc.message)
            if allowed:
                self.set_meta("api_football_seasons", list(allowed))
                if season not in range(allowed[0], allowed[1] + 1):
                    return 0  # sezon poza planem – pominięty, zakres zapamiętany
            raise
        count = self._save(records)
        self._mark_history(self.api_football.name, league.code, season, count, complete=True)
        return count

    def _sync_csv(self, report: SyncReport, leagues: list[League], current: int) -> None:
        seasons = list(range(current - self.settings().sync.csv_seasons + 1, current + 1))
        for league in leagues:
            if league.fdcuk_format == "main":
                for season in seasons:
                    if self._history_fresh(self.csv.name, league.code, season, season < current):
                        continue
                    self._step(report, self.csv, "history", league,
                               lambda lg=league, s=season: self._csv_main(lg, s, s < current),
                               detail=f"sezon {season}/{(season + 1) % 100:02d}")
            elif league.fdcuk_format == "extra" and not self._history_fresh(self.csv.name, league.code, 0, False):
                self._step(report, self.csv, "history", league, lambda lg=league: self._csv_extra(lg, set(seasons)))

    def _csv_main(self, league: League, season: int, past: bool) -> int:
        try:
            records = self.csv.season(league, season, ttl=0)
        except SourceError as exc:
            if not past and "404" in exc.message:
                return 0  # plik bieżącego sezonu jeszcze nie istnieje
            raise
        count = self._save(records)
        self._mark_history(self.csv.name, league.code, season, count, complete=past)
        return count

    def _csv_extra(self, league: League, seasons: set[int]) -> int:
        count = self._save(self.csv.extra(league, seasons, ttl=0))
        self._mark_history(self.csv.name, league.code, 0, count, complete=False)
        return count

    # -- bieżący sezon -------------------------------------------------------------------
    def sync_fixtures(self, report: SyncReport, days_ahead: int | None = None) -> None:
        """Terminarz i wyniki bieżącego sezonu."""
        settings = self.settings()
        ahead = days_ahead if days_ahead is not None else max(settings.coupon.days_ahead, 7)
        ttl = settings.sync.fixtures_every_hours * HOUR
        season = self.current_season()
        today = self.now().date()
        apif_current = season in self.apif_seasons()
        for league in self.leagues.all(enabled_only=True):
            if league.fd_org_code and self.fd_org.has_key():
                self._step(report, self.fd_org, "fixtures", league,
                           lambda lg=league: self._save(self.fd_org.season_matches(lg, season, ttl=ttl)))
                continue
            if league.api_football_id and apif_current and self.api_football.has_key():
                done = self._step(report, self.api_football, "fixtures", league, lambda lg=league: self._save(
                    self.api_football.fixtures(lg, season, date(season, 7, 1), today + timedelta(days=ahead), ttl=ttl)))
                if done is not None:
                    continue
            if league.oddspapi_id and self.oddspapi.has_key():
                self._step(report, self.oddspapi, "fixtures", league, lambda lg=league: self._save(
                    self.oddspapi.fixtures(lg, date(season, 7, 1), today + timedelta(days=ahead), ttl=12 * HOUR)))
            self._results_without_score(report, league, settings)

    def _results_without_score(self, report: SyncReport, league: League, settings: Settings) -> None:
        """Wyniki dla lig bez pełnego źródła wyników (np. Ekstraklasa w planach darmowych):
        najpierw The Odds API (ostatnie 3 dni, 2 kredyty), potem pojedyncze mecze z OddsPapi."""
        now = self.now()
        recent = [r for r in self.matches.unfinished_before(now - timedelta(hours=3))
                  if r["league_code"] == league.code and r["kickoff"] >= to_iso(now - timedelta(days=3))]
        recent += [r for r in self.matches.finished_without_score(league.code, now - timedelta(days=3))]
        if recent and league.odds_api_key and self.odds_api.has_key():
            self._step(report, self.odds_api, "results", league,
                       lambda: self._save(self.odds_api.scores(league, 3, ttl=3 * HOUR)))
        missing = self.matches.finished_without_score(league.code, datetime(self.current_season(), 7, 1,
                                                                             tzinfo=timezone.utc))
        if not missing or not self.oddspapi.has_key():
            return
        budget = min(settings.sync.oddspapi_scores_monthly - self._calls_this_month(self.oddspapi.name, "/scores"),
                     self._oddspapi_left(settings))
        if budget <= 0:
            report.add(StepResult(self.oddspapi.name, "results", league.code, "skipped",
                                  f"{len(missing)} meczów bez wyniku – budżet wyników na ten miesiąc wykorzystany"))
            return
        self._step(report, self.oddspapi, "results", league, lambda: self._oddspapi_scores(missing[:budget]))

    def _oddspapi_scores(self, rows) -> int:
        saved = 0
        for row in rows:
            ref = self.matches.source_id(row["id"], self.oddspapi.name)
            if ref is None:
                continue
            score = self.oddspapi.score(ref[0])
            if score is not None:
                self.matches.set_score(row["id"], *score)
                saved += 1
        return saved

    # -- kursy ------------------------------------------------------------------------------
    def leagues_needing_odds(self, start: datetime, end: datetime) -> list[League]:
        with_matches = {r["league_code"] for r in self.matches.matches_between(start, end, statuses=["SCHEDULED"])}
        return [lg for lg in self.leagues.all(enabled_only=True) if lg.code in with_matches]

    def sync_odds(self, report: SyncReport, days_ahead: int | None = None,
                  leagues: list[League] | None = None) -> None:
        """Średnia rynkowa z The Odds API i kursy Superbet z OddsPapi."""
        settings = self.settings()
        ahead = days_ahead if days_ahead is not None else max(settings.coupon.days_ahead, 7)
        start = self.now()
        targets = leagues if leagues is not None else self.leagues_needing_odds(start, start + timedelta(days=ahead))
        active = self._active_odds_sports()
        for league in targets:
            if not league.odds_api_key:
                continue
            if active is not None and league.odds_api_key not in active:
                report.add(StepResult(self.odds_api.name, "odds", league.code, "skipped", "poza sezonem"))
                continue
            self._step(report, self.odds_api, "odds", league, lambda lg=league: self._save(self.odds_api.odds(
                lg, region=settings.odds.region, ttl=settings.odds.cache_hours * HOUR)))
        papi = [lg for lg in targets if lg.oddspapi_id]
        if papi and settings.odds.bookmaker:
            if self._oddspapi_left(settings) <= 0:
                report.add(StepResult(self.oddspapi.name, "odds", None, "quota",
                                      "wykorzystano miesięczny budżet OddsPapi"))
            else:
                self._step(report, self.oddspapi, "odds", None,
                           lambda: self._bookmaker_odds(papi, settings, start, ahead))

    def _bookmaker_odds(self, leagues: list[League], settings: Settings, start: datetime, ahead: int) -> int:
        self._verify_tournament_ids(leagues)
        leagues = [self.leagues.get(lg.code) or lg for lg in leagues]
        slug = self.meta("oddspapi_bookmaker_slug")
        if slug is None or not str(slug).startswith(settings.odds.bookmaker.lower()):
            slug = self.oddspapi.find_bookmaker(settings.odds.bookmaker)
            self.set_meta("oddspapi_bookmaker_slug", slug)
        odds = self.oddspapi.odds_by_tournaments(leagues, slug, ttl=settings.odds.cache_hours * HOUR)
        unknown = [fid for fid in odds if self._match_for_fixture(fid) is None]
        if unknown:
            # Mecze spoza terminarza OddsPapi – dociągamy terminarz lig (cache 24 h), żeby znać drużyny.
            for lg in leagues:
                self._save(self.oddspapi.fixtures(lg, start.date(), start.date() + timedelta(days=ahead), ttl=DAY))
        saved = 0
        fetched = to_iso(self.now())
        for fid, quotes in odds.items():
            match_id = self._match_for_fixture(fid)
            if match_id is None or not quotes:
                continue
            with self.matches.write_lock, self.db.transaction() as conn:
                self.matches._save_odds(conn, match_id, self.oddspapi.name, quotes, fetched)
            saved += 1
        return saved

    def _match_for_fixture(self, fixture_id: str) -> int | None:
        row = self.db.query_one("SELECT match_id FROM match_sources WHERE source = ? AND external_id = ?",
                                (self.oddspapi.name, fixture_id))
        return int(row["match_id"]) if row else None

    def _verify_tournament_ids(self, leagues: list[League]) -> None:
        """Raz na 30 dni sprawdza identyfikatory lig w OddsPapi po nazwie kraju i ligi."""
        if self.meta("oddspapi_tournaments_checked", 0) > self.clock() - 30 * DAY:
            return
        by_slug = {f"{t.get('categorySlug')}/{t.get('tournamentSlug')}": t.get("tournamentId")
                   for t in self.oddspapi.tournaments()}
        for lg in leagues:
            tid = by_slug.get(lg.oddspapi_slug)
            if tid and tid != lg.oddspapi_id:
                log.info("OddsPapi: poprawiono identyfikator %s: %s -> %s", lg.code, lg.oddspapi_id, tid)
                self.leagues.set_oddspapi_id(lg.code, int(tid))
        self.set_meta("oddspapi_tournaments_checked", self.clock())

    def _active_odds_sports(self) -> set[str] | None:
        if not self.odds_api.has_key():
            return None
        try:
            return {s.get("key") for s in self.odds_api.sports() if s.get("active", True)}
        except SourceError:
            return None

    def sync_event_markets(self, report: SyncReport, match_ids: Iterable[int]) -> None:
        """BTTS i podwójna szansa dla wybranych meczów (The Odds API, 2 kredyty na mecz)."""
        settings = self.settings()
        budget = settings.sync.event_markets_daily_budget - self._event_calls_today()
        for match_id in match_ids:
            if budget <= 0:
                report.add(StepResult(self.odds_api.name, "event_odds", None, "skipped",
                                      "wykorzystano dzienny budżet zapytań o BTTS/podwójną szansę"))
                break
            ref = self.matches.source_id(match_id, self.odds_api.name)
            row = self.db.query_one("SELECT league_code FROM matches WHERE id = ?", (match_id,))
            if ref is None or row is None:
                continue
            league = self.leagues.get(row["league_code"])
            if league is None:
                continue
            result = self._step(report, self.odds_api, "event_odds", league,
                                lambda lg=league, ev=ref[0]: self._save_event(lg, ev, settings))
            budget -= 1
            if result is None and report.steps[-1].state in ("quota", "auth", "no_key"):
                break

    def _save_event(self, league: League, event_id: str, settings: Settings) -> int:
        rec = self.odds_api.event_odds(league, event_id, region=settings.odds.region,
                                       ttl=settings.odds.cache_hours * HOUR)
        return self._save([rec]) if rec else 0

    def _event_calls_today(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM api_calls WHERE source = ? AND endpoint LIKE '%/events/%' AND ts >= ?",
            (self.odds_api.name, period_start("day", self.clock())),
        )
        return int(row["n"]) if row else 0

    # -- xG ---------------------------------------------------------------------------------
    def backfill_xg(self, report: SyncReport, budget: int | None = None) -> None:
        """xG z API-Football dla meczów z sezonów dostępnych w planie, w ramach dziennego budżetu."""
        if not self.api_football.has_key():
            report.add(StepResult(self.api_football.name, "xg", None, "no_key", STATE_LABELS["no_key"]))
            return
        settings = self.settings()
        left = budget if budget is not None else (
            settings.sync.xg_daily_budget - self.quota.calls_today(self.api_football.name))
        leagues = [lg for lg in self.leagues.all(enabled_only=True) if lg.api_football_id]
        since = datetime(min(self.apif_seasons()), 7, 1, tzinfo=timezone.utc)
        for i, league in enumerate(leagues):
            if left <= 0:
                break
            share = max(1, left // (len(leagues) - i))
            try:
                used, saved = self._xg_for_league(league, since, share)
            except (PlanRestrictionError, QuotaExceededError) as exc:
                report.add(StepResult(self.api_football.name, "xg", league.code, exc.state, exc.message))
                break
            except SourceError as exc:
                report.add(StepResult(self.api_football.name, "xg", league.code, exc.state, exc.message))
                continue
            left -= used
            report.add(StepResult(self.api_football.name, "xg", league.code, "ok", records=saved))

    def _xg_for_league(self, league: League, since: datetime, budget: int) -> tuple[int, int]:
        calls = saved = 0
        rows = self.db.query(
            "SELECT m.id, s.external_id, s.extra FROM matches m JOIN match_sources s ON s.match_id = m.id "
            "WHERE s.source = ? AND m.league_code = ? AND m.status = 'FINISHED' AND m.stats_checked = 0 "
            "AND m.home_goals IS NOT NULL AND m.kickoff >= ? ORDER BY m.kickoff DESC LIMIT ?",
            (self.api_football.name, league.code, to_iso(since), budget),
        )
        for row in rows:
            extra = json.loads(row["extra"]) if row["extra"] else {}
            stats = self.api_football.fixture_statistics(row["external_id"], extra.get("home_id"))
            calls += 1
            self.matches.save_stats(row["id"], stats)
            saved += int(bool(stats and stats.has_xg))
        return calls, saved

    # -- całość ----------------------------------------------------------------------
    def run_all(self, *, force: bool = False, history: bool = True, odds: bool = True, xg: bool = True) -> SyncReport:
        """Pełna synchronizacja. Wywołania są serializowane (jeden zapis naraz)."""
        with self._run_lock, self._forced(force):
            report = SyncReport(started=self.clock())
            if history:
                self.sync_history(report)
            self.sync_fixtures(report)
            if odds:
                self.sync_odds(report)
            if xg:
                self.backfill_xg(report)
            return self._finish(report)

    def run(self, fn: Callable[[SyncReport], None], *, force: bool = False) -> SyncReport:
        """Pojedynczy krok (np. tylko kursy) z tą samą obsługą błędów i statusów."""
        with self._run_lock, self._forced(force):
            report = SyncReport(started=self.clock())
            fn(report)
            return self._finish(report)

    # -- limity ----------------------------------------------------------------------
    def quota_rows(self) -> list[QuotaRow]:
        rows = []
        states = self.status.all()
        settings = self.settings()
        for src in self.sources.values():
            if src is self.csv and not settings.sync.csv_import:
                continue
            info: QuotaInfo | None = src.quota_info()
            state = states.get(src.name)
            if not src.has_key():
                st, msg = "no_key", STATE_LABELS["no_key"]
            elif state:
                st, msg = state.state, state.message
            else:
                st, msg = "idle", ""
            rows.append(QuotaRow(
                source=src.name, label=src.label,
                period=info.period if info else "-",
                used=(info.used if info and info.used is not None else (info.local_used if info else None)),
                limit=info.limit if info else None,
                remaining=info.remaining if info else None,
                from_headers=bool(info and info.from_headers),
                calls_today=self.quota.calls_today(src.name),
                state=st, message=msg,
            ))
        return rows
