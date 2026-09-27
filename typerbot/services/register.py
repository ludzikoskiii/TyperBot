"""Historia wygenerowanych kuponów i automatyczne rozliczanie – bez kwot.

Każdy kupon ułożony przez generator trafia do historii (ten sam zestaw typów tylko raz).
Wynik liczymy w jednostkach: 1 kupon = 1 jednostka stawki, zwrot = kurs po podatku
od stawki (12%, chyba że bukmacher go pokrywa). Podatku od wygranej nie uwzględniamy –
zależy od kwoty, a aplikacja kwot nie zna.

Zasady rozliczania (jak u polskich bukmacherów):
  * zdarzenie rozstrzygane wynikiem po 90 minutach,
  * mecz odwołany, przyznany walkowerem albo przełożony o ponad 48 h – zwrot
    (zdarzenie liczy się po kursie 1,00),
  * kupon jest nietrafiony, gdy przegra choć jedno zdarzenie; trafiony, gdy wszystkie
    pozostałe są trafione lub zwrócone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from typerbot.betting.settlement import settle
from typerbot.config.settings import SettingsStore, TaxSettings
from typerbot.data.db import Database
from typerbot.data.records import AWARDED, CANCELLED, FINISHED, POSTPONED, parse_iso, to_iso
from typerbot.model.markets import label

PENDING, WON, LOST, VOID = "pending", "won", "lost", "void"
STATUS_LABELS = {PENDING: "w trakcie", WON: "trafiony", LOST: "nietrafiony", VOID: "zwrot"}
LEG_LABELS = {PENDING: "w trakcie", WON: "trafiony", LOST: "nietrafiony", VOID: "zwrot (kurs 1,00)"}
POSTPONE_VOID_AFTER = timedelta(hours=48)


def units_returned(odds: float, tax: TaxSettings) -> float:
    """Zwrot z 1 jednostki stawki przy danym kursie – po podatku od stawki."""
    return odds * (1.0 if tax.bookmaker_pays_tax else 1.0 - tax.stake_tax)


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
    estimated: bool = False       # kurs szacunkowy (wyliczony), a nie z oferty bukmachera


def legs_key(legs: list[LegInput]) -> str:
    return "|".join(sorted(f"{x.match_id}:{x.market}:{x.selection}:{x.line:g}" for x in legs))


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
    estimated: bool = False

    @property
    def label(self) -> str:
        return label((self.market, self.selection, self.line))

    @property
    def result_label(self) -> str:
        return LEG_LABELS.get(self.result, self.result)


@dataclass
class StoredCoupon:
    id: int
    created_at: str
    odds: float
    status: str
    returned: float | None          # zwrot w jednostkach (stawka = 1 j.), po podatku; None – w trakcie
    settled_at: str | None
    probability: float | None
    probability_model: float | None
    probability_market: float | None
    copied: bool
    target_odds: float | None
    legs: list[StoredLeg] = field(default_factory=list)

    @property
    def profit(self) -> float | None:
        """Wynik w jednostkach: zwrot − 1 (stawka)."""
        return None if self.status == PENDING or self.returned is None else self.returned - 1.0

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def decided_legs(self) -> int:
        return sum(leg.result != PENDING for leg in self.legs)


class CouponRegister:
    def __init__(self, db: Database, now=None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.settings_store = SettingsStore(db)

    # -- zapis ------------------------------------------------------------------------------
    @staticmethod
    def _validate(legs: list[LegInput]) -> None:
        if not legs:
            raise ValueError("Kupon musi mieć co najmniej jedno zdarzenie")
        if len({leg.match_id for leg in legs}) != len(legs):
            raise ValueError("Na kuponie może być tylko jedno zdarzenie z danego meczu")

    def find(self, legs: list[LegInput]) -> int | None:
        row = self.db.query_one("SELECT id FROM coupons WHERE legs_key = ? ORDER BY id LIMIT 1", (legs_key(legs),))
        return int(row["id"]) if row else None

    def save(self, legs: list[LegInput], *, probability: float | None = None, probability_model: float | None = None,
             probability_market: float | None = None, target_odds: float | None = None, copied: bool = False) -> int:
        """Zapisuje wygenerowany kupon w historii. Ten sam zestaw typów zapisujemy tylko raz (zwraca istniejący)."""
        self._validate(legs)
        existing = self.find(legs)
        if existing is not None:
            if copied:
                self.mark_copied(existing)
            return existing
        total = math.prod(leg.odds for leg in legs)
        now = to_iso(self._now())
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO coupons(created_at, placed_at, stake, odds, status, probability, probability_model, "
                "probability_market, legs_key, copied, target_odds) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, now, round(total, 4), PENDING, probability, probability_model, probability_market,
                 legs_key(legs), int(copied), target_odds),
            )
            coupon_id = int(cur.lastrowid)
            self._insert_legs(conn, coupon_id, legs)
        return coupon_id

    @staticmethod
    def _insert_legs(conn, coupon_id: int, legs: list[LegInput]) -> None:
        conn.executemany(
            "INSERT INTO coupon_legs(coupon_id, match_id, league_code, market, selection, line, odds, probability, "
            "p_model, p_market, estimated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(coupon_id, leg.match_id, leg.league, leg.market, leg.selection, leg.line, leg.odds, leg.probability,
              leg.p_model, leg.p_market, int(leg.estimated)) for leg in legs],
        )

    def replace_legs(self, coupon_id: int, legs: list[LegInput], *, probability: float | None = None,
                     probability_model: float | None = None, probability_market: float | None = None) -> int:
        """Po ręcznej zmianie kuponu (wymiana, usunięcie typu, zmiana kursu) – aktualizuje zapis w historii.
        Gdy taki zestaw już jest w historii, zostaje tamten wpis. Kuponu z rozstrzygniętym zdarzeniem nie zmieniamy."""
        self._validate(legs)
        current = self.get(coupon_id)
        if current is None:
            return self.save(legs, probability=probability, probability_model=probability_model,
                             probability_market=probability_market)
        if current.status != PENDING or current.decided_legs:
            return coupon_id
        other = self.find(legs)
        if other is not None and other != coupon_id:
            if current.copied:
                self.mark_copied(other)
            self.delete(coupon_id)
            return other
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM coupon_legs WHERE coupon_id = ?", (coupon_id,))
            self._insert_legs(conn, coupon_id, legs)
            conn.execute("UPDATE coupons SET odds = ?, probability = ?, probability_model = ?, probability_market = ?, "
                         "legs_key = ? WHERE id = ?",
                         (round(math.prod(leg.odds for leg in legs), 4), probability, probability_model,
                          probability_market, legs_key(legs), coupon_id))
        return coupon_id

    def mark_copied(self, coupon_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE coupons SET copied = 1 WHERE id = ?", (coupon_id,))

    def delete(self, coupon_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM coupons WHERE id = ?", (coupon_id,))

    # -- odczyt ------------------------------------------------------------------------------
    def get(self, coupon_id: int) -> StoredCoupon | None:
        rows = self.list(ids=[coupon_id])
        return rows[0] if rows else None

    def list(self, status: str | None = None, ids: list[int] | None = None, since: datetime | None = None,
             copied_only: bool = False) -> list[StoredCoupon]:
        sql = "SELECT * FROM coupons WHERE 1 = 1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += ids
        if since:
            sql += " AND created_at >= ?"
            params.append(to_iso(since))
        if copied_only:
            sql += " AND copied = 1"
        coupons = [self._coupon(r) for r in self.db.query(sql + " ORDER BY created_at DESC, id DESC", tuple(params))]
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
                    r["probability"], r["result"], r["home"], r["away"], r["kickoff"], score, bool(r["estimated"])))
        return coupons

    @staticmethod
    def _coupon(r) -> StoredCoupon:
        return StoredCoupon(
            r["id"], r["created_at"], r["odds"], r["status"], r["returned"], r["settled_at"], r["probability"],
            r["probability_model"], r["probability_market"], bool(r["copied"]), r["target_odds"])

    # -- rozliczanie ---------------------------------------------------------------------------
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

        tax = self.settings_store.load().tax
        settled = []
        for coupon in self.list(status=PENDING):
            results = [leg.result for leg in coupon.legs]
            if LOST in results:
                status, value = LOST, 0.0
            elif PENDING in results or not results:
                continue
            else:
                effective = math.prod(leg.odds for leg in coupon.legs if leg.result == WON)
                status = VOID if all(r == VOID for r in results) else WON
                value = round(units_returned(effective, tax), 4)
            with self.db.transaction() as conn:
                conn.execute("UPDATE coupons SET status = ?, returned = ?, settled_at = ? WHERE id = ?",
                             (status, value, to_iso(now), coupon.id))
            settled.append(coupon.id)
        return settled


def export_csv(coupons: list[StoredCoupon], path: str) -> int:
    """Eksport historii (wiersz na zdarzenie) do CSV otwieranego w polskim Excelu (średniki, UTF-8 z BOM)."""
    import csv

    def dec(x: float | None, digits: int = 2) -> str:
        return "" if x is None else f"{x:.{digits}f}".replace(".", ",")

    rows = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Nr kuponu", "Wygenerowano", "Skopiowany", "Kurs łączny", "Szansa (prognoza)", "Status",
                    "Wynik (jednostki)", "Data meczu", "Liga", "Mecz", "Typ", "Kurs typu", "Kurs szacunkowy",
                    "Prognoza typu", "Wynik meczu", "Rozstrzygnięcie"])
        for c in coupons:
            for leg in c.legs:
                w.writerow([c.id, c.created_at.replace("T", " ").rstrip("Z"), "tak" if c.copied else "nie",
                            dec(c.odds), dec(c.probability, 4), c.status_label, dec(c.profit),
                            leg.kickoff.replace("T", " ").rstrip("Z"), leg.league, f"{leg.home} – {leg.away}",
                            leg.label, dec(leg.odds), "tak" if leg.estimated else "nie", dec(leg.probability, 4),
                            leg.score, leg.result_label])
                rows += 1
    return rows
