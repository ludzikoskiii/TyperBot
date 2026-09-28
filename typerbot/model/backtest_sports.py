"""Backtest modelu wyników dla dyscyplin innych niż piłka nożna.

Co tydzień model jest dopasowywany na meczach sprzed tego tygodnia („bez podglądania przyszłości”)
i prognozuje mecze tygodnia. Porównujemy go z:
  * prognozą naiwną – odsetkiem zwycięstw gospodarzy w historii (zwycięzca / 1X2),
  * rynkiem – kursami zamknięcia bez marży (gdy źródło je podaje, np. NFL w nflverse).
Miary: log loss i Brier (niżej = lepiej), trafność i kalibracja – średnia prognoza a odsetek trafień.
Rynek zwykle wygrywa z modelem – dlatego przy dostępnych kursach prognoza opiera się na rynku.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from typerbot.betting.odds import remove_margin
from typerbot.config.sports import SPORTS
from typerbot.data.db import Database
from typerbot.model.data import load_matches
from typerbot.model.scores import ScoreModel

WEEK = 7.0


@dataclass
class MarketScore:
    n: int = 0
    log_loss: float = 0.0
    brier: float = 0.0
    hits: int = 0

    def add(self, p: float, won: bool) -> None:
        p = min(max(p, 1e-6), 1 - 1e-6)
        self.n += 1
        self.log_loss += -math.log(p if won else 1 - p)
        self.brier += (p - won) ** 2
        self.hits += int((p >= 0.5) == won)

    def summary(self) -> dict:
        n = max(self.n, 1)
        return {"n": self.n, "log_loss": self.log_loss / n, "brier": self.brier / n, "accuracy": self.hits / n}


@dataclass
class SportBacktest:
    sport: str
    seasons: list[int]
    matches: int = 0
    model: dict[str, MarketScore] = field(default_factory=lambda: defaultdict(MarketScore))
    market: dict[str, MarketScore] = field(default_factory=lambda: defaultdict(MarketScore))
    naive: MarketScore = field(default_factory=MarketScore)
    calibration: list[tuple[float, float, int]] = field(default_factory=list)   # (śr. prognoza, odsetek, n)


def _closing(db: Database, match_ids: list[int]) -> dict[int, dict[tuple[str, str, float], float]]:
    out: dict[int, dict] = defaultdict(dict)
    for i in range(0, len(match_ids), 800):
        chunk = match_ids[i:i + 800]
        for r in db.query(f"SELECT match_id, market, selection, line, price, kind FROM odds WHERE match_id IN "
                          f"({','.join('?' * len(chunk))}) AND market IN ('ML', '1X2', 'OU', 'HCP')", tuple(chunk)):
            key = (r["market"], r["selection"], float(r["line"]))
            if r["kind"] == "close" or key not in out[r["match_id"]]:
                out[r["match_id"]][key] = r["price"]
    return out


def run_sport_backtest(db: Database, sport: str, seasons: list[int] | None = None,
                       half_life: float | None = None, ridge: float | None = None) -> SportBacktest | None:
    spec = SPORTS[sport]
    table = load_matches(db, sports=(sport,))
    if len(table) < 100:
        return None
    all_seasons = sorted(set(table.season.tolist()))
    seasons = seasons or all_seasons[-3:]
    result = SportBacktest(sport, seasons)
    idx = np.flatnonzero(np.isin(table.season, seasons))
    odds = _closing(db, [int(table.match_id[i]) for i in idx])
    win_market = "1X2" if spec.draws else "ML"
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    weeks = sorted(set((table.t[idx] // WEEK).astype(int).tolist()))
    for wk in weeks:
        rows = [i for i in idx if int(table.t[i] // WEEK) == wk]
        cut = float(min(table.t[i] for i in rows)) - 0.01
        model = ScoreModel.fit(table, cut, spec, half_life=half_life, ridge=ridge)
        if model is None:
            continue
        past = table.t < cut
        home_rate = float(np.mean(table.hg[past] > table.ag[past])) if past.any() else 0.5
        for i in rows:
            hg, ag = int(table.hg[i]), int(table.ag[i])
            if not spec.draws and hg == ag:
                continue
            mid = int(table.match_id[i])
            quotes = odds.get(mid, {})
            lines = {m: sorted({k[2] for k in quotes if k[0] == m}) for m in ("OU", "HCP")}
            neutral = bool(table.neutral[i]) if table.neutral is not None else False
            pred = model.predict(int(table.home_id[i]), int(table.away_id[i]), str(table.league[i]), neutral=neutral,
                                 ou_lines=lines["OU"], hcp_lines=lines["HCP"])
            result.matches += 1
            ph = pred.probs[(win_market, "H", 0.0)]
            if not spec.draws:
                ph = ph / max(ph + pred.probs[("ML", "A", 0.0)], 1e-9)
            won = hg > ag
            result.model[win_market].add(ph, won)
            result.naive.add(home_rate, won)
            bins[min(int(ph * 10), 9)].append((ph, won))
            pair = [quotes.get((win_market, "H", 0.0)), quotes.get((win_market, "A", 0.0))]
            if spec.draws:
                pair.insert(1, quotes.get(("1X2", "D", 0.0)))
            if all(pair):
                result.market[win_market].add(remove_margin(pair, "shin")[0], won)
            for market, a, b in (("OU", "O", "U"), ("HCP", "H", "A")):
                for line in lines[market]:
                    value = (hg + ag - line) if market == "OU" else (hg - ag + line)
                    qa, qb = quotes.get((market, a, line)), quotes.get((market, b, line))
                    pa, pb = pred.probs.get((market, a, line)), pred.probs.get((market, b, line))
                    if value == 0 or not (qa and qb and pa is not None and pb is not None):
                        continue
                    result.model[market].add(pa / max(pa + pb, 1e-9), value > 0)
                    result.market[market].add(remove_margin([qa, qb], "shin")[0], value > 0)
    result.calibration = [(float(np.mean([p for p, _ in v])), float(np.mean([w for _, w in v])), len(v))
                          for _, v in sorted(bins.items()) if v]
    return result
