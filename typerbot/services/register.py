"""Rejestr postawionych kuponów i automatyczne rozliczanie.

Zasady rozliczania (jak u polskich bukmacherów):
  * zdarzenie rozstrzygane wynikiem po 90 minutach,
  * mecz odwołany, przyznany walkowerem albo przełożony o ponad 48 h – zwrot
    (zdarzenie liczy się po kursie 1,00),
  * kupon przegrywa, gdy przegra choć jedno zdarzenie; wygrywa, gdy wszystkie
    pozostałe są trafione lub zwrócone,
  * wypłata = stawka po podatku × kurs (po zwrotach), minus 10% podatku od wygranej
    powyżej progu.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from typerbot.betting.odds import payout
from typerbot.betting.settlement import settle
from typerbot.config.settings import SettingsStore, TaxSettings
from typerbot.data.db import Database
from typerbot.data.records import AWARDED, CANCELLED, FINISHED, POSTPONED, parse_iso, to_iso
from typerbot.model.markets import label

PENDING, WON, LOST, VOID = "pending", "won", "lost", "void"
STATUS_LABELS = {PENDING: "w grze", WON: "wygrany", LOST: "przegrany", VOID: "zwrot"}
POSTPONE_VOID_AFTER = timedelta(hours=48)


@dataclass
class LegInput:
    match_id: int
    league: str
    market: str
    selection: str
    line: float
    odds: float
    probability: float | None = None
    p_model: float | None = None
    p_market: float | None = None


@dataclass
class StoredLeg:
    id: int
    match_id: int
    league: str
    market: str
    selection: str
    line: float
    odds: float
    probability: float | None
    result: str
    home: str = ""
    away: str = ""
    kickoff: str = ""
    score: str = ""

    @property
    def label(self) -> str:
        return label((self.market, self.selection, self.line))


@dataclass
class StoredCoupon:
    id: int
    placed_at: str
    bookmaker: str
    stake: float
    odds: float
    bookmaker_pays_tax: bool
    status: str
    payout: float | None
    settled_at: str | None
    manual: bool
    probability: float | None
    probability_model: float | None
    probability_market: float | None
    ev: float | None
    note: str
    legs: list[StoredLeg] = field(default_factory=list)

    @property
    def profit(self) -> float | None:
        return None if self.status == PENDING or self.payout is None else self.payout - self.stake

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class CouponRegister:
    def __init__(self, db: Database, now=None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.settings_store = SettingsStore(db)

    # -- zapis ------------------------------------------------------------------------------
    def save(self, legs: list[LegInput], stake: float, *, odds: float | None = None, bookmaker: str = "",
             placed_at: datetime | None = None, bookmaker_pays_tax: bool | None = None,
             probability: float | None = None, probability_model: float | None = None,
             probability_market: float | None = None, ev: float | None = None, note: str = "") -> int:
        """Zapisuje postawiony kupon. `odds` – kurs łączny z kuponu bukmachera (domyślnie iloczyn kursów)."""
        if not legs:
            raise ValueError("Kupon musi mieć co najmniej jedno zdarzenie")
        if stake <= 0:
            raise ValueError("Stawka musi być dodatnia")
        if len({leg.match_id for leg in legs}) != len(legs):
            raise ValueError("Na kuponie może być tylko jedno zdarzenie z danego meczu")
        tax_flag = self.settings_store.load().tax.bookmaker_pays_tax if bookmaker_pays_tax is None else bookmaker_pays_tax
        total = odds or math.prod(leg.odds for leg in legs)
        now = to_iso(self._now())
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO coupons(created_at, placed_at, bookmaker, stake, odds, bookmaker_pays_tax, status, "
                "probability, probability_model, probability_market, ev, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, to_iso(placed_at) if placed_at else now, bookmaker, stake, round(total, 4), int(tax_flag),
                 PENDING, probability, probability_model, probability_market, ev, note),
            )
            coupon_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO coupon_legs(coupon_id, match_id, league_code, market, selection, line, odds, probability, "
                "p_model, p_market) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(coupon_id, leg.match_id, leg.league, leg.market, leg.selection, leg.line, leg.odds, leg.probability,
                  leg.p_model, leg.p_market) for leg in legs],
            )
        return coupon_id

    def delete(self, coupon_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM coupons WHERE id = ?", (coupon_id,))

    def set_manual_result(self, coupon_id: int, status: str, payout_value: float | None = None,
                          note: str | None = None) -> None:
        """Ręczne rozliczenie (np. wypłata wcześniejsza / cash out)."""
        if status not in STATUS_LABELS:
            raise ValueError(f"Nieznany status: {status}")
        coupon = self.get(coupon_id)
        if coupon is None:
            raise ValueError("Nie ma takiego kuponu")
        if payout_value is None:
            payout_value = {LOST: 0.0, PENDING: None}.get(status, self._payout(coupon, coupon.odds))
            if status == VOID:
                payout_value = self._payout(coupon, 1.0)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE coupons SET status = ?, payout = ?, manual = ?, settled_at = ?, note = COALESCE(?, note) "
                "WHERE id = ?",
                (status, payout_value, int(status != PENDING), None if status == PENDING else to_iso(self._now()),
                 note, coupon_id),
            )

    # -- odczyt ------------------------------------------------------------------------------
    def get(self, coupon_id: int) -> StoredCoupon | None:
        rows = self.list(ids=[coupon_id])
        return rows[0] if rows else None

    def list(self, status: str | None = None, ids: list[int] | None = None,
             since: datetime | None = None) -> list[StoredCoupon]:
        sql = "SELECT * FROM coupons WHERE 1 = 1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += ids
        if since:
            sql += " AND placed_at >= ?"
            params.append(to_iso(since))
        coupons = [self._coupon(r) for r in self.db.query(sql + " ORDER BY placed_at DESC, id DESC", tuple(params))]
        if coupons:
            by_id = {c.id: c for c in coupons}
            legs = self.db.query(
                f"SELECT l.*, h.name AS home, a.name AS away, m.kickoff, m.home_goals, m.away_goals FROM coupon_legs l "
                f"JOIN matches m ON m.id = l.match_id JOIN teams h ON h.id = m.home_team_id "
                f"JOIN teams a ON a.id = m.away_team_id WHERE l.coupon_id IN ({','.join('?' * len(by_id))}) "
                f"ORDER BY m.kickoff, l.id", tuple(by_id))
            for r in legs:
                score = f"{r['home_goals']}:{r['away_goals']}" if r["home_goals"] is not None else ""
                by_id[r["coupon_id"]].legs.append(StoredLeg(
                    r["id"], r["match_id"], r["league_code"], r["market"], r["selection"], r["line"], r["odds"],
                    r["probability"], r["result"], r["home"], r["away"], r["kickoff"], score))
        return coupons

    @staticmethod
    def _coupon(r) -> StoredCoupon:
        return StoredCoupon(
            r["id"], r["placed_at"], r["bookmaker"], r["stake"], r["odds"], bool(r["bookmaker_pays_tax"]), r["status"],
            r["payout"], r["settled_at"], bool(r["manual"]), r["probability"], r["probability_model"],
            r["probability_market"], r["ev"], r["note"])

    # -- rozliczanie ---------------------------------------------------------------------------
    def _tax(self, coupon: StoredCoupon) -> TaxSettings:
        tax = self.settings_store.load().tax
        return replace(tax, bookmaker_pays_tax=coupon.bookmaker_pays_tax)

    def _payout(self, coupon: StoredCoupon, odds: float) -> float:
        return round(payout(coupon.stake, odds, self._tax(coupon)), 2)

    def settle_pending(self) -> list[int]:
        """Rozlicza zdarzenia i kupony, których mecze się zakończyły. Zwraca id rozliczonych kuponów."""
        now = self._now()
        changed_legs = []
        for r in self.db.query(
            "SELECT l.id, l.market, l.selection, l.line, m.status, m.home_goals, m.away_goals, m.kickoff "
            "FROM coupon_legs l JOIN matches m ON m.id = l.match_id WHERE l.result = ?", (PENDING,)):
            result = None
            if r["status"] == FINISHED and r["home_goals"] is not None:
                outcome = settle(r["market"], r["selection"], r["line"], r["home_goals"], r["away_goals"])
                result = VOID if outcome is None else (WON if outcome else LOST)
            elif r["status"] in (CANCELLED, AWARDED):
                result = VOID
            elif r["status"] == POSTPONED and now - parse_iso(r["kickoff"]) > POSTPONE_VOID_AFTER:
                result = VOID
            if result:
                changed_legs.append((result, r["id"]))
        if changed_legs:
            with self.db.transaction() as conn:
                conn.executemany("UPDATE coupon_legs SET result = ? WHERE id = ?", changed_legs)

        settled = []
        for coupon in self.list(status=PENDING):
            if coupon.manual:
                continue
            results = [leg.result for leg in coupon.legs]
            if LOST in results:
                status, value = LOST, 0.0
            elif PENDING in results:
                continue
            else:
                void_factor = math.prod(leg.odds for leg in coupon.legs if leg.result == VOID)
                effective = coupon.odds / void_factor if void_factor else coupon.odds
                status = VOID if all(r == VOID for r in results) else WON
                value = self._payout(coupon, 1.0 if status == VOID else effective)
            with self.db.transaction() as conn:
                conn.execute("UPDATE coupons SET status = ?, payout = ?, settled_at = ? WHERE id = ?",
                             (status, value, to_iso(now), coupon.id))
            settled.append(coupon.id)
        return settled

    def matches_to_refresh(self) -> set[str]:
        """Ligi z nierozliczonymi zdarzeniami, których mecze już się odbyły."""
        rows = self.db.query(
            "SELECT DISTINCT l.league_code FROM coupon_legs l JOIN matches m ON m.id = l.match_id "
            "WHERE l.result = ? AND m.kickoff < ?", (PENDING, to_iso(self._now() - timedelta(hours=2))))
        return {r["league_code"] for r in rows}
