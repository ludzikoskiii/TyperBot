"""Synchronizacja danych – wyłącznie darmowe źródła.

Każdy krok (liga × źródło) jest uruchamiany osobno: błąd jednego źródła trafia
do raportu i statusu źródła, ale nie przerywa pozostałych kroków.
Serwis jest niezależny od Qt – w interfejsie uruchamiamy go w wątku w tle.

Role źródeł (wszystkie bez opłat i bez karty):
  * football-data.co.uk (bez klucza i limitu) – główne źródło: historia wyników i kursów
    (model, backtest) oraz nadchodzące mecze z kursami 1X2 i powyżej/poniżej 2,5;
  * football-data.org (darmowy klucz, 10 zapytań/min) – terminarz i wyniki lig top-5
    i Ligi Mistrzów (szybkie wyniki do rozliczania kuponów);
  * The Odds API (darmowy klucz, 500 kredytów/mies.) – bezpłatna lista meczów lig spoza
    football-data.org oraz uzupełnienie brakujących kursów 1X2 i powyżej/poniżej 2,5;
  * OddsPapi (darmowy klucz, 250 zapytań/mies.) – uzupełnienie brakujących kursów
    (BTTS, podwójna szansa, kursy Superbet), do 5 lig w jednym zapytaniu.

Źródła z limitem są tylko uzupełnieniem. Pytamy je wyłącznie o ligi, w których w najbliższych
dniach brakuje kursów, najwyżej raz dziennie na ligę i w ramach budżetu dziennego
(pozostały budżet miesięczny aplikacji / pozostałe dni miesiąca). Budżet aplikacji jest
niższy od limitu planu, więc limit nie wyczerpie się przed końcem miesiąca.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from typerbot.config.leagues import League, season_of
from typerbot.config.secrets import REMOVED_SOURCES, SecretStore
from typerbot.config.settings import Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.errors import STATE_LABELS, SourceError
from typerbot.data.http import HttpClient, Transport
from typerbot.data.quota import QuotaInfo, QuotaTracker, StatusBoard, period_start
from typerbot.data.ratelimit import RateLimiter
from typerbot.data.records import LIVE, MARKET_1X2, MARKET_OU, SCHEDULED, MatchRecord, to_iso
from typerbot.data.repository import LeagueRepository, MatchRepository
from typerbot.data.sources import ApiSource, FootballDataCsv, FootballDataOrg, OddsPapi, TheOddsApi
from typerbot.data.sources.the_odds_api import MARKET_KEYS

log = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 24 * HOUR
CURRENT_SEASON_REFRESH = 12 * HOUR     # plik bieżącego sezonu (wyniki) – co 12 godzin
FIXTURE_FILES_TTL = 6 * HOUR           # pliki z nadchodzącymi meczami football-data.co.uk
EVENTS_TTL = 6 * HOUR                  # bezpłatna lista meczów The Odds API
SUPPLEMENT_TTL = 20 * HOUR             # kursy z uzupełnień (i tak najwyżej raz dziennie)
PAPI_FIXTURES_DAYS = 3                 # terminarz OddsPapi – najwyżej raz na 3 dni na ligę
RESULTS_LOOKBACK = timedelta(days=3)
_SEVERITY = {"ok": 0, "skipped": 0, "offline": 2, "plan": 3, "error": 4, "quota": 5, "no_key": 6, "auth": 7}
# Rynki, których kursy daje football-data.co.uk (reszta – z uzupełnień albo szacowana).
CSV_MARKETS = {"main": {MARKET_1X2, MARKET_OU}, "extra": {MARKET_1X2}}


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

    def to_json(self) -> dict:
        return {"started": self.started, "finished": self.finished,
                "steps": [{"source": s.source, "step": s.step, "league": s.league, "state": s.state,
                           "message": s.message, "records": s.records, "stale": s.stale} for s in self.steps]}

    @classmethod
    def from_json(cls, data: dict) -> "SyncReport":
        report = cls(started=data.get("started") or 0.0, finished=data.get("finished"))
        for d in data.get("steps") or []:
            report.add(StepResult(d["source"], d["step"], d.get("league"), d["state"], d.get("message", ""),
                                  d.get("records", 0), d.get("stale", False)))
        return report


def load_last_report(db: Database) -> SyncReport | None:
    row = db.query_one("SELECT value FROM settings WHERE key = 'meta.last_sync'")
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    return SyncReport.from_json(data) if isinstance(data, dict) else None


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


@dataclass
class Budget:
    """Budżet źródła z limitem miesięcznym: miesięczny limit aplikacji rozłożony równo na dni."""
    source: str
    plan_limit: int
    app_limit: int
    used_month: int      # zużyte w tym miesiącu (z nagłówków odpowiedzi albo policzone lokalnie)
    used_today: int
    days_left: int       # łącznie z dzisiejszym

    @property
    def month_left(self) -> int:
        return max(0, min(self.app_limit, self.plan_limit) - self.used_month)

    @property
    def daily_allowance(self) -> int:
        at_day_start = max(0, min(self.app_limit, self.plan_limit) - (self.used_month - self.used_today))
        return math.ceil(at_day_start / max(1, self.days_left))

    @property
    def today_left(self) -> int:
        return max(0, min(self.daily_allowance - self.used_today, self.month_left))

    def allows(self, cost: int) -> bool:
        return cost <= self.today_left


@dataclass
class UsageEstimate:
    source: str
    label: str
    plan_limit: int
    app_limit: int
    used_month: int
    used_today: int
    daily_allowance: int
    projected: int        # szacunek na cały miesiąc (zużyte + prognoza według terminarza i reguł)
    rule: str


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
        self.csv = FootballDataCsv(self.http, self.quota, None, clock=clock)
        self.fd_org = FootballDataOrg(self.http, self.quota, secrets, clock=clock)
        self.odds_api = TheOddsApi(self.http, self.quota, secrets, clock=clock)
        self.oddspapi = OddsPapi(self.http, self.quota, secrets, clock=clock)
        self.sources: dict[str, ApiSource] = {
            s.name: s for s in (self.csv, self.fd_org, self.odds_api, self.oddspapi)
        }
        if not rate_limits:  # transport lokalny (demo, testy) – bez czekania między zapytaniami
            for src in self.sources.values():
                src.limiter = RateLimiter(None)
        self._run_lock = threading.Lock()
        self._forget_removed_keys()

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

    def _forget_removed_keys(self) -> None:
        """Klucze usuniętych źródeł (np. API-Football) kasujemy z magazynu poświadczeń."""
        for name in REMOVED_SOURCES:
            try:
                if self.secrets.get(name):
                    self.secrets.delete(name)
            except Exception:  # magazyn niedostępny – nic nie szkodzi
                log.debug("Nie udało się usunąć klucza %s", name, exc_info=True)

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

    def _skip(self, report: SyncReport, source: ApiSource, step: str, league: League | None, message: str) -> None:
        report.add(StepResult(source.name, step, league.code if league else None, "skipped", message))

    def _save(self, records: Iterable[MatchRecord]) -> int:
        return len(self.matches.save_records(list(records)))

    def last_report(self) -> SyncReport | None:
        """Raport ostatniej pełnej synchronizacji (lista problemów w interfejsie i diagnostyce)."""
        return load_last_report(self.db)

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

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).date().isoformat()

    def _done_today(self, what: str, code: str) -> bool:
        return self.meta(f"daily.{what}.{code}") == self._today()

    def _mark_today(self, what: str, code: str) -> None:
        self.set_meta(f"daily.{what}.{code}", self._today())

    # -- historia --------------------------------------------------------------------
    def sync_history(self, report: SyncReport) -> None:
        """Wyniki i kursy z football-data.co.uk: zakończone sezony raz na zawsze, bieżący co 12 h."""
        leagues = self.leagues.all(enabled_only=True)
        current = self.current_season()
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
            if "404" not in exc.message:
                raise
            if past:   # brak pliku zakończonego sezonu (liga nieobjęta w tym sezonie) – nie pytamy ponownie
                self._mark_history(self.csv.name, league.code, season, 0, complete=True)
            return 0   # plik bieżącego sezonu jeszcze nie istnieje
        count = self._save(records)
        self._mark_history(self.csv.name, league.code, season, count, complete=past)
        return count

    def _csv_extra(self, league: League, seasons: set[int]) -> int:
        count = self._save(self.csv.extra(league, seasons, ttl=0))
        self._mark_history(self.csv.name, league.code, 0, count, complete=False)
        return count

    # -- terminarz i wyniki -------------------------------------------------------------
    def sync_fixtures(self, report: SyncReport) -> None:
        """Nadchodzące mecze: football-data.co.uk (z kursami), football-data.org (terminarz i wyniki),
        a dla lig bez tych źródeł – bezpłatna lista meczów The Odds API."""
        settings = self.settings()
        leagues = self.leagues.all(enabled_only=True)
        now = self.now()
        main = [lg for lg in leagues if lg.fdcuk_format == "main"]
        extra = [lg for lg in leagues if lg.fdcuk_format == "extra"]
        if main:
            self._step(report, self.csv, "fixtures", None,
                       lambda: self._save(self.csv.upcoming_main(main, now, ttl=FIXTURE_FILES_TTL)),
                       detail="fixtures.csv: " + ", ".join(lg.code for lg in main))
        if extra:
            self._step(report, self.csv, "fixtures", None,
                       lambda: self._save(self.csv.upcoming_extra(extra, now, ttl=FIXTURE_FILES_TTL)),
                       detail="new_league_fixtures.csv: " + ", ".join(lg.code for lg in extra))

        fd_ok: set[str] = set()
        if not self.fd_org.has_key() and any(lg.fd_org_code for lg in leagues):
            report.add(StepResult(self.fd_org.name, "fixtures", None, "no_key",
                                  "brak klucza – terminarz i szybkie wyniki tych lig tylko z innych źródeł"))
        season = self.current_season()
        ttl = settings.sync.fixtures_every_hours * HOUR
        for league in leagues:
            if league.fd_org_code and self.fd_org.has_key():
                done = self._step(report, self.fd_org, "fixtures", league,
                                  lambda lg=league: self._save(self.fd_org.season_matches(lg, season, ttl=ttl)))
                if done is not None:
                    fd_ok.add(league.code)

        # Ligi bez terminarza z football-data.org (np. Ekstraklasa) – bezpłatna lista meczów The Odds API.
        if self.odds_api.has_key() and not self._events_cost_credits():
            for league in leagues:
                if league.code in fd_ok or not league.odds_api_key:
                    continue
                self._step(report, self.odds_api, "fixtures", league,
                           lambda lg=league: self._save(self.odds_api.events(lg, ttl=EVENTS_TTL)),
                           detail="lista meczów (bezpłatna)")

    def _events_cost_credits(self) -> bool:
        """Zabezpieczenie: gdyby lista meczów zaczęła kosztować kredyty, przestajemy z niej korzystać."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM api_calls WHERE source = ? AND endpoint LIKE '%/events' AND cost > 0 "
            "AND ts >= ?", (self.odds_api.name, period_start("month", self.clock())))
        return bool(row and row["n"])

    def sync_missing_results(self, report: SyncReport) -> None:
        """Wyniki meczów z kuponów w grze, których nie mają jeszcze źródła bez limitu (np. Ekstraklasa
        przed aktualizacją pliku CSV) – The Odds API (2 kredyty za ligę), najwyżej raz dziennie."""
        now = self.now()
        rows = self.db.query(
            "SELECT DISTINCT m.id, m.league_code FROM coupon_legs l JOIN matches m ON m.id = l.match_id "
            "WHERE l.result = 'pending' AND m.status IN (?, ?) AND m.kickoff < ? AND m.kickoff >= ?",
            (SCHEDULED, LIVE, to_iso(now - timedelta(hours=3)), to_iso(now - RESULTS_LOOKBACK)))
        waiting = sorted({r["league_code"] for r in rows})
        if not waiting or not self.odds_api.has_key():
            return
        budget = self.budget(self.odds_api)
        for code in waiting:
            league = self.leagues.get(code)
            if league is None or not league.odds_api_key:
                continue
            if self._done_today("results", code):
                self._skip(report, self.odds_api, "results", league, "wyniki pobrane już dziś")
                continue
            if not budget.allows(2):
                self._skip(report, self.odds_api, "results", league,
                           f"dzienny budżet kredytów wykorzystany ({budget.daily_allowance}/dzień)")
                continue
            self._step(report, self.odds_api, "results", league,
                       lambda lg=league: self._save(self.odds_api.scores(lg, 3, ttl=12 * HOUR)))
            if report.steps[-1].state != "offline":
                self._mark_today("results", code)
            budget = self.budget(self.odds_api)

    # -- brakujące kursy ----------------------------------------------------------------
    def odds_gaps(self, start: datetime, end: datetime, markets: Iterable[str] | None = None
                  ) -> dict[str, dict[str, list[int]]]:
        """{liga: {rynek: [mecze bez kursu]}} dla nadchodzących meczów w zakresie."""
        wanted = list(markets if markets is not None else self.settings().markets_enabled)
        leagues = [lg.code for lg in self.leagues.all(enabled_only=True)]
        rows = self.matches.matches_between(start, end, leagues=leagues, statuses=[SCHEDULED])
        if not rows:
            return {}
        ids = [r["id"] for r in rows]
        present: dict[int, set[str]] = {}
        for r in self.db.query(
                f"SELECT DISTINCT match_id, market FROM odds WHERE kind = 'pre' AND (market != 'OU' OR line = 2.5) "
                f"AND match_id IN ({','.join('?' * len(ids))})", tuple(ids)):
            present.setdefault(r["match_id"], set()).add(r["market"])
        gaps: dict[str, dict[str, list[int]]] = {}
        for r in rows:
            have = present.get(r["id"], set())
            for market in wanted:
                if market not in have:
                    gaps.setdefault(r["league_code"], {}).setdefault(market, []).append(r["id"])
        return gaps

    def sync_missing_odds(self, report: SyncReport) -> None:
        """Uzupełnienie kursów, których nie ma w plikach football-data.co.uk: najpierw OddsPapi
        (do 5 lig w jednym zapytaniu, wszystkie rynki), potem The Odds API (tylko 1X2 i powyżej/poniżej)."""
        settings = self.settings()
        start = self.now()
        end = start + timedelta(days=max(1, settings.sync.odds_horizon_days))
        leagues = {lg.code: lg for lg in self.leagues.all(enabled_only=True)}
        gaps = self.odds_gaps(start, end, settings.markets_enabled)
        if not gaps:
            return

        if self.oddspapi.has_key() and settings.odds.bookmaker:
            todo = [leagues[c] for c in gaps if c in leagues and leagues[c].oddspapi_id]
            fresh = [lg for lg in todo if not self._done_today("oddspapi", lg.code)]
            for lg in todo:
                if lg not in fresh:
                    self._skip(report, self.oddspapi, "odds", lg, "uzupełnione już dziś")
            for i in range(0, len(fresh), 5):
                chunk = fresh[i:i + 5]
                budget = self.budget(self.oddspapi)
                if not budget.allows(1):
                    for lg in chunk:
                        self._skip(report, self.oddspapi, "odds", lg,
                                   f"dzienny budżet zapytań wykorzystany ({budget.daily_allowance}/dzień)")
                    continue
                self._step(report, self.oddspapi, "odds", None,
                           lambda ch=chunk: self._papi_odds(ch, settings, start, end),
                           detail="brakujące kursy: " + ", ".join(lg.code for lg in chunk))
                if report.steps[-1].state != "offline":     # bez połączenia – spróbujemy przy kolejnym odświeżeniu
                    for lg in chunk:
                        self._mark_today("oddspapi", lg.code)
            gaps = self.odds_gaps(start, end, settings.markets_enabled)

        if not self.odds_api.has_key():
            return
        active = self._active_odds_sports()
        region = settings.odds.region
        order = sorted(gaps, key=lambda c: -sum(len(v) for v in gaps[c].values()))
        for code in order:
            league = leagues.get(code)
            need = [MARKET_KEYS[m] for m in (MARKET_1X2, MARKET_OU) if gaps[code].get(m)]
            if league is None or not league.odds_api_key or not need:
                continue
            if active is not None and league.odds_api_key not in active:
                self._skip(report, self.odds_api, "odds", league, "poza sezonem")
                continue
            if self._done_today("odds_api", code):
                self._skip(report, self.odds_api, "odds", league, "uzupełnione już dziś")
                continue
            cost = len(need) * len(region.split(","))
            budget = self.budget(self.odds_api)
            if not budget.allows(cost):
                self._skip(report, self.odds_api, "odds", league,
                           f"dzienny budżet kredytów wykorzystany ({budget.daily_allowance}/dzień)")
                continue
            missing = sum(len(gaps[code].get(m, [])) for m in (MARKET_1X2, MARKET_OU))
            self._step(report, self.odds_api, "odds", league,
                       lambda lg=league, mk=tuple(need): self._save(
                           self.odds_api.odds(lg, region=region, ttl=SUPPLEMENT_TTL, markets=mk)),
                       detail=f"brakujące kursy ({missing}): {', '.join(need)}")
            if report.steps[-1].state != "offline":
                self._mark_today("odds_api", code)

    def _papi_odds(self, leagues: list[League], settings: Settings, start: datetime, end: datetime) -> int:
        self._verify_tournament_ids(leagues)
        leagues = [self.leagues.get(lg.code) or lg for lg in leagues]
        slug = self.meta("oddspapi_bookmaker_slug")
        if slug is None or not str(slug).startswith(settings.odds.bookmaker.lower()):
            slug = self.oddspapi.find_bookmaker(settings.odds.bookmaker)
            self.set_meta("oddspapi_bookmaker_slug", slug)
        odds, records = self.oddspapi.odds_by_tournaments(leagues, slug, ttl=SUPPLEMENT_TTL)
        saved = self._save(records) if records else 0
        if any(self._match_for_fixture(fid) is None for fid in odds):
            # Mecze spoza znanych – terminarz ligi z OddsPapi, najwyżej raz na 3 dni (oszczędzamy limit).
            for lg in leagues:
                if self.meta(f"papi_fixtures.{lg.code}", 0) > self.clock() - PAPI_FIXTURES_DAYS * DAY:
                    continue
                if not self.budget(self.oddspapi).allows(1):
                    break
                self.set_meta(f"papi_fixtures.{lg.code}", self.clock())
                self._save(self.oddspapi.fixtures(lg, start.date(), end.date() + timedelta(days=4),
                                                  ttl=PAPI_FIXTURES_DAYS * DAY))
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

    # -- budżety i szacunki ------------------------------------------------------------
    def _app_limit(self, source: ApiSource) -> int:
        sync = self.settings().sync
        limits = {self.odds_api.name: sync.odds_api_monthly_budget, self.oddspapi.name: sync.oddspapi_monthly_budget}
        return min(limits.get(source.name, source.quota_limit or 0), source.quota_limit or 0)

    def budget(self, source: ApiSource) -> Budget:
        info = self.quota.get(source.name, "month", source.quota_limit)
        local = info.local_used
        used = max(info.used or 0, local) if info.from_headers else local
        today = self.db.query_one("SELECT COALESCE(SUM(cost), 0) AS n FROM api_calls WHERE source = ? AND ts >= ?",
                                  (source.name, period_start("day", self.clock())))
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
        return Budget(source.name, source.quota_limit or 0, self._app_limit(source), used,
                      int(today["n"]) if today else 0, days_left)

    def usage_estimates(self) -> list[UsageEstimate]:
        """Zużycie w tym miesiącu i szacunek na cały miesiąc według terminarza i reguł uzupełniania."""
        settings = self.settings()
        forecast = self._forecast(settings)
        out = []
        for src, rule in (
            (self.odds_api, "raz dziennie na ligę, tylko brakujące kursy 1X2 i powyżej/poniżej 2,5 "
                            "(np. Liga Mistrzów, powyżej/poniżej w Ekstraklasie); lista meczów bezpłatnie"),
            (self.oddspapi, "raz dziennie, do 5 lig w jednym zapytaniu – brakujące BTTS, podwójna szansa, "
                            "kursy Superbet"),
        ):
            b = self.budget(src)
            projected = min(b.app_limit, b.used_month + forecast.get(src.name, 0))
            out.append(UsageEstimate(src.name, src.label, b.plan_limit, b.app_limit, b.used_month, b.used_today,
                                     b.daily_allowance, projected, rule))
        return out

    def _forecast(self, settings: Settings) -> dict[str, int]:
        """Prognoza zapytań do końca miesiąca: dla każdego dnia ligi z meczami w horyzoncie i rynki,
        których nie dają pliki football-data.co.uk. Ligi bez znanego terminarza liczymy jako aktywne."""
        now = self.now()
        horizon = timedelta(days=max(1, settings.sync.odds_horizon_days))
        month_end = datetime(now.year, now.month, calendar.monthrange(now.year, now.month)[1], 23, 59,
                             tzinfo=timezone.utc)
        leagues = self.leagues.all(enabled_only=True)
        wanted = set(settings.markets_enabled)
        rows = self.matches.matches_between(now, month_end + horizon, leagues=[lg.code for lg in leagues],
                                            statuses=[SCHEDULED])
        kickoffs: dict[str, list[datetime]] = {}
        for r in rows:
            kickoffs.setdefault(r["league_code"], []).append(datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00")))
        odds_api = oddspapi = 0
        day = now
        first = True
        while day <= month_end:
            papi_leagues = 0
            for lg in leagues:
                ks = kickoffs.get(lg.code, [])
                known_until = max(ks) if ks else None
                active = any(day <= k <= day + horizon for k in ks) or known_until is None or day > known_until
                if not active:
                    continue
                missing = wanted - CSV_MARKETS.get(lg.fdcuk_format or "", set())
                done_api = first and self._done_today("odds_api", lg.code)
                if lg.odds_api_key and self.odds_api.has_key() and not done_api:
                    odds_api += len({MARKET_1X2, MARKET_OU} & missing)
                if lg.oddspapi_id and missing and not (first and self._done_today("oddspapi", lg.code)):
                    papi_leagues += 1
            if self.oddspapi.has_key() and settings.odds.bookmaker:
                oddspapi += math.ceil(papi_leagues / 5)
            day += timedelta(days=1)
            first = False
        return {self.odds_api.name: odds_api, self.oddspapi.name: oddspapi}

    # -- całość ----------------------------------------------------------------------
    def run_all(self, *, force: bool = False, history: bool = True, odds: bool = True) -> SyncReport:
        """Pełna synchronizacja. Wywołania są serializowane (jeden zapis naraz)."""
        with self._run_lock, self._forced(force):
            report = SyncReport(started=self.clock())
            if history:
                self.sync_history(report)
            self.sync_fixtures(report)
            if odds:
                self.sync_missing_odds(report)
            self.sync_missing_results(report)
            self.http.purge(older_than_days=30)   # stare, przeterminowane odpowiedzi z cache
            self._finish(report)
            self.set_meta("last_sync", report.to_json())
            return report

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
        for src in self.sources.values():
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


__all__ = ["Budget", "QuotaRow", "StepResult", "SyncReport", "SyncService", "UsageEstimate", "load_last_report"]
