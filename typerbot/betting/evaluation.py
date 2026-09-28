"""Ocena typów: prawdopodobieństwo, kurs, prawdopodobieństwo implikowane, EV.

Dla każdego typu liczymy trzy prawdopodobieństwa:
  * model  – z modelu Dixona-Colesa,
  * rynek  – z kursów średnich po usunięciu marży,
  * prognoza = w·model + (1−w)·rynek (w – „udział modelu”, dobierany backtestem);
    gdy brak kursów rynku, prognoza = model.
Typ jest „value”, gdy prognoza·kurs > 1 (przed podatkiem – podatek od stawki płaci
się raz za cały kupon). Osobno pokazujemy EV po podatku dla gry pojedynczej.

Kurs szacunkowy („kurs szacunkowy – sprawdź u bukmachera”):
  * gdy mecz ma kursy 1X2, a brakuje innych rynków – liczymy je z kursów, które są: podwójną
    szansę z 1X2, a BTTS i powyżej/poniżej 2,5 z oczekiwanych goli dopasowanych do kursów 1X2
    (i powyżej/poniżej, jeśli są) – z marżą jak w 1X2;
  * gdy mecz nie ma żadnych kursów (np. terminarz tylko z openfootball) – z prognozy modelu,
    pomniejszonej o typową marżę bukmachera z ustawień: kurs = 1 / (p · (1 + marża)).
Taki kurs jest wyraźnie oznaczony: u bukmachera może być inny.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

from typerbot.betting.odds import expected_value, remove_margin
from typerbot.config.settings import Settings
from typerbot.config.sports import football_like
from typerbot.data.repository import OddsView, odds_view
from typerbot.model.markets import Key, label, sort_keys

MARKET_GROUPS: dict[str, list[Key]] = {
    "1X2": [("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0)],
    "DC": [("DC", "1X", 0.0), ("DC", "12", 0.0), ("DC", "X2", 0.0)],
    "OU": [("OU", "O", 2.5), ("OU", "U", 2.5)],
    "BTTS": [("BTTS", "Y", 0.0), ("BTTS", "N", 0.0)],
}
ODDS_SOURCE_LABELS = {"bookmaker": "", "average": "średnia", "best": "najlepszy", "estimated": "szacunkowy",
                      "manual": "ręczny"}
ESTIMATED_NOTE = "kurs szacunkowy – sprawdź u bukmachera"
BOOKMAKER_REFERENCES = ("pinnacle", "bet365")     # kolumny bukmacherów w plikach football-data.co.uk
MARGIN_RANGE = (1.03, 1.10)      # marża kursu szacunkowego (jak w 1X2, w rozsądnych granicach)
MIN_ESTIMATED_ODDS = 1.05        # niższego kursu szacunkowego nie podajemy – bukmacherzy rzadko go oferują,
#                                  a oszacowanie skrajnych prawdopodobieństw jest najmniej pewne


@dataclass
class SelectionEval:
    match_id: int
    key: Key
    p_model: float
    p_market: float | None          # rynek bez marży (średnia kursów)
    probability: float              # prognoza użyta do oceny i kuponu
    odds: float | None              # kurs referencyjny (średnia rynkowa, najwyższy albo bukmacher)
    odds_source: str                # 'bookmaker' | 'average' | 'best' | 'estimated' | 'manual'
    odds_average: float | None
    implied: float | None           # prawdopodobieństwo z kursu referencyjnego bez marży
    bookmaker: str = ""

    @property
    def label(self) -> str:
        return label(self.key)

    @property
    def ev(self) -> float | None:
        """EV przed podatkiem na 1 zł (p·kurs − 1)."""
        return None if self.odds is None else expected_value(self.probability, self.odds)

    @property
    def is_value(self) -> bool:
        return self.ev is not None and self.ev > 0

    def ev_after_tax(self, settings: Settings) -> float | None:
        return None if self.odds is None else expected_value(self.probability, self.odds, settings.tax)

    @property
    def estimated(self) -> bool:
        return self.odds_source == "estimated"

    @property
    def source_label(self) -> str:
        return self.bookmaker.split(".")[0].capitalize() if self.odds_source == "bookmaker" else (
            ODDS_SOURCE_LABELS.get(self.odds_source, self.odds_source))


def _fair(prices: dict[Key, float], keys: list[Key], method: str) -> dict[Key, float]:
    if not all(k in prices for k in keys):
        return {}
    return dict(zip(keys, remove_margin([prices[k] for k in keys], method)))


def _dc_from_1x2(fair: dict[Key, float]) -> dict[Key, float]:
    keys = MARKET_GROUPS["1X2"]
    if not all(k in fair for k in keys):
        return {}
    h, d, a = (fair[k] for k in keys)
    return {("DC", "1X", 0.0): h + d, ("DC", "12", 0.0): h + a, ("DC", "X2", 0.0): d + a}


def _poisson(lam: float, n: int = 11) -> np.ndarray:
    k = np.arange(n)
    return np.exp(-lam + k * math.log(max(lam, 1e-9)) - np.array([math.lgamma(i + 1) for i in k]))


def market_goal_rates(fair: dict[Key, float]) -> tuple[float, float] | None:
    """Oczekiwane gole gospodarzy i gości zgodne z rynkiem (1X2 i – jeśli jest – powyżej/poniżej 2,5)."""
    keys = MARKET_GROUPS["1X2"]
    if not all(k in fair for k in keys):
        return None
    target = [fair[k] for k in keys]
    over = fair.get(("OU", "O", 2.5))
    tri = np.tril(np.ones((11, 11)), -1)

    def loss(x: np.ndarray) -> float:
        m = np.outer(_poisson(math.exp(x[0])), _poisson(math.exp(x[1])))
        probs = [float((m * tri).sum()), float(np.trace(m)), float((m * tri.T).sum())]
        err = sum((a - b) ** 2 for a, b in zip(probs, target))
        if over is not None:
            under = sum(m[i, j] for i in range(3) for j in range(3 - i))
            err += (1 - under - over) ** 2
        return err

    res = minimize(loss, np.log([1.4, 1.1]), method="L-BFGS-B", bounds=[(-3, 1.8), (-3, 1.8)])
    return math.exp(res.x[0]), math.exp(res.x[1])


def derived_probabilities(lam_home: float, lam_away: float) -> dict[Key, float]:
    """BTTS i powyżej/poniżej 2,5 z oczekiwanych goli (rozkład Poissona)."""
    m = np.outer(_poisson(lam_home), _poisson(lam_away))
    under = float(sum(m[i, j] for i in range(3) for j in range(3 - i)))
    btts = float((1 - math.exp(-lam_home)) * (1 - math.exp(-lam_away)))
    return {("OU", "O", 2.5): 1 - under, ("OU", "U", 2.5): under, ("BTTS", "Y", 0.0): btts, ("BTTS", "N", 0.0): 1 - btts}


def _pairs(prices: dict[Key, float]) -> list[list[Key]]:
    """Pary typów jednego rynku z tą samą linią: powyżej/poniżej, handicap gospodarzy/gości, zwycięzca."""
    lines = sorted({(m, line) for m, _, line in prices if m in ("OU", "HCP", "ML")})
    return [[(m, "O", line), (m, "U", line)] if m == "OU" else [(m, "H", line), (m, "A", line)] for m, line in lines]


def fair_probabilities(prices: dict[Key, float], method: str) -> dict[Key, float]:
    """Prawdopodobieństwa bez marży dla wszystkich rynków, dla których są komplety kursów.
    Podwójną szansę liczymy z 1X2 (pewniejsze niż z kursów DC, które się nakładają)."""
    out: dict[Key, float] = {}
    for keys in (MARKET_GROUPS["1X2"], MARKET_GROUPS["BTTS"], *_pairs(prices)):
        out.update(_fair(prices, keys, method))
    dc = _dc_from_1x2(out)
    if dc:
        out.update(dc)
    elif all(k in prices for k in MARKET_GROUPS["DC"]):
        raw = {k: 1 / prices[k] for k in MARKET_GROUPS["DC"]}
        total = sum(raw.values())
        out.update({k: 2 * v / total for k, v in raw.items()})   # w DC prawdopodobieństwa sumują się do 2
    return out


def evaluate_match(match_id: int, model_probs: dict[Key, float], odds_rows: Iterable, settings: Settings,
                   markets: Iterable[str] | None = None, manual_odds: dict[Key, float] | None = None,
                   sport: str = "football") -> list[SelectionEval]:
    """Ocena wszystkich typów meczu dla włączonych rynków."""
    ref_mode = settings.odds.reference
    book = ref_mode if ref_mode in BOOKMAKER_REFERENCES else ""
    view: OddsView = odds_view(list(odds_rows), book)
    method = settings.odds.margin_method
    market_fair = fair_probabilities(view.average, method)
    book_fair = fair_probabilities(view.bookmaker, method)
    # Rynki bez żadnego kursu (np. BTTS, powyżej/poniżej w Ekstraklasie) – prawdopodobieństwo rynku
    # z oczekiwanych goli dopasowanych do kursów 1X2; kurs będzie oznaczony jako szacunkowy.
    derived: dict[Key, float] = {}
    if football_like(sport) and any(k not in market_fair for grp in ("OU", "BTTS") for k in MARKET_GROUPS[grp]):
        rates = market_goal_rates(market_fair)
        if rates is not None:
            derived = {k: p for k, p in derived_probabilities(*rates).items() if k not in market_fair}
            market_fair = {**market_fair, **derived}
    w = min(max(settings.model.model_weight, 0.0), 1.0)
    enabled = set(markets if markets is not None else settings.markets_enabled)

    # Kurs referencyjny i jego źródło.
    reference: dict[Key, tuple[float, str]] = {}
    for key, price in view.average.items():
        reference[key] = (price, "average")
    if ref_mode == "best":
        reference.update({k: (p, "best") for k, p in view.best.items()})
    elif book:
        reference.update({k: (p, "bookmaker") for k, p in view.bookmaker.items()})
    # Rynki bez oferty – kurs szacunkowy: podwójna szansa z 1X2, BTTS i powyżej/poniżej z oczekiwanych
    # goli; marża jak w 1X2 bukmachera referencyjnego (w granicach MARGIN_RANGE).
    ref_1x2 = {k: reference[k][0] for k in MARKET_GROUPS["1X2"] if k in reference}
    if len(ref_1x2) == 3:
        margin = min(max(sum(1 / p for p in ref_1x2.values()), MARGIN_RANGE[0]), MARGIN_RANGE[1])
        dc_fair = _dc_from_1x2(_fair(ref_1x2, MARKET_GROUPS["1X2"], method))
        for key, p in {**dc_fair, **derived}.items():
            if key not in reference and p > 0 and 1 / (p * margin) >= MIN_ESTIMATED_ODDS:
                reference[key] = (round(1 / (p * margin), 2), "estimated")
    # Mecz bez żadnych kursów – kurs szacunkowy z prognozy modelu z typową marżą bukmachera.
    if not reference:
        margin = 1.0 + min(max(settings.odds.estimated_margin, 0.0), 0.5)
        for key, p in model_probs.items():
            if 0.0 < p < 1.0 and 1 / (p * margin) >= MIN_ESTIMATED_ODDS:
                reference[key] = (round(1 / (p * margin), 2), "estimated")
    for key, price in (manual_odds or {}).items():
        reference[key] = (price, "manual")

    out: list[SelectionEval] = []
    for key in sort_keys(model_probs):
        if key[0] not in enabled:
            continue
        p_model = model_probs[key]
        p_market = market_fair.get(key)
        prob = p_model if p_market is None else w * p_model + (1 - w) * p_market
        odds, source = reference.get(key, (None, ""))
        implied = (book_fair.get(key) if source == "bookmaker" else None) or market_fair.get(key)
        if source == "manual" and odds:
            implied = 1 / odds   # kurs z jednego typu – bez marży nie da się usunąć
        out.append(SelectionEval(
            match_id=match_id, key=key, p_model=p_model, p_market=p_market, probability=prob,
            odds=odds, odds_source=source, odds_average=view.average.get(key), implied=implied,
            bookmaker=book if source == "bookmaker" else "",
        ))
    return out
