"""Kontrola budżetu: miesięczny limit stawek i bilans bieżącego miesiąca.

Liczymy „przepływ pieniędzy” w miesiącu kalendarzowym (czas polski):
  * wydano   – stawki kuponów postawionych w tym miesiącu (także tych w grze),
  * wypłaty  – wypłaty kuponów rozliczonych w tym miesiącu,
  * bilans   – wypłaty − wydano.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from typerbot.config.settings import SettingsStore
from typerbot.data.db import Database
from typerbot.fmt import num
from typerbot.services.register import PENDING, CouponRegister
from typerbot.services.stats import local_month

LOCAL = ZoneInfo("Europe/Warsaw")
MONTHS = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień",
          "październik", "listopad", "grudzień"]
OK, WARNING, EXCEEDED, NO_LIMIT = "ok", "warning", "exceeded", "none"


def month_label(month: str) -> str:
    year, m = month.split("-")
    return f"{MONTHS[int(m) - 1]} {year}"


@dataclass
class BudgetStatus:
    month: str
    limit: float
    warn_at: float          # próg ostrzeżenia jako ułamek limitu (np. 0.8)
    staked: float           # wydano (postawione w miesiącu)
    pending_stake: float    # z tego w grze
    payouts: float          # wypłaty rozliczone w miesiącu
    coupons: int

    @property
    def label(self) -> str:
        return month_label(self.month)

    @property
    def balance(self) -> float:
        return self.payouts - self.staked

    @property
    def remaining(self) -> float:
        return self.limit - self.staked

    @property
    def fraction(self) -> float | None:
        return self.staked / self.limit if self.limit > 0 else None

    @property
    def level(self) -> str:
        if self.limit <= 0:
            return NO_LIMIT
        if self.staked > self.limit + 1e-9:
            return EXCEEDED
        if self.staked >= self.warn_at * self.limit:
            return WARNING
        return OK

    def after_stake(self, stake: float) -> "BudgetStatus":
        return BudgetStatus(self.month, self.limit, self.warn_at, self.staked + stake, self.pending_stake + stake,
                            self.payouts, self.coupons + 1)

    def summary(self) -> str:
        spent = f"wydano {num(self.staked)} zł" + (f" z {num(self.limit)} zł" if self.limit > 0 else "")
        return f"{self.label.capitalize()}: {spent} · wypłaty {num(self.payouts)} zł · bilans {self.balance:+.2f} zł".replace(
            ".", ",")

    def warning(self) -> str | None:
        if self.level == EXCEEDED:
            return (f"Przekroczono miesięczny limit stawek: {num(self.staked)} zł z {num(self.limit)} zł "
                    f"(o {num(self.staked - self.limit)} zł).")
        if self.level == WARNING:
            return f"Wykorzystano {100 * (self.fraction or 0):.0f}% miesięcznego limitu stawek ({num(self.limit)} zł)."
        return None


class BudgetService:
    def __init__(self, db: Database, now=None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.register = CouponRegister(db, now=self._now)
        self.settings_store = SettingsStore(db)

    def status(self, month: str | None = None) -> BudgetStatus:
        settings = self.settings_store.load().budget
        month = month or self._now().astimezone(LOCAL).strftime("%Y-%m")
        staked = pending = payouts = 0.0
        coupons = 0
        for c in self.register.list():
            if local_month(c.placed_at) == month:
                coupons += 1
                staked += c.stake
                if c.status == PENDING:
                    pending += c.stake
            if c.status != PENDING and c.settled_at and local_month(c.settled_at) == month:
                payouts += c.payout or 0.0
        return BudgetStatus(month, settings.monthly_limit, settings.warn_at, round(staked, 2), round(pending, 2),
                            round(payouts, 2), coupons)

    def check_stake(self, stake: float) -> tuple[bool, str | None]:
        """(czy mieści się w limicie, komunikat ostrzeżenia) dla planowanego kuponu."""
        after = self.status().after_stake(stake)
        if after.level == EXCEEDED:
            return False, (f"Ten kupon przekroczy miesięczny limit stawek: po nim wydasz {num(after.staked)} zł "
                           f"z limitu {num(after.limit)} zł.")
        return True, after.warning()
