"""Rozstrzyganie typów na podstawie wyniku: w piłce nożnej po 90 minutach, w dyscyplinach
bez remisów (zwycięzca, handicap, suma) – wyniku końcowego z dogrywką."""

from __future__ import annotations

WIN, LOSS, VOID = 1, 0, None


def settle(market: str, selection: str, line: float, home_goals: int, away_goals: int) -> int | None:
    """1 – trafiony, 0 – nietrafiony, None – zwrot (np. linia całkowita równa liczbie bramek)."""
    diff = home_goals - away_goals
    if market == "1X2":
        result = "H" if diff > 0 else ("A" if diff < 0 else "D")
        return int(selection == result)
    if market == "DC":
        result = "H" if diff > 0 else ("A" if diff < 0 else "D")
        return int({"1X": result in "HD", "12": result in "HA", "X2": result in "DA"}[selection])
    if market == "OU":
        total = home_goals + away_goals
        if total == line:
            return VOID
        return int((total > line) == (selection == "O"))
    if market == "ML":
        if diff == 0:
            return VOID          # remis (np. w NFL po dogrywce) – zwrot stawki
        return int((diff > 0) == (selection == "H"))
    if market == "HCP":
        value = diff + line     # linia z perspektywy gospodarzy
        if value == 0:
            return VOID
        return int((value > 0) == (selection == "H"))
    if market == "BTTS":
        both = home_goals > 0 and away_goals > 0
        return int(both == (selection == "Y"))
    raise ValueError(f"Nieznany rynek: {market}")
