"""Diagnostyka generatora kuponów.

Odpowiada na pytanie „dlaczego nie ma kuponu?”: ile meczów w zakresie dat przyszło
z każdego źródła (i z jakich dni), ile ma kursy, z kiedy są dane, ile zostaje po każdym
filtrze generatora i – gdy kuponu nie da się ułożyć – jaki jest konkretny powód i co zmienić
(np. przerwa w rozgrywkach wybranych lig i najbliższy termin meczów).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from typerbot.betting.optimizer import conflict_groups
from typerbot.config.settings import CouponSettings
from typerbot.data.errors import STATE_LABELS
from typerbot.data.quota import StatusBoard
from typerbot.data.records import SCHEDULED, parse_iso, to_iso
from typerbot.data.sources import SOURCE_LABELS
from typerbot.fmt import form, num, pct, plural
from typerbot.services.sync import SyncReport, load_last_report

if TYPE_CHECKING:
    from typerbot.config.secrets import SecretStore
    from typerbot.services.coupons import Coupon, CouponService

LOCAL = ZoneInfo("Europe/Warsaw")
STEP_LABELS = {"history": "historia", "fixtures": "terminarz", "names": "nazwy klubów"}
ODDS_STEPS = ("fixtures",)
STATE_HINTS = {
    "offline": "sprawdź połączenie z internetem i kliknij Odśwież dane – do tego czasu aplikacja używa danych z bazy",
    "error": "szczegóły w pliku typerbot.log",
}
WEEKDAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
BREAK_DAYS = 10          # przerwa w rozgrywkach: wybrane ligi nie grają co najmniej tyle dni


def day_label(d) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def when_label(ts: float | None) -> str:
    if not ts:
        return "nigdy"
    return datetime.fromtimestamp(ts, tz=LOCAL).strftime("%d.%m %H:%M")


@dataclass
class SourceCount:
    source: str
    label: str
    matches: int        # mecze w zakresie dat (wybrane ligi) znane z tego źródła
    with_odds: int      # z kursami z tego źródła
    upcoming: int       # wszystkie nadchodzące mecze z tego źródła (bez względu na daty i ligi)
    state: str
    message: str
    last_ok: float | None = None           # dane z tej chwili (ostatnia udana aktualizacja)
    days: dict[str, int] = field(default_factory=dict)    # dzień (RRRR-MM-DD) -> mecze w zakresie


@dataclass
class DayCount:
    day: str            # RRRR-MM-DD (czas polski)
    matches: int        # wszystkie ligi
    selected: int       # wybrane ligi
    with_odds: int      # wybrane ligi, z kursem bukmachera
    leagues: int        # liczba lig z meczami (wszystkie)


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
    days: list[DayCount] = field(default_factory=list)
    data_as_of: float | None = None                    # ostatnia udana aktualizacja danych

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
        lines = [f"Diagnostyka generatora ({self.period()})", f"Dane z: {when_label(self.data_as_of)}", "",
                 "Źródła – mecze w zakresie dat w wybranych ligach (z kursami):"]
        for s in self.sources:
            state = STATE_LABELS.get(s.state, s.state)
            days = ", ".join(f"{day_label(_date(d))}: {n}" for d, n in sorted(s.days.items()))
            lines.append(f"  {s.label:<22}{plural(s.matches, 'mecz', 'mecze', 'meczów')} ({s.with_odds} z kursami); "
                         f"wszystkich nadchodzących: {s.upcoming}; stan: {state}; dane z: {when_label(s.last_ok)}"
                         + (f"\n  {'':<22}dni: {days}" if days else ""))
        if self.days:
            lines += ["", "Dni w zakresie – mecze: wszystkie ligi / wybrane ligi / z kursem bukmachera (ligi):"]
            lines += [f"  {day_label(_date(d.day))}: {d.matches} / {d.selected} / {d.with_odds} ({d.leagues})"
                      for d in self.days]
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
def _date(day: str):
    from datetime import date
    return date.fromisoformat(day)


def _local_day(iso: str) -> str:
    return parse_iso(iso).astimezone(LOCAL).date().isoformat()


def diagnose(service: CouponService, cfg: CouponSettings, coupons: list[Coupon],
             secrets: SecretStore | None = None) -> Diagnosis:
    db = service.db
    start, end = service.window or service.date_window(cfg)
    leagues = service.selected_leagues(cfg)
    report = load_last_report(db)
    states = StatusBoard(db).all()

    rows = db.query("SELECT id, league_code, kickoff FROM matches WHERE status = ? AND kickoff >= ? AND kickoff < ?",
                    (SCHEDULED, to_iso(start), to_iso(end)))
    in_window = [r["id"] for r in rows]
    in_leagues = [r["id"] for r in rows if r["league_code"] in leagues]
    other_leagues = sorted({r["league_code"] for r in rows if r["league_code"] not in leagues})

    sources = _source_counts(db, in_leagues, states)
    evaluated = service._evaluated
    stages = [Stage("Mecze w bazie w zakresie dat (wszystkie ligi)", len(in_window)),
              Stage("W wybranych ligach", len(in_leagues)),
              Stage("Z prognozą modelu", len(evaluated))]
    for _key, label, cands in service.candidate_stages(cfg):
        stages.append(Stage(label, len({c.match_id for c in cands}), len(cands)))
    final = service.candidates(cfg)
    stages.append(Stage("Na kuponach", len({leg.match.match_id for c in coupons for leg in c.legs}),
                        sum(len(c.legs) for c in coupons)))

    diag = Diagnosis(start, end, sources, stages, coupons=len(coupons), problems=sync_problems(report),
                     days=_day_counts(db, rows, leagues), data_as_of=_data_as_of(states))
    _explain(diag, service, cfg, report, leagues, in_window, in_leagues, other_leagues, final, secrets)
    if not diag.ok and in_window:          # mało meczów w terminie – kiedy będzie więcej i kiedy pojawią się kursy
        diag.hints.extend(h for h in _scarcity(service, diag, leagues, len(in_window)) if h not in diag.hints)
    return diag


def _data_as_of(states) -> float | None:
    main = states.get("football_data_csv")
    stamps = [s.last_ok for s in states.values() if s.last_ok]
    return (main.last_ok if main and main.last_ok else None) or (max(stamps) if stamps else None)


def _odds_matches(db, match_ids: list[int]) -> set[int]:
    out: set[int] = set()
    for i in range(0, len(match_ids), 800):
        chunk = match_ids[i:i + 800]
        out |= {r["match_id"] for r in db.query(
            f"SELECT DISTINCT match_id FROM odds WHERE kind = 'pre' AND match_id IN ({','.join('?' * len(chunk))})",
            tuple(chunk))}
    return out


def _day_counts(db, rows, leagues: set[str]) -> list[DayCount]:
    odds = _odds_matches(db, [r["id"] for r in rows if r["league_code"] in leagues])
    days: dict[str, list] = defaultdict(list)
    for r in rows:
        days[_local_day(r["kickoff"])].append(r)
    out = []
    for day, items in sorted(days.items()):
        selected = [r for r in items if r["league_code"] in leagues]
        out.append(DayCount(day, len(items), len(selected), sum(1 for r in selected if r["id"] in odds),
                            len({r["league_code"] for r in items})))
    return out


def _source_counts(db, match_ids: list[int], states) -> list[SourceCount]:
    marks = ",".join("?" * len(match_ids))
    in_range: dict[str, int] = defaultdict(int)
    with_odds: dict[str, int] = defaultdict(int)
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if match_ids:
        for r in db.query(f"SELECT s.source, m.kickoff FROM match_sources s JOIN matches m ON m.id = s.match_id "
                          f"WHERE s.match_id IN ({marks})", tuple(match_ids)):
            in_range[r["source"]] += 1
            per_day[r["source"]][_local_day(r["kickoff"])] += 1
        for r in db.query(f"SELECT source, COUNT(DISTINCT match_id) AS n FROM odds "
                          f"WHERE kind = 'pre' AND match_id IN ({marks}) GROUP BY source", tuple(match_ids)):
            with_odds[r["source"]] = r["n"]
    upcoming = {r["source"]: r["n"] for r in db.query(
        "SELECT s.source, COUNT(*) AS n FROM match_sources s JOIN matches m ON m.id = s.match_id "
        "WHERE m.status = ? GROUP BY s.source", (SCHEDULED,))}
    out = []
    for source, label in SOURCE_LABELS.items():
        if source == "club_names":
            continue
        st = states.get(source)
        state, message = (st.state, st.message) if st else ("idle", "")
        out.append(SourceCount(source, label, in_range.get(source, 0), with_odds.get(source, 0),
                               upcoming.get(source, 0), state, message, st.last_ok if st else None,
                               dict(per_day.get(source, {}))))
    return out


def _league_names(db, codes) -> dict[str, str]:
    return {r["code"]: (f"{r['name']} ({r['country']})" if r["country"] else r["name"])
            for r in db.query("SELECT code, name, country FROM leagues") if r["code"] in set(codes)}


def _neighbours(db, leagues: set[str], start: datetime, end: datetime) -> tuple[str | None, str | None, list[str]]:
    """(ostatni mecz wybranych lig przed zakresem, najbliższy po nim, ligi grające najbliżej po zakresie)."""
    codes = sorted(leagues)
    if not codes:
        return None, None, []
    marks = ",".join("?" * len(codes))
    before = db.query_one(f"SELECT MAX(kickoff) AS k FROM matches WHERE league_code IN ({marks}) AND kickoff < ?",
                          (*codes, to_iso(start)))
    after = db.query_one(f"SELECT MIN(kickoff) AS k FROM matches WHERE status = ? AND league_code IN ({marks}) "
                         f"AND kickoff >= ?", (SCHEDULED, *codes, to_iso(end)))
    nxt = after["k"] if after else None
    first: list[str] = []
    if nxt:
        day = parse_iso(nxt)
        first = [r["league_code"] for r in db.query(
            f"SELECT DISTINCT league_code FROM matches WHERE status = ? AND league_code IN ({marks}) "
            f"AND kickoff >= ? AND kickoff < ? ORDER BY league_code",
            (SCHEDULED, *codes, to_iso(day), to_iso(day.replace(hour=23, minute=59))))]
    return (before["k"] if before else None), nxt, first


def _explain(diag: Diagnosis, service: CouponService, cfg: CouponSettings, report: SyncReport | None,
             leagues: set[str], in_window: list[int], in_leagues: list[int], other_leagues: list[str],
             final, secrets) -> None:
    reasons, hints, notes = diag.reasons, diag.hints, diag.notes
    evaluated = service._evaluated
    period = diag.period()
    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)

    # Informacje, które nie blokują kuponu: mecze bez kursów bukmacherów (kurs szacunkowy).
    no_odds = [mid for mid, (_, evals) in evaluated.items()
               if not any(e.odds and e.odds_source not in ("estimated", "manual") for e in evals)]
    if no_odds and len(no_odds) < len(evaluated):
        notes.append(f"{plural(len(no_odds), 'mecz', 'mecze', 'meczów')} bez kursów bukmacherów – mają kurs "
                     "szacunkowy z prognozy modelu (sprawdź u bukmachera). Pliki z kursami pojawiają się w piątek "
                     "(weekend) i we wtorek (środek tygodnia).")

    if diag.ok:
        if diag.coupons < cfg.alternatives:
            n = len(conflict_groups(final))
            rule = "co najmniej połowę innych meczów" if cfg.min_difference >= 0.5 else "co najmniej jeden inny mecz"
            notes.append(f"Ułożono {diag.coupons} z {cfg.alternatives} kuponów: po filtrach "
                         f"{form(n, 'został', 'zostały', 'zostało')} {plural(n, 'mecz', 'mecze', 'meczów')} z typem, "
                         f"a każdy kolejny kupon musi mieć {rule} niż poprzednie.")
            notes.extend(_scarcity(service, diag, leagues, len(in_window)))
        return

    db = service.db
    names = _league_names(db, set(leagues) | set(other_leagues))
    before, nxt, first = _neighbours(db, leagues, diag.start, diag.end)
    next_text = ""
    if nxt:
        day = parse_iso(nxt).astimezone(LOCAL).date()
        shown = ", ".join(names.get(c, c) for c in first[:4]) + (" i inne" if len(first) > 4 else "")
        next_text = f"Najbliższe mecze w wybranych ligach: {day_label(day)} ({shown})."

    # 1. Brak meczów w zakresie dat.
    if not in_window:
        if report is None and not db.query_one("SELECT 1 FROM matches LIMIT 1"):
            reasons.append(f"Brak meczów na {period}: dane nie zostały jeszcze pobrane.")
            hints.append("Kliknij „Odśwież dane” na dole okna i poczekaj na koniec pobierania.")
            return
        why = _blockers(report, ("fixtures",), leagues)
        if why:
            reasons.append(f"Brak meczów na {period}: terminarz nie został pobrany – {'; '.join(why)}. "
                           f"Aplikacja pokazuje dane z bazy z {when_label(diag.data_as_of)}.")
            hints.extend(_state_hints(report, ("fixtures",), leagues))
        else:
            reasons.append(f"Brak meczów na {period} w żadnej lidze z bazy.")
        _break_hint(reasons, hints, before, nxt)
        if next_text:
            hints.append(next_text + " Wybierz zakres „Własny” od tego dnia.")
        hints.append("Poszerz zakres dat albo kliknij „Odśwież dane”.")
        return

    # 2. Mecze są, ale w innych ligach.
    if not in_leagues:
        playing = sorted(((sum(1 for d in _day_list(db, c, diag.start, diag.end)), c) for c in other_leagues),
                         reverse=True)
        shown = ", ".join(f"{names.get(c, c)}: {n}" for n, c in playing[:6]) + (" i inne" if len(playing) > 6 else "")
        reasons.append(f"W wybranych ligach nie ma meczów na {period}. W tym terminie grają: {shown}.")
        _break_hint(reasons, hints, before, nxt)
        hints.append("Zaznacz te ligi albo całe kraje w sekcji „Zaawansowane”.")
        if next_text:
            hints.append(next_text)
        return

    # 3. Brak modelu (brak historii wyników).
    if not evaluated:
        reasons.append(f"{plural(len(in_leagues), 'mecz', 'mecze', 'meczów')} w zakresie, ale brak historii "
                       "wyników do zbudowania prognoz.")
        hints.append("Kliknij „Odśwież dane” – historia pobiera się z plików football-data.co.uk i openfootball.")
        return

    stages = {key: cands for key, _label, cands in service.candidate_stages(cfg)}
    count = {key: len({c.match_id for c in cands}) for key, cands in stages.items()}

    # 4. Brak kursów (także szacunkowych) na wybranych rynkach albo tylko kursy szacunkowe.
    if count["odds"] == 0:
        reasons.append("Brak typów na zaznaczonych rynkach.")
        hints.append("Zaznacz więcej rynków (np. 1X2 i powyżej/poniżej 2,5).")
        return
    if "estimated" in count and count["estimated"] == 0:
        days = sorted({_local_day(evaluated[m][0].kickoff) for m in evaluated})
        reasons.append(f"Żaden mecz na {period} nie ma jeszcze kursów bukmacherów "
                       f"({plural(len(evaluated), 'mecz', 'mecze', 'meczów')} tylko z terminarza: "
                       f"{', '.join(day_label(_date(d)) for d in days[:5])}).")
        hints.append("Kursy football-data.co.uk pojawiają się w piątek po południu (mecze weekendowe) i we wtorek "
                     "po południu (mecze w środku tygodnia).")
        if cfg.estimated_odds == "never":
            hints.append("Albo w „Zaawansowanych” wybierz „Kursy szacunkowe: gdy brak prawdziwych” – kupon dostanie "
                         "kursy szacunkowe z prognozy modelu (sprawdź je u bukmachera).")
        return

    # 5. Minimalne prawdopodobieństwo.
    if count["prob"] == 0:
        best = max(c.probability for c in stages["odds"])
        reasons.append(f"Żaden typ nie ma szansy co najmniej {pct(cfg.min_probability)} "
                       f"(najwyższa: {pct(best)}).")
        hints.append(f"Obniż minimalne prawdopodobieństwo typu do ok. {pct(max(0.05, best - 0.05))}.")
        return

    # 6. Zgodność modelu z rynkiem / typy z przewagą.
    if count.get("agree") == 0:
        diffs = [abs(c.divergence) for c in stages["prob"] if c.divergence is not None]
        smallest = f"{min(diffs) * 100:.0f} pkt proc." if diffs else "–"
        reasons.append(f"W każdym pasującym typie model mocno odbiega od rynku (najmniejsza różnica: {smallest}) – "
                       "takich typów nie stawiamy bez wyraźnego powodu.")
        hints.append("Zwiększ dopuszczalną różnicę model–rynek w sekcji „Zaawansowane” albo poszerz zakres dat.")
        return
    if count.get("value") == 0:
        reasons.append("Żaden pasujący typ nie ma przewagi nad kursem (EV > 0) – w trybie „Najwyższa wartość” "
                       "na kupon trafiają tylko takie typy.")
        hints.append("Wybierz tryb „Najwyższa szansa trafienia” albo poszerz zakres dat.")
        return

    # 7. Mało danych.
    if count.get("low") == 0:
        reasons.append("Wszystkie mecze z pasującymi typami dotyczą drużyn z małą liczbą danych "
                       "(np. beniaminków).")
        hints.append("Zaznacz „Dopuść drużyny z małą liczbą danych” w sekcji „Zaawansowane”.")
        return

    # 8. Kurs pojedynczego typu powyżej górnej granicy.
    if count["cap"] == 0:
        reasons.append(f"Każdy pasujący typ ma kurs wyższy niż górna granica kuponu ({num(hi)}).")
        hints.append("Podnieś kurs docelowy.")
        return

    # 9. Liczba meczów (drużyna najwyżej raz) i zakres kursu łącznego.
    groups = {i: [c.odds for c in g] for i, g in enumerate(conflict_groups(final))}
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


MIDWEEK = (1, 2, 3)          # wtorek–czwartek: kursy football-data.co.uk pojawiają się we wtorek po południu


def _busy_day(db, leagues: set[str], after: datetime, at_least: int) -> tuple[str, int] | None:
    """Pierwszy dzień po `after` z co najmniej `at_least` meczami w wybranych ligach (do 3 tygodni)."""
    codes = sorted(leagues)
    if not codes:
        return None
    rows = db.query(f"SELECT kickoff FROM matches WHERE status = ? AND league_code IN ({','.join('?' * len(codes))}) "
                    f"AND kickoff >= ? AND kickoff < ?",
                    (SCHEDULED, *codes, to_iso(after), to_iso(after + timedelta(days=21))))
    days: dict[str, int] = defaultdict(int)
    for r in rows:
        days[_local_day(r["kickoff"])] += 1
    return next(((d, n) for d, n in sorted(days.items()) if n >= at_least), None)


def _scarcity(service: CouponService, diag: Diagnosis, leagues: set[str], in_window: int) -> list[str]:
    """Dlaczego w zakresie jest mało meczów i kiedy będzie ich więcej."""
    out = []
    if in_window < 15:
        out.append(f"W tym terminie w bazie {form(in_window, 'jest', 'są', 'jest')} tylko "
                   f"{plural(in_window, 'mecz', 'mecze', 'meczów')} (wszystkie ligi) – np. przerwa reprezentacyjna "
                   "albo środek tygodnia.")
    now = service._now().astimezone(LOCAL)
    first, last = diag.start.astimezone(LOCAL).date(), diag.end.astimezone(LOCAL).date()
    tue_thu = [first + timedelta(days=i) for i in range((last - first).days + 1)
               if (first + timedelta(days=i)).weekday() in MIDWEEK]
    with_odds = {_date(d.day) for d in diag.days if d.with_odds}
    before_file = now.weekday() in (4, 5, 6, 0) or (now.weekday() == 1 and now.hour < 18)
    if tue_thu and before_file and not any(d in with_odds for d in tue_thu):
        out.append("Kursy na mecze od wtorku do czwartku football-data.co.uk publikuje we wtorek po południu – "
                   "kliknij „Odśwież dane” we wtorek wieczorem, będzie więcej meczów z kursami.")
    busy = _busy_day(service.db, leagues, diag.end, max(8, 2 * max(in_window, 1)))
    if busy:
        out.append(f"Więcej meczów w wybranych ligach: od {day_label(_date(busy[0]))} "
                   f"({plural(busy[1], 'mecz', 'mecze', 'meczów')} tego dnia) – wybierz zakres „Własny”.")
    return out


def _day_list(db, league: str, start: datetime, end: datetime) -> list[str]:
    return [r["kickoff"] for r in db.query("SELECT kickoff FROM matches WHERE status = ? AND league_code = ? "
                                           "AND kickoff >= ? AND kickoff < ?", (SCHEDULED, league, to_iso(start),
                                                                               to_iso(end)))]


def _break_hint(reasons: list[str], hints: list[str], before: str | None, nxt: str | None) -> None:
    """Wybrane ligi nie grają co najmniej BREAK_DAYS dni – najpewniej przerwa (np. reprezentacyjna)."""
    if not before or not nxt:
        return
    a, b = parse_iso(before).astimezone(LOCAL).date(), parse_iso(nxt).astimezone(LOCAL).date()
    if (b - a).days >= BREAK_DAYS:
        reasons.append(f"Wybrane ligi mają przerwę w rozgrywkach: ostatni mecz {day_label(a)}, następny "
                       f"{day_label(b)} (np. przerwa na mecze reprezentacji).")


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
