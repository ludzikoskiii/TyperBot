"""Zasięg światowy: każdy dzień tygodnia ma mecze z kilku lig, a generator zawsze daje kupon albo powód.

Świat demo: Anglia (Premier League, League One), Polska, Niemcy (3. Liga – tylko OpenLigaDB i openfootball,
bez kursów), Brazylia, Japonia, USA i reprezentacje; czołowe ligi pauzują w oknie FIFA 21.09–06.10.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.test_sync import make_service
from typerbot.config.settings import CouponSettings
from typerbot.data.db import Database
from typerbot.data.records import to_iso
from typerbot.demo.world import DemoWorld
from typerbot.services.coupons import CouponService

MONDAY = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)       # zwykły tydzień sezonu (po przerwie)
IN_BREAK = datetime(2026, 9, 27, 17, 13, tzinfo=timezone.utc)    # termin ze zgłoszenia (przerwa FIFA)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _synced(tmp_path_factory, now, name, **transport_kw):
    db = Database(tmp_path_factory.mktemp(name) / "w.db")
    world = DemoWorld(now)
    service, _ = make_service(db, None, world, Clock(now.timestamp()), now=now, **transport_kw)
    service.run_all()
    return db


@pytest.fixture(scope="module")
def week_db(tmp_path_factory):
    db = _synced(tmp_path_factory, MONDAY, "week")
    yield db
    db.close()


def _day_cfg(day: date, **kw) -> CouponSettings:
    cfg = CouponSettings(date_range="custom", date_from=day.isoformat(), date_to=day.isoformat(), target_odds=3.0,
                         tolerance=0.2, min_probability=0.35)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


@pytest.mark.parametrize("offset", range(7))
def test_every_weekday_has_matches_from_several_leagues_and_a_coupon_or_reason(week_db, offset):
    day = (MONDAY + timedelta(days=offset)).date()
    now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=4)
    service = CouponService(week_db, now=lambda: now)
    start, end = service.date_window(_day_cfg(day))
    leagues = {r["league_code"] for r in week_db.query(
        "SELECT DISTINCT league_code FROM matches WHERE status = 'SCHEDULED' AND kickoff >= ? AND kickoff < ?",
        (to_iso(start), to_iso(end)))}
    assert len(leagues) >= 2, (day, leagues)                  # także we wtorek i w środę
    result = service.run(_day_cfg(day))
    if result.coupons:
        for c in result.coupons:
            assert c.in_range and len(c.match_ids) == len(c.legs)
            if c.estimated_legs:
                assert result.estimated_fallback or service.settings().coupon.estimated_odds == "always"
    else:
        assert result.diagnosis.reasons and result.diagnosis.hints, (day, result.diagnosis.to_text())
    assert result.diagnosis.days and result.diagnosis.days[0].leagues >= 2


def test_matches_without_bookmaker_odds_get_estimated_coupon_clearly_marked(tmp_path_factory):
    db = _synced(tmp_path_factory, MONDAY, "no_odds", fixtures_days=0)   # pliki z kursami jeszcze nieopublikowane
    service = CouponService(db, now=lambda: MONDAY)
    tuesday = (MONDAY + timedelta(days=1)).date()
    result = service.run(_day_cfg(tuesday))
    assert result.coupons and result.estimated_fallback
    assert all(c.estimated_legs == len(c.legs) for c in result.coupons)
    assert "kursami szacunkowymi" in result.diagnosis.notes[0]
    strict = service.run(_day_cfg(tuesday, estimated_odds="never"))
    assert not strict.coupons
    assert "nie ma jeszcze kursów bukmacherów" in strict.diagnosis.reasons[0]
    db.close()


def test_international_break_gives_concrete_reason_and_next_date(tmp_path_factory):
    db = _synced(tmp_path_factory, IN_BREAK, "break")
    service = CouponService(db, now=lambda: IN_BREAK)
    cfg = CouponSettings(days_ahead=3, leagues=["PL", "EKS"])
    result = service.run(cfg)
    assert not result.coupons
    text = " ".join(result.diagnosis.reasons + result.diagnosis.hints)
    assert "W tym terminie grają" in text and "League One" in text        # inne ligi grają
    assert "przerwę w rozgrywkach" in text                                # przerwa rozpoznana
    assert "Najbliższe mecze w wybranych ligach" in text
    anywhere = service.run(CouponSettings(days_ahead=3))                  # wszystkie ligi – mecze są
    assert anywhere.diagnosis.stages[0].matches > 0
    db.close()
