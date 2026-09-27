"""Testy rejestru kuponów, rozliczania i statystyk."""

from datetime import datetime, timedelta, timezone

import pytest

from typerbot.data.records import MatchRecord
from typerbot.data.repository import LeagueRepository, MatchRepository
from typerbot.services.register import LOST, PENDING, VOID, WON, CouponRegister, LegInput
from typerbot.services.stats import StatsService

NOW = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
KICK = NOW - timedelta(hours=5)


@pytest.fixture
def setup(db):
    LeagueRepository(db).ensure_defaults()
    repo = MatchRepository(db)
    ids = {}
    for ext, (home, away, league) in enumerate([("Legia Warszawa", "Lech Poznan", "EKS"),
                                                 ("Arsenal", "Chelsea", "PL"),
                                                 ("Everton", "Fulham", "PL"),
                                                 ("Raków Częstochowa", "Piast Gliwice", "EKS")]):
        rec = MatchRecord("api_football", str(ext), league, 2026, KICK, home, away)
        ids[ext] = repo.save_records([rec])[0].match_id
    register = CouponRegister(db, now=lambda: NOW)
    return repo, register, ids


def finish(repo, match_id, hg, ag):
    repo.set_score(match_id, hg, ag)


def leg(mid, market="1X2", sel="H", line=0.0, odds=2.0, p=0.5, league="PL"):
    return LegInput(mid, league, market, sel, line, odds, p)


def test_save_validates(setup):
    _, register, ids = setup
    with pytest.raises(ValueError):
        register.save([], 10)
    with pytest.raises(ValueError):
        register.save([leg(ids[0]), leg(ids[0], sel="A")], 10)      # dwa typy z jednego meczu
    with pytest.raises(ValueError):
        register.save([leg(ids[0])], 0)
    cid = register.save([leg(ids[0], odds=1.8), leg(ids[1], odds=2.5)], 20, bookmaker="Superbet")
    c = register.get(cid)
    assert c.odds == pytest.approx(4.5) and c.status == PENDING and len(c.legs) == 2 and c.bookmaker == "Superbet"


def test_won_coupon_with_tax(setup):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], odds=1.8, league="EKS"), leg(ids[1], "OU", "O", 2.5, 2.0)], 10)
    finish(repo, ids[0], 2, 0)
    assert register.settle_pending() == []            # drugi mecz jeszcze trwa
    finish(repo, ids[1], 2, 1)
    assert register.settle_pending() == [cid]
    c = register.get(cid)
    assert c.status == WON and c.payout == pytest.approx(10 * 0.88 * 3.6, abs=0.01)
    assert {x.result for x in c.legs} == {WON}


def test_lost_coupon_settles_early(setup):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], league="EKS"), leg(ids[1])], 10)
    finish(repo, ids[0], 0, 1)                         # 1 przegrane – kupon przegrany od razu
    assert register.settle_pending() == [cid]
    c = register.get(cid)
    assert c.status == LOST and c.payout == 0.0 and c.profit == -10


def test_void_leg_counts_as_one(setup, db):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], odds=2.0, league="EKS"), leg(ids[2], odds=3.0)], 10)
    finish(repo, ids[0], 1, 0)
    with db.transaction() as conn:
        conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE id = ?", (ids[2],))
    register.settle_pending()
    c = register.get(cid)
    assert c.status == WON and c.payout == pytest.approx(10 * 0.88 * 2.0)
    assert [x.result for x in c.legs if x.match_id == ids[2]] == [VOID]


def test_postponed_voids_after_48h(setup, db):
    repo, register, ids = setup
    cid = register.save([leg(ids[3], league="EKS")], 10)
    with db.transaction() as conn:
        conn.execute("UPDATE matches SET status = 'POSTPONED' WHERE id = ?", (ids[3],))
    register.settle_pending()
    assert register.get(cid).status == PENDING         # tylko 5 h po terminie
    later = CouponRegister(db, now=lambda: NOW + timedelta(days=3))
    later.settle_pending()
    c = later.get(cid)
    assert c.status == VOID and c.payout == pytest.approx(8.8)   # zwrot stawki po podatku


def test_manual_result_and_delete(setup):
    _, register, ids = setup
    cid = register.save([leg(ids[0], league="EKS")], 10)
    register.set_manual_result(cid, WON, 15.0, note="cash out")
    c = register.get(cid)
    assert c.status == WON and c.payout == 15.0 and c.manual and c.note == "cash out"
    register.settle_pending()                           # ręczne rozliczenie nie jest nadpisywane
    assert register.get(cid).payout == 15.0
    register.delete(cid)
    assert register.get(cid) is None


def test_stats(setup, db):
    repo, register, ids = setup
    a = register.save([leg(ids[0], odds=2.0, p=0.5, league="EKS")], 10, placed_at=NOW - timedelta(days=40))
    b = register.save([leg(ids[1], "OU", "O", 2.5, 1.9, 0.55), leg(ids[2], odds=2.2, p=0.45)], 20)
    register.save([leg(ids[3], odds=1.5, league="EKS")], 5)   # w grze
    finish(repo, ids[0], 2, 1)
    finish(repo, ids[1], 3, 1)
    finish(repo, ids[2], 0, 0)
    register.settle_pending()
    stats = StatsService(db, now=lambda: NOW)
    t = stats.totals()
    assert (t.coupons, t.settled, t.won, t.lost, t.pending) == (3, 2, 1, 1, 1)
    assert t.staked == 30 and t.returned == pytest.approx(17.6) and t.pending_stake == 5
    assert t.roi == pytest.approx((17.6 - 30) / 30)
    months = stats.by_month()
    assert months[0].month == "2026-09" and months[0].staked_all == 25
    assert stats.current_month().month == "2026-09"
    markets = {g.name: g for g in stats.by_market()}
    assert markets["1X2"].legs == 3 and markets["1X2"].won == 1 and markets["1X2"].lost == 1
    assert markets["Powyżej/poniżej"].won == 1
    assert markets["1X2"].coupons == 1 and markets["1X2"].coupons_profit == pytest.approx(7.6)  # tylko kupon A
    leagues = {g.name: g for g in stats.by_league()}
    assert leagues["Ekstraklasa"].legs == 2 and leagues["Premier League"].coupons == 1
    equity = stats.equity()
    assert len(equity) == 2 and equity[-1][1] == pytest.approx(17.6 - 30)
    assert register.get(a).status == WON and register.get(b).status == LOST
