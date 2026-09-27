"""Wspólny format danych zwracanych przez moduły źródeł.

Każde źródło tłumaczy swoje odpowiedzi na `MatchRecord` – dzięki temu
reszta aplikacji nie zależy od formatu konkretnego API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Statusy meczu wspólne dla wszystkich źródeł.
SCHEDULED = "SCHEDULED"
LIVE = "LIVE"
FINISHED = "FINISHED"
POSTPONED = "POSTPONED"
CANCELLED = "CANCELLED"
AWARDED = "AWARDED"
FINAL_STATUSES = frozenset({FINISHED, CANCELLED, AWARDED})

# Rynki i typy.
MARKET_1X2 = "1X2"      # H / D / A
MARKET_DC = "DC"        # 1X / 12 / X2
MARKET_OU = "OU"        # O / U, z linią (np. 2.5)
MARKET_BTTS = "BTTS"    # Y / N


@dataclass
class OddsQuote:
    bookmaker: str
    market: str
    selection: str
    price: float
    line: float = 0.0
    kind: str = "pre"   # 'pre' – kurs przedmeczowy, 'close' – kurs zamknięcia


@dataclass
class MatchRecord:
    source: str
    external_id: str
    league_code: str
    season: int
    kickoff: datetime
    home: str
    away: str
    status: str = SCHEDULED
    home_goals: int | None = None
    away_goals: int | None = None
    home_xg: float | None = None
    away_xg: float | None = None
    home_shots: int | None = None
    away_shots: int | None = None
    home_sot: int | None = None
    away_sot: int | None = None
    home_hints: tuple[str, ...] = ()
    away_hints: tuple[str, ...] = ()
    odds: list[OddsQuote] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    kickoff_exact: bool = True     # False – znana tylko data (godzina przybliżona)
    neutral: bool = False          # teren neutralny (bez przewagi własnego boiska)

    @property
    def kickoff_iso(self) -> str:
        return to_iso(self.kickoff)


@dataclass
class MatchStats:
    home_xg: float | None = None
    away_xg: float | None = None
    home_shots: int | None = None
    away_shots: int | None = None
    home_sot: int | None = None
    away_sot: int | None = None

    @property
    def has_xg(self) -> bool:
        return self.home_xg is not None and self.away_xg is not None


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parsuje daty ISO 8601 (także z 'Z' i przesunięciem strefy) do UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(text))
