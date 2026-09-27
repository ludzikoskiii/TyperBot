"""Kandydaci na kupon (pojedyncze typy) i proste funkcje kuponu."""

from __future__ import annotations

from dataclasses import dataclass

from typerbot.model.markets import Key


@dataclass
class Candidate:
    match_id: int
    key: Key                 # (rynek, typ, linia)
    probability: float       # prognoza użyta na kuponie (mieszanka model + rynek)
    odds: float
    league: str = ""
    estimated_odds: bool = False       # kurs szacunkowy (wyliczony), a nie z oferty bukmachera
    p_model: float | None = None       # prawdopodobieństwo z modelu
    p_market: float | None = None      # prawdopodobieństwo rynku (kursy bez marży)
    teams: tuple[int, ...] = ()        # drużyny meczu – dwa typy z tą samą drużyną są zależne

    @property
    def value(self) -> float:
        """Wartość przed podatkiem: p·kurs − 1 (dodatnia = „value”)."""
        return self.probability * self.odds - 1.0

    @property
    def divergence(self) -> float | None:
        """Różnica model − rynek (w punktach prawdopodobieństwa)."""
        if self.p_model is None or self.p_market is None:
            return None
        return self.p_model - self.p_market


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
