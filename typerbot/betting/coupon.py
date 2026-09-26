"""Kandydaci na kupon i prosty dobór kuponu.

Etap 2 używa zachłannego doboru (do symulacji kuponów w backteście);
w etapie 3 dochodzi optymalizator szukający najlepszej kombinacji.
"""

from __future__ import annotations

from dataclasses import dataclass

from typerbot.config.settings import CouponSettings
from typerbot.model.markets import Key


@dataclass
class Candidate:
    match_id: int
    key: Key                 # (rynek, typ, linia)
    probability: float
    odds: float
    league: str = ""
    estimated_odds: bool = False   # kurs wyliczony, a nie z oferty bukmachera

    @property
    def value(self) -> float:
        """Wartość przed podatkiem: p·kurs − 1 (dodatnia = „value”)."""
        return self.probability * self.odds - 1.0


def greedy_coupon(candidates: list[Candidate], cfg: CouponSettings) -> list[Candidate] | None:
    """Zachłanny kupon: najlepsze typy (po jednym z meczu), aż kurs trafi w zakres."""
    lo, hi = cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)
    score = (lambda c: c.probability) if cfg.mode == "probability" else (lambda c: c.probability * c.odds)
    best: dict[int, Candidate] = {}
    for c in candidates:
        if c.probability < cfg.min_probability or c.odds <= 1.0 or c.key[0] not in cfg.markets:
            continue
        if c.match_id not in best or score(c) > score(best[c.match_id]):
            best[c.match_id] = c
    chosen: list[Candidate] = []
    total = 1.0
    for c in sorted(best.values(), key=score, reverse=True):
        if len(chosen) >= cfg.max_events:
            break
        if total * c.odds > hi:
            continue
        chosen.append(c)
        total *= c.odds
        if total >= lo and len(chosen) >= cfg.min_events:
            return chosen
    return None


def coupon_odds(selections: list[Candidate]) -> float:
    total = 1.0
    for c in selections:
        total *= c.odds
    return total


def coupon_probability(selections: list[Candidate]) -> float:
    """Szacowane prawdopodobieństwo trafienia – przy założeniu niezależności meczów."""
    total = 1.0
    for c in selections:
        total *= c.probability
    return total
