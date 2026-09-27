"""Historia kuponów (bez kwot): zapis wygenerowanych kuponów, rozliczanie i statystyki w jednostkach."""

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
        rec = MatchRecord("oddspapi", str(ext), league, 2026, KICK, home, away)
        ids[ext] = repo.save_records([rec])[0].match_id
    register = CouponRegister(db, now=lambda: NOW)
    return repo, register, ids


def finish(repo, match_id, hg, ag):
    repo.set_score(match_id, hg, ag)


def leg(mid, market="1X2", sel="H", line=0.0, odds=2.0, p=0.5, league="PL", estimated=False):
    return LegInput(mid, league, market, sel, line, odds, p, estimated=estimated)


def test_save_validates_and_dedupes(setup):
    _, register, ids = setup
    with pytest.raises(ValueError):
        register.save([])
    with pytest.raises(ValueError):
        register.save([leg(ids[0]), leg(ids[0], sel="A")])      # dwa typy z jednego meczu
    cid = register.save([leg(ids[0], odds=1.8), leg(ids[1], odds=2.5, estimated=True)], probability=0.3,
                        target_odds=4.5)
    c = register.get(cid)
    assert c.odds == pytest.approx(4.5) and c.status == PENDING and len(c.legs) == 2 and not c.copied
    assert c.target_odds == 4.5 and [x.estimated for x in c.legs] == [False, True]
    # ten sam zestaw typów (inna kolejność) – bez duplikatu; skopiowanie oznacza istniejący wpis
    again = register.save([leg(ids[1], odds=2.5), leg(ids[0], odds=1.8)], copied=True)
    assert again == cid and register.get(cid).copied
    assert len(register.list()) == 1


def test_won_coupon_returns_units_after_tax(setup):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], odds=1.8, league="EKS"), leg(ids[1], "OU", "O", 2.5, 2.0)])
    finish(repo, ids[0], 2, 0)
    assert register.settle_pending() == []            # drugi mecz jeszcze trwa
    assert register.get(cid).decided_legs == 1
    finish(repo, ids[1], 2, 1)
    assert register.settle_pending() == [cid]
    c = register.get(cid)
    assert c.status == WON and c.returned == pytest.approx(0.88 * 3.6) and c.profit == pytest.approx(0.88 * 3.6 - 1)
    assert {x.result for x in c.legs} == {WON} and c.status_label == "trafiony"


def test_lost_coupon_settles_early(setup):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], league="EKS"), leg(ids[1])])
    finish(repo, ids[0], 0, 1)                         # 1 nietrafione – kupon nietrafiony od razu
    assert register.settle_pending() == [cid]
    c = register.get(cid)
    assert c.status == LOST and c.returned == 0.0 and c.profit == -1 and c.status_label == "nietrafiony"


def test_void_leg_counts_as_one(setup, db):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], odds=2.0, league="EKS"), leg(ids[2], odds=3.0)])
    finish(repo, ids[0], 1, 0)
    with db.transaction() as conn:
        conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE id = ?", (ids[2],))
    register.settle_pending()
    c = register.get(cid)
    assert c.status == WON and c.returned == pytest.approx(0.88 * 2.0)
    assert [x.result for x in c.legs if x.match_id == ids[2]] == [VOID]


def test_postponed_voids_after_48h(setup, db):
    repo, register, ids = setup
    cid = register.save([leg(ids[3], league="EKS")])
    with db.transaction() as conn:
        conn.execute("UPDATE matches SET status = 'POSTPONED' WHERE id = ?", (ids[3],))
    register.settle_pending()
    assert register.get(cid).status == PENDING         # tylko 5 h po terminie
    later = CouponRegister(db, now=lambda: NOW + timedelta(days=3))
    later.settle_pending()
    c = later.get(cid)
    assert c.status == VOID and c.returned == pytest.approx(0.88)   # zwrot stawki po podatku


def test_bookmaker_pays_tax(setup, db):
    from typerbot.config.settings import SettingsStore

    repo, register, ids = setup
    store = SettingsStore(db)
    s = store.load()
    s.tax.bookmaker_pays_tax = True
    store.save(s)
    cid = register.save([leg(ids[1], odds=2.0)])
    finish(repo, ids[1], 1, 0)
    register.settle_pending()
    assert register.get(cid).returned == pytest.approx(2.0)


def test_replace_legs_after_manual_change(setup):
    repo, register, ids = setup
    cid = register.save([leg(ids[0], league="EKS"), leg(ids[1])])
    other = register.save([leg(ids[2]), leg(ids[3], league="EKS")])
    # wymiana zdarzenia – ten sam wpis w historii
    assert register.replace_legs(cid, [leg(ids[0], league="EKS"), leg(ids[2], odds=3.0)]) == cid
    c = register.get(cid)
    assert {x.match_id for x in c.legs} == {ids[0], ids[2]} and c.odds == pytest.approx(6.0)
    # zmiana na zestaw, który już jest w historii – zostaje tamten wpis
    assert register.replace_legs(cid, [leg(ids[2]), leg(ids[3], league="EKS")]) == other
    assert register.get(cid) is None
    # kuponu z rozstrzygniętym zdarzeniem nie zmieniamy
    finish(repo, ids[2], 1, 0)
    register.settle_pending()
    assert register.replace_legs(other, [leg(ids[0], league="EKS")]) == other
    assert len(register.get(other).legs) == 2


def test_copied_filter_and_delete(setup):
    _, register, ids = setup
    a = register.save([leg(ids[0], league="EKS")])
    b = register.save([leg(ids[1])])
    register.mark_copied(b)
    assert [c.id for c in register.list(copied_only=True)] == [b]
    register.delete(a)
    assert register.get(a) is None and len(register.list()) == 1


def test_stats_in_units(setup, db):
    repo, register, ids = setup
    a = register.save([leg(ids[0], odds=2.0, p=0.5, league="EKS")], probability=0.5)
    b = register.save([leg(ids[1], "OU", "O", 2.5, 1.9, 0.55), leg(ids[2], odds=2.2, p=0.45)], probability=0.25)
    register.save([leg(ids[3], odds=1.5, league="EKS")])   # w trakcie
    register.mark_copied(a)
    finish(repo, ids[0], 2, 1)
    finish(repo, ids[1], 3, 1)
    finish(repo, ids[2], 0, 0)
    register.settle_pending()
    stats = StatsService(db, now=lambda: NOW)
    t = stats.totals()
    assert (t.coupons, t.settled, t.won, t.lost, t.pending) == (3, 2, 1, 1, 1)
    assert t.staked == 2 and t.returned == pytest.approx(1.76) and t.profit == pytest.approx(1.76 - 2)
    assert t.roi == pytest.approx((1.76 - 2) / 2) and t.hit_rate == 0.5
    assert t.expected_hit_rate == pytest.approx((0.5 + 0.25) / 2)
    months = stats.by_month()
    assert months[0].month == "2026-09" and months[0].coupons == 3 and months[0].won == 1
    markets = {g.name: g for g in stats.by_market()}
    assert markets["1X2"].legs == 3 and markets["1X2"].won == 1 and markets["1X2"].lost == 1
    assert markets["Powyżej/poniżej"].won == 1 and markets["Powyżej/poniżej"].hit_rate == 1.0
    # typy pojedynczo za 1 j.: 1X2 – trafiony 2,0 i nietrafiony 2,2 -> 0,88·2 − 2
    assert markets["1X2"].singles_profit == pytest.approx(0.88 * 2.0 - 2)
    assert markets["1X2"].avg_probability == pytest.approx((0.5 + 0.45) / 2)
    leagues = {g.name: g for g in stats.by_league()}
    assert leagues["Ekstraklasa"].legs == 2 and leagues["Premier League"].won == 1
    legs = stats.legs_total()
    assert (legs.won, legs.lost) == (2, 1)
    equity = stats.equity()
    assert len(equity) == 2 and equity[-1][1] == pytest.approx(1.76 - 2)
    copied = stats.totals(stats.coupons(copied_only=True))
    assert (copied.coupons, copied.won) == (1, 1)
    assert register.get(a).status == WON and register.get(b).status == LOST


def test_export_csv(setup, tmp_path):
    from typerbot.services.register import export_csv

    repo, register, ids = setup
    register.save([leg(ids[0], league="EKS", estimated=True), leg(ids[1])])
    rows = export_csv(register.list(), str(tmp_path / "h.csv"))
    text = (tmp_path / "h.csv").read_text(encoding="utf-8-sig")
    assert rows == 2 and "Wynik (jednostki)" in text and "zł" not in text and "Stawka" not in text
