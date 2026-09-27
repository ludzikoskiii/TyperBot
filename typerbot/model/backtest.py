"""Backtest modelu „tydzień po tygodniu” (walk-forward).

Przed każdym tygodniem model jest dopasowywany wyłącznie do meczów rozegranych
wcześniej, a potem prognozuje mecze z tego tygodnia – tak, jak działałby
w aplikacji. Wyniki porównujemy z rzeczywistością i z samym rynkiem
(prawdopodobieństwa z kursów zamknięcia po usunięciu marży).

Mierzymy:
  * skuteczność – trafność, log-loss, Brier (niżej = lepiej) vs rynek,
  * kalibrację – czy 60% w modelu to naprawdę ~60% trafień,
  * wynik finansowy – pojedyncze typy „value” i symulowane kupony, po podatku.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np

from typerbot.betting.coupon import Candidate, coupon_odds
from typerbot.betting.optimizer import alternatives
from typerbot.betting.evaluation import fair_probabilities
from typerbot.betting.odds import payout, remove_margin
from typerbot.betting.settlement import settle
from typerbot.config.settings import CouponSettings, ModelSettings, TaxSettings
from typerbot.data.db import Database
from typerbot.data.repository import odds_view
from typerbot.model.data import DAY_SECONDS, MatchTable, load_matches
from typerbot.model.markets import Key
from typerbot.model.elo import EloModel, EloTable
from typerbot.model.predictor import FittedModel, HybridModel

MARKET_KEYS: dict[str, list[Key]] = {
    "1X2": [("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0)],
    "DC": [("DC", "1X", 0.0), ("DC", "12", 0.0), ("DC", "X2", 0.0)],
    "OU": [("OU", "O", 2.5), ("OU", "U", 2.5)],
    "BTTS": [("BTTS", "Y", 0.0), ("BTTS", "N", 0.0)],
}


EDGE_BUCKETS = [("0–5%", 0.05), ("5–10%", 0.10), ("10–20%", 0.20), ("20%+", math.inf)]


@dataclass
class BacktestConfig:
    leagues: list[str]
    seasons: list[int]
    stake: float = 1.0                  # stawka pojedynczego zakładu / kuponu – 1 jednostka
    value_threshold: float = 0.0        # typ „value”: p·kurs − 1 > próg (przed podatkiem)
    bet_odds: str = "pre"               # kursy do symulacji: 'pre' (przedmeczowe) lub 'close'
    odds_haircut: float = 0.0           # obniżka kursów, np. 0.03 ≈ wyższa marża bukmachera
    include_low_data: bool = False
    min_train_matches: int = 80
    margin_method: str = "proportional"
    estimate_dc_odds: bool = True       # kursy podwójnej szansy wyliczone z 1X2 (brak w danych historycznych)


@dataclass
class EvalRow:
    match_id: int
    league: str
    season: int
    t: float
    hg: int
    ag: int
    probs: dict[Key, float]             # model
    market: dict[Key, float]            # prawdopodobieństwa rynku (kursy zamknięcia bez marży)
    bet_odds: dict[Key, float]          # kursy użyte w symulacji
    estimated: set[Key]                 # kursy wyliczone (np. podwójna szansa)
    low_data: bool
    market_pre: dict[Key, float] = field(default_factory=dict)   # rynek w chwili zakładu (kursy przedmeczowe)
    used: dict[Key, float] = field(default_factory=dict)         # prognoza: mieszanka model + rynek
    teams: tuple[int, ...] = ()                                  # drużyny (zależne typy na kuponie)

    def __post_init__(self) -> None:
        if not self.used:
            self.used = dict(self.probs)

    def result(self, key: Key) -> int | None:
        return settle(key[0], key[1], key[2], self.hg, self.ag)


@dataclass
class MarketMetrics:
    market: str
    n: int
    accuracy: float
    log_loss: float
    brier: float
    n_market: int = 0                   # mecze z kursami (porównanie z rynkiem)
    model_log_loss_on_market: float | None = None
    market_log_loss: float | None = None
    model_brier_on_market: float | None = None
    market_brier: float | None = None
    market_accuracy: float | None = None

    @property
    def beats_market(self) -> bool | None:
        if self.market_log_loss is None or self.model_log_loss_on_market is None:
            return None
        return self.model_log_loss_on_market < self.market_log_loss


@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    predicted: float
    observed: float


@dataclass
class BetStats:
    bets: int = 0
    hits: int = 0
    staked: float = 0.0
    returned: float = 0.0
    odds_sum: float = 0.0

    def add(self, stake: float, odds: float, hit: int | None, returned: float) -> None:
        self.bets += 1
        self.hits += int(bool(hit))
        self.staked += stake
        self.returned += returned
        self.odds_sum += odds

    @property
    def profit(self) -> float:
        return self.returned - self.staked

    @property
    def roi(self) -> float:
        return self.profit / self.staked if self.staked else 0.0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.bets if self.bets else 0.0

    @property
    def avg_odds(self) -> float:
        return self.odds_sum / self.bets if self.bets else 0.0

    def as_dict(self) -> dict:
        return {"bets": self.bets, "hits": self.hits, "staked": round(self.staked, 2),
                "returned": round(self.returned, 2), "profit": round(self.profit, 2), "roi": round(self.roi, 4),
                "hit_rate": round(self.hit_rate, 4), "avg_odds": round(self.avg_odds, 3)}


@dataclass
class CouponRecord:
    week: str
    selections: list[Candidate]
    odds: float
    probability: float                  # szansa trafienia wg modelu
    market_probability: float | None    # szansa trafienia wg rynku (kursy bez marży)
    hit: bool
    returned: float
    rank: int = 0                       # 0 – kupon z najwyższą szansą, 1–2 – alternatywy


def week_candidates(rows: list[EvalRow], config: BacktestConfig) -> list[Candidate]:
    """Typy z kursami z jednego tygodnia – kandydaci na kupony (tak jak w generatorze)."""
    return [
        Candidate(r.match_id, key, r.used[key], odds, r.league, key in r.estimated, r.probs.get(key),
                  r.market_pre.get(key, r.market.get(key)), r.teams)
        for r in rows if config.include_low_data or not r.low_data
        for key, odds in r.bet_odds.items() if key in r.used
    ]


@dataclass
class BacktestResult:
    config: BacktestConfig
    model: ModelSettings
    coupon_settings: CouponSettings
    tax: TaxSettings
    rows: list[EvalRow]
    metrics: dict[str, MarketMetrics]
    calibration: dict[str, list[CalibrationBin]]
    ece: dict[str, float]
    singles: BetStats
    singles_by_market: dict[str, BetStats]
    singles_by_league: dict[str, BetStats]
    singles_by_edge: dict[str, BetStats]
    singles_positive_after_tax: BetStats
    blend: list[tuple[float, float]]    # (udział modelu, log-loss 1X2) dla mieszanki model + rynek
    coupons: BetStats
    coupon_records: list[CouponRecord]
    equity_singles: list[tuple[str, float]]
    equity_coupons: list[tuple[str, float]]
    fits: int
    seconds: float
    skipped_weeks: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "matches": len(self.rows), "fits": self.fits, "seconds": round(self.seconds, 1),
            "metrics": {k: asdict(v) for k, v in self.metrics.items()},
            "ece": {k: round(v, 4) for k, v in self.ece.items()},
            "calibration": {k: [asdict(b) for b in v] for k, v in self.calibration.items()},
            "singles": self.singles.as_dict(),
            "singles_by_market": {k: v.as_dict() for k, v in self.singles_by_market.items()},
            "singles_by_league": {k: v.as_dict() for k, v in self.singles_by_league.items()},
            "singles_by_edge": {k: v.as_dict() for k, v in self.singles_by_edge.items()},
            "singles_positive_after_tax": self.singles_positive_after_tax.as_dict(),
            "blend": self.blend,
            "coupons": self.coupons.as_dict(),
            "equity_singles": self.equity_singles, "equity_coupons": self.equity_coupons,
            "notes": self.notes,
        }


# -- przebieg ------------------------------------------------------------------------
def run_backtest(db: Database, config: BacktestConfig, model_settings: ModelSettings,
                 coupon_settings: CouponSettings, tax: TaxSettings,
                 progress=None) -> BacktestResult:
    started = time.time()
    cups = {r["code"] for r in db.query("SELECT code FROM leagues WHERE is_cup = 1")}
    national = {r["code"] for r in db.query("SELECT code FROM leagues WHERE national = 1")}
    table_all = load_matches(db)
    # Ranking Elo liczony raz na całej historii (wszystkie ligi) – w każdym tygodniu używamy ocen sprzed odcięcia.
    elo_table = EloTable.build(table_all, model_settings.elo_k) if len(table_all) else None
    rows: list[EvalRow] = []
    fits = skipped = 0
    notes: list[str] = []
    total_leagues = len(config.leagues)
    for li, league in enumerate(config.leagues):
        # Liga krajowa – dopasowanie do meczów tej ligi; puchar – wspólny model wszystkich lig.
        table = table_all if league in cups else table_all.subset(table_all.league == league)
        targets = np.flatnonzero((table.league == league) & np.isin(table.season, config.seasons))
        if targets.size == 0:
            notes.append(f"{league}: brak meczów w wybranych sezonach")
            continue
        weeks: dict[float, list[int]] = defaultdict(list)
        for i in targets.tolist():
            weeks[_week_start(table.t[i])].append(i)
        odds = _load_odds(db, [int(table.match_id[i]) for i in targets.tolist()])
        previous: FittedModel | None = None
        for wi, (week, idx) in enumerate(sorted(weeks.items())):
            if progress:
                progress(league, li, total_leagues, wi, len(weeks))
            dc_model = FittedModel.fit(table, week, model_settings, previous=previous) if league not in national else None
            elo = EloModel.fit(table_all, elo_table, week, rho=dc_model.params.rho if dc_model else -0.05)
            end = int(np.searchsorted(table_all.t, week, side="left"))
            history = int((table_all.league[:end] == league).sum())
            trained = len(dc_model.window.rows) if dc_model else history
            if trained < config.min_train_matches:
                skipped += 1
                continue
            fits += 1
            previous = dc_model or previous
            model = HybridModel(dc_model, elo, model_settings, national, {league: history})
            for i in idx:
                rows.append(_evaluate(table, i, league, model, odds.get(int(table.match_id[i]), {}), config,
                                      model_settings.model_weight))
    result = _summarize(rows, config, model_settings, coupon_settings, tax)
    result.fits, result.skipped_weeks, result.seconds = fits, skipped, time.time() - started
    result.notes = notes + result.notes
    return result


def save_run(db: Database, result: BacktestResult) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO backtest_runs(created_at, config, summary) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(),
             json.dumps({"backtest": asdict(result.config), "model": asdict(result.model),
                         "coupon": asdict(result.coupon_settings)}, ensure_ascii=False),
             json.dumps(result.summary(), ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def _week_start(t_days: float) -> float:
    """Poniedziałek 00:00 UTC tygodnia, w którym jest mecz (w dniach)."""
    day = math.floor(t_days)
    weekday = (day + 3) % 7        # 1970-01-01 to czwartek
    return float(day - weekday)


def _load_odds(db: Database, match_ids: list[int]) -> dict[int, dict[str, list[dict]]]:
    out: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for i in range(0, len(match_ids), 800):
        chunk = match_ids[i:i + 800]
        for r in db.query(
            f"SELECT match_id, bookmaker, market, selection, line, kind, price FROM odds "
            f"WHERE match_id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
            out[r["match_id"]][r["kind"]].append(dict(r))
    return out


def _fair(prices: dict[Key, float], keys: list[Key], method: str) -> dict[Key, float]:
    if not all(k in prices for k in keys):
        return {}
    probs = remove_margin([prices[k] for k in keys], method)
    return dict(zip(keys, probs))


def _evaluate(table: MatchTable, i: int, league: str, model: HybridModel,
              odds: dict[str, list[dict]], config: BacktestConfig, model_weight: float = 1.0) -> EvalRow:
    neutral = bool(table.neutral[i]) if table.neutral is not None else False
    pred = model.predict(int(table.home_id[i]), int(table.away_id[i]), league, neutral=neutral)
    close = odds_view(odds.get("close") or odds.get("pre") or []).average
    pre = odds_view(odds.get(config.bet_odds) or odds.get("close") or odds.get("pre") or []).average
    market: dict[Key, float] = {}
    for m in ("1X2", "OU"):
        market.update(_fair(close, MARKET_KEYS[m], config.margin_method))
    if all(k in market for k in MARKET_KEYS["1X2"]):
        h, d, a = (market[k] for k in MARKET_KEYS["1X2"])
        market.update({("DC", "1X", 0.0): h + d, ("DC", "12", 0.0): h + a, ("DC", "X2", 0.0): d + a})
    haircut = 1.0 - config.odds_haircut
    bet = {k: p * haircut for k, p in pre.items() if k[0] in ("1X2", "OU", "BTTS", "DC")}
    estimated: set[Key] = set()
    if config.estimate_dc_odds and all(k in pre for k in MARKET_KEYS["1X2"]):
        prices = [pre[k] for k in MARKET_KEYS["1X2"]]
        margin = sum(1 / p for p in prices)
        fair = _fair(pre, MARKET_KEYS["1X2"], config.margin_method)
        h, d, a = (fair[k] for k in MARKET_KEYS["1X2"])
        for key, p in ((("DC", "1X", 0.0), h + d), (("DC", "12", 0.0), h + a), (("DC", "X2", 0.0), d + a)):
            if key not in bet:
                bet[key] = round(1.0 / (p * margin), 2) * haircut
                estimated.add(key)
    market_pre = fair_probabilities(pre, config.margin_method)
    w = min(max(model_weight, 0.0), 1.0)
    used = {k: (w * p + (1 - w) * market_pre[k]) if k in market_pre else p for k, p in pred.probs.items()}
    return EvalRow(
        match_id=int(table.match_id[i]), league=league, season=int(table.season[i]), t=float(table.t[i]),
        hg=int(table.hg[i]), ag=int(table.ag[i]), probs=pred.probs, market=market, bet_odds=bet,
        estimated=estimated, low_data=pred.low_data, market_pre=market_pre, used=used,
        teams=(int(table.home_id[i]), int(table.away_id[i])),
    )


# -- podsumowanie ------------------------------------------------------------------------
def _clip(p: float) -> float:
    return min(max(p, 1e-12), 1 - 1e-12)


def _metrics(rows: list[EvalRow], market: str) -> MarketMetrics:
    keys = MARKET_KEYS[market]
    ll, br, acc = [], [], []
    mll, mbr, macc, ll_sub, br_sub = [], [], [], [], []
    for r in rows:
        if not all(k in r.probs for k in keys):
            continue
        res = [r.result(k) for k in keys]
        if any(x is None for x in res):
            continue
        p = [r.probs[k] for k in keys]
        if market == "DC":   # trzy nakładające się zdarzenia – oceniamy każde jako tak/nie
            ll.append(np.mean([-math.log(_clip(pi)) if y else -math.log(_clip(1 - pi)) for pi, y in zip(p, res)]))
            br.append(np.mean([(pi - y) ** 2 for pi, y in zip(p, res)]))
            acc.append(np.mean([(pi >= 0.5) == bool(y) for pi, y in zip(p, res)]))
        else:
            k = res.index(1)
            ll.append(-math.log(_clip(p[k])))
            br.append(sum((pi - y) ** 2 for pi, y in zip(p, res)))
            acc.append(int(np.argmax(p) == k))
        if all(key in r.market for key in keys):
            q = [r.market[key] for key in keys]
            if market == "DC":
                mll.append(np.mean([-math.log(_clip(qi)) if y else -math.log(_clip(1 - qi)) for qi, y in zip(q, res)]))
                mbr.append(np.mean([(qi - y) ** 2 for qi, y in zip(q, res)]))
                macc.append(np.mean([(qi >= 0.5) == bool(y) for qi, y in zip(q, res)]))
            else:
                k = res.index(1)
                mll.append(-math.log(_clip(q[k])))
                mbr.append(sum((qi - y) ** 2 for qi, y in zip(q, res)))
                macc.append(int(np.argmax(q) == k))
            ll_sub.append(ll[-1])
            br_sub.append(br[-1])
    mean = lambda xs: float(np.mean(xs)) if xs else float("nan")  # noqa: E731
    out = MarketMetrics(market, len(ll), mean(acc), mean(ll), mean(br), n_market=len(mll))
    if mll:
        out.model_log_loss_on_market, out.market_log_loss = mean(ll_sub), mean(mll)
        out.model_brier_on_market, out.market_brier, out.market_accuracy = mean(br_sub), mean(mbr), mean(macc)
    return out


def _calibration(rows: list[EvalRow], market: str, bins: int = 10) -> tuple[list[CalibrationBin], float]:
    preds, hits = [], []
    for r in rows:
        for k in MARKET_KEYS[market]:
            if k in r.probs:
                y = r.result(k)
                if y is not None:
                    preds.append(r.probs[k])
                    hits.append(y)
    if not preds:
        return [], float("nan")
    p, y = np.array(preds), np.array(hits, dtype=float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    out, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        cb = CalibrationBin(b / bins, (b + 1) / bins, int(m.sum()), float(p[m].mean()), float(y[m].mean()))
        out.append(cb)
        ece += m.sum() / len(p) * abs(cb.observed - cb.predicted)
    return out, float(ece)


def _date(t: float) -> str:
    return datetime.fromtimestamp(t * DAY_SECONDS, tz=timezone.utc).strftime("%Y-%m-%d")


def _summarize(rows: list[EvalRow], config: BacktestConfig, model: ModelSettings,
               coupon_cfg: CouponSettings, tax: TaxSettings) -> BacktestResult:
    rows.sort(key=lambda r: r.t)
    metrics = {m: _metrics(rows, m) for m in MARKET_KEYS}
    calibration, ece = {}, {}
    for m in MARKET_KEYS:
        calibration[m], ece[m] = _calibration(rows, m)

    singles, positive = BetStats(), BetStats()
    by_market: dict[str, BetStats] = defaultdict(BetStats)
    by_league: dict[str, BetStats] = defaultdict(BetStats)
    by_edge: dict[str, BetStats] = {label: BetStats() for label, _ in EDGE_BUCKETS}
    equity_s: list[tuple[str, float]] = []
    tax_factor = 1.0 if tax.bookmaker_pays_tax else 1.0 - tax.stake_tax
    for r in rows:
        if r.low_data and not config.include_low_data:
            continue
        # Jeden zakład na mecz: typ z najwyższą wartością, o ile przekracza próg.
        best: tuple[float, Key] | None = None
        for key, odds in r.bet_odds.items():
            if key in r.estimated or key not in r.used or key[0] not in coupon_cfg.markets:
                continue
            edge = r.used[key] * odds - 1.0
            if edge > config.value_threshold and (best is None or edge > best[0]):
                best = (edge, key)
        if best is None:
            continue
        key = best[1]
        odds = r.bet_odds[key]
        hit = r.result(key)
        returned = config.stake if hit is None else (payout(config.stake, odds, tax) if hit else 0.0)
        bucket = next(label for label, upper in EDGE_BUCKETS if best[0] < upper)
        for stats in (singles, by_market[key[0]], by_league[r.league], by_edge[bucket]):
            stats.add(config.stake, odds, hit, returned)
        if r.used[key] * odds * tax_factor > 1.0:
            positive.add(config.stake, odds, hit, returned)
        equity_s.append((_date(r.t), round(singles.profit, 2)))

    coupons = BetStats()
    records: list[CouponRecord] = []
    equity_c: list[tuple[str, float]] = []
    weeks: dict[float, list[EvalRow]] = defaultdict(list)
    for r in rows:
        weeks[_week_start(r.t)].append(r)
    for week, wrows in sorted(weeks.items()):
        by_id = {r.match_id: r for r in wrows}
        for rank, chosen in enumerate(alternatives(week_candidates(wrows, config), coupon_cfg, coupon_cfg.alternatives)):
            results = [by_id[c.match_id].result(c.key) for c in chosen]
            total = coupon_odds([c for c, res in zip(chosen, results) if res is not None])  # zwrot = kurs 1,00
            hit = all(res in (1, None) for res in results)
            returned = payout(config.stake, total, tax) if hit else 0.0
            coupons.add(config.stake, total, hit, returned)
            market_p = [by_id[c.match_id].market.get(c.key) for c in chosen]
            records.append(CouponRecord(
                _date(week), chosen, total, float(np.prod([c.probability for c in chosen])),
                float(np.prod(market_p)) if all(x is not None for x in market_p) else None, hit, returned, rank))
            equity_c.append((_date(week), round(coupons.profit, 2)))

    notes = []
    blend = _blend_analysis(rows)
    if any(r.estimated for r in rows):
        notes.append("Kursy podwójnej szansy w danych historycznych wyliczono z kursów 1X2 (z tą samą marżą).")
    if not any(k[0] == "BTTS" for r in rows for k in r.bet_odds):
        notes.append("Brak historycznych kursów BTTS – ten rynek ma tylko ocenę trafności i kalibracji.")
    return BacktestResult(
        config=config, model=model, coupon_settings=coupon_cfg, tax=tax, rows=rows, metrics=metrics,
        calibration=calibration, ece=ece, singles=singles, singles_by_market=dict(by_market),
        singles_by_league=dict(by_league), singles_by_edge={k: v for k, v in by_edge.items() if v.bets},
        singles_positive_after_tax=positive, blend=blend, coupons=coupons,
        coupon_records=records, equity_singles=equity_s, equity_coupons=equity_c, fits=0, seconds=0.0,
        notes=notes,
    )


def _blend_analysis(rows: list[EvalRow]) -> list[tuple[float, float]]:
    """Log-loss 1X2 dla mieszanki w·model + (1−w)·rynek (kursy z chwili zakładu) –
    ile model wnosi ponad kursy. Najlepsze w to sugerowany „udział modelu”."""
    keys = MARKET_KEYS["1X2"]
    model, market, outcome = [], [], []
    for r in rows:
        mk = r.market_pre if all(k in r.market_pre for k in keys) else r.market
        if all(k in r.probs and k in mk for k in keys):
            model.append([r.probs[k] for k in keys])
            market.append([mk[k] for k in keys])
            outcome.append([r.result(k) for k in keys].index(1))
    if not model:
        return []
    pm, pk, y = np.array(model), np.array(market), np.array(outcome)
    out = []
    for w in np.linspace(0.0, 1.0, 11):
        p = w * pm + (1 - w) * pk
        out.append((round(float(w), 2), round(float(-np.mean(np.log(np.clip(p[np.arange(len(y)), y], 1e-12, 1)))), 5)))
    return out


def default_seasons(db: Database, leagues: list[str], current: int, count: int = 3) -> list[int]:
    """Ostatnie zakończone sezony z danymi dla wybranych lig (pierwszy sezon w bazie
    pomijamy – służy tylko jako historia do nauki modelu)."""
    rows = db.query(
        f"SELECT DISTINCT season FROM matches WHERE status = 'FINISHED' AND home_goals IS NOT NULL "
        f"AND league_code IN ({','.join('?' * len(leagues))}) ORDER BY season", tuple(leagues))
    seasons = [int(r["season"]) for r in rows][1:]
    return [s for s in seasons if s < current][-count:]
