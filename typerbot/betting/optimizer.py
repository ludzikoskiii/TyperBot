"""Optymalizator kuponu – programowanie dynamiczne (problem plecakowy z wyborem).

Szukamy zestawu typów, dla którego:
  * kurs łączny mieści się w [cel·(1−tolerancja), cel·(1+tolerancja)],
  * liczba zdarzeń mieści się w [min, max],
  * z jednego meczu jest najwyżej jeden typ, a mecze z tą samą drużyną (np. puchar i liga
    w jednym zakresie dat) nie trafiają na kupon razem – takie typy są od siebie zależne,
  * (opcjonalnie) z każdym wcześniejszym kuponem jest najwyżej L wspólnych meczów,
  * kupon jest najlepszy „przy tym samym kursie łącznym”: maksymalizujemy
        Σ log(p·kurs) − kara·liczba zdarzeń,
    czyli szansę trafienia w przeliczeniu na kurs (przy równym kursie – większa szansa),
    a przy remisie mniej zdarzeń (każde zdarzenie to kolejna marża bukmachera);
    typ z kursem szacunkowym ma dodatkową małą karę – przy podobnej szansie wygrywa prawdziwy kurs.

Filtry typów zależą od trybu:
  * „probability” (najwyższa szansa trafienia) – tylko typy, w których model i rynek są
    zgodni (różnica prawdopodobieństw ≤ max_divergence): duża różnica bez wyraźnego powodu
    to częściej błąd modelu niż okazja;
  * „value” (najwyższa wartość) – tylko typy z dodatnim EV według prognozy (p·kurs > 1).

Kryterium jest addytywne w logarytmach, więc stan DP to (liczba zdarzeń, liczby wspólnych
meczów z poprzednimi kuponami, log-kurs zaokrąglony do 0,005). Wynik sprawdzamy na dokładnym kursie.
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
LEG_PENALTY = 0.02  # ~2% „szansy w przeliczeniu na kurs” za każde dodatkowe zdarzenie – przy remisie mniej zdarzeń
ESTIMATED_PENALTY = 0.03   # kurs szacunkowy (u bukmachera może być inny) – prawdziwy kurs ma pierwszeństwo

OverlapLimit = tuple[frozenset[int], int]   # (mecze wcześniejszego kuponu, max wspólnych)


def _score(c: Candidate) -> float:
    return math.log(c.probability) + math.log(c.odds) - LEG_PENALTY - (ESTIMATED_PENALTY if c.estimated_odds else 0.0)


def agrees(c: Candidate, cfg: CouponSettings) -> bool:
    """Model i rynek są zgodni (albo brak jednego z nich – wtedy nie ma czego porównywać)."""
    d = c.divergence
    return d is None or abs(d) <= cfg.max_divergence


def eligible(candidates: list[Candidate], cfg: CouponSettings,
             exclude_matches: frozenset[int] | set[int] = frozenset()) -> list[Candidate]:
    hi = cfg.target_odds * (1 + cfg.tolerance)
    out = []
    for c in candidates:
        if (c.match_id in exclude_matches or c.key[0] not in cfg.markets or c.probability <= 0
                or c.probability < cfg.min_probability or not 1.01 <= c.odds <= hi):
            continue
        if c.estimated_odds and cfg.estimated_odds != "always":   # 'fallback' – drugie podejście w generatorze
            continue
        if cfg.mode == "value":
            if c.value <= 0:
                continue
        elif not agrees(c, cfg):
            continue
        out.append(c)
    return out


def conflict_groups(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Grupy, z których bierzemy najwyżej jeden typ: mecz albo mecze połączone wspólną drużyną."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_team: dict[int, int] = {}
    for c in candidates:
        find(c.match_id)
        for team in c.teams:
            if team in by_team:
                parent[find(c.match_id)] = find(by_team[team])
            else:
                by_team[team] = c.match_id
    groups: dict[int, list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[find(c.match_id)].append(c)
    return list(groups.values())


def optimize(candidates: list[Candidate], cfg: CouponSettings,
             exclude_matches: frozenset[int] | set[int] = frozenset(),
             overlap_limits: Sequence[OverlapLimit] = (), max_tries: int = 50) -> list[Candidate] | None:
    """Najlepszy kupon albo None, gdy żadna kombinacja nie spełnia warunków."""
    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)
    if hi <= 1.0 or cfg.max_events < 1:
        return None
    groups = conflict_groups(eligible(candidates, cfg, exclude_matches))
    if len(groups) < max(cfg.min_events, 1):
        return None
    limits = [max(0, lim) for _, lim in overlap_limits]
    sets = [s for s, _ in overlap_limits]
    k_max = min(cfg.max_events, len(groups))
    b_max = int(math.floor(math.log(hi) / STEP)) + 1
    shape = (k_max + 1, *(lim + 1 for lim in limits), b_max + 1)
    best = np.full(shape, NEG)
    best[(0,) * len(shape)] = 0.0
    history: list[np.ndarray] = []
    moves: list[list[tuple[int, tuple[int, ...]]]] = []
    for opts in groups:
        new = best.copy()
        choice = np.full(shape, -1, dtype=np.int16)
        match_moves = []
        for j, c in enumerate(opts):
            inc = tuple(int(c.match_id in s) for s in sets)
            s = max(1, int(round(math.log(c.odds) / STEP)))
            match_moves.append((s, inc))
            if s > b_max or any(i > lim for i, lim in zip(inc, limits)):
                continue
            src = (slice(0, k_max), *(slice(0, lim + 1 - i) for i, lim in zip(inc, limits)), slice(0, b_max + 1 - s))
            dst = (slice(1, k_max + 1), *(slice(i, lim + 1) for i, lim in zip(inc, limits)), slice(s, b_max + 1))
            cand = np.full(shape, NEG)
            cand[dst] = best[src] + _score(c)
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
        picked = _backtrack(groups, history, moves, idx)
        total = math.prod(c.odds for c in picked)
        if lo <= total <= hi and cfg.min_events <= len(picked) <= cfg.max_events:
            return sorted(picked, key=lambda c: c.match_id)
    return None


def _backtrack(groups, history, moves, idx: tuple[int, ...]) -> list[Candidate]:
    state = list(idx)
    picked = []
    for m in range(len(groups) - 1, -1, -1):
        j = int(history[m][tuple(state)])
        if j >= 0:
            picked.append(groups[m][j])
            s, inc = moves[m][j]
            state[0] -= 1
            for d, i in enumerate(inc):
                state[1 + d] -= i
            state[-1] -= s
    return picked


def probability(coupon: list[Candidate]) -> float:
    return math.prod(c.probability for c in coupon)


def overlap_limit(coupon: list[Candidate], min_difference: float) -> int:
    """Ile meczów kolejny kupon może mieć wspólnych z danym: (1 − różnica)·liczba zdarzeń, ale zawsze
    co najmniej jeden mecz inny – ten sam zestaw meczów nie wraca jako „alternatywa”."""
    n = len(coupon)
    return max(0, min(int(math.floor((1 - min_difference) * n)), n - 1))


def alternatives(candidates: list[Candidate], cfg: CouponSettings, count: int | None = None,
                 exclude_matches: frozenset[int] | set[int] = frozenset(),
                 previous: Sequence[list[Candidate]] = ()) -> list[list[Candidate]]:
    """Do `count` kuponów, posortowanych od najwyższej szansy trafienia; każdy kolejny ma z każdym
    wcześniejszym (także z `previous` – kuponami ułożonymi wcześniej) ograniczoną liczbę wspólnych meczów."""
    out: list[list[Candidate]] = []
    for _ in range(count if count is not None else cfg.alternatives):
        limits = [(frozenset(c.match_id for c in prev), overlap_limit(prev, cfg.min_difference))
                  for prev in (*previous, *out)]
        coupon = optimize(candidates, cfg, exclude_matches, limits)
        if coupon is None:
            break
        out.append(coupon)
    return sorted(out, key=lambda c: (-probability(c), len(c)))


def brute_force(candidates: list[Candidate], cfg: CouponSettings,
                overlap_limits: Sequence[OverlapLimit] = ()) -> list[Candidate] | None:
    """Pełne przeszukanie – tylko do testów poprawności na małych danych."""
    import itertools

    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)
    options = [[None, *opts] for opts in conflict_groups(eligible(candidates, cfg))]
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
        score = sum(_score(c) for c in chosen)
        if score > best_score:
            best, best_score = chosen, score
    return sorted(best, key=lambda c: c.match_id) if best else None
