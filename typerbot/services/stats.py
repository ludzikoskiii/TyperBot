"""Statystyki historii kuponów – trafność i zwrot w jednostkach (1 kupon = 1 jednostka), bez kwot.

  * kupony: trafność, oczekiwana trafność (średnia szansa z prognozy), zwrot i wynik w jednostkach
    po podatku od stawki, podział na miesiące i krzywa wyniku;
  * pojedyncze typy: trafność osobno dla rynków i lig, porównanie ze średnią prognozą (test kalibracji)
    i wynik, gdyby każdy typ zagrać pojedynczo za 1 jednostkę.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from typerbot.config.settings import SettingsStore
from typerbot.data.db import Database
from typerbot.data.records import parse_iso
from typerbot.services.register import LOST, PENDING, VOID, WON, CouponRegister, StoredCoupon, units_returned

LOCAL = ZoneInfo("Europe/Warsaw")
MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej", "BTTS": "Obie strzelą"}


def local_month(iso: str) -> str:
    return parse_iso(iso).astimezone(LOCAL).strftime("%Y-%m")


@dataclass
class Totals:
    coupons: int = 0
    settled: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    pending: int = 0
    returned: float = 0.0          # zwrot w jednostkach (rozliczone kupony)
    expected_sum: float = 0.0      # suma szans z prognozy (rozstrzygnięte kupony)
    expected_n: int = 0

    @property
    def staked(self) -> int:
        return self.settled

    @property
    def profit(self) -> float:
        return self.returned - self.settled

    @property
    def roi(self) -> float:
        return self.profit / self.settled if self.settled else 0.0

    @property
    def hit_rate(self) -> float:
        decided = self.won + self.lost
        return self.won / decided if decided else 0.0

    @property
    def expected_hit_rate(self) -> float | None:
        return self.expected_sum / self.expected_n if self.expected_n else None


@dataclass
class MonthRow:
    month: str
    coupons: int = 0
    settled: int = 0
    won: int = 0
    lost: int = 0
    returned: float = 0.0

    @property
    def profit(self) -> float:
        return self.returned - self.settled

    @property
    def hit_rate(self) -> float | None:
        decided = self.won + self.lost
        return self.won / decided if decided else None


@dataclass
class GroupRow:
    name: str
    legs: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    odds_sum: float = 0.0
    prob_sum: float = 0.0          # suma prognoz rozstrzygniętych typów
    prob_n: int = 0
    singles_returned: float = 0.0  # zwrot, gdyby każdy typ zagrać pojedynczo za 1 j. (po podatku)
    singles_settled: int = 0

    @property
    def hit_rate(self) -> float | None:
        decided = self.won + self.lost
        return self.won / decided if decided else None

    @property
    def avg_odds(self) -> float:
        return self.odds_sum / self.legs if self.legs else 0.0

    @property
    def avg_probability(self) -> float | None:
        return self.prob_sum / self.prob_n if self.prob_n else None

    @property
    def singles_profit(self) -> float:
        return self.singles_returned - self.singles_settled

    @property
    def singles_roi(self) -> float | None:
        return self.singles_profit / self.singles_settled if self.singles_settled else None


class StatsService:
    def __init__(self, db: Database, now=None):
        self.db = db
        self.register = CouponRegister(db, now=now)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def coupons(self, copied_only: bool = False) -> list[StoredCoupon]:
        return self.register.list(copied_only=copied_only)

    def totals(self, coupons: list[StoredCoupon] | None = None) -> Totals:
        t = Totals()
        for c in coupons if coupons is not None else self.coupons():
            t.coupons += 1
            if c.status == PENDING:
                t.pending += 1
                continue
            t.settled += 1
            t.returned += c.returned or 0.0
            t.won += c.status == WON
            t.lost += c.status == LOST
            t.void += c.status == VOID
            if c.status in (WON, LOST) and c.probability is not None:
                t.expected_sum += c.probability
                t.expected_n += 1
        return t

    def by_month(self, coupons: list[StoredCoupon] | None = None) -> list[MonthRow]:
        rows: dict[str, MonthRow] = {}
        for c in coupons if coupons is not None else self.coupons():
            row = rows.setdefault(local_month(c.created_at), MonthRow(local_month(c.created_at)))
            row.coupons += 1
            if c.status != PENDING:
                row.settled += 1
                row.returned += c.returned or 0.0
                row.won += c.status == WON
                row.lost += c.status == LOST
        return [rows[k] for k in sorted(rows, reverse=True)]

    def _groups(self, key, coupons: list[StoredCoupon] | None) -> list[GroupRow]:
        tax = SettingsStore(self.db).load().tax
        groups: dict[str, GroupRow] = defaultdict(lambda: GroupRow(""))
        seen: set[tuple] = set()      # ten sam typ na kilku kuponach liczymy raz
        for c in coupons if coupons is not None else self.coupons():
            for leg in c.legs:
                ident = (leg.match_id, leg.market, leg.selection, leg.line)
                if ident in seen:
                    continue
                seen.add(ident)
                name = key(leg)
                g = groups[name]
                g.name = name
                g.legs += 1
                g.odds_sum += leg.odds
                g.won += leg.result == WON
                g.lost += leg.result == LOST
                g.void += leg.result == VOID
                if leg.result in (WON, LOST):
                    if leg.probability is not None:
                        g.prob_sum += leg.probability
                        g.prob_n += 1
                    g.singles_settled += 1
                    g.singles_returned += units_returned(leg.odds, tax) if leg.result == WON else 0.0
        return sorted(groups.values(), key=lambda g: -g.legs)

    def by_market(self, coupons: list[StoredCoupon] | None = None) -> list[GroupRow]:
        return self._groups(lambda leg: MARKET_NAMES.get(leg.market, leg.market), coupons)

    def by_league(self, coupons: list[StoredCoupon] | None = None) -> list[GroupRow]:
        names = {r["code"]: r["name"] for r in self.db.query("SELECT code, name FROM leagues")}
        return self._groups(lambda leg: names.get(leg.league, leg.league), coupons)

    def legs_total(self, coupons: list[StoredCoupon] | None = None) -> GroupRow:
        total = GroupRow("Wszystkie typy")
        for g in self.by_market(coupons):
            for f in ("legs", "won", "lost", "void", "odds_sum", "prob_sum", "prob_n", "singles_returned",
                      "singles_settled"):
                setattr(total, f, getattr(total, f) + getattr(g, f))
        return total

    @staticmethod
    def result_date(c: StoredCoupon) -> str:
        """Dzień rozstrzygnięcia kuponu: ostatni mecz na kuponie (a nie moment rozliczenia w aplikacji)."""
        kickoffs = [leg.kickoff for leg in c.legs if leg.kickoff]
        return (max(kickoffs) if kickoffs else (c.settled_at or c.created_at))[:10]

    def equity(self, coupons: list[StoredCoupon] | None = None) -> list[tuple[str, float]]:
        """Skumulowany wynik w jednostkach w kolejności rozstrzygania kuponów."""
        settled = sorted((c for c in (coupons if coupons is not None else self.coupons()) if c.status != PENDING),
                         key=lambda c: (self.result_date(c), c.id))
        total, out = 0.0, []
        for c in settled:
            total += (c.returned or 0.0) - 1.0
            out.append((self.result_date(c), round(total, 2)))
        return out
