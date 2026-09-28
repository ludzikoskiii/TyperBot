"""Prawdopodobieństwa rynków liczone z macierzy wyników."""

from __future__ import annotations

import numpy as np

Key = tuple[str, str, float]   # (rynek, typ, linia) – ten sam format co kursy w bazie

SELECTION_LABELS = {
    ("1X2", "H"): "1", ("1X2", "D"): "X", ("1X2", "A"): "2",
    ("DC", "1X"): "1X", ("DC", "12"): "12", ("DC", "X2"): "X2",
    ("ML", "H"): "1 (z dogrywką)", ("ML", "A"): "2 (z dogrywką)",
    ("OU", "O"): "Powyżej", ("OU", "U"): "Poniżej",
    ("BTTS", "Y"): "Obie strzelą – tak", ("BTTS", "N"): "Obie strzelą – nie",
}
# Kolejność rynków i typów na listach (ocena typów, kupony).
MARKET_ORDER = ("1X2", "DC", "ML", "HCP", "OU", "BTTS")
MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "ML": "Zwycięzca (z dogrywką)", "HCP": "Handicap",
                "OU": "Powyżej/poniżej", "BTTS": "Obie strzelą"}
MARKET_HINTS = {
    "1X2": "Wynik meczu: 1 – gospodarze, X – remis, 2 – goście (piłka nożna, piłka ręczna)",
    "DC": "Podwójna szansa: 1X, 12, X2 (piłka nożna, piłka ręczna)",
    "ML": "Zwycięzca meczu z dogrywką i rzutami karnymi (futbol amerykański, baseball, hokej, koszykówka)",
    "HCP": "Handicap (np. „1 (−3,5)” – gospodarze wygrywają różnicą co najmniej 4 punktów; inne dyscypliny)",
    "OU": "Suma goli lub punktów powyżej/poniżej linii (w piłce nożnej 2,5)",
    "BTTS": "Obie drużyny strzelą gola (piłka nożna)",
}
_SEL_ORDER = {"H": 0, "D": 1, "A": 2, "1X": 0, "12": 1, "X2": 2, "O": 0, "U": 1, "Y": 0, "N": 1}


def sort_keys(keys) -> list[Key]:
    """Typy w kolejności wyświetlania: rynek, linia, typ."""
    order = {m: i for i, m in enumerate(MARKET_ORDER)}
    return sorted(keys, key=lambda k: (order.get(k[0], 99), k[2], _SEL_ORDER.get(k[1], 9)))


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


def sport_market_probabilities(m: np.ndarray, draws: bool, ou_lines=(), hcp_lines=()) -> dict[Key, float]:
    """Rynki dyscyplin innych niż piłka nożna – z macierzy wyniku końcowego (z dogrywką).

    Handicap: linia z perspektywy gospodarzy – typ „H” z linią −3,5 wygrywa, gdy gospodarze wygrają
    różnicą co najmniej 4 punktów; typ „A” z tą samą linią to goście +3,5. Linia całkowita równa różnicy
    lub sumie oznacza zwrot stawki (reszta prawdopodobieństwa).
    """
    home = float(np.tril(m, -1).sum())
    draw = float(np.trace(m))
    away = float(np.triu(m, 1).sum())
    out: dict[Key, float] = {}
    if draws:
        out.update({("1X2", "H", 0.0): home, ("1X2", "D", 0.0): draw, ("1X2", "A", 0.0): away,
                    ("DC", "1X", 0.0): home + draw, ("DC", "12", 0.0): home + away, ("DC", "X2", 0.0): draw + away})
    else:
        out.update({("ML", "H", 0.0): home, ("ML", "A", 0.0): away})
    n = m.shape[0]
    idx = np.arange(n)
    totals = np.add.outer(idx, idx)
    diff = np.subtract.outer(idx, idx)
    for line in sorted(set(float(x) for x in ou_lines)):
        out[("OU", "O", line)] = float(m[totals > line].sum())
        out[("OU", "U", line)] = float(m[totals < line].sum())
    for line in sorted(set(float(x) for x in hcp_lines)):
        out[("HCP", "H", line)] = float(m[diff + line > 0].sum())
        out[("HCP", "A", line)] = float(m[diff + line < 0].sum())
    return out


def most_likely_score(m: np.ndarray) -> tuple[int, int, float]:
    i, j = np.unravel_index(int(np.argmax(m)), m.shape)
    return int(i), int(j), float(m[i, j])


def _signed(x: float) -> str:
    text = f"{x:+g}".replace(".", ",")
    return text.replace("-", "−") if x < 0 else text


def label(key: Key) -> str:
    market, sel, line = key
    if market == "HCP":
        return f"1 ({_signed(line)})" if sel == "H" else f"2 ({_signed(-line)})"
    text = SELECTION_LABELS.get((market, sel), f"{market} {sel}")
    return f"{text} {line:g}".replace(".", ",") if market == "OU" else text
