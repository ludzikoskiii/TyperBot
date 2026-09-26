"""Kursy: marża bukmachera, prawdopodobieństwo implikowane, podatek, wypłata.

Podatek w Polsce:
  * 12% od stawki – do gry trafia 88% kwoty (chyba że bukmacher pokrywa podatek),
  * 10% od wygranej, jeśli wypłata przekracza 2280 zł (od całej kwoty wygranej).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from typerbot.config.settings import TaxSettings


def implied(prices: Sequence[float]) -> list[float]:
    return [1.0 / p for p in prices]


def overround(prices: Sequence[float]) -> float:
    """Marża bukmachera: suma odwrotności kursów minus 1 (np. 0,05 = 5%)."""
    return sum(implied(prices)) - 1.0


def remove_margin(prices: Sequence[float], method: str = "proportional") -> list[float]:
    """Prawdopodobieństwa „uczciwe” z kursów wszystkich typów jednego rynku."""
    q = implied(prices)
    total = sum(q)
    if method == "shin" and len(q) > 1:
        return _shin(q, total)
    return [x / total for x in q]


def _shin(q: list[float], total: float) -> list[float]:
    """Metoda Shina – większą część marży przypisuje typom z wysokim kursem
    (koryguje tzw. efekt faworyta i outsidera)."""

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * x * x / total) - z) / (2 * (1 - z)) for x in q]

    lo, hi = 0.0, 0.4
    for _ in range(80):
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1.0:
            lo = mid
        else:
            hi = mid
    p = probs((lo + hi) / 2)
    s = sum(p)
    return [x / s for x in p]


def effective_stake(stake: float, tax: TaxSettings) -> float:
    return stake if tax.bookmaker_pays_tax else stake * (1.0 - tax.stake_tax)


def payout(stake: float, odds: float, tax: TaxSettings) -> float:
    """Kwota wypłaty przy trafieniu (po podatku od stawki i od wygranej)."""
    gross = effective_stake(stake, tax) * odds
    if tax.win_tax_enabled and gross > tax.win_tax_threshold:
        gross -= gross * tax.win_tax_rate
    return gross


def odds_after_tax(odds: float, tax: TaxSettings) -> float:
    """Kurs „na rękę”: ile wraca z 1 zł wydanego na zakład (dla małych stawek)."""
    return odds * (1.0 if tax.bookmaker_pays_tax else 1.0 - tax.stake_tax)


def expected_value(probability: float, odds: float, tax: TaxSettings | None = None) -> float:
    """EV na 1 zł stawki. Bez podatku: p·kurs − 1; z podatkiem: p·kurs·0,88 − 1."""
    factor = 1.0 if tax is None else odds_after_tax(1.0, tax)
    return probability * odds * factor - 1.0
