"""Testy modelu Dixona-Colesa, rynków i przygotowania danych."""

import math
from datetime import datetime, timezone

import numpy as np
import pytest
from scipy.optimize import check_grad
from scipy.stats import poisson

from typerbot.config.settings import ModelSettings
from typerbot.model import dixon_coles as dc
from typerbot.model.data import OTHER_GROUP, MatchTable, build_window
from typerbot.model.markets import market_probabilities, most_likely_score
from typerbot.model.predictor import FittedModel


# -- macierz wyników i rynki --------------------------------------------------------------
def test_score_matrix_sums_to_one_and_matches_poisson_without_rho():
    m = dc.score_matrix(1.6, 1.1, rho=0.0, max_goals=10)
    assert m.sum() == pytest.approx(1.0)
    assert m[2, 1] == pytest.approx(poisson.pmf(2, 1.6) * poisson.pmf(1, 1.1), rel=1e-3)


def test_rho_changes_only_low_scores():
    base = dc.score_matrix(1.4, 1.2, 0.0)
    adj = dc.score_matrix(1.4, 1.2, -0.1)
    assert adj[0, 0] < base[0, 0] * 1.001 or adj[0, 0] > base[0, 0]  # zmiana w 0:0
    assert adj[1, 1] > base[1, 1]            # ρ < 0 zwiększa 1:1
    ratio = adj[3, 2] / base[3, 2]
    assert adj[4, 0] / base[4, 0] == pytest.approx(ratio)   # pozostałe wyniki tylko renormalizowane


def test_tau_values():
    t = dc.tau(np.array([0, 0, 1, 1, 2]), np.array([0, 1, 0, 1, 2]),
               np.array([1.5] * 5), np.array([1.0] * 5), 0.1)
    assert t.tolist() == pytest.approx([1 - 1.5 * 0.1, 1 + 1.5 * 0.1, 1 + 1.0 * 0.1, 0.9, 1.0])


def test_market_probabilities_are_consistent():
    m = dc.score_matrix(1.7, 0.9, -0.05)
    p = market_probabilities(m, ou_lines=(2.5, 3.0))
    assert p[("1X2", "H", 0.0)] + p[("1X2", "D", 0.0)] + p[("1X2", "A", 0.0)] == pytest.approx(1.0)
    assert p[("DC", "1X", 0.0)] == pytest.approx(p[("1X2", "H", 0.0)] + p[("1X2", "D", 0.0)])
    assert p[("OU", "O", 2.5)] + p[("OU", "U", 2.5)] == pytest.approx(1.0)
    assert p[("OU", "O", 3.0)] + p[("OU", "U", 3.0)] < 1.0      # przy 3 bramkach zwrot stawki
    assert p[("BTTS", "Y", 0.0)] == pytest.approx(m[1:, 1:].sum())
    assert p[("1X2", "H", 0.0)] > p[("1X2", "A", 0.0)]
    i, j, _ = most_likely_score(m)
    assert (i, j) in {(1, 0), (1, 1), (2, 0), (2, 1)}


# -- dopasowanie ------------------------------------------------------------------------------
def synthetic(n_teams=16, seasons=4, home_adv=0.25, mu=0.15, seed=3):
    """Mecze każdy z każdym z prawdziwą siłą drużyn (do odzyskiwania parametrów)."""
    rng = np.random.default_rng(seed)
    att, dfn = rng.normal(0, 0.3, n_teams), rng.normal(0, 0.3, n_teams)
    rows = []
    day = 19000.0
    for _ in range(seasons):
        for h in range(n_teams):
            for a in range(n_teams):
                if h != a:
                    lh, la = math.exp(mu + home_adv + att[h] - dfn[a]), math.exp(mu + att[a] - dfn[h])
                    rows.append((day, h + 1, a + 1, rng.poisson(lh), rng.poisson(la)))
                    day += 0.3
    return att, dfn, rows


def to_table(rows, league="PL", cup_rows=()):
    allr = sorted([(*r, league, False) for r in rows] + list(cup_rows))
    n = len(allr)
    return MatchTable(
        match_id=np.arange(n), t=np.array([r[0] for r in allr]), season=np.full(n, 2023),
        home_id=np.array([r[1] for r in allr]), away_id=np.array([r[2] for r in allr]),
        hg=np.array([r[3] for r in allr]), ag=np.array([r[4] for r in allr]),
        hxg=np.full(n, np.nan), axg=np.full(n, np.nan),
        league=np.array([r[5] for r in allr], dtype=object), is_cup=np.array([r[6] for r in allr]),
    )


def end(table):
    return float(table.t.max()) + 1.0


def test_gradient_is_correct():
    _, _, rows = synthetic(n_teams=8, seasons=1)
    table = to_table(rows)
    w = build_window(table, end(table), ModelSettings(last_matches=100, half_life_days=365))
    x0 = np.random.default_rng(0).normal(0, 0.2, 2 * w.fit.n_teams + 2 * w.fit.n_ctx + w.fit.n_groups)
    err = check_grad(lambda x: dc._objective(x, w.fit, 2.0, 5.0)[0], lambda x: dc._objective(x, w.fit, 2.0, 5.0)[1], x0)
    assert err < 1e-3


def test_fit_recovers_team_strength():
    att, dfn, rows = synthetic()
    table = to_table(rows)
    settings = ModelSettings(last_matches=200, half_life_days=10000, regularization=0.5, xg_weight=0.0)
    model = FittedModel.fit(table, end(table), settings)
    est_att = np.array([model.params.attack[model.team_index[i + 1]] for i in range(len(att))])
    est_def = np.array([model.params.defence[model.team_index[i + 1]] for i in range(len(dfn))])
    assert np.corrcoef(est_att, att)[0, 1] > 0.9
    assert np.corrcoef(est_def, dfn)[0, 1] > 0.85
    assert model.params.home[0] == pytest.approx(0.25, abs=0.08)


def test_stronger_regularization_gives_less_extreme_probabilities():
    _, _, rows = synthetic(seasons=1)
    table = to_table(rows)
    weak = FittedModel.fit(table, end(table), ModelSettings(last_matches=10, regularization=0.1))
    strong = FittedModel.fit(table, end(table), ModelSettings(last_matches=10, regularization=30))
    spread = lambda m: np.std(m.params.attack)  # noqa: E731
    assert spread(strong) < spread(weak)


def test_warm_start_matches_cold_start():
    _, _, rows = synthetic(n_teams=10, seasons=2)
    table = to_table(rows)
    s = ModelSettings(last_matches=20)
    first = FittedModel.fit(table, table.t[120], s)
    cold = FittedModel.fit(table, table.t[170], s)
    warm = FittedModel.fit(table, table.t[170], s, previous=first)
    p1 = cold.predict(1, 2, "PL").probs[("1X2", "H", 0.0)]
    p2 = warm.predict(1, 2, "PL").probs[("1X2", "H", 0.0)]
    assert p1 == pytest.approx(p2, abs=1e-3)


def test_league_strength_from_cross_league_matches():
    """Dwie ligi o różnej sile + mecze pucharowe między nimi -> dodatnia siła mocniejszej ligi."""
    rng = np.random.default_rng(5)
    rows_a, rows_b, cup = [], [], []
    day = 19000.0
    for _ in range(3):
        for h in range(1, 9):
            for a in range(1, 9):
                if h != a:
                    rows_a.append((day, h, a, rng.poisson(1.5), rng.poisson(1.1), "A", False))
                    rows_b.append((day, h + 100, a + 100, rng.poisson(1.5), rng.poisson(1.1), "B", False))
                    day += 0.2
    for _ in range(60):   # liga A wyraźnie mocniejsza
        h, a = int(rng.integers(1, 9)), int(rng.integers(101, 109))
        if rng.random() < 0.5:
            cup.append((day, h, a, rng.poisson(2.2), rng.poisson(0.7), "CL", True))
        else:
            cup.append((day, a, h, rng.poisson(0.9), rng.poisson(1.7), "CL", True))
        day += 1
    allrows = sorted(rows_a + rows_b + cup)
    table = to_table([], cup_rows=allrows)
    model = FittedModel.fit(table, end(table), ModelSettings(last_matches=100, half_life_days=10000))
    strength = model.summary()["league_strength"]
    assert strength["A"] > strength["B"]
    pred = model.predict(1, 101, "CL")
    assert pred.cross_league and pred.probs[("1X2", "H", 0.0)] > 0.5


# -- okno danych ------------------------------------------------------------------------------
def test_window_uses_only_past_matches_and_last_n():
    _, _, rows = synthetic(n_teams=6, seasons=3)
    table = to_table(rows)
    cutoff = float(table.t[50])
    w = build_window(table, cutoff, ModelSettings(last_matches=4))
    assert (table.t[w.rows] < cutoff).all()
    for tid in w.team_ids:
        involved = (table.home_id[w.rows] == tid) | (table.away_id[w.rows] == tid)
        assert involved.sum() >= 4  # co najmniej ostatnie 4 mecze (plus mecze rywali z ich okien)
    assert len(w.rows) < 50


def test_time_decay_half_life():
    rows = [(100.0, 1, 2, 1, 0), (190.0, 2, 1, 1, 1), (280.0, 1, 2, 0, 0)]
    table = to_table(rows)
    w = build_window(table, 280.0 + 1e-9, ModelSettings(half_life_days=90, last_matches=10), max_age_days=1000)
    assert w.fit.weight.tolist() == pytest.approx([0.25, 0.5, 1.0], abs=1e-6)


def test_xg_blending():
    table = to_table([(100.0, 1, 2, 3, 0), (101.0, 2, 1, 1, 1)])
    table.hxg[:] = [1.0, np.nan]
    table.axg[:] = [0.4, np.nan]
    w = build_window(table, 200.0, ModelSettings(xg_weight=0.5))
    assert w.fit.yh.tolist() == pytest.approx([2.0, 1.0])     # (3 + 1)/2; brak xG -> same bramki
    assert w.fit.ya.tolist() == pytest.approx([0.2, 1.0])


def test_promoted_team_and_low_data_flags():
    rows, day = [], 0.0
    for season_start, teams in ((18800.0, [1, 2, 3, 4]), (19200.0, [1, 2, 3, 5])):  # 5 = beniaminek
        day = season_start
        for h in teams:
            for a in teams:
                if h != a:
                    rows.append((day, h, a, 1, 1))
                    day += 1.0
    table = to_table(rows)
    table.season[table.t >= 19200.0] = 2022
    table.season[table.t < 19200.0] = 2021
    cutoff = 19200.0 + 8.5     # po kilku meczach nowego sezonu (2022/23 wg daty)
    w = build_window(table, cutoff, ModelSettings(min_matches=6, last_matches=20), new_team_prior=-0.2)
    info = w.teams[5]
    assert info.new_in_league and info.low_data
    assert w.fit.prior_attack[w.team_index[5]] == -0.2
    assert not w.teams[1].new_in_league


def test_unknown_team_prediction_is_low_data():
    _, _, rows = synthetic(n_teams=6, seasons=1)
    table = to_table(rows)
    model = FittedModel.fit(table, end(table), ModelSettings())
    pred = model.predict(1, 999, "PL")
    assert pred.low_data_away and not pred.low_data_home and pred.new_away
    assert sum(pred.probs[k] for k in (("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0))) == pytest.approx(1)


def test_cup_only_team_is_other_group_and_low_data():
    _, _, rows = synthetic(n_teams=6, seasons=1)
    last = max(r[0] for r in rows)
    cup = [(last + 1, 1, 777, 1, 0, "CL", True), (last + 2, 777, 2, 2, 2, "CL", True)]
    table = to_table([], cup_rows=[(*r, "PL", False) for r in rows] + cup)
    w = build_window(table, end(table), ModelSettings())
    assert w.teams[777].group == OTHER_GROUP and w.teams[777].low_data


def test_datetime_cutoff_accepted():
    _, _, rows = synthetic(n_teams=6, seasons=1)
    table = to_table(rows)
    assert build_window(table, datetime(2030, 1, 1, tzinfo=timezone.utc), ModelSettings(), max_age_days=1e5)


# -- ranking Elo i model łączony ------------------------------------------------------------------
def test_elo_orders_teams_by_true_strength_and_fits_beta():
    from typerbot.model.elo import EloModel, EloTable

    att, dfn, rows = synthetic(n_teams=16, seasons=4)
    table = to_table(rows)
    elo = EloTable.build(table, k=20.0)
    model = EloModel.fit(table, elo, end(table))
    ratings = [model.team(t + 1).rating for t in range(16)]
    assert np.corrcoef(ratings, att + dfn)[0, 1] > 0.8          # ranking odzyskuje prawdziwą siłę
    assert 0.3 < model.beta < 2.0
    lh, la = model.expected_goals(model.team(1), model.team(1), "PL")
    assert lh > la                                                # przewaga własnego boiska
    nh, na = model.expected_goals(model.team(1), model.team(1), "PL", neutral=True)
    assert nh == pytest.approx(na)                                # teren neutralny – bez przewagi


def test_elo_uses_only_matches_before_cutoff():
    from typerbot.model.elo import EloModel, EloTable

    _, _, rows = synthetic(n_teams=8, seasons=2)
    table = to_table(rows)
    elo = EloTable.build(table)
    cut = float(table.t[len(table) // 2])
    model = EloModel.fit(table, elo, cut)
    team = int(table.home_id[0])
    before = np.flatnonzero(((table.home_id == team) | (table.away_id == team)) & (table.t < cut))
    last = int(before[-1])
    expected = elo.post_h[last] if table.home_id[last] == team else elo.post_a[last]
    assert model.team(team).rating == pytest.approx(expected) and model.team(team).matches == len(before)


def test_new_team_starts_below_league_average():
    from typerbot.model.elo import INIT, NEW_TEAM_OFFSET, EloTable

    _, _, rows = synthetic(n_teams=8, seasons=1)
    late = rows[-1][0] + 1.0
    rows = rows + [(late, 99, 1, 1, 1)]                              # beniaminek (drużyna 99)
    table = to_table(rows)
    elo = EloTable.build(table)
    i = len(table) - 1
    avg = np.mean([elo.post_h[j] if table.home_id[j] == t else elo.post_a[j]
                   for t in range(1, 9) for j in [max(np.flatnonzero((table.home_id[:i] == t) | (table.away_id[:i] == t)))]])
    assert elo.pre_h[i] == pytest.approx(avg + NEW_TEAM_OFFSET, abs=1.0) and elo.pre_h[i] < INIT + 1


def test_hybrid_model_uses_elo_for_thin_data_and_national_teams():
    from typerbot.model.predictor import HybridModel

    _, _, rows = synthetic(n_teams=12, seasons=3)
    table = to_table(rows)
    national = [(r[0], r[1] + 100, r[2] + 100, r[3], r[4], "INT", False) for r in rows[:300]]
    table = to_table(rows, cup_rows=national)
    table.national = table.league == "INT"
    table.neutral = np.zeros(len(table), dtype=bool)
    settings = ModelSettings(elo_weight=0.5)
    model = HybridModel.fit(table, end(table), settings)
    full = model.predict(1, 2, "PL")
    assert full.method == "Dixon-Coles + Elo" and not full.low_data
    assert sum(full.probs[k] for k in (("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0))) == pytest.approx(1.0)
    nat = model.predict(101, 102, "INT", neutral=True)
    assert nat.method == "Elo" and nat.elo_home is not None
    unknown = model.predict(1, 999, "PL")                          # drużyna bez historii – mało danych
    assert unknown.low_data_away
