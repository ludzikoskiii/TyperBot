"""Synchronizacja danych – wyłącznie źródła bez klucza, rejestracji i limitu miesięcznego.

Każdy krok (liga × źródło) jest uruchamiany osobno: błąd jednego źródła trafia do raportu
i statusu źródła, ale nie przerywa pozostałych kroków. Wszystko, co pobrane, zostaje w bazie –
bez internetu aplikacja działa na ostatnich danych (data ostatniej udanej aktualizacji każdego
źródła jest zapisywana i pokazywana w interfejsie).
Serwis jest niezależny od Qt – w interfejsie uruchamiamy go w wątku w tle.

Role źródeł:
  * football-data.co.uk – główne: nadchodzące mecze z kursami (piątek: weekend, wtorek: środek
    tygodnia), wyniki i historia z kursami dla 38 lig w 27 krajach;
  * openfootball – terminarz całego sezonu z wyprzedzeniem i wyniki (mecze, dla których
    football-data.co.uk nie opublikował jeszcze pliku, np. w poniedziałek na środę);
  * OpenLigaDB – ligi niemieckie (Bundesliga 1–3, Puchar Niemiec) na bieżąco;
  * international_results – wyniki reprezentacji (ranking Elo);
  * openfootball/clubs – warianty nazw klubów (ujednolicanie nazw między źródłami).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from typerbot.config.leagues import FDCUK_EXTRA_COUNTRIES, League, season_of
from typerbot.config.secrets import REMOVED_SOURCES, SecretStore
from typerbot.config.settings import Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.errors import SourceError
from typerbot.data.http import HttpClient, Transport
from typerbot.data.quota import QuotaTracker, SourceState, StatusBoard
from typerbot.data.ratelimit import RateLimiter
from typerbot.data.records import SCHEDULED, MatchRecord
from typerbot.data.repository import LeagueRepository, MatchRepository
from typerbot.data.sources import (
    SOURCE_ROLES, ApiSource, ClubNames, FootballDataCsv, InternationalResults, OpenFootball, OpenLigaDb,
)
from typerbot.data.sources.club_names import CLUB_FILES
from typerbot.data.teams import normalize

log = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 24 * HOUR
CURRENT_SEASON_REFRESH = 12 * HOUR     # plik bieżącego sezonu z wynikami – co 12 godzin
MISSING_FILE_RETRY = DAY               # brak pliku sezonu w źródle – ponowna próba po dobie
CLUB_NAMES_REFRESH = 30 * DAY          # warianty nazw klubów zmieniają się rzadko
INTERNATIONAL_TTL = 3 * DAY            # zbiór wyników reprezentacji aktualizowany ok. raz w miesiącu
INTERNATIONAL_SINCE_YEARS = 12         # historia reprezentacji do rankingu Elo
EXTRA_HISTORY_SEASONS = 3              # historia lig spoza football-data.co.uk (openfootball, OpenLigaDB)
_SEVERITY = {"ok": 0, "skipped": 0, "offline": 2, "error": 4}


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
class SourceRow:
    """Stan źródła do wyświetlenia: czy włączone, stan, data ostatniej udanej aktualizacji."""
    source: str
    label: str
    role: str
    enabled: bool
    state: str
    message: str
    last_ok: float | None
    calls_today: int


class SyncService:
    def __init__(
        self,
        db: Database,
        secrets: SecretStore | None = None,
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
        self.csv = FootballDataCsv(self.http, self.quota, clock=clock)
        self.openfootball = OpenFootball(self.http, self.quota, clock=clock)
        self.openligadb = OpenLigaDb(self.http, self.quota, clock=clock)
        self.international = InternationalResults(self.http, self.quota, clock=clock)
        self.club_names = ClubNames(self.http, self.quota, clock=clock)
        self.sources: dict[str, ApiSource] = {
            s.name: s for s in (self.csv, self.openfootball, self.openligadb, self.international, self.club_names)
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
        """Klucze usuniętych źródeł kasujemy (raz) z magazynu poświadczeń – aplikacja nie używa kluczy."""
        if self.secrets is None or self.meta("removed_keys_cleared"):
            return
        for name in REMOVED_SOURCES:
            try:
                if self.secrets.get(name):
                    self.secrets.delete(name)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:  # magazyn niedostępny lub uszkodzony backend – nic nie szkodzi
                log.debug("Nie udało się usunąć klucza %s", name, exc_info=True)
                return
        self.set_meta("removed_keys_cleared", True)

    def enabled_sources(self, settings: Settings | None = None) -> dict[str, bool]:
        sync = (settings or self.settings()).sync
        return {self.csv.name: True, self.openfootball.name: sync.openfootball, self.openligadb.name: sync.openligadb,
                self.international.name: sync.international, self.club_names.name: True}

    def _step(self, report: SyncReport, source: ApiSource, step: str, league: League | None,
              fn: Callable[[], int], detail: str = "") -> int | None:
        code = league.code if league else None
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

    def _file_row(self, source: str, code: str, season: int):
        return self.db.query_one(
            "SELECT fetched_at, complete, rows FROM history_files WHERE source = ? AND league_code = ? AND season = ?",
            (source, code, season))

    def _file_fresh(self, source: str, code: str, season: int, refresh: float) -> bool:
        """Plik zakończonego sezonu pobieramy raz; bieżący – co `refresh` sekund; brakujący – raz na dobę."""
        row = self._file_row(source, code, season)
        if row is None:
            return False
        if row["complete"]:
            return True
        age = self.clock() - row["fetched_at"]
        return age < (MISSING_FILE_RETRY if row["rows"] < 0 else refresh)

    def _mark_file(self, source: str, code: str, season: int, rows: int, complete: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO history_files(source, league_code, season, fetched_at, rows, complete) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, league_code, season) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, rows = excluded.rows, complete = excluded.complete",
                (source, code, season, self.clock(), rows, int(complete)),
            )

    def _seasons(self, league: League, count: int) -> list[int]:
        """Sezony od bieżącego wstecz (bieżący pierwszy – aplikacja szybciej ma aktualne dane)."""
        current = league.season_at(self.now())
        return list(range(current, current - count, -1))

    # -- nazwy klubów -------------------------------------------------------------------
    def sync_club_names(self, report: SyncReport) -> None:
        """Warianty nazw klubów (openfootball/clubs) dla krajów z aktywnymi ligami – raz na 30 dni."""
        countries = sorted({lg.country for lg in self.leagues.all(enabled_only=True)} & set(CLUB_FILES))
        for country in countries:
            last = self.meta(f"club_names.{country}", 0)
            if last > self.clock() - CLUB_NAMES_REFRESH and not self.club_names.force_refresh:
                continue
            count = self._step(report, self.club_names, "names", None,
                               lambda c=country: self._save_club_names(c), detail=country)
            if count is not None:
                self.set_meta(f"club_names.{country}", self.clock())

    def _save_club_names(self, country: str) -> int:
        clubs = self.club_names.country(country, ttl=CLUB_NAMES_REFRESH)
        if not clubs:
            return 0
        variants: dict[str, str | None] = {}
        for canonical, names in clubs:
            for name in (canonical, *names):
                key = normalize(name)
                if not key:
                    continue
                if key in variants and variants[key] != canonical:
                    variants[key] = None           # ten sam wariant dla dwóch klubów – niejednoznaczny
                else:
                    variants.setdefault(key, canonical)
        rows = [(country, k, v) for k, v in variants.items() if v]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM club_names WHERE country = ?", (country,))
            conn.executemany("INSERT INTO club_names(country, variant, canonical) VALUES (?, ?, ?)", rows)
        self.matches.matcher.clear_cache()
        return len(rows)

    # -- historia --------------------------------------------------------------------
    def sync_history(self, report: SyncReport) -> None:
        """Wyniki (z kursami) do modelu, rankingu Elo i backtestu. Zakończone sezony pobieramy raz na zawsze,
        bieżący co 12 godzin. Najpierw bieżące sezony wszystkich lig, potem starsze."""
        settings = self.settings()
        use = self.enabled_sources(settings)
        leagues = self.leagues.all(enabled_only=True)
        n_csv = max(1, settings.sync.csv_seasons)
        plan: list[tuple[int, Callable[[], None]]] = []
        for league in leagues:
            if league.fdcuk_format == "main" and league.fdcuk_code:
                for i, season in enumerate(self._seasons(league, n_csv)):
                    plan.append((i, lambda lg=league, s=season, past=i > 0: self._csv_main(report, lg, s, past)))
            elif league.fdcuk_format == "extra" and league.fdcuk_code:
                plan.append((0, lambda lg=league: self._csv_extra(report, lg, n_csv)))
            else:     # ligi spoza football-data.co.uk – historia z openfootball albo OpenLigaDB
                for i, season in enumerate(self._seasons(league, EXTRA_HISTORY_SEASONS)):
                    if league.openligadb and use[self.openligadb.name]:
                        plan.append((i, lambda lg=league, s=season, past=i > 0: self._season_file(
                            report, self.openligadb, lg, s, past)))
                    elif league.openfootball and use[self.openfootball.name]:
                        plan.append((i, lambda lg=league, s=season, past=i > 0: self._season_file(
                            report, self.openfootball, lg, s, past)))
        for _, fn in sorted(plan, key=lambda x: x[0]):
            fn()
        if use[self.international.name] and any(lg.national for lg in leagues):
            self._international(report)

    def _csv_main(self, report: SyncReport, league: League, season: int, past: bool) -> None:
        if self._file_fresh(self.csv.name, league.code, season, CURRENT_SEASON_REFRESH):
            return

        def fetch() -> int:
            try:
                records = self.csv.season(league, season, ttl=0)
            except SourceError as exc:
                if "404" not in exc.message:
                    raise
                # brak pliku: zakończony sezon (liga nieobjęta) – nie pytamy ponownie; bieżący – jeszcze nie ma
                self._mark_file(self.csv.name, league.code, season, -1, complete=past)
                return 0
            count = self._save(records)
            self._mark_file(self.csv.name, league.code, season, count, complete=past)
            return count

        self._step(report, self.csv, "history", league, fetch, detail=f"sezon {league.season_label(season)}")

    def _csv_extra(self, report: SyncReport, league: League, n_seasons: int) -> None:
        if self._file_fresh(self.csv.name, league.code, 0, CURRENT_SEASON_REFRESH):
            return
        current = league.season_at(self.now())
        seasons = set(range(current - n_seasons + 1, current + 1))

        def fetch() -> int:
            count = self._save(self.csv.extra(league, seasons, ttl=0))
            self._mark_file(self.csv.name, league.code, 0, count, complete=False)
            return count

        self._step(report, self.csv, "history", league, fetch)

    def _season_file(self, report: SyncReport, source: ApiSource, league: League, season: int, past: bool,
                     refresh: float = CURRENT_SEASON_REFRESH, step: str = "history") -> None:
        """Plik sezonu z openfootball albo OpenLigaDB (terminarz i wyniki)."""
        if self._file_fresh(source.name, league.code, season, refresh):
            return

        def fetch() -> int:
            try:
                records = source.season(league, season, ttl=0)
            except SourceError as exc:
                if "404" not in exc.message:
                    raise
                self._mark_file(source.name, league.code, season, -1, complete=False)
                return 0
            count = self._save(records)
            if source is self.openfootball:
                self._prune_stale(source.name, league.code, season, {r.external_id for r in records})
            finished = past and records and all(r.status != SCHEDULED for r in records)
            self._mark_file(source.name, league.code, season, count, complete=bool(finished))
            return count

        self._step(report, source, step, league, fetch, detail=f"sezon {league.season_label(season)}")

    def _prune_stale(self, source: str, league: str, season: int, current_ids: set[str]) -> None:
        """Mecze usunięte z terminarza źródła (np. przełożone na nową kolejkę): powiązanie ze źródłem znika,
        a mecz znany tylko z tego źródła – z bazy (inaczej wisiałby jako nierozegrany)."""
        prefix = f"{league}:{season}:"
        rows = self.db.query(
            "SELECT s.external_id, s.match_id FROM match_sources s JOIN matches m ON m.id = s.match_id "
            "WHERE s.source = ? AND s.external_id LIKE ? AND m.status = ?", (source, prefix + "%", SCHEDULED))
        stale = [r for r in rows if r["external_id"] not in current_ids]
        if not stale:
            return
        with self.matches.write_lock, self.db.transaction() as conn:
            for r in stale:
                conn.execute("DELETE FROM match_sources WHERE source = ? AND external_id = ?",
                             (source, r["external_id"]))
                others = conn.execute("SELECT COUNT(*) FROM match_sources WHERE match_id = ?",
                                      (r["match_id"],)).fetchone()[0]
                used = conn.execute("SELECT COUNT(*) FROM coupon_legs WHERE match_id = ?",
                                    (r["match_id"],)).fetchone()[0]
                if not others and not used:
                    conn.execute("DELETE FROM matches WHERE id = ?", (r["match_id"],))

    def _international(self, report: SyncReport) -> None:
        if self._file_fresh(self.international.name, "INT", 0, INTERNATIONAL_TTL):
            return
        since = date(self.now().year - INTERNATIONAL_SINCE_YEARS, 1, 1)

        def fetch() -> int:
            count = self._save(self.international.results(since, ttl=0))
            self._mark_file(self.international.name, "INT", 0, count, complete=False)
            return count

        self._step(report, self.international, "history", self.leagues.get("INT"), fetch)

    # -- terminarz -------------------------------------------------------------------------
    def sync_fixtures(self, report: SyncReport) -> None:
        """Nadchodzące mecze: football-data.co.uk (z kursami), openfootball (terminarz z wyprzedzeniem),
        OpenLigaDB (ligi niemieckie). Ligi nowe w plikach football-data.co.uk dopisujemy automatycznie."""
        settings = self.settings()
        use = self.enabled_sources(settings)
        now = self.now()
        ttl = max(0.5, settings.sync.fixtures_every_hours) * HOUR
        leagues = self.leagues.all()
        by_main = {lg.fdcuk_code: lg for lg in leagues if lg.fdcuk_format == "main" and lg.fdcuk_code}
        by_extra = {lg.fdcuk_code: lg for lg in leagues if lg.fdcuk_format == "extra" and lg.fdcuk_code}
        discovered: list[str] = []

        def main_league(div: str) -> League | None:
            lg = by_main.get(div)
            if lg is None:
                lg = by_main[div] = self.leagues.add_discovered(League(
                    div, f"Liga {div}", "Inne", fdcuk_code=div, fdcuk_format="main"))
                discovered.append(lg.code)
            return lg if lg.enabled else None

        def extra_league(country: str, name: str) -> League | None:
            code = FDCUK_EXTRA_COUNTRIES.get(country)
            lg = by_extra.get(code) if code else None
            if lg is None:
                key = code or ("X" + "".join(ch for ch in country.upper() if ch.isalpha())[:4])
                lg = by_extra[key] = self.leagues.add_discovered(League(
                    key, name or country, country, fdcuk_code=code, fdcuk_format="extra" if code else None,
                    season_style="calendar", timezone="UTC"))
                discovered.append(lg.code)
            return lg if lg.enabled else None

        self._step(report, self.csv, "fixtures", None,
                   lambda: self._save(self.csv.upcoming_main(main_league, now, ttl=ttl)), detail="fixtures.csv")
        self._step(report, self.csv, "fixtures", None,
                   lambda: self._save(self.csv.upcoming_extra(extra_league, now, ttl=ttl)),
                   detail="new_league_fixtures.csv")
        if discovered:
            log.info("Nowe ligi w plikach football-data.co.uk: %s", ", ".join(discovered))

        for league in self.leagues.all(enabled_only=True):
            for season in self._fixture_seasons(league, now):
                if league.openligadb and use[self.openligadb.name]:
                    self._season_file(report, self.openligadb, league, season, past=False, refresh=ttl,
                                      step="fixtures")
                if league.openfootball and use[self.openfootball.name]:
                    self._season_file(report, self.openfootball, league, season, past=False,
                                      refresh=CURRENT_SEASON_REFRESH, step="fixtures")

    @staticmethod
    def _fixture_seasons(league: League, now: datetime) -> list[int]:
        """Bieżący sezon, a na przełomie sezonów także następny (terminarz nowego sezonu pojawia się wcześniej)."""
        current = league.season_at(now)
        edge = now.month in (5, 6, 7) if league.season_style == "split" else now.month == 12
        return [current, current + 1] if edge else [current]

    # -- całość ----------------------------------------------------------------------
    def run_all(self, *, force: bool = False, history: bool = True, odds: bool = True) -> SyncReport:
        """Pełna synchronizacja. Wywołania są serializowane (jeden zapis naraz).
        (`odds` – zgodność wsteczna: kursy przychodzą razem z terminarzem football-data.co.uk.)"""
        with self._run_lock, self._forced(force):
            report = SyncReport(started=self.clock())
            self.sync_club_names(report)
            self.sync_fixtures(report)       # najpierw terminarz (szybko), potem historia (pierwszy raz – minuty)
            if history:
                self.sync_history(report)
            self.http.purge(older_than_days=30)   # stare, przeterminowane odpowiedzi z cache
            self._finish(report)
            self.set_meta("last_sync", report.to_json())
            return report

    def run(self, fn: Callable[[SyncReport], None], *, force: bool = False) -> SyncReport:
        """Pojedynczy krok z tą samą obsługą błędów i statusów."""
        with self._run_lock, self._forced(force):
            report = SyncReport(started=self.clock())
            fn(report)
            return self._finish(report)

    # -- stan źródeł ----------------------------------------------------------------------
    def source_rows(self) -> list[SourceRow]:
        states: dict[str, SourceState] = self.status.all()
        use = self.enabled_sources()
        rows = []
        for src in self.sources.values():
            st = states.get(src.name)
            state, message = (st.state, st.message) if st else ("idle", "")
            if not use.get(src.name, True):
                state, message = "disabled", "wyłączone w ustawieniach"
            rows.append(SourceRow(src.name, src.label, SOURCE_ROLES.get(src.name, ""), use.get(src.name, True),
                                  state, message, st.last_ok if st else None, self.quota.calls_today(src.name)))
        return rows

    def data_as_of(self) -> float | None:
        """Chwila ostatniej udanej aktualizacji głównego źródła (terminarz z kursami)."""
        st = self.status.get(self.csv.name)
        return st.last_ok if st else None


__all__ = ["SourceRow", "StepResult", "SyncReport", "SyncService", "load_last_report"]
