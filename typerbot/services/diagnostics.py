"""Diagnostyka generatora kuponów.

Odpowiada na pytanie „dlaczego nie ma kuponu?”: ile meczów w zakresie dat przyszło
z każdego źródła, ile ma kursy, ile zostaje po każdym filtrze generatora i – gdy
kuponu nie da się ułożyć – jaki jest konkretny powód i co zmienić.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from typerbot.config.secrets import KEYED_SOURCES
from typerbot.config.settings import CouponSettings
from typerbot.data.errors import STATE_LABELS
from typerbot.data.quota import StatusBoard
from typerbot.data.records import SCHEDULED, to_iso
from typerbot.data.sources import SOURCE_LABELS
from typerbot.fmt import num, pct, plural
from typerbot.services.sync import SyncReport, load_last_report

if TYPE_CHECKING:
    from typerbot.config.secrets import SecretStore
    from typerbot.services.coupons import Coupon, CouponService

LOCAL = ZoneInfo("Europe/Warsaw")
STEP_LABELS = {"history": "historia", "fixtures": "terminarz", "odds": "brakujące kursy", "results": "wyniki"}
ODDS_STEPS = ("odds",)
STATE_HINTS = {
    "no_key": "wpisz klucz w Ustawieniach",
    "auth": "sprawdź klucz w Ustawieniach (bez spacji)",
    "quota": "limit odnowi się w następnym okresie – do tego czasu aplikacja korzysta z innych źródeł",
    "plan": "niedostępne w darmowym planie – aplikacja korzysta z innych źródeł",
    "offline": "sprawdź połączenie z internetem i kliknij Odśwież dane",
    "error": "szczegóły w pliku typerbot.log",
}


@dataclass
class SourceCount:
    source: str
    label: str
    matches: int        # mecze w zakresie dat (wybrane ligi) znane z tego źródła
    with_odds: int      # z kursami z tego źródła
    upcoming: int       # wszystkie nadchodzące mecze z tego źródła (bez względu na daty i ligi)
    state: str
    message: str


@dataclass
class Stage:
    label: str
    matches: int
    selections: int | None = None


@dataclass
class Problem:
    source: str
    step: str
    leagues: list[str]
    state: str
    message: str

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def hint(self) -> str:
        return STATE_HINTS.get(self.state, "")

    def text(self) -> str:
        where = f" [{', '.join(self.leagues)}]" if self.leagues else ""
        state = STATE_LABELS.get(self.state, self.state)
        msg = f" – {self.message}" if self.message and not self.message.startswith(state) else ""
        return f"{self.label}, {STEP_LABELS.get(self.step, self.step)}{where}: {state}{msg}"


@dataclass
class Diagnosis:
    start: datetime
    end: datetime
    sources: list[SourceCount]
    stages: list[Stage]
    coupons: int = 0
    reasons: list[str] = field(default_factory=list)   # dlaczego nie ma (pełnego) wyniku
    hints: list[str] = field(default_factory=list)     # co zmienić
    notes: list[str] = field(default_factory=list)     # informacje, które nie blokują kuponu
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.coupons > 0

    def headline(self) -> str:
        if self.reasons and not self.ok:
            return self.reasons[0]
        return f"Ułożono {plural(self.coupons, 'kupon', 'kupony', 'kuponów')}."

    def period(self) -> str:
        fmt = "%d.%m %H:%M"
        return f"{self.start.astimezone(LOCAL).strftime(fmt)} – {self.end.astimezone(LOCAL).strftime(fmt)}"

    def to_text(self) -> str:
        lines = [f"Diagnostyka generatora ({self.period()})", "", "Źródła – mecze w zakresie dat (z kursami):"]
        for s in self.sources:
            state = STATE_LABELS.get(s.state, s.state)
            lines.append(f"  {s.label:<22}{plural(s.matches, 'mecz', 'mecze', 'meczów')} ({s.with_odds} z kursami); "
                         f"wszystkich nadchodzących: {s.upcoming}; stan: {state}")
        lines += ["", "Po kolejnych filtrach:"]
        for st in self.stages:
            extra = f", {plural(st.selections, 'typ', 'typy', 'typów')}" if st.selections is not None else ""
            lines.append(f"  {st.label:<60}{plural(st.matches, 'mecz', 'mecze', 'meczów')}{extra}")
        if self.reasons:
            lines += ["", "Powód:" if not self.ok else "Uwagi:"] + [f"  • {r}" for r in self.reasons]
        if self.hints:
            lines += ["", "Co zmienić:"] + [f"  • {h}" for h in self.hints]
        if self.notes:
            lines += ["", "Informacje:"] + [f"  • {n}" for n in self.notes]
        if self.problems:
            lines += ["", f"Problemy ze źródeł przy ostatniej synchronizacji ({len(self.problems)}):"]
            lines += [f"  • {p.text()}" + (f" → {p.hint}" if p.hint else "") for p in self.problems]
        return "\n".join(lines)


# -- problemy ze źródeł ----------------------------------------------------------------------------
def sync_problems(report: SyncReport | None) -> list[Problem]:
    """Problemy z ostatniej synchronizacji – ten sam problem w kilku ligach to jeden wpis."""
    if report is None:
        return []
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    for st in report.steps:
        if st.ok:
            continue
        key = (st.source, st.step, st.state, st.message)
        grouped.setdefault(key, [])
        if st.league and st.league not in grouped[key]:
            grouped[key].append(st.league)
    return [Problem(src, step, leagues, state, msg) for (src, step, state, msg), leagues in grouped.items()]


def _blockers(report: SyncReport | None, steps: tuple[str, ...], leagues: set[str]) -> list[str]:
    """Opisy nieudanych kroków (np. kursy) dotyczących danych lig – do powodu w diagnostyce."""
    if report is None:
        return []
    out: dict[str, list[str]] = {}
    for st in report.steps:
        if st.step not in steps or st.ok or (st.league and st.league not in leagues):
            continue
        label = SOURCE_LABELS.get(st.source, st.source)
        state = STATE_LABELS.get(st.state, st.state)
        state = state[:1].lower() + state[1:]
        if st.state == "quota":
            state = "wyczerpany limit"
        text = f"{label} – {state}"
        out.setdefault(text, [])
        if st.league:
            out[text].append(st.league)
    return [t + (f" ({', '.join(sorted(set(lg)))})" if lg else "") for t, lg in out.items()]


# -- diagnostyka --------------------------------------------------------------------------------------
def diagnose(service: CouponService, cfg: CouponSettings, coupons: list[Coupon],
             secrets: SecretStore | None = None) -> Diagnosis:
    db = service.db
    start, end = service.window or service.date_window(cfg)
    leagues = service.selected_leagues(cfg)
    report = load_last_report(db)
    states = StatusBoard(db).all()

    rows = db.query("SELECT id, league_code FROM matches WHERE status = ? AND kickoff >= ? AND kickoff < ?",
                    (SCHEDULED, to_iso(start), to_iso(end)))
    in_window = [r["id"] for r in rows]
    in_leagues = [r["id"] for r in rows if r["league_code"] in leagues]
    other_leagues = sorted({r["league_code"] for r in rows if r["league_code"] not in leagues})

    sources = _source_counts(db, in_leagues, states, secrets)
    evaluated = service._evaluated
    stages = [Stage("Mecze w bazie w zakresie dat (wszystkie ligi)", len(in_window)),
              Stage("W wybranych ligach", len(in_leagues)),
              Stage("Z prognozą modelu", len(evaluated))]
    for label, cands in service.candidate_stages(cfg):
        stages.append(Stage(label, len({c.match_id for c in cands}), len(cands)))
    final = service.candidates(cfg)
    stages.append(Stage("Na kuponach", len({leg.match.match_id for c in coupons for leg in c.legs}),
                        sum(len(c.legs) for c in coupons)))

    diag = Diagnosis(start, end, sources, stages, coupons=len(coupons), problems=sync_problems(report))
    _explain(diag, service, cfg, report, leagues, in_window, in_leagues, other_leagues, final, secrets)
    return diag


def _source_counts(db, match_ids: list[int], states, secrets=None) -> list[SourceCount]:
    marks = ",".join("?" * len(match_ids))
    in_range: dict[str, int] = defaultdict(int)
    with_odds: dict[str, int] = defaultdict(int)
    if match_ids:
        for r in db.query(f"SELECT source, COUNT(DISTINCT match_id) AS n FROM match_sources "
                          f"WHERE match_id IN ({marks}) GROUP BY source", tuple(match_ids)):
            in_range[r["source"]] = r["n"]
        for r in db.query(f"SELECT source, COUNT(DISTINCT match_id) AS n FROM odds "
                          f"WHERE kind = 'pre' AND match_id IN ({marks}) GROUP BY source", tuple(match_ids)):
            with_odds[r["source"]] = r["n"]
    upcoming = {r["source"]: r["n"] for r in db.query(
        "SELECT s.source, COUNT(*) AS n FROM match_sources s JOIN matches m ON m.id = s.match_id "
        "WHERE m.status = ? GROUP BY s.source", (SCHEDULED,))}
    out = []
    for source, label in SOURCE_LABELS.items():
        st = states.get(source)
        state, message = (st.state, st.message) if st else ("idle", "")
        if secrets is not None and source in KEYED_SOURCES and not secrets.get(source):
            state, message = "no_key", STATE_LABELS["no_key"]
        out.append(SourceCount(source, label, in_range.get(source, 0), with_odds.get(source, 0),
                               upcoming.get(source, 0), state, message))
    return out


def _explain(diag: Diagnosis, service: CouponService, cfg: CouponSettings, report: SyncReport | None,
             leagues: set[str], in_window: list[int], in_leagues: list[int], other_leagues: list[str],
             final, secrets) -> None:
    reasons, hints, notes = diag.reasons, diag.hints, diag.notes
    evaluated = service._evaluated
    period = diag.period()
    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)

    # Informacje, które nie blokują kuponu: mecze bez kursów.
    no_odds = [mid for mid, (_, evals) in evaluated.items() if not any(e.odds for e in evals)]
    if no_odds and len(no_odds) < len(evaluated):
        why = _blockers(report, ODDS_STEPS, {evaluated[m][0].league for m in no_odds})
        notes.append(f"{plural(len(no_odds), 'mecz', 'mecze', 'meczów')} bez kursów"
                     + (f": {'; '.join(why)}" if why else " – kursy zwykle pojawiają się 2–5 dni przed meczem") + ".")

    if diag.ok:
        if diag.coupons < cfg.alternatives:
            notes.append(f"Ułożono {diag.coupons} z {cfg.alternatives} kuponów – za mało różnych meczów, żeby "
                         f"kolejny kupon różnił się od poprzednich co najmniej w {pct(cfg.min_difference)} meczów.")
        return

    # 1. Brak meczów w zakresie dat.
    if not in_window:
        if report is None:
            reasons.append(f"Brak meczów na {period}: dane nie zostały jeszcze pobrane.")
            hints.append("Kliknij „Odśwież dane” na dole okna i poczekaj na koniec pobierania.")
            return
        why = _blockers(report, ("fixtures",), leagues)
        if why:
            reasons.append(f"Brak meczów na {period}: terminarz nie został pobrany – {'; '.join(why)}.")
            hints.extend(_state_hints(report, ("fixtures",), leagues))
        else:
            reasons.append(f"Brak meczów na {period}: źródła nie zwróciły żadnego meczu w tym terminie "
                           "(np. przerwa na mecze reprezentacji).")
        hints.append("Poszerz zakres dat (np. „Najbliższe 3 dni” lub własny zakres) albo kliknij „Odśwież dane”.")
        return

    # 2. Mecze są, ale w innych ligach.
    if not in_leagues:
        reasons.append(f"W wybranych ligach brak meczów na {period} "
                       f"(mecze są w: {', '.join(other_leagues)}).")
        hints.append("Zaznacz więcej lig w sekcji „Zaawansowane”.")
        return

    # 3. Brak modelu (brak historii wyników).
    if not evaluated:
        reasons.append(f"{plural(len(in_leagues), 'mecz', 'mecze', 'meczów')} w zakresie, ale brak historii "
                       "wyników do zbudowania prognoz.")
        hints.append("Kliknij „Odśwież dane” – historia pobiera się z plików football-data.co.uk.")
        return

    stages = service.candidate_stages(cfg)
    counts = [len({c.match_id for c in cands}) for _, cands in stages]

    # 4. Brak kursów.
    if counts[0] == 0:
        with_any = [mid for mid, (_, evals) in evaluated.items() if any(e.odds for e in evals)]
        if with_any:
            reasons.append(f"Kursy są tylko dla rynków, których nie zaznaczono ({plural(len(with_any), 'mecz', 'mecze', 'meczów')}).")
            hints.append("Zaznacz więcej rynków (np. 1X2 i powyżej/poniżej 2,5).")
            return
        why = _blockers(report, ODDS_STEPS, {info.league for info, _ in evaluated.values()})
        horizon = service.settings().sync.odds_horizon_days
        reasons.append(f"Brak kursów dla {plural(len(evaluated), 'meczu', 'meczów', 'meczów')}"
                       + (f": {'; '.join(why)}." if why else " – kursy nie zostały jeszcze pobrane."))
        hints.extend(_state_hints(report, ODDS_STEPS, leagues))
        hints.append("Kliknij „Odśwież dane”. Pliki football-data.co.uk z kursami pojawiają się zwykle 2–4 dni "
                     f"przed kolejką, a brakujące kursy uzupełniamy dla meczów z najbliższych {horizon} dni – "
                     "wybierz bliższy zakres dat.")
        return

    # 5. Minimalne prawdopodobieństwo.
    if counts[1] == 0:
        best = max(c.probability for c in stages[0][1])
        reasons.append(f"Żaden typ nie ma szansy co najmniej {pct(cfg.min_probability)} "
                       f"(najwyższa: {pct(best)}).")
        hints.append(f"Obniż minimalne prawdopodobieństwo typu do ok. {pct(max(0.05, best - 0.05))}.")
        return

    # 6. Mało danych.
    idx = 2
    if not cfg.include_low_data:
        if counts[2] == 0:
            reasons.append("Wszystkie mecze z pasującymi typami dotyczą drużyn z małą liczbą danych "
                           "(np. beniaminków).")
            hints.append("Zaznacz „Dopuść drużyny z małą liczbą danych” w sekcji „Zaawansowane”.")
            return
        idx = 3

    # 7. Kurs pojedynczego typu powyżej górnej granicy.
    if counts[idx] == 0:
        reasons.append(f"Każdy pasujący typ ma kurs wyższy niż górna granica kuponu ({num(hi)}).")
        hints.append("Podnieś kurs docelowy.")
        return

    # 8. Liczba meczów i zakres kursu łącznego.
    groups: dict[int, list[float]] = defaultdict(list)
    for c in final:
        groups[c.match_id].append(c.odds)
    n = len(groups)
    if n < cfg.min_events:
        reasons.append(f"Tylko {plural(n, 'mecz ma', 'mecze mają', 'meczów ma')} typ spełniający warunki, "
                       f"a minimalna liczba zdarzeń to {cfg.min_events}.")
        hints.append("Zmniejsz minimalną liczbę zdarzeń, poszerz zakres dat albo obniż minimalne prawdopodobieństwo.")
        return
    k_max = min(cfg.max_events, n)
    top = sorted((max(v) for v in groups.values()), reverse=True)[:k_max]
    max_odds = math.prod(top)
    low = sorted(min(v) for v in groups.values())[:max(cfg.min_events, 1)]
    min_odds = math.prod(low)
    target = f"{num(cfg.target_odds)} ±{pct(cfg.tolerance)} ({num(max(lo, 1.01))}–{num(hi)})"
    if max_odds < lo:
        reasons.append(f"Żadna kombinacja nie mieści się w kursie {target}: najwyższy możliwy kurs to "
                       f"{num(max_odds)} przy {plural(k_max, 'zdarzeniu', 'zdarzeniach', 'zdarzeniach')}.")
        more = ("zwiększ maksymalną liczbę zdarzeń" if k_max == cfg.max_events
                else "poszerz zakres dat lub dodaj ligi (więcej meczów)")
        hints.append(f"Obniż kurs docelowy do najwyżej {num(max_odds)}, {more} albo obniż minimalne "
                     "prawdopodobieństwo (więcej typów z wyższym kursem).")
    elif min_odds > hi:
        reasons.append(f"Żadna kombinacja nie mieści się w kursie {target}: najniższy możliwy kurs przy "
                       f"{plural(cfg.min_events, 'zdarzeniu', 'zdarzeniach', 'zdarzeniach')} to {num(min_odds)}.")
        hints.append("Podnieś kurs docelowy albo zmniejsz minimalną liczbę zdarzeń.")
    else:
        reasons.append(f"Żadna kombinacja typów nie trafia w kurs {target} "
                       f"(możliwe kursy: {num(min_odds)}–{num(max_odds)}, ale bez kombinacji w tym przedziale).")
        hints.append("Zwiększ tolerancję (np. do 15–20%) albo zmień kurs docelowy.")


def _state_hints(report: SyncReport | None, steps: tuple[str, ...], leagues: set[str]) -> list[str]:
    if report is None:
        return []
    out: list[str] = []
    for st in report.steps:
        if st.step in steps and not st.ok and (not st.league or st.league in leagues):
            hint = STATE_HINTS.get(st.state)
            text = f"{SOURCE_LABELS.get(st.source, st.source)}: {hint}." if hint else ""
            if text and text not in out:
                out.append(text)
    return out
