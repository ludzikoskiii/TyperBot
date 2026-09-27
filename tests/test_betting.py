"""Testy liczenia kursów, marży, podatku, rozliczania typów i optymalizatora kuponu."""

import itertools
import random
from dataclasses import replace

import pytest

from typerbot.betting.coupon import Candidate, coupon_odds, coupon_probability
from typerbot.betting.odds import (
    effective_stake, expected_value, odds_after_tax, overround, payout, remove_margin,
)
from typerbot.betting.optimizer import _score, alternatives, brute_force, conflict_groups, eligible, optimize
from typerbot.betting.settlement import settle
from typerbot.config.settings import CouponSettings, TaxSettings


# -- marża i prawdopodobieństwo implikowane --------------------------------------------------
def test_overround_and_proportional_margin_removal():
    prices = [2.0, 3.4, 4.0]
    assert overround(prices) == pytest.approx(1 / 2 + 1 / 3.4 + 1 / 4 - 1)
    probs = remove_margin(prices)
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] / probs[2] == pytest.approx(4.0 / 2.0)   # proporcje zachowane


def test_fair_odds_have_no_margin():
    assert remove_margin([2.0, 2.0]) == pytest.approx([0.5, 0.5])
    assert remove_margin([2.0, 2.0], "shin") == pytest.approx([0.5, 0.5])


def test_shin_moves_probability_to_favourite():
    prices = [1.30, 5.0, 11.0]
    prop, shin = remove_margin(prices), remove_margin(prices, "shin")
    assert sum(shin) == pytest.approx(1.0)
    assert shin[0] > prop[0] and shin[2] < prop[2]   # outsider traci najwięcej marży


# -- podatek i wypłata ------------------------------------------------------------------------
def test_polish_stake_tax():
    tax = TaxSettings()
    assert effective_stake(100, tax) == pytest.approx(88.0)
    assert payout(10, 2.0, tax) == pytest.approx(17.6)
    assert odds_after_tax(5.0, tax) == pytest.approx(4.4)


def test_bookmaker_pays_tax_option():
    tax = TaxSettings(bookmaker_pays_tax=True)
    assert payout(10, 2.0, tax) == pytest.approx(20.0)


def test_expected_value_before_and_after_tax():
    tax = TaxSettings()
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)             # value przed podatkiem
    assert expected_value(0.55, 2.0, tax) == pytest.approx(0.55 * 2 * 0.88 - 1)
    assert expected_value(0.55, 2.0, tax) < 0                          # ... ale nie po podatku
    assert expected_value(0.6, 1.9, tax) == pytest.approx(0.6 * 1.9 * 0.88 - 1)


# -- rozliczanie ---------------------------------------------------------------------------------
@pytest.mark.parametrize("market,sel,line,hg,ag,expected", [
    ("1X2", "H", 0, 2, 1, 1), ("1X2", "D", 0, 2, 1, 0), ("1X2", "D", 0, 1, 1, 1), ("1X2", "A", 0, 0, 3, 1),
    ("DC", "1X", 0, 1, 1, 1), ("DC", "1X", 0, 0, 1, 0), ("DC", "12", 0, 1, 1, 0), ("DC", "X2", 0, 0, 2, 1),
    ("OU", "O", 2.5, 2, 1, 1), ("OU", "O", 2.5, 1, 1, 0), ("OU", "U", 2.5, 0, 0, 1), ("OU", "O", 3.0, 2, 1, None),
    ("BTTS", "Y", 0, 1, 1, 1), ("BTTS", "Y", 0, 2, 0, 0), ("BTTS", "N", 0, 0, 0, 1),
])
def test_settle(market, sel, line, hg, ag, expected):
    assert settle(market, sel, float(line), hg, ag) == expected


# -- kupony ----------------------------------------------------------------------------------------
def cand(mid, sel, p, o, market="1X2"):
    return Candidate(mid, (market, sel, 2.5 if market == "OU" else 0.0), p, o)


def random_candidates(seed, n_matches=7):
    rng = random.Random(seed)
    out = []
    for mid in range(n_matches):
        for sel in rng.sample(["H", "D", "A"], k=rng.randint(1, 3)):
            p = rng.uniform(0.3, 0.85)
            out.append(cand(mid, sel, p, round(max(1.05, 1 / p * rng.uniform(0.85, 1.15)), 2)))
        if rng.random() < 0.5:
            p = rng.uniform(0.4, 0.7)
            out.append(cand(mid, "O", p, round(1 / p * rng.uniform(0.9, 1.1), 2), "OU"))
    return out


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("mode", ["probability", "value"])
def test_optimizer_matches_brute_force(seed, mode):
    cfg = CouponSettings(target_odds=4.0, tolerance=0.1, min_events=2, max_events=4, min_probability=0.35, mode=mode)
    cands = random_candidates(seed)
    fast, exact = optimize(cands, cfg), brute_force(cands, cfg)
    if exact is None:
        assert fast is None
        return
    assert fast is not None
    # DP na zaokrąglonych kursach może minimalnie odbiegać od pełnego przeszukania
    assert sum(map(_score, fast)) == pytest.approx(sum(map(_score, exact)), abs=0.02)


@pytest.mark.parametrize("seed", range(8))
def test_optimizer_respects_constraints(seed):
    cfg = CouponSettings(target_odds=5.0, tolerance=0.1, min_events=2, max_events=5, min_probability=0.5)
    cands = random_candidates(seed, n_matches=12)
    chosen = optimize(cands, cfg)
    if chosen is None:
        return
    total = coupon_odds(chosen)
    assert 4.5 <= total <= 5.5
    assert 2 <= len(chosen) <= 5
    assert len({c.match_id for c in chosen}) == len(chosen)          # max jeden typ z meczu
    assert all(c.probability >= 0.5 for c in chosen)


def test_equal_odds_prefers_higher_probability():
    cfg = CouponSettings(target_odds=3.0, tolerance=0.05, min_events=2, max_events=2, min_probability=0.3)
    cands = [cand(1, "H", 0.60, 1.75), cand(2, "H", 0.58, 1.72), cand(3, "H", 0.40, 1.73), cand(4, "A", 0.35, 1.74)]
    chosen = optimize(cands, cfg)
    assert {c.match_id for c in chosen} == {1, 2}
    assert coupon_probability(chosen) == pytest.approx(0.60 * 0.58)


def test_no_drift_to_bottom_of_tolerance():
    """Stary algorytm (max szansy) brał kurs 3,61 z dolnej granicy. Teraz porównujemy przy tym samym kursie:
    kupon 4,20 ma większą szansę w przeliczeniu na kurs (0,244·4,2 > 0,25·3,61)."""
    cfg = CouponSettings(target_odds=4.0, tolerance=0.1, min_events=2, max_events=2, min_probability=0.3)
    low = [cand(1, "H", 0.50, 1.90), cand(2, "H", 0.50, 1.90)]          # kurs 3,61, szansa 25,0%
    fair = [cand(3, "H", 0.52, 2.00), cand(4, "H", 0.47, 2.10)]         # kurs 4,20, szansa 24,4%
    chosen = optimize(low + fair, cfg)
    assert {c.match_id for c in chosen} == {3, 4}


def test_fewer_events_when_equal():
    cfg = CouponSettings(target_odds=4.0, tolerance=0.1, min_events=1, max_events=3, min_probability=0.2)
    one = [cand(1, "H", 0.25, 4.0)]                                    # p·kurs = 1,00
    three = [cand(2, "H", 0.63, 1.587), cand(3, "H", 0.63, 1.587), cand(4, "H", 0.63, 1.587)]  # też 1,00
    chosen = optimize(one + three, cfg)
    assert [c.match_id for c in chosen] == [1]


def test_agreement_filter_and_value_mode():
    agree = Candidate(1, ("1X2", "H", 0.0), 0.56, 1.8, p_model=0.60, p_market=0.54)
    wild = Candidate(2, ("1X2", "H", 0.0), 0.60, 1.9, p_model=0.75, p_market=0.53)     # model +22 pkt vs rynek
    other = Candidate(3, ("1X2", "H", 0.0), 0.55, 1.85, p_model=0.56, p_market=0.55)
    cfg = CouponSettings(target_odds=3.4, tolerance=0.1, min_events=2, max_events=2, min_probability=0.3)
    assert 2 not in {c.match_id for c in eligible([agree, wild, other], cfg)}
    chosen = optimize([agree, wild, other], cfg)
    assert {c.match_id for c in chosen} == {1, 3}
    value = replace(cfg, mode="value")
    assert {c.match_id for c in eligible([agree, wild, other], value)} == {1, 2, 3}   # wszystkie mają p·kurs > 1
    assert eligible([Candidate(4, ("1X2", "H", 0.0), 0.5, 1.9)], value) == []          # EV < 0


def test_same_team_not_twice():
    cfg = CouponSettings(target_odds=3.5, tolerance=0.15, min_events=2, max_events=2, min_probability=0.3)
    a = Candidate(1, ("1X2", "H", 0.0), 0.62, 1.85, teams=(10, 11))   # drużyna 11 gra w obu meczach
    b = Candidate(2, ("1X2", "A", 0.0), 0.60, 1.90, teams=(12, 11))
    c = Candidate(3, ("1X2", "H", 0.0), 0.55, 1.88, teams=(13, 14))
    chosen = optimize([a, b, c], cfg)
    assert chosen and not ({1, 2} <= {x.match_id for x in chosen})
    assert len(conflict_groups([a, b, c])) == 2


def test_alternatives_sorted_by_probability():
    cfg = CouponSettings(target_odds=4.0, tolerance=0.15, min_events=2, max_events=4, min_probability=0.35)
    coupons = alternatives(random_candidates(5, n_matches=14), cfg, 3)
    probs = [coupon_probability(c) for c in coupons]
    assert len(coupons) >= 2 and probs == sorted(probs, reverse=True)


def test_optimizer_excludes_matches_and_markets():
    cfg = CouponSettings(target_odds=3.0, tolerance=0.1, min_events=2, max_events=3, min_probability=0.3,
                         markets=["1X2"])
    cands = [cand(1, "H", 0.6, 1.75), cand(2, "H", 0.58, 1.72), cand(3, "H", 0.55, 1.75), cand(4, "O", 0.9, 1.7, "OU")]
    chosen = optimize(cands, cfg, exclude_matches={1})
    assert chosen and 1 not in {c.match_id for c in chosen} and 4 not in {c.match_id for c in chosen}


def test_optimizer_returns_none_when_impossible():
    cfg = CouponSettings(target_odds=50.0, tolerance=0.1, min_events=2, max_events=3, min_probability=0.3)
    assert optimize([cand(1, "H", 0.6, 1.5), cand(2, "H", 0.6, 1.6)], cfg) is None


def test_brute_force_small_example():
    cfg = CouponSettings(target_odds=2.0, tolerance=0.1, min_events=1, max_events=2, min_probability=0.1)
    cands = [cand(1, "H", 0.5, 2.0), cand(1, "A", 0.3, 1.4), cand(2, "H", 0.7, 1.4)]
    best = brute_force(cands, cfg)
    # kandydaci: {H1}: p·kurs = 1,0; {A1,H2}: 0,42·0,98 – najlepszy pojedynczy H1
    assert [(c.match_id, c.key[1]) for c in best] == [(1, "H")]
    assert list(itertools.chain(best))
