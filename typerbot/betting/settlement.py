"""Rozstrzyganie typów na podstawie wyniku po 90 minutach."""

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
    if market == "BTTS":
        both = home_goals > 0 and away_goals > 0
        return int(both == (selection == "Y"))
    raise ValueError(f"Nieznany rynek: {market}")
