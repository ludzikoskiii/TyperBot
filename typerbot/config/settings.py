"""Ustawienia aplikacji zapisywane w tabeli `settings` (JSON).

Nieznane klucze z bazy są ignorowane, a brakujące uzupełniane wartościami
domyślnymi – dzięki temu dodanie nowego ustawienia nie psuje starej bazy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, TypeVar

from typerbot.data.db import Database

MARKETS = ("1X2", "DC", "OU", "BTTS")


@dataclass
class ModelSettings:
    last_matches: int = 20            # liczba ostatnich meczów drużyny
    half_life_days: float = 180.0     # po ilu dniach mecz waży o połowę mniej
    min_matches: int = 6              # poniżej – drużyna oznaczona "mało danych"
    xg_weight: float = 0.5            # udział xG w celu dopasowania (0 = tylko bramki)
    dixon_coles: bool = True          # korekta niskich wyników (0:0, 1:0, 0:1, 1:1)
    regularization: float = 10.0      # ściąganie siły drużyn do średniej ligi (mniej = model pewniejszy siebie)
    new_team_prior: float = -0.15     # startowa siła beniaminka (poniżej średniej ligi)
    max_goals: int = 10
    model_weight: float = 0.3         # udział modelu w prognozie; reszta to rynek (kursy bez marży)


@dataclass
class TaxSettings:
    stake_tax: float = 0.12           # podatek od stawki
    bookmaker_pays_tax: bool = False  # bukmacher pokrywa podatek
    win_tax_enabled: bool = True      # 10% od wygranej powyżej progu
    win_tax_rate: float = 0.10
    win_tax_threshold: float = 2280.0


@dataclass
class OddsSettings:
    region: str = "eu"                # region bukmacherów w The Odds API
    reference: str = "bookmaker"      # kurs do EV: 'bookmaker' (z uzupełnieniem średnią) | 'average' | 'best'
    bookmaker: str = "superbet"       # bukmacher referencyjny (OddsPapi)
    margin_method: str = "proportional"  # 'proportional' | 'shin'
    cache_hours: float = 6.0


@dataclass
class CouponSettings:
    target_odds: float = 5.0
    tolerance: float = 0.10
    min_events: int = 2
    max_events: int = 6
    min_probability: float = 0.55
    mode: str = "probability"         # 'probability' | 'value'
    date_range: str = "days"          # 'today' | 'tomorrow' | 'days' (najbliższe X dni) | 'custom'
    days_ahead: int = 3
    date_from: str = ""               # zakres własny (RRRR-MM-DD), gdy date_range == 'custom'
    date_to: str = ""
    leagues: list[str] = field(default_factory=list)   # puste = wszystkie włączone ligi
    markets: list[str] = field(default_factory=lambda: list(MARKETS))
    include_low_data: bool = False
    min_difference: float = 0.5      # alternatywne kupony różnią się min. połową zdarzeń
    alternatives: int = 3
    stake: float = 10.0               # stawka do wyliczenia wygranej i podatku od wygranej


@dataclass
class SyncSettings:
    fixtures_every_hours: float = 3.0
    xg_daily_budget: int = 50             # ile ze 100 dziennych zapytań API-Football na xG
    event_markets_daily_budget: int = 10  # max meczów dziennie z BTTS/DC z The Odds API
    oddspapi_monthly_budget: int = 250    # łączny limit OddsPapi
    oddspapi_scores_monthly: int = 60     # z tego: wyniki meczów (Ekstraklasa)
    oddspapi_history_monthly: int = 40    # z tego: historia kursów do backtestu
    csv_import: bool = True               # pliki CSV football-data.co.uk (sezon 2025/26, historyczne kursy)
    csv_seasons: int = 7


@dataclass
class BudgetSettings:
    monthly_limit: float = 200.0      # 0 = bez limitu
    warn_at: float = 0.8              # ostrzeżenie po wykorzystaniu 80% limitu
    currency: str = "PLN"


@dataclass
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    tax: TaxSettings = field(default_factory=TaxSettings)
    odds: OddsSettings = field(default_factory=OddsSettings)
    coupon: CouponSettings = field(default_factory=CouponSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    markets_enabled: list[str] = field(default_factory=lambda: list(MARKETS))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Settings":
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        return _from_dict(cls, data if isinstance(data, dict) else {})


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
        return Settings.from_json(row["value"]) if row else Settings()

    def save(self, settings: Settings) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.KEY, settings.to_json()),
            )
