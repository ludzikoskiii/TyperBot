"""Statystyki postawionych kuponów: bilans, trafność, ROI, podział na miesiące, rynki, ligi."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from typerbot.data.db import Database
from typerbot.services.register import LOST, PENDING, VOID, WON, CouponRegister, StoredCoupon

MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej", "BTTS": "Obie strzelą"}


@dataclass
class Totals:
    coupons: int = 0
    settled: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    pending: int = 0
    pending_stake: float = 0.0
    staked: float = 0.0          # tylko rozliczone kupony
    returned: float = 0.0

    @property
    def profit(self) -> float:
        return self.returned - self.staked

    @property
    def roi(self) -> float:
        return self.profit / self.staked if self.staked else 0.0

    @property
    def hit_rate(self) -> float:
        decided = self.won + self.lost
        return self.won / decided if decided else 0.0


@dataclass
class MonthRow:
    month: str
    coupons: int
    staked_all: float            # wszystkie stawki postawione w miesiącu (także w grze) – do budżetu
    staked: float                # rozliczone
    returned: float

    @property
    def profit(self) -> float:
        return self.returned - self.staked

    @property
    def roi(self) -> float:
        return self.profit / self.staked if self.staked else 0.0


@dataclass
class GroupRow:
    name: str
    legs: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    odds_sum: float = 0.0
    prob_sum: float = 0.0
    prob_n: int = 0
    coupons: int = 0             # kupony w całości z tej grupy
    coupons_staked: float = 0.0
    coupons_returned: float = 0.0

    @property
    def hit_rate(self) -> float:
        decided = self.won + self.lost
        return self.won / decided if decided else 0.0

    @property
    def avg_odds(self) -> float:
        return self.odds_sum / self.legs if self.legs else 0.0

    @property
    def avg_probability(self) -> float | None:
        return self.prob_sum / self.prob_n if self.prob_n else None

    @property
    def coupons_profit(self) -> float:
        return self.coupons_returned - self.coupons_staked

    @property
    def coupons_roi(self) -> float | None:
        return self.coupons_profit / self.coupons_staked if self.coupons_staked else None


class StatsService:
    def __init__(self, db: Database, now=None):
        self.db = db
        self.register = CouponRegister(db, now=now)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def coupons(self) -> list[StoredCoupon]:
        return self.register.list()

    def totals(self, coupons: list[StoredCoupon] | None = None) -> Totals:
        t = Totals()
        for c in coupons if coupons is not None else self.coupons():
            t.coupons += 1
            if c.status == PENDING:
                t.pending += 1
                t.pending_stake += c.stake
                continue
            t.settled += 1
            t.staked += c.stake
            t.returned += c.payout or 0.0
            t.won += c.status == WON
            t.lost += c.status == LOST
            t.void += c.status == VOID
        return t

    def by_month(self, coupons: list[StoredCoupon] | None = None) -> list[MonthRow]:
        rows: dict[str, MonthRow] = {}
        for c in coupons if coupons is not None else self.coupons():
            month = c.placed_at[:7]
            row = rows.setdefault(month, MonthRow(month, 0, 0.0, 0.0, 0.0))
            row.coupons += 1
            row.staked_all += c.stake
            if c.status != PENDING:
                row.staked += c.stake
                row.returned += c.payout or 0.0
        return [rows[k] for k in sorted(rows, reverse=True)]

    def current_month(self) -> MonthRow:
        month = self._now().strftime("%Y-%m")
        return next((r for r in self.by_month() if r.month == month), MonthRow(month, 0, 0.0, 0.0, 0.0))

    def _groups(self, key, coupons: list[StoredCoupon] | None) -> list[GroupRow]:
        groups: dict[str, GroupRow] = defaultdict(lambda: GroupRow(""))
        for c in coupons if coupons is not None else self.coupons():
            names = set()
            for leg in c.legs:
                name = key(leg)
                names.add(name)
                g = groups[name]
                g.name = name
                g.legs += 1
                g.odds_sum += leg.odds
                if leg.probability is not None:
                    g.prob_sum += leg.probability
                    g.prob_n += 1
                g.won += leg.result == WON
                g.lost += leg.result == LOST
                g.void += leg.result == VOID
            if len(names) == 1 and c.status != PENDING:
                g = groups[names.pop()]
                g.coupons += 1
                g.coupons_staked += c.stake
                g.coupons_returned += c.payout or 0.0
        return sorted(groups.values(), key=lambda g: -g.legs)

    def by_market(self, coupons: list[StoredCoupon] | None = None) -> list[GroupRow]:
        return self._groups(lambda leg: MARKET_NAMES.get(leg.market, leg.market), coupons)

    def by_league(self, coupons: list[StoredCoupon] | None = None) -> list[GroupRow]:
        names = {r["code"]: r["name"] for r in self.db.query("SELECT code, name FROM leagues")}
        return self._groups(lambda leg: names.get(leg.league, leg.league), coupons)

    @staticmethod
    def result_date(c: StoredCoupon) -> str:
        """Dzień rozstrzygnięcia kuponu: ostatni mecz na kuponie (a nie moment rozliczenia w aplikacji)."""
        kickoffs = [leg.kickoff for leg in c.legs if leg.kickoff]
        return (max(kickoffs) if kickoffs else (c.settled_at or c.placed_at))[:10]

    def equity(self, coupons: list[StoredCoupon] | None = None) -> list[tuple[str, float]]:
        """Skumulowany bilans w kolejności rozstrzygania kuponów."""
        settled = sorted((c for c in (coupons if coupons is not None else self.coupons()) if c.status != PENDING),
                         key=lambda c: (self.result_date(c), c.id))
        total, out = 0.0, []
        for c in settled:
            total += (c.payout or 0.0) - c.stake
            out.append((self.result_date(c), round(total, 2)))
        return out
