"""Testy etapu 3: ocena typów, generator kuponów, alternatywy, wymiana zdarzeń."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import NOW
from tests.test_betting import random_candidates
from tests.test_sync import make_service
from typerbot.betting.evaluation import evaluate_match, fair_probabilities
from typerbot.betting.optimizer import alternatives, brute_force, optimize
from typerbot.config.settings import CouponSettings, Settings
from typerbot.demo.world import DemoWorld
from typerbot.fmt import plural
from typerbot.services.coupons import CouponService

H, D, A = ("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0)
O, U = ("OU", "O", 2.5), ("OU", "U", 2.5)
MODEL = {H: 0.50, D: 0.28, A: 0.22, ("DC", "1X", 0.0): 0.78, ("DC", "12", 0.0): 0.72, ("DC", "X2", 0.0): 0.50,
         O: 0.55, U: 0.45, ("BTTS", "Y", 0.0): 0.52, ("BTTS", "N", 0.0): 0.48}


def odds_row(book, key, price):
    return {"bookmaker": book, "market": key[0], "selection": key[1], "line": key[2], "price": price}


def rows(**books):
    out = []
    for book, prices in books.items():
        for key, price in prices.items():
            out.append(odds_row(book.replace("_", "."), key, price))
    return out


# -- ocena typów ---------------------------------------------------------------------------------
def test_evaluation_blend_reference_odds_implied_and_value():
    s = Settings()
    s.model.model_weight = 0.3
    odds = rows(superbet={H: 2.10, D: 3.30, A: 3.60}, unibet_eu={H: 2.00, D: 3.40, A: 3.80},
                pinnacle={H: 2.05, D: 3.50, A: 3.90})
    ev = {e.key: e for e in evaluate_match(1, MODEL, odds, s)}
    avg = {k: (odds[i]["price"] + odds[i + 3]["price"] + odds[i + 6]["price"]) / 3 for i, k in enumerate((H, D, A))}
    fair = fair_probabilities({k: round(v, 3) for k, v in avg.items()}, "proportional")
    h = ev[H]
    assert h.p_market == pytest.approx(fair[H], abs=1e-3)
    assert h.probability == pytest.approx(0.3 * 0.50 + 0.7 * h.p_market, abs=1e-6)
    assert h.odds == 2.10 and h.source_label == "Superbet"
    sb = 1 / 2.10 / (1 / 2.10 + 1 / 3.30 + 1 / 3.60)
    assert h.implied == pytest.approx(sb)                       # marża Superbet usunięta
    assert h.ev == pytest.approx(h.probability * 2.10 - 1)
    assert h.is_value == (h.probability * 2.10 > 1)
    assert h.ev_after_tax(s) == pytest.approx(h.probability * 2.10 * 0.88 - 1)
    # Podwójna szansa: brak oferty -> kurs szacunkowy z 1X2 Superbet, prawdopodobieństwo rynku z 1X2
    dc = ev[("DC", "1X", 0.0)]
    assert dc.odds_source == "estimated" and dc.odds > 1.0
    assert dc.p_market == pytest.approx(ev[H].p_market + ev[D].p_market)


def test_evaluation_falls_back_to_average_and_model():
    s = Settings()
    odds = rows(unibet_eu={O: 1.90, U: 1.95}, pinnacle={O: 1.95, U: 1.95})
    ev = {e.key: e for e in evaluate_match(1, MODEL, odds, s)}
    assert ev[O].odds_source == "average" and ev[O].odds == pytest.approx(1.925)
    assert ev[("BTTS", "Y", 0.0)].p_market is None                                # brak kursów rynku
    assert ev[("BTTS", "Y", 0.0)].probability == MODEL[("BTTS", "Y", 0.0)]        # prognoza = model
    assert ev[("BTTS", "Y", 0.0)].odds is None


def test_evaluation_respects_enabled_markets_and_manual_odds():
    s = Settings()
    odds = rows(superbet={H: 2.1, D: 3.3, A: 3.6})
    ev = evaluate_match(1, MODEL, odds, s, markets=["1X2"], manual_odds={H: 2.25})
    assert {e.key[0] for e in ev} == {"1X2"}
    h = next(e for e in ev if e.key == H)
    assert h.odds == 2.25 and h.odds_source == "manual"


def test_model_weight_one_uses_model_only():
    s = Settings()
    s.model.model_weight = 1.0
    ev = evaluate_match(1, MODEL, rows(superbet={H: 2.1, D: 3.3, A: 3.6}), s)
    assert next(e for e in ev if e.key == H).probability == MODEL[H]


# -- alternatywy i ograniczenie nakładania ------------------------------------------------------------
@pytest.mark.parametrize("seed", range(8))
def test_overlap_limit_matches_brute_force(seed):
    cfg = CouponSettings(target_odds=4.0, tolerance=0.15, min_events=2, max_events=4, min_probability=0.35)
    cands = random_candidates(seed, n_matches=7)
    first = optimize(cands, cfg)
    if first is None:
        return
    limits = [(frozenset(c.match_id for c in first), 1)]
    fast, exact = optimize(cands, cfg, overlap_limits=limits), brute_force(cands, cfg, overlap_limits=limits)
    assert (fast is None) == (exact is None)
    if fast:
        assert len({c.match_id for c in fast} & limits[0][0]) <= 1
        score = lambda cs: sum(math.log(c.probability) for c in cs)  # noqa: E731
        assert score(fast) == pytest.approx(score(exact), abs=0.02)


def test_alternatives_differ_by_at_least_half():
    cfg = CouponSettings(target_odds=4.0, tolerance=0.15, min_events=2, max_events=4, min_probability=0.35,
                         min_difference=0.5)
    coupons = alternatives(random_candidates(3, n_matches=14), cfg, 3)
    assert len(coupons) == 3
    for i, a in enumerate(coupons):
        for b in coupons[:i]:
            common = {c.match_id for c in a} & {c.match_id for c in b}
            assert len(common) <= math.floor(0.5 * len(b))


# -- serwis kuponów (dane demo) ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


@pytest.fixture
def coupon_service(db, secrets, world, clock):
    sync, _ = make_service(db, secrets, world, clock)
    sync.run_all()
    return CouponService(db, now=lambda: NOW)


def demo_cfg(**kw):
    cfg = CouponSettings(days_ahead=7, target_odds=4.0, tolerance=0.15, min_probability=0.5)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_generate_three_valid_coupons(coupon_service):
    cfg = demo_cfg()
    coupons = coupon_service.generate(cfg)
    assert len(coupons) == 3
    for i, c in enumerate(coupons):
        assert c.in_range and cfg.min_events <= len(c.legs) <= cfg.max_events
        assert len(c.match_ids) == len(c.legs)                               # jeden typ z meczu
        assert all(leg.selection.probability >= 0.5 for leg in c.legs)
        assert all(leg.rationale and "Forma:" in leg.rationale[1] for leg in c.legs)
        assert all(not leg.match.low_data for leg in c.legs)                 # „mało danych” pominięte
        assert c.odds_after_tax == pytest.approx(c.odds * 0.88)
        assert c.ev == pytest.approx(c.probability * c.odds * 0.88 - 1)
        for prev in coupons[:i]:
            assert len(c.match_ids & prev.match_ids) <= math.floor(0.5 * len(prev.legs))


def test_low_data_teams_can_be_included(coupon_service):
    settings = coupon_service.settings()
    settings.model.min_matches = 10      # beniaminek ma dopiero 6–7 meczów w sezonie
    evaluated = coupon_service.evaluate(demo_cfg())
    flagged = [info for info, _ in evaluated.values() if info.low_data]
    assert flagged, "beniaminek (Sunderland) powinien być oznaczony jako mało danych"
    assert any("mało danych" in f for f in flagged[0].flags)
    assert any(any(f.startswith("beniaminek") for f in info.flags) for info, _ in evaluated.values())
    ids_default = {c.match_id for c in coupon_service.candidates(demo_cfg())}
    ids_all = {c.match_id for c in coupon_service.candidates(demo_cfg(include_low_data=True))}
    assert {i.match_id for i in flagged} <= ids_all and not ({i.match_id for i in flagged} & ids_default)


def test_league_and_market_filters(coupon_service):
    coupons = coupon_service.generate(demo_cfg(leagues=["PL"], markets=["1X2", "DC"]))
    assert coupons
    for c in coupons:
        assert {leg.match.league for leg in c.legs} == {"PL"}
        assert {leg.key[0] for leg in c.legs} <= {"1X2", "DC"}


def test_swap_and_manual_odds_recalculate(coupon_service):
    cfg = demo_cfg()
    coupon = coupon_service.generate(cfg)[0]
    leg = coupon.legs[0]
    options = coupon_service.swap_options(coupon, leg.match.match_id, cfg)
    assert options and options[0].in_range
    new = coupon_service.swap(coupon, leg.match.match_id, options[0])
    assert new.odds == pytest.approx(options[0].new_odds)
    assert (options[0].leg.match.match_id, options[0].leg.key) in {(x.match.match_id, x.key) for x in new.legs}
    manual = coupon_service.set_manual_odds(new, new.legs[0].match.match_id, 2.0)
    assert manual.legs[0].selection.odds == 2.0 and manual.legs[0].selection.odds_source == "manual"
    assert manual.odds == pytest.approx(new.odds / new.legs[0].selection.odds * 2.0)
    smaller = coupon_service.remove(manual, manual.legs[0].match.match_id)
    assert len(smaller.legs) == len(manual.legs) - 1


def test_win_tax_applies_for_large_stake(coupon_service):
    coupon = coupon_service.generate(demo_cfg())[0]
    from dataclasses import replace
    big = replace(coupon, stake=1000.0)       # wypłata > 2280 zł -> 10% podatku od wygranej
    gross = 1000 * 0.88 * coupon.odds
    assert big.payout == pytest.approx(gross * 0.9)
    assert big.odds_after_tax < coupon.odds_after_tax


def test_date_windows(coupon_service):
    local = coupon_service.date_window(demo_cfg(date_range="tomorrow"))
    assert (local[1] - local[0]) <= timedelta(days=1) and local[0] > NOW
    today = coupon_service.date_window(demo_cfg(date_range="today"))
    assert today[0] == NOW and today[1] < NOW + timedelta(days=1)
    custom = coupon_service.date_window(demo_cfg(date_range="custom", date_from="2026-10-01", date_to="2026-10-03"))
    assert custom[0] == datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)   # 1.10 00:00 czasu polskiego
    assert custom[1].date().isoformat() == "2026-10-03"


def test_impossible_target_returns_no_coupons(coupon_service):
    assert coupon_service.generate(demo_cfg(target_odds=500.0, max_events=3)) == []


def test_plural():
    assert [plural(n, "zdarzenie", "zdarzenia", "zdarzeń") for n in (1, 2, 5, 12, 22, 25)] == [
        "1 zdarzenie", "2 zdarzenia", "5 zdarzeń", "12 zdarzeń", "22 zdarzenia", "25 zdarzeń"]
