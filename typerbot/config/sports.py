"""Dyscypliny: nazwy, rynki i parametry modelu wyników.

  * piłka nożna (także kobiet) – model Dixona-Colesa z rankingiem Elo (typerbot.model.predictor),
  * pozostałe – model wyników (typerbot.model.scores): siła ataku i obrony drużyn z wyników,
    rozkład normalny punktów (futbol amerykański, koszykówka, piłka ręczna) albo rozkład liczby
    goli/runów (hokej, baseball – Poisson, a przy większym rozrzucie ujemny dwumianowy).

W dyscyplinach bez remisów (dogrywka, dodatkowe zmiany, rzuty karne) rynki liczymy z wyniku
końcowego: remis po czasie podstawowym rozstrzyga dogrywka (`ot_step` – o ile zwykle wygrywa
drużyna, która ją wygrywa). Tak też rozliczamy: zwycięzca, handicap i suma – z dogrywką.
"""

from __future__ import annotations

from dataclasses import dataclass

FOOTBALL = "football"


@dataclass(frozen=True)
class Sport:
    code: str
    label: str                       # nazwa w interfejsie
    model: str                       # 'football' | 'normal' | 'count'
    draws: bool                      # remis możliwy w wyniku końcowym (rynek 1X2)
    markets: tuple[str, ...]         # rynki oferowane w tej dyscyplinie
    unit: tuple[str, str, str]       # odmiana jednostki wyniku: 1 gol, 2 gole, 5 goli
    short: str                       # krótka nazwa (nagłówek typu na kuponie)
    unit_frac: str                   # jednostka po liczbie ułamkowej: 0,9 gola, 2,5 punktu
    ot_step: int = 1                 # przewaga zwycięzcy dogrywki (wynik końcowy)
    half_life_days: float = 240.0    # po ilu dniach mecz waży o połowę mniej (dobrane backtestem)
    ridge: float = 4.0               # ściąganie siły drużyn do średniej (w meczach „wirtualnych”)
    max_score: int = 12              # zakres wyników w macierzy
    total_step: float = 1.0          # rozstaw linii powyżej/poniżej wokół mediany sumy
    hcp_step: float = 0.0            # rozstaw linii handicapu wokół mediany różnicy (0 – tylko hcp_lines)
    hcp_lines: tuple[float, ...] = ()  # stałe linie handicapu (np. ±1,5 w hokeju i baseballu)
    sd_margin: float = 0.0           # typowe odchylenie różnicy punktów (rozkład normalny – wartość startowa)
    sd_total: float = 0.0            # typowe odchylenie sumy punktów
    min_matches: int = 6             # mniej meczów w oknie – drużyna „mało danych”
    window_days: float = 800.0       # z ilu dni wstecz bierzemy mecze do modelu
    sd_scale: float = 1.0            # poszerzenie rozrzutu (niepewność siły drużyn) – z backtestu


SPORTS: dict[str, Sport] = {s.code: s for s in (
    Sport(FOOTBALL, "Piłka nożna", "football", True, ("1X2", "DC", "OU", "BTTS"), ("gol", "gole", "goli"),
          "Piłka nożna", "gola"),
    Sport("football_women", "Piłka nożna kobiet", "football", True, ("1X2", "DC", "OU", "BTTS"),
          ("gol", "gole", "goli"), "Piłka kobiet", "gola"),
    Sport("american_football", "Futbol amerykański", "normal", False, ("ML", "HCP", "OU"),
          ("punkt", "punkty", "punktów"), "Futbol am.", "punktu", ot_step=3, half_life_days=150.0, ridge=4.0, max_score=75,
          total_step=3.0, hcp_step=3.0, sd_margin=13.5, sd_total=13.5, min_matches=6, window_days=900.0,
          sd_scale=1.03),
    Sport("basketball", "Koszykówka", "normal", False, ("ML", "HCP", "OU"), ("punkt", "punkty", "punktów"),
          "Koszykówka", "punktu", ot_step=4, half_life_days=200.0, ridge=6.0, max_score=160, total_step=5.0, hcp_step=4.0,
          sd_margin=12.0, sd_total=18.0),
    Sport("handball", "Piłka ręczna", "normal", True, ("1X2", "DC", "HCP", "OU"), ("bramka", "bramki", "bramek"),
          "Piłka ręczna", "bramki", half_life_days=240.0, ridge=6.0, max_score=55, total_step=2.0, hcp_step=2.0, sd_margin=6.0,
          sd_total=7.0),
    Sport("hockey", "Hokej na lodzie", "count", False, ("ML", "HCP", "OU"), ("gol", "gole", "goli"),
          "Hokej", "gola", half_life_days=240.0, ridge=6.0, max_score=14, total_step=1.0, hcp_lines=(-1.5, 1.5)),
    Sport("baseball", "Baseball", "count", False, ("ML", "HCP", "OU"), ("run", "runy", "runów"),
          "Baseball", "runa", half_life_days=180.0, ridge=10.0, max_score=25, total_step=1.0, hcp_lines=(-1.5, 1.5),
          min_matches=15, window_days=600.0),
)}
SPORT_ORDER = tuple(SPORTS)


def sport_of(code: str | None) -> Sport:
    return SPORTS.get(code or FOOTBALL, SPORTS[FOOTBALL])


def sport_label(code: str | None) -> str:
    return sport_of(code).label


def football_like(code: str | None) -> bool:
    return sport_of(code).model == "football"
