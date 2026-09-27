"""Ocena typów: prawdopodobieństwo, kurs, prawdopodobieństwo implikowane, EV.

Dla każdego typu liczymy trzy prawdopodobieństwa:
  * model  – z modelu Dixona-Colesa,
  * rynek  – z kursów średnich po usunięciu marży,
  * prognoza = w·model + (1−w)·rynek (w – „udział modelu”, dobierany backtestem);
    gdy brak kursów rynku, prognoza = model.
Typ jest „value”, gdy prognoza·kurs > 1 (przed podatkiem – podatek od stawki płaci
się raz za cały kupon). Osobno pokazujemy EV po podatku dla gry pojedynczej.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from typerbot.betting.odds import expected_value, remove_margin
from typerbot.config.settings import Settings
from typerbot.data.repository import OddsView, odds_view
from typerbot.model.markets import Key, label

MARKET_GROUPS: dict[str, list[Key]] = {
    "1X2": [("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0)],
    "DC": [("DC", "1X", 0.0), ("DC", "12", 0.0), ("DC", "X2", 0.0)],
    "OU": [("OU", "O", 2.5), ("OU", "U", 2.5)],
    "BTTS": [("BTTS", "Y", 0.0), ("BTTS", "N", 0.0)],
}
ODDS_SOURCE_LABELS = {"bookmaker": "", "average": "średnia", "best": "najlepszy", "estimated": "szacowany",
                      "manual": "ręczny"}


@dataclass
class SelectionEval:
    match_id: int
    key: Key
    p_model: float
    p_market: float | None          # rynek bez marży (średnia kursów)
    probability: float              # prognoza użyta do oceny i kuponu
    odds: float | None              # kurs referencyjny (np. Superbet)
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


def fair_probabilities(prices: dict[Key, float], method: str) -> dict[Key, float]:
    """Prawdopodobieństwa bez marży dla wszystkich rynków, dla których są komplety kursów.
    Podwójną szansę liczymy z 1X2 (pewniejsze niż z kursów DC, które się nakładają)."""
    out: dict[Key, float] = {}
    for market in ("1X2", "OU", "BTTS"):
        out.update(_fair(prices, MARKET_GROUPS[market], method))
    dc = _dc_from_1x2(out)
    if dc:
        out.update(dc)
    elif all(k in prices for k in MARKET_GROUPS["DC"]):
        raw = {k: 1 / prices[k] for k in MARKET_GROUPS["DC"]}
        total = sum(raw.values())
        out.update({k: 2 * v / total for k, v in raw.items()})   # w DC prawdopodobieństwa sumują się do 2
    return out


def evaluate_match(match_id: int, model_probs: dict[Key, float], odds_rows: Iterable, settings: Settings,
                   markets: Iterable[str] | None = None, manual_odds: dict[Key, float] | None = None
                   ) -> list[SelectionEval]:
    """Ocena wszystkich typów meczu dla włączonych rynków."""
    view: OddsView = odds_view(list(odds_rows), settings.odds.bookmaker)
    method = settings.odds.margin_method
    market_fair = fair_probabilities(view.average, method)
    book_fair = fair_probabilities(view.bookmaker, method)
    w = min(max(settings.model.model_weight, 0.0), 1.0)
    enabled = set(markets if markets is not None else settings.markets_enabled)

    # Kurs referencyjny i jego źródło.
    reference: dict[Key, tuple[float, str]] = {}
    for key, price in view.average.items():
        reference[key] = (price, "average")
    if settings.odds.reference == "best":
        reference.update({k: (p, "best") for k, p in view.best.items()})
    elif settings.odds.reference == "bookmaker":
        reference.update({k: (p, "bookmaker") for k, p in view.bookmaker.items()})
    # Podwójna szansa bez oferty – kurs szacowany z 1X2 (z marżą bukmachera referencyjnego).
    ref_1x2 = {k: reference[k][0] for k in MARKET_GROUPS["1X2"] if k in reference}
    if len(ref_1x2) == 3:
        margin = sum(1 / p for p in ref_1x2.values())
        dc_fair = _dc_from_1x2(_fair(ref_1x2, MARKET_GROUPS["1X2"], method))
        for key, p in dc_fair.items():
            if key not in reference:
                reference[key] = (round(1 / (p * margin), 2), "estimated")
    for key, price in (manual_odds or {}).items():
        reference[key] = (price, "manual")

    out: list[SelectionEval] = []
    for market, keys in MARKET_GROUPS.items():
        if market not in enabled:
            continue
        for key in keys:
            if key not in model_probs:
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
                bookmaker=settings.odds.bookmaker if source == "bookmaker" else "",
            ))
    return out
