"""Optymalizator kuponu – programowanie dynamiczne (problem plecakowy z wyborem).

Szukamy zestawu typów (najwyżej jeden z meczu), dla którego:
  * kurs łączny mieści się w [cel·(1−tolerancja), cel·(1+tolerancja)],
  * liczba zdarzeń mieści się w [min, max],
  * (opcjonalnie) z każdym wcześniejszym kuponem ma najwyżej L wspólnych meczów,
  * suma kryterium jest największa:
      – tryb „probability”: Σ log p        (najwyższa szansa trafienia kuponu),
      – tryb „value”:       Σ log(p·kurs)  (najwyższa wartość oczekiwana kuponu).
Oba kryteria są addytywne w logarytmach, więc stan DP to (liczba zdarzeń,
liczby wspólnych meczów z poprzednimi kuponami, log-kurs zaokrąglony do 0,005).
Wynik sprawdzamy na dokładnym kursie.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

import numpy as np

from typerbot.betting.coupon import Candidate
from typerbot.config.settings import CouponSettings

STEP = 0.005        # rozdzielczość log-kursu (~0,5% kursu)
NEG = -1e18

OverlapLimit = tuple[frozenset[int], int]   # (mecze wcześniejszego kuponu, max wspólnych)


def _score(c: Candidate, mode: str) -> float:
    return math.log(c.probability) + (math.log(c.odds) if mode == "value" else 0.0)


def eligible(candidates: list[Candidate], cfg: CouponSettings,
             exclude_matches: frozenset[int] | set[int] = frozenset()) -> list[Candidate]:
    hi = cfg.target_odds * (1 + cfg.tolerance)
    return [c for c in candidates
            if c.match_id not in exclude_matches and c.key[0] in cfg.markets
            and c.probability >= cfg.min_probability and 1.01 <= c.odds <= hi and c.probability > 0]


def optimize(candidates: list[Candidate], cfg: CouponSettings,
             exclude_matches: frozenset[int] | set[int] = frozenset(),
             overlap_limits: Sequence[OverlapLimit] = (), max_tries: int = 50) -> list[Candidate] | None:
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
    limits = [max(0, lim) for _, lim in overlap_limits]
    sets = [s for s, _ in overlap_limits]
    k_max = min(cfg.max_events, len(matches))
    b_max = int(math.floor(math.log(hi) / STEP)) + 1
    shape = (k_max + 1, *(lim + 1 for lim in limits), b_max + 1)
    best = np.full(shape, NEG)
    best[(0,) * len(shape)] = 0.0
    history: list[np.ndarray] = []
    moves: list[list[tuple[int, tuple[int, ...]]]] = []
    for opts in matches:
        new = best.copy()
        choice = np.full(shape, -1, dtype=np.int16)
        match_moves = []
        inc = tuple(int(opts[0].match_id in s) for s in sets)
        for j, c in enumerate(opts):
            s = max(1, int(round(math.log(c.odds) / STEP)))
            match_moves.append((s, inc))
            if s > b_max or any(i > lim for i, lim in zip(inc, limits)):
                continue
            src = (slice(0, k_max), *(slice(0, lim + 1 - i) for i, lim in zip(inc, limits)), slice(0, b_max + 1 - s))
            dst = (slice(1, k_max + 1), *(slice(i, lim + 1) for i, lim in zip(inc, limits)), slice(s, b_max + 1))
            cand = np.full(shape, NEG)
            cand[dst] = best[src] + _score(c, cfg.mode)
            better = cand > new
            new[better] = cand[better]
            choice[better] = j
        history.append(choice)
        moves.append(match_moves)
        best = new

    b_lo = max(0, int(math.ceil(math.log(lo) / STEP)) - 1) if lo > 1 else 0
    cells = [(best[idx], idx) for idx in map(tuple, np.argwhere(best > NEG / 2))
             if max(cfg.min_events, 1) <= idx[0] <= k_max and b_lo <= idx[-1]]
    cells.sort(key=lambda x: x[0], reverse=True)
    for _, idx in cells[:max_tries]:
        picked = _backtrack(matches, history, moves, idx)
        total = math.prod(c.odds for c in picked)
        if lo <= total <= hi and cfg.min_events <= len(picked) <= cfg.max_events:
            return sorted(picked, key=lambda c: c.match_id)
    return None


def _backtrack(matches, history, moves, idx: tuple[int, ...]) -> list[Candidate]:
    state = list(idx)
    picked = []
    for m in range(len(matches) - 1, -1, -1):
        j = int(history[m][tuple(state)])
        if j >= 0:
            picked.append(matches[m][j])
            s, inc = moves[m][j]
            state[0] -= 1
            for d, i in enumerate(inc):
                state[1 + d] -= i
            state[-1] -= s
    return picked


def alternatives(candidates: list[Candidate], cfg: CouponSettings, count: int | None = None,
                 exclude_matches: frozenset[int] | set[int] = frozenset()) -> list[list[Candidate]]:
    """Do `count` kuponów; każdy kolejny ma z każdym wcześniejszym co najwyżej
    (1 − min_difference)·liczba zdarzeń wspólnych meczów."""
    out: list[list[Candidate]] = []
    for _ in range(count if count is not None else cfg.alternatives):
        limits = [(frozenset(c.match_id for c in prev), int(math.floor((1 - cfg.min_difference) * len(prev))))
                  for prev in out]
        coupon = optimize(candidates, cfg, exclude_matches, limits)
        if coupon is None:
            break
        out.append(coupon)
    return out


def brute_force(candidates: list[Candidate], cfg: CouponSettings,
                overlap_limits: Sequence[OverlapLimit] = ()) -> list[Candidate] | None:
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
        ids = {c.match_id for c in chosen}
        if any(len(ids & s) > lim for s, lim in overlap_limits):
            continue
        score = sum(_score(c, cfg.mode) for c in chosen)
        if score > best_score:
            best, best_score = chosen, score
    return sorted(best, key=lambda c: c.match_id) if best else None
