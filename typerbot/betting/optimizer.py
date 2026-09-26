"""Optymalizator kuponu – programowanie dynamiczne (problem plecakowy z wyborem).

Szukamy zestawu typów (najwyżej jeden z meczu), dla którego:
  * kurs łączny mieści się w [cel·(1−tolerancja), cel·(1+tolerancja)],
  * liczba zdarzeń mieści się w [min, max],
  * suma kryterium jest największa:
      – tryb „probability”: Σ log p        (najwyższa szansa trafienia kuponu),
      – tryb „value”:       Σ log(p·kurs)  (najwyższa wartość oczekiwana kuponu).
Oba kryteria są addytywne w logarytmach, więc stan DP to (liczba zdarzeń,
log-kurs zaokrąglony do kroku 0,005). Wynik sprawdzamy na dokładnym kursie.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from typerbot.betting.coupon import Candidate
from typerbot.config.settings import CouponSettings

STEP = 0.005        # rozdzielczość log-kursu (~0,5% kursu)
NEG = -1e18


def _score(c: Candidate, mode: str) -> float:
    return math.log(c.probability) + (math.log(c.odds) if mode == "value" else 0.0)


def eligible(candidates: list[Candidate], cfg: CouponSettings, exclude_matches: set[int] = frozenset()) -> list[Candidate]:
    hi = cfg.target_odds * (1 + cfg.tolerance)
    return [c for c in candidates
            if c.match_id not in exclude_matches and c.key[0] in cfg.markets
            and c.probability >= cfg.min_probability and 1.01 <= c.odds <= hi and c.probability > 0]


def optimize(candidates: list[Candidate], cfg: CouponSettings,
             exclude_matches: set[int] = frozenset(), max_tries: int = 50) -> list[Candidate] | None:
    """Najlepszy kupon albo None, gdy żadna kombinacja nie spełnia warunków."""
    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)
    if hi <= 1.0 or cfg.max_events < 1:
        return None
    groups: dict[int, list[Candidate]] = defaultdict(list)
    for c in eligible(candidates, cfg, exclude_matches):
        groups[c.match_id].append(c)
    if len(groups) < max(cfg.min_events, 1):
        return None
    matches = list(groups.values())
    k_max = min(cfg.max_events, len(matches))
    b_max = int(math.floor(math.log(hi) / STEP)) + 1
    best = np.full((k_max + 1, b_max + 1), NEG)
    best[0, 0] = 0.0
    history: list[np.ndarray] = []
    steps: list[list[int]] = []
    for opts in matches:
        new = best.copy()
        choice = np.full(best.shape, -1, dtype=np.int16)
        shifts = []
        for j, c in enumerate(opts):
            s = max(1, int(round(math.log(c.odds) / STEP)))
            shifts.append(s)
            if s > b_max:
                continue
            cand = np.full(best.shape, NEG)
            cand[1:, s:] = best[:-1, :b_max + 1 - s] + _score(c, cfg.mode)
            better = cand > new
            new[better] = cand[better]
            choice[better] = j
        history.append(choice)
        steps.append(shifts)
        best = new

    b_lo = max(0, int(math.ceil(math.log(lo) / STEP)) - 1) if lo > 1 else 0
    cells = [(best[k, b], k, b) for k in range(max(cfg.min_events, 1), k_max + 1)
             for b in range(b_lo, b_max + 1) if best[k, b] > NEG / 2]
    cells.sort(reverse=True)
    for _, k, b in cells[:max_tries]:
        picked = _backtrack(matches, history, steps, k, b)
        total = math.prod(c.odds for c in picked)
        if lo <= total <= hi and cfg.min_events <= len(picked) <= cfg.max_events:
            return sorted(picked, key=lambda c: c.match_id)
    return None


def _backtrack(matches, history, steps, k: int, b: int) -> list[Candidate]:
    picked = []
    for m in range(len(matches) - 1, -1, -1):
        j = int(history[m][k, b])
        if j >= 0:
            picked.append(matches[m][j])
            k -= 1
            b -= steps[m][j]
    return picked


def brute_force(candidates: list[Candidate], cfg: CouponSettings) -> list[Candidate] | None:
    """Pełne przeszukanie – tylko do testów poprawności na małych danych."""
    import itertools

    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)
    groups: dict[int, list[Candidate]] = defaultdict(list)
    for c in eligible(candidates, cfg):
        groups[c.match_id].append(c)
    options = [[None, *opts] for opts in groups.values()]
    best, best_score = None, NEG
    for combo in itertools.product(*options):
        chosen = [c for c in combo if c is not None]
        if not cfg.min_events <= len(chosen) <= cfg.max_events:
            continue
        total = math.prod(c.odds for c in chosen)
        if not lo <= total <= hi:
            continue
        score = sum(_score(c, cfg.mode) for c in chosen)
        if score > best_score:
            best, best_score = chosen, score
    return sorted(best, key=lambda c: c.match_id) if best else None
