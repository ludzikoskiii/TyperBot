"""Ustawienia aplikacji zapisywane w tabeli `settings` (JSON).

Nieznane klucze z bazy są ignorowane, a brakujące uzupełniane wartościami
domyślnymi – dzięki temu dodanie nowego ustawienia nie psuje starej bazy.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, TypeVar

from typerbot.data.db import Database

MARKETS = ("1X2", "DC", "OU", "BTTS")

SETTINGS_VERSION = 2

# Wersja 2: wartości domyślne po kalibracji modelu (README, „Kalibracja modelu”). Zapisane ustawienia
# z wersji 1 przenosimy tylko wtedy, gdy użytkownik zostawił starą wartość domyślną – własnych nie ruszamy.
_V2_CHANGES: dict[tuple[str, str], tuple[Any, Any]] = {
    ("model", "last_matches"): (20, 80),
    ("model", "half_life_days"): (180.0, 365.0),
    ("model", "regularization"): (10.0, 5.0),
    ("model", "model_weight"): (0.3, 0.0),
    ("odds", "margin_method"): ("proportional", "shin"),
    ("coupon", "max_events"): (6, 4),
    ("coupon", "min_probability"): (0.55, 0.40),
}


@dataclass
class ModelSettings:
    # Wartości domyślne dobrane strojeniem na 6 ligach (sezony 2021–22) i sprawdzone na 8 innych sezonach
    # (football-data.co.uk, 16 tys. meczów) – opis w README, „Kalibracja modelu”.
    last_matches: int = 80            # liczba ostatnich meczów drużyny (w praktyce: wszystkie z 2 lat)
    half_life_days: float = 365.0     # po ilu dniach mecz waży o połowę mniej
    min_matches: int = 6              # poniżej – drużyna oznaczona "mało danych"
    xg_weight: float = 0.5            # udział xG w celu dopasowania (0 = tylko bramki; używany, gdy są dane xG)
    dixon_coles: bool = True          # korekta niskich wyników (0:0, 1:0, 0:1, 1:1)
    regularization: float = 5.0       # ściąganie siły drużyn do średniej ligi (mniej = model pewniejszy siebie)
    new_team_prior: float = -0.15     # startowa siła beniaminka (poniżej średniej ligi)
    max_goals: int = 10
    model_weight: float = 0.0         # udział modelu w prognozie; reszta to rynek (kursy bez marży) –
    #                                   backtest: każdy udział modelu > 0 pogarszał prognozę


@dataclass
class TaxSettings:
    stake_tax: float = 0.12           # podatek od stawki – kurs po podatku = kurs × 0,88
    bookmaker_pays_tax: bool = False  # bukmacher pokrywa podatek


@dataclass
class OddsSettings:
    region: str = "eu"                # region bukmacherów w The Odds API
    reference: str = "bookmaker"      # kurs do EV: 'bookmaker' (z uzupełnieniem średnią) | 'average' | 'best'
    bookmaker: str = "superbet"       # bukmacher referencyjny (OddsPapi)
    margin_method: str = "shin"       # 'proportional' | 'shin' – Shin lepiej ujmuje przewagę faworytów
    cache_hours: float = 6.0


@dataclass
class CouponSettings:
    target_odds: float = 5.0
    tolerance: float = 0.10
    min_events: int = 2
    max_events: int = 4               # mniej zdarzeń = mniej marży bukmachera na kuponie
    min_probability: float = 0.40
    mode: str = "probability"         # 'probability' (najwyższa szansa) | 'value' (tylko typy z przewagą)
    max_divergence: float = 0.08      # tryb 'probability': maks. różnica model − rynek (8 pkt proc.)
    allow_estimated_odds: bool = False  # kursy szacunkowe na kuponie – backtest: typy na szacowanych kursach
    #                                     trafiały rzadziej, niż zapowiadały
    date_range: str = "days"          # 'today' | 'tomorrow' | 'days' (najbliższe X dni) | 'custom'
    days_ahead: int = 3
    date_from: str = ""               # zakres własny (RRRR-MM-DD), gdy date_range == 'custom'
    date_to: str = ""
    leagues: list[str] = field(default_factory=list)   # puste = wszystkie włączone ligi
    markets: list[str] = field(default_factory=lambda: list(MARKETS))
    include_low_data: bool = False
    min_difference: float = 0.5      # alternatywne kupony różnią się min. połową zdarzeń
    alternatives: int = 3


@dataclass
class SyncSettings:
    fixtures_every_hours: float = 3.0     # odświeżanie terminarza i wyników (źródła bez limitu miesięcznego)
    csv_seasons: int = 10                 # ile sezonów historii z football-data.co.uk (model i backtest)
    odds_horizon_days: int = 3            # brakujące kursy uzupełniamy dla meczów z najbliższych X dni
    odds_api_monthly_budget: int = 400    # ile z 500 kredytów The Odds API może zużyć aplikacja
    oddspapi_monthly_budget: int = 200    # ile z 250 zapytań OddsPapi może zużyć aplikacja


@dataclass
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    tax: TaxSettings = field(default_factory=TaxSettings)
    odds: OddsSettings = field(default_factory=OddsSettings)
    coupon: CouponSettings = field(default_factory=CouponSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    markets_enabled: list[str] = field(default_factory=lambda: list(MARKETS))
    version: int = SETTINGS_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Settings":
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        migrate(data)
        return _from_dict(cls, data)


def migrate(data: dict[str, Any]) -> bool:
    """Przenosi zapisane ustawienia do bieżącej wersji (w miejscu). Zwraca True, gdy coś zmieniono."""
    version = data.get("version", 1)
    if isinstance(version, int) and version >= SETTINGS_VERSION:
        return False
    for (section, name), (old, new) in _V2_CHANGES.items():
        part = data.get(section)
        if isinstance(part, dict) and name in part and _same(part[name], old):
            part[name] = new
    data["version"] = SETTINGS_VERSION
    return True


def _same(value: Any, old: Any) -> bool:
    if isinstance(old, str) or isinstance(value, bool):
        return value == old
    return isinstance(value, (int, float)) and math.isclose(value, old)


T = TypeVar("T")


def _from_dict(cls: type[T], data: dict[str, Any]) -> T:
    default = cls()
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        current = getattr(default, f.name)
        value = data[f.name]
        if is_dataclass(current):
            kwargs[f.name] = _from_dict(type(current), value if isinstance(value, dict) else {})
        elif isinstance(current, bool):
            kwargs[f.name] = bool(value)
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)) and not isinstance(value, bool):
            kwargs[f.name] = type(current)(value)
        elif isinstance(current, list) and isinstance(value, list):
            kwargs[f.name] = value
        elif isinstance(current, str) and isinstance(value, str):
            kwargs[f.name] = value
    return cls(**kwargs)


class SettingsStore:
    KEY = "app"

    def __init__(self, db: Database):
        self.db = db

    def load(self) -> Settings:
        row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (self.KEY,))
        if not row:
            return Settings()
        settings = Settings.from_json(row["value"])
        try:
            stored = json.loads(row["value"]).get("version", 1)
        except (TypeError, ValueError, AttributeError):
            stored = SETTINGS_VERSION
        if stored != settings.version:
            self.save(settings)       # migracja zapisana raz – późniejsze zmiany użytkownika zostają
        return settings

    def save(self, settings: Settings) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.KEY, settings.to_json()),
            )
