"""Testy backtestu (walk-forward) i serwisu prognoz."""

import json
import math
from datetime import timedelta

import pytest

from tests.conftest import NOW
from tests.test_sync import make_service
from typerbot.config.settings import CouponSettings, ModelSettings, TaxSettings
from typerbot.demo.world import DemoWorld
from typerbot.model import backtest as bt
from typerbot.model.predictor import FittedModel
from typerbot.services.predict import PredictionService, key_to_str, str_to_key

H, D, A = ("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0)
O, U = ("OU", "O", 2.5), ("OU", "U", 2.5)


@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


@pytest.fixture
def synced(db, secrets, world, clock):
    service, _ = make_service(db, secrets, world, clock)
    service.run_all()
    return service


def row(hg, ag, probs, market=None, bet=None, t=20000.0, league="PL", low=False, mid=1):
    return bt.EvalRow(mid, league, 2024, t, hg, ag, probs, market or {}, bet or {}, set(), low)


# -- metryki --------------------------------------------------------------------------------
def test_metrics_log_loss_brier_accuracy():
    rows = [row(2, 0, {H: 0.5, D: 0.3, A: 0.2}, market={H: 0.4, D: 0.3, A: 0.3}),
            row(0, 0, {H: 0.5, D: 0.3, A: 0.2}, market={H: 0.4, D: 0.3, A: 0.3})]
    m = bt._metrics(rows, "1X2")
    assert m.n == 2 and m.accuracy == 0.5
    assert m.log_loss == pytest.approx((-math.log(0.5) - math.log(0.3)) / 2)
    brier1 = 0.5 ** 2 + 0.3 ** 2 + 0.2 ** 2
    brier2 = 0.5 ** 2 + 0.7 ** 2 + 0.2 ** 2
    assert m.brier == pytest.approx((brier1 + brier2) / 2)
    assert m.market_log_loss == pytest.approx((-math.log(0.4) - math.log(0.3)) / 2)
    assert m.beats_market is True    # 0,5 i 0,3 dla faktycznych wyników vs 0,4 i 0,3 w rynku


def test_calibration_bins():
    rows = [row(1, 0, {O: 0.65, U: 0.35}), row(0, 0, {O: 0.65, U: 0.35}),
            row(3, 1, {O: 0.62, U: 0.38}), row(2, 2, {O: 0.68, U: 0.32})]
    bins, ece = bt._calibration(rows, "OU")
    b60 = next(b for b in bins if b.lo == pytest.approx(0.6))
    assert b60.n == 4 and b60.observed == pytest.approx(0.5)   # 1:0 i 0:0 poniżej, 3:1 i 2:2 powyżej
    assert b60.predicted == pytest.approx((0.65 + 0.65 + 0.62 + 0.68) / 4)
    assert 0 <= ece < 0.5


def test_singles_simulation_with_tax():
    probs = {H: 0.6, D: 0.25, A: 0.15}
    rows = [row(1, 0, probs, bet={H: 2.0, D: 3.5, A: 6.0}, mid=1),        # value na 1 (0.6·2 = 1.2) – trafiony
            row(0, 1, probs, bet={H: 2.0, D: 3.5, A: 6.0}, mid=2, t=20001),  # ten sam typ – nietrafiony
            row(0, 1, probs, bet={H: 1.5, D: 3.5, A: 6.0}, mid=3, t=20002, low=True)]  # mało danych – pomijamy
    cfg = bt.BacktestConfig(leagues=["PL"], seasons=[2024], stake=10)
    res = bt._summarize(rows, cfg, ModelSettings(), CouponSettings(), TaxSettings())
    assert res.singles.bets == 2 and res.singles.hits == 1
    assert res.singles.returned == pytest.approx(10 * 0.88 * 2.0)
    assert res.singles.profit == pytest.approx(17.6 - 20)
    assert res.singles_positive_after_tax.bets == 2          # 0.6·2·0.88 = 1.056 > 1


def test_blend_analysis_prefers_better_source():
    rows = [row(1, 0, {H: 0.34, D: 0.33, A: 0.33}, market={H: 0.7, D: 0.2, A: 0.1}, mid=i) for i in range(20)]
    blend = bt._blend_analysis(rows)
    best_w = min(blend, key=lambda x: x[1])[0]
    assert best_w == 0.0   # rynek trafniejszy od modelu -> najlepiej 0% modelu


def test_week_start_is_monday():
    # 2024-08-17 (sobota) -> poniedziałek 2024-08-12
    from datetime import datetime, timezone
    sat = datetime(2024, 8, 17, 15, tzinfo=timezone.utc).timestamp() / 86400
    monday = bt._week_start(sat)
    assert datetime.fromtimestamp(monday * 86400, tz=timezone.utc).strftime("%Y-%m-%d %a") == "2024-08-12 Mon"


# -- pełny przebieg ------------------------------------------------------------------------------
def test_backtest_has_no_lookahead(synced, monkeypatch):
    seen = []
    original = FittedModel.fit.__func__

    def spy(cls, table, cutoff, settings, previous=None):
        model = original(cls, table, cutoff, settings, previous)
        if model is not None:
            seen.append((cutoff, float(table.t[model.window.rows].max())))
        return model

    monkeypatch.setattr(FittedModel, "fit", classmethod(spy))
    cfg = bt.BacktestConfig(leagues=["PL"], seasons=[2025])
    res = bt.run_backtest(synced.db, cfg, ModelSettings(), CouponSettings(), TaxSettings())
    assert seen and all(last < cutoff for cutoff, last in seen)
    assert all(bt._week_start(r.t) >= min(c for c, _ in seen) for r in res.rows)


def test_backtest_on_demo_data_is_sane(synced):
    cfg = bt.BacktestConfig(leagues=["PL", "EKS"], seasons=[2024, 2025])
    res = bt.run_backtest(synced.db, cfg, ModelSettings(), CouponSettings(), TaxSettings())
    m = res.metrics["1X2"]
    assert m.n > 1000 and 0.40 < m.accuracy < 0.60
    assert 0.95 < m.log_loss < 1.10 and m.market_log_loss is not None
    assert res.ece["1X2"] < 0.03                       # dobrze skalibrowany na danych syntetycznych
    assert res.metrics["OU"].n_market > 0              # kursy O/U z plików CSV (Premier League)
    assert res.coupons.bets > 20
    for rec in res.coupon_records:
        assert 4.5 <= rec.odds <= 5.5 and len({c.match_id for c in rec.selections}) == len(rec.selections)
    run_id = bt.save_run(synced.db, res)
    saved = json.loads(synced.db.query_one("SELECT summary FROM backtest_runs WHERE id = ?", (run_id,))["summary"])
    assert saved["metrics"]["1X2"]["n"] == m.n


def test_default_seasons(synced):
    assert bt.default_seasons(synced.db, ["PL"], current=2026, count=3) == [2023, 2024, 2025]


# -- prognozy ------------------------------------------------------------------------------------
def test_prediction_service_saves_upcoming(synced):
    service = PredictionService(synced.db, now=lambda: NOW)
    preds = service.predict_between(NOW, NOW + timedelta(days=7))
    assert preds
    p = preds[0]
    total = sum(p.prediction.probs[k] for k in (H, D, A))
    assert total == pytest.approx(1.0)
    stored = service.stored(p.match_id)
    assert stored[H] == pytest.approx(p.prediction.probs[H], abs=1e-5)
    promoted = [x for x in preds if "Sunderland" in (x.home, x.away)]
    if promoted:  # beniaminek w świecie demo
        pr = promoted[0].prediction
        assert pr.new_home or pr.new_away


def test_key_serialization_roundtrip():
    for key in (H, O, ("DC", "1X", 0.0), ("BTTS", "N", 0.0)):
        assert str_to_key(key_to_str(key)) == key
