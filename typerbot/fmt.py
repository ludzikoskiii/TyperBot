"""Formatowanie liczb i odmiana słów po polsku."""

from __future__ import annotations


def num(x: float, digits: int = 2) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def pct(x: float | None, digits: int = 0) -> str:
    return "–" if x is None else f"{100 * x:.{digits}f}%".replace(".", ",")


def signed_pct(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{100 * x:+.{digits}f}%".replace(".", ",")


def money(x: float) -> str:
    return f"{x:,.2f} zł".replace(",", " ").replace(".", ",")


def form(n: int, one: str, few: str, many: str) -> str:
    """Forma słowa dla liczby: form(2, 'jest', 'są', 'jest') -> 'są'."""
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(1, 'zdarzenie', 'zdarzenia', 'zdarzeń') -> '1 zdarzenie'."""
    return f"{n} {form(n, one, few, many)}"
