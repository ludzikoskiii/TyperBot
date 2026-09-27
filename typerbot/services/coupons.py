"""Generator kuponów: ocena typów nadchodzących meczów i dobór kombinacji.

Przepływ: prognozy modelu → ocena typów (mieszanka z rynkiem, kurs referencyjny,
EV) → kandydaci spełniający filtry → optymalizator (3 alternatywy) → uzasadnienia.
Kupon można modyfikować ręcznie – każda wymiana przelicza kurs, szansę i EV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from typerbot.betting.coupon import Candidate
from typerbot.betting.evaluation import SelectionEval, evaluate_match
from typerbot.betting.odds import payout
from typerbot.betting.optimizer import alternatives
from typerbot.betting.rationale import build_rationale
from typerbot.config.settings import CouponSettings, Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.repository import MatchRepository
from typerbot.model.markets import Key
from typerbot.services.predict import PredictionService

LOCAL = ZoneInfo("Europe/Warsaw")
START_BUFFER = timedelta(minutes=5)   # mecze zaczynające się za chwilę pomijamy


@dataclass
class MatchInfo:
    match_id: int
    league: str
    kickoff: str
    home_id: int
    away_id: int
    home: str
    away: str
    lam_home: float
    lam_away: float
    low_data: bool
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"home_id": self.home_id, "away_id": self.away_id, "home": self.home, "away": self.away,
                "kickoff": self.kickoff}


@dataclass
class CouponLeg:
    match: MatchInfo
    selection: SelectionEval
    rationale: list[str] = field(default_factory=list)

    @property
    def key(self) -> Key:
        return self.selection.key


@dataclass
class Coupon:
    legs: list[CouponLeg]
    stake: float
    settings: Settings
    target: tuple[float, float]    # dopuszczalny zakres kursu

    @property
    def odds(self) -> float:
        return math.prod(leg.selection.odds or 1.0 for leg in self.legs)

    @property
    def odds_after_tax(self) -> float:
        """Kurs „na rękę”: wypłata / stawka po podatku od stawki (i od wygranej, jeśli dotyczy)."""
        return payout(self.stake, self.odds, self.settings.tax) / self.stake if self.stake else 0.0

    @property
    def payout(self) -> float:
        return payout(self.stake, self.odds, self.settings.tax)

    @property
    def probability(self) -> float:
        return math.prod(leg.selection.probability for leg in self.legs)

    @property
    def probability_model(self) -> float:
        return math.prod(leg.selection.p_model for leg in self.legs)

    @property
    def probability_market(self) -> float | None:
        values = [leg.selection.p_market for leg in self.legs]
        return None if any(v is None for v in values) else math.prod(values)

    @property
    def ev(self) -> float:
        """EV kuponu po podatku na 1 zł stawki."""
        return self.probability * self.odds_after_tax - 1.0

    @property
    def ev_before_tax(self) -> float:
        return self.probability * self.odds - 1.0

    @property
    def in_range(self) -> bool:
        return self.target[0] <= self.odds <= self.target[1]

    @property
    def match_ids(self) -> set[int]:
        return {leg.match.match_id for leg in self.legs}


@dataclass
class SwapOption:
    leg: CouponLeg
    new_odds: float
    in_range: bool


class CouponService:
    def __init__(self, db: Database, now=None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.settings_store = SettingsStore(db)
        self.predictions = PredictionService(db, now=self._now)
        self.matches = MatchRepository(db)
        self._evaluated: dict[int, tuple[MatchInfo, list[SelectionEval]]] = {}
        self._settings: Settings | None = None

    # -- zakres i ocena -------------------------------------------------------------------
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self.settings_store.load()
        return self._settings

    def date_window(self, cfg: CouponSettings) -> tuple[datetime, datetime]:
        now = self._now()
        today = now.astimezone(LOCAL).date()

        def local(d: date, end: bool = False) -> datetime:
            return datetime.combine(d, time.max if end else time.min, tzinfo=LOCAL).astimezone(timezone.utc)

        if cfg.date_range == "today":
            return now, local(today, end=True)
        if cfg.date_range == "tomorrow":
            day = today + timedelta(days=1)
            return max(now, local(day)), local(day, end=True)
        if cfg.date_range == "custom" and cfg.date_from and cfg.date_to:
            return max(now, local(date.fromisoformat(cfg.date_from))), local(date.fromisoformat(cfg.date_to), end=True)
        return now, now + timedelta(days=max(1, cfg.days_ahead))

    def evaluate(self, cfg: CouponSettings | None = None) -> dict[int, tuple[MatchInfo, list[SelectionEval]]]:
        """Prognozy i ocena wszystkich typów w zakresie dat (wyniki w pamięci do wymiany typów)."""
        settings = self.settings()
        cfg = cfg or settings.coupon
        start, end = self.date_window(cfg)
        leagues = set(cfg.leagues) if cfg.leagues else {
            r["code"] for r in self.db.query("SELECT code FROM leagues WHERE enabled = 1")}
        markets = [m for m in cfg.markets if m in settings.markets_enabled]
        self.predictions.fit(start, settings.model)
        preds = self.predictions.predict_between(start + START_BUFFER, end)
        out: dict[int, tuple[MatchInfo, list[SelectionEval]]] = {}
        for mp in preds:
            if mp.league not in leagues:
                continue
            row = self.db.query_one("SELECT home_team_id, away_team_id FROM matches WHERE id = ?", (mp.match_id,))
            pr = mp.prediction
            flags = []
            if pr.low_data_home or pr.low_data_away:
                who = [n for n, low in ((mp.home, pr.low_data_home), (mp.away, pr.low_data_away)) if low]
                flags.append("mało danych: " + ", ".join(who))
            new = [n for n, flag in ((mp.home, pr.new_home), (mp.away, pr.new_away)) if flag]
            if new:
                flags.append("beniaminek: " + ", ".join(new))
            if pr.cross_league:
                flags.append("drużyny z różnych lig – niższa pewność")
            info = MatchInfo(mp.match_id, mp.league, mp.kickoff, row["home_team_id"], row["away_team_id"],
                             mp.home, mp.away, pr.lam_home, pr.lam_away, pr.low_data, flags)
            evals = evaluate_match(mp.match_id, pr.probs, self.matches.odds_for_match(mp.match_id), settings, markets)
            out[mp.match_id] = (info, evals)
        self._evaluated = out
        return out

    # -- kupony ---------------------------------------------------------------------------------
    def candidates(self, cfg: CouponSettings) -> list[Candidate]:
        out = []
        for info, evals in self._evaluated.values():
            if info.low_data and not cfg.include_low_data:
                continue
            for sel in evals:
                if sel.odds is None:
                    continue
                out.append(Candidate(info.match_id, sel.key, sel.probability, sel.odds, info.league,
                                     sel.odds_source == "estimated"))
        return out

    def generate(self, cfg: CouponSettings | None = None, *, evaluate: bool = True) -> list[Coupon]:
        settings = self.settings()
        cfg = cfg or settings.coupon
        if evaluate or not self._evaluated:
            self.evaluate(cfg)
        coupons = alternatives(self.candidates(cfg), cfg, cfg.alternatives)
        return [self._build(c, cfg) for c in coupons]

    def _leg(self, match_id: int, key: Key, with_rationale: bool = True) -> CouponLeg:
        info, evals = self._evaluated[match_id]
        sel = next(s for s in evals if s.key == key)
        leg = CouponLeg(info, sel)
        if with_rationale:
            leg.rationale = build_rationale(self.db, sel, info.as_dict(), info.lam_home, info.lam_away,
                                            self.settings(), info.flags)
        return leg

    def _build(self, chosen: list[Candidate], cfg: CouponSettings) -> Coupon:
        legs = sorted((self._leg(c.match_id, c.key) for c in chosen), key=lambda leg: leg.match.kickoff)
        return Coupon(legs, cfg.stake, self.settings(), _range(cfg))

    # -- ręczna zmiana -------------------------------------------------------------------------
    def swap_options(self, coupon: Coupon, match_id: int, cfg: CouponSettings | None = None,
                     limit: int = 15) -> list[SwapOption]:
        """Zamienniki dla zdarzenia: inne typy z tego meczu i typy z meczów spoza kuponu."""
        cfg = cfg or self.settings().coupon
        others = [leg for leg in coupon.legs if leg.match.match_id != match_id]
        base = math.prod(leg.selection.odds or 1.0 for leg in others)
        used = {leg.match.match_id for leg in others}
        current = next((leg.key for leg in coupon.legs if leg.match.match_id == match_id), None)
        score = (lambda s: s.probability) if cfg.mode == "probability" else (lambda s: s.probability * (s.odds or 0))
        options = []
        for mid, (info, evals) in self._evaluated.items():
            if mid in used or (info.low_data and not cfg.include_low_data):
                continue
            for sel in evals:
                if sel.odds is None or (mid == match_id and sel.key == current):
                    continue
                if sel.probability < cfg.min_probability:
                    continue
                total = base * sel.odds
                options.append((score(sel), SwapOption(CouponLeg(info, sel), total, coupon.target[0] <= total <= coupon.target[1])))
        options.sort(key=lambda x: (not x[1].in_range, -x[0]))
        return [o for _, o in options[:limit]]

    def swap(self, coupon: Coupon, match_id: int, option: SwapOption) -> Coupon:
        """Nowy kupon z wymienionym zdarzeniem (kurs, szansa i EV przeliczają się same)."""
        leg = self._leg(option.leg.match.match_id, option.leg.key)
        legs = [x for x in coupon.legs if x.match.match_id != match_id] + [leg]
        legs.sort(key=lambda x: x.match.kickoff)
        return replace(coupon, legs=legs)

    def remove(self, coupon: Coupon, match_id: int) -> Coupon:
        return replace(coupon, legs=[x for x in coupon.legs if x.match.match_id != match_id])

    def add(self, coupon: Coupon, match_id: int, key: Key) -> Coupon:
        legs = [x for x in coupon.legs if x.match.match_id != match_id] + [self._leg(match_id, key)]
        legs.sort(key=lambda x: x.match.kickoff)
        return replace(coupon, legs=legs)

    def set_manual_odds(self, coupon: Coupon, match_id: int, odds: float) -> Coupon:
        """Kurs wpisany ręcznie (np. z oferty bukmachera) – przelicza EV i kurs łączny."""
        legs = []
        for leg in coupon.legs:
            if leg.match.match_id == match_id:
                sel = replace(leg.selection, odds=odds, odds_source="manual", implied=1 / odds)
                leg = CouponLeg(leg.match, sel, leg.rationale)
            legs.append(leg)
        return replace(coupon, legs=legs)

    def top_candidate_matches(self, cfg: CouponSettings, n: int = 10) -> list[int]:
        """Mecze z najlepszymi typami – dla nich warto dociągnąć kursy BTTS i podwójnej szansy."""
        best: dict[int, float] = {}
        for c in self.candidates(cfg):
            best[c.match_id] = max(best.get(c.match_id, 0.0), c.probability)
        return [mid for mid, _ in sorted(best.items(), key=lambda x: -x[1])[:n]]


def _range(cfg: CouponSettings) -> tuple[float, float]:
    return cfg.target_odds * (1 - cfg.tolerance), cfg.target_odds * (1 + cfg.tolerance)


def kickoff_local(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL).strftime("%d.%m %H:%M")


__all__ = ["Coupon", "CouponLeg", "CouponService", "MatchInfo", "SwapOption", "kickoff_local"]
