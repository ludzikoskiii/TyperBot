"""Prawdopodobieństwa rynków liczone z macierzy wyników."""

from __future__ import annotations

import numpy as np

Key = tuple[str, str, float]   # (rynek, typ, linia) – ten sam format co kursy w bazie

SELECTION_LABELS = {
    ("1X2", "H"): "1", ("1X2", "D"): "X", ("1X2", "A"): "2",
    ("DC", "1X"): "1X", ("DC", "12"): "12", ("DC", "X2"): "X2",
    ("OU", "O"): "Powyżej", ("OU", "U"): "Poniżej",
    ("BTTS", "Y"): "Obie strzelą – tak", ("BTTS", "N"): "Obie strzelą – nie",
}


def market_probabilities(m: np.ndarray, ou_lines: tuple[float, ...] = (2.5,)) -> dict[Key, float]:
    home = float(np.tril(m, -1).sum())   # wiersz > kolumna -> wygrywają gospodarze
    draw = float(np.trace(m))
    away = float(np.triu(m, 1).sum())
    out: dict[Key, float] = {
        ("1X2", "H", 0.0): home, ("1X2", "D", 0.0): draw, ("1X2", "A", 0.0): away,
        ("DC", "1X", 0.0): home + draw, ("DC", "12", 0.0): home + away, ("DC", "X2", 0.0): draw + away,
    }
    n = m.shape[0]
    totals = np.add.outer(np.arange(n), np.arange(n))
    for line in ou_lines:
        over = float(m[totals > line].sum())
        under = float(m[totals < line].sum())
        out[("OU", "O", float(line))] = over
        out[("OU", "U", float(line))] = under   # przy linii całkowitej reszta to zwrot stawki
    btts = float(m[1:, 1:].sum())
    out[("BTTS", "Y", 0.0)] = btts
    out[("BTTS", "N", 0.0)] = 1.0 - btts
    return out


def most_likely_score(m: np.ndarray) -> tuple[int, int, float]:
    i, j = np.unravel_index(int(np.argmax(m)), m.shape)
    return int(i), int(j), float(m[i, j])


def label(key: Key) -> str:
    market, sel, line = key
    text = SELECTION_LABELS.get((market, sel), f"{market} {sel}")
    return f"{text} {line:g}".replace(".", ",") if market == "OU" else text
