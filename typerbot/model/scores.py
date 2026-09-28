"""Model wyników dla dyscyplin innych niż piłka nożna (futbol amerykański, baseball, hokej, piłka ręczna…).

Siła drużyn z wyników meczów – dla każdej drużyny atak i obrona, dla każdej ligi średnia i przewaga
gospodarzy, mecze starsze ważą mniej (okres połowicznego zaniku w typerbot.config.sports):

  * rozkład normalny (dużo punktów: futbol amerykański, koszykówka, piłka ręczna):
        punkty gospodarzy = średnia + przewaga/2 + atak_gosp − obrona_gości,
        punkty gości      = średnia − przewaga/2 + atak_gości − obrona_gosp;
    dopasowanie: ważona regresja grzbietowa (siły ściągane do średniej ligi); rozrzut różnicy i sumy
    punktów z reszt modelu. Macierz wyników: gęstość normalna różnicy i sumy w punktach siatki;
  * rozkład liczby goli/runów (hokej, baseball) – to samo na logarytmie średniej (regresja Poissona);
    gdy wyniki mają większy rozrzut niż w rozkładzie Poissona (baseball), rozkład ujemny dwumianowy.

W dyscyplinach bez remisów remis po czasie podstawowym rozstrzyga dogrywka: prawdopodobieństwo remisu
przechodzi na wynik o `ot_step` wyższy dla jednej z drużyn (lepsza drużyna ma trochę większą szansę).
Rynki (zwycięzca, handicap, suma) liczymy z tej macierzy wyniku końcowego – tak jak są rozliczane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from typerbot.config.sports import Sport
from typerbot.model.data import MatchTable, to_days
from typerbot.model.markets import most_likely_score, sport_market_probabilities
from typerbot.model.predictor import Prediction

LEAGUE_MIN_MATCHES = 30        # liga z mniejszą liczbą meczów w oknie – „mało danych”
PRIOR_MATCHES = 60.0           # rozrzut punktów: ściąganie do wartości typowej dla dyscypliny
MIN_MATCHES = 20


@dataclass
class ScoreModel:
    sport: Sport
    cutoff: float
    leagues: list[str]
    teams: list[int]
    beta: np.ndarray
    sd_margin: float = 0.0
    sd_total: float = 0.0
    dispersion: float | None = None          # parametr r rozkładu ujemnego dwumianowego (None – Poisson)
    team_n: dict[int, int] = field(default_factory=dict)
    team_league: dict[int, str] = field(default_factory=dict)
    league_n: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.league_index = {c: i for i, c in enumerate(self.leagues)}
        self.team_index = {t: i for i, t in enumerate(self.teams)}

    # -- dopasowanie --------------------------------------------------------------------------------------
    @classmethod
    def fit(cls, table: MatchTable, cutoff: datetime | float, sport: Sport, *, half_life: float | None = None,
            ridge: float | None = None) -> "ScoreModel | None":
        cut = cutoff if isinstance(cutoff, float) else to_days(cutoff)
        end = int(np.searchsorted(table.t, cut, side="left"))
        start = int(np.searchsorted(table.t, cut - sport.window_days, side="left"))
        rows = np.arange(start, end)
        if rows.size < MIN_MATCHES:
            return None
        home, away = table.home_id[rows], table.away_id[rows]
        league = table.league[rows].astype(str)
        teams = sorted(set(home.tolist()) | set(away.tolist()))
        leagues = sorted(set(league.tolist()))
        ti = {t: i for i, t in enumerate(teams)}
        li = {c: i for i, c in enumerate(leagues)}
        n, n_t, n_l = rows.size, len(teams), len(leagues)
        hl = max(half_life or sport.half_life_days, 1.0)
        w = np.power(0.5, (cut - table.t[rows]) / hl)
        neutral = table.neutral[rows].astype(bool) if table.neutral is not None else np.zeros(n, dtype=bool)
        at_home = (~neutral).astype(float)
        hg, ag = table.hg[rows].astype(float), table.ag[rows].astype(float)

        r = np.arange(n)
        lg = np.array([li[x] for x in league.tolist()], dtype=np.int64)
        hi = np.array([ti[x] for x in home.tolist()], dtype=np.int64)
        ai = np.array([ti[x] for x in away.tolist()], dtype=np.int64)
        p = 2 * n_l + 2 * n_t
        x = np.zeros((2 * n, p))
        x[r, lg] = 1.0
        x[n + r, lg] = 1.0
        if sport.model == "normal":
            x[r, n_l + lg] = 0.5 * at_home
            x[n + r, n_l + lg] = -0.5 * at_home
        else:
            x[r, n_l + lg] = at_home
        x[r, 2 * n_l + hi] = 1.0
        x[r, 2 * n_l + n_t + ai] = -1.0
        x[n + r, 2 * n_l + ai] = 1.0
        x[n + r, 2 * n_l + n_t + hi] = -1.0
        y = np.concatenate([hg, ag])
        wts = np.concatenate([w, w])
        lam = max(ridge if ridge is not None else sport.ridge, 1e-6)
        penalty = np.zeros(p)
        penalty[2 * n_l:] = lam

        model = cls(sport, cut, leagues, teams, np.zeros(p))
        if sport.model == "normal":
            beta = _ridge(x, y, wts, penalty)
            res = y - x @ beta
            rd, rt = res[:n] - res[n:], res[:n] + res[n:]
            n_eff = float(w.sum() ** 2 / max((w * w).sum(), 1e-12))
            dof = n / max(n - p / 2.0, 1.0)
            var_d = float((w * rd * rd).sum() / w.sum()) * dof
            var_t = float((w * rt * rt).sum() / w.sum()) * dof
            prior = PRIOR_MATCHES
            model.sd_margin = sport.sd_scale * math.sqrt((n_eff * var_d + prior * sport.sd_margin ** 2) / (n_eff + prior))
            model.sd_total = sport.sd_scale * math.sqrt((n_eff * var_t + prior * sport.sd_total ** 2) / (n_eff + prior))
        else:
            mean = max(float(y.mean()), 0.1)
            penalty[2 * n_l:] = lam * mean          # „mecze wirtualne” w skali wag regresji Poissona
            beta, disp = _count_fit(x, y, wts, penalty, league, leagues, hg, ag)
            model.dispersion = disp
        model.beta = beta
        counts: dict[int, dict[str, int]] = {}
        for h, a, c in zip(home.tolist(), away.tolist(), league.tolist()):
            for t in (h, a):
                counts.setdefault(t, {}).setdefault(c, 0)
                counts[t][c] += 1
        model.team_n = {t: sum(v.values()) for t, v in counts.items()}
        model.team_league = {t: max(v, key=v.get) for t, v in counts.items()}
        codes, n_by = np.unique(league, return_counts=True)
        model.league_n = dict(zip(codes.tolist(), (int(c) for c in n_by)))
        return model

    # -- prognoza ------------------------------------------------------------------------------------------
    def _team(self, team_id: int) -> tuple[float, float]:
        i = self.team_index.get(team_id)
        if i is None:
            return 0.0, 0.0
        n_l, n_t = len(self.leagues), len(self.teams)
        return float(self.beta[2 * n_l + i]), float(self.beta[2 * n_l + n_t + i])

    def _league(self, league: str) -> tuple[float, float]:
        n_l = len(self.leagues)
        c = self.league_index.get(league)
        if c is None:
            return float(np.mean(self.beta[:n_l])), float(np.mean(self.beta[n_l:2 * n_l]))
        return float(self.beta[c]), float(self.beta[n_l + c])

    def expected(self, home_id: int, away_id: int, league: str, neutral: bool = False) -> tuple[float, float]:
        """Oczekiwane punkty (gole, runy) gospodarzy i gości w czasie podstawowym."""
        mu, adv = self._league(league)
        ah, dh = self._team(home_id)
        aa, da = self._team(away_id)
        adv = 0.0 if neutral else adv
        if self.sport.model == "normal":
            return max(mu + adv / 2 + ah - da, 0.5), max(mu - adv / 2 + aa - dh, 0.5)
        return (math.exp(min(mu + adv + ah - da, 4.0)), math.exp(min(mu + aa - dh, 4.0)))

    def regulation_matrix(self, eh: float, ea: float) -> np.ndarray:
        size = self.sport.max_score + 1
        k = np.arange(size, dtype=float)
        if self.sport.model == "normal":
            d = np.subtract.outer(k, k)
            t = np.add.outer(k, k)
            logf = -0.5 * ((d - (eh - ea)) / self.sd_margin) ** 2 - 0.5 * ((t - (eh + ea)) / self.sd_total) ** 2
            m = np.exp(logf - logf.max())
        else:
            m = np.outer(_count_pmf(eh, size, self.dispersion), _count_pmf(ea, size, self.dispersion))
        return m / m.sum()

    def final_matrix(self, eh: float, ea: float) -> np.ndarray:
        m = self.regulation_matrix(eh, ea)
        return m if self.sport.draws else resolve_draws(m, self.sport.ot_step)

    def default_lines(self, m: np.ndarray) -> tuple[list[float], list[float]]:
        """Linie sumy i handicapu wokół mediany (typy blisko 50%) – gdy bukmacher nie podał swoich."""
        s = self.sport
        n = m.shape[0]
        totals = np.add.outer(np.arange(n), np.arange(n))
        diff = np.subtract.outer(np.arange(n), np.arange(n))
        mid_t = _median(np.bincount(totals.ravel(), weights=m.ravel()))
        ou = sorted({max(0.5, math.floor(mid_t) + 0.5 + k * s.total_step) for k in (-1, 0, 1)})
        if s.hcp_lines:
            hcp = list(s.hcp_lines)
        elif s.hcp_step:
            dist = np.bincount((diff + n - 1).ravel(), weights=m.ravel())
            mid_d = _median(dist) - (n - 1)
            base = math.floor(-mid_d) + 0.5
            hcp = sorted({base + k * s.hcp_step for k in (-1, 0, 1)})
        else:
            hcp = []
        return ou, hcp

    def predict(self, home_id: int, away_id: int, league: str, *, neutral: bool = False,
                ou_lines: tuple[float, ...] | list[float] = (), hcp_lines: tuple[float, ...] | list[float] = (),
                keep_matrix: bool = False) -> Prediction:
        eh, ea = self.expected(home_id, away_id, league, neutral)
        m = self.final_matrix(eh, ea)
        ou, hcp = self.default_lines(m)
        probs = sport_market_probabilities(m, self.sport.draws, [*ou, *ou_lines], [*hcp, *hcp_lines])
        thin = self.league_n.get(league, 0) < LEAGUE_MIN_MATCHES
        low_h = thin or self.team_n.get(home_id, 0) < self.sport.min_matches
        low_a = thin or self.team_n.get(away_id, 0) < self.sport.min_matches
        lh, la = self.team_league.get(home_id), self.team_league.get(away_id)
        cross = lh is not None and la is not None and lh != la
        method = "model wyników (rozkład normalny)" if self.sport.model == "normal" else (
            "model wyników (Poisson)" if self.dispersion is None else "model wyników (ujemny dwumianowy)")
        return Prediction(
            home_id=home_id, away_id=away_id, league=league, lam_home=eh, lam_away=ea, rho=0.0, probs=probs,
            low_data_home=low_h, low_data_away=low_a, new_home=home_id not in self.team_index,
            new_away=away_id not in self.team_index, cross_league=cross, likely_score=most_likely_score(m),
            matrix=m if keep_matrix else None, method=method,
        )

    def team_matches(self, team_id: int) -> int:
        return self.team_n.get(team_id, 0)

    def summary(self) -> dict:
        n_l = len(self.leagues)
        out = {"matches": int(sum(self.league_n.values())), "teams": len(self.teams),
               "home_advantage": {c: round(float(self.beta[n_l + i]), 3) for c, i in self.league_index.items()}}
        if self.sport.model == "normal":
            out.update(sd_margin=round(self.sd_margin, 2), sd_total=round(self.sd_total, 2))
        else:
            out["dispersion"] = None if self.dispersion is None else round(self.dispersion, 2)
        return out


# -- pomocnicze ---------------------------------------------------------------------------------------------
def _ridge(x: np.ndarray, y: np.ndarray, w: np.ndarray, penalty: np.ndarray) -> np.ndarray:
    a = x.T @ (w[:, None] * x) + np.diag(penalty)
    b = x.T @ (w * y)
    return np.linalg.lstsq(a, b, rcond=None)[0] if np.linalg.cond(a) > 1e12 else np.linalg.solve(a, b)


def _count_fit(x, y, wts, penalty, league, leagues, hg, ag) -> tuple[np.ndarray, float | None]:
    """Regresja Poissona (IRLS) z karą grzbietową; przy nadmiernym rozrzucie – wagi ujemnego dwumianowego."""
    beta = np.zeros(x.shape[1])
    for i, code in enumerate(leagues):
        mask = league == code
        beta[i] = math.log(max(float((hg[mask].sum() + ag[mask].sum()) / (2 * mask.sum())), 0.1))
    disp: float | None = None
    for stage in range(2):
        for _ in range(40):
            eta = np.clip(x @ beta, -6.0, 5.0)
            mu = np.exp(eta)
            var_w = mu if disp is None else mu / (1.0 + mu / disp)
            z = eta + (y - mu) / mu
            new = _ridge(x, z, wts * var_w, penalty)
            done = float(np.max(np.abs(new - beta))) < 1e-7
            beta = new
            if done:
                break
        if stage == 1:
            break
        mu = np.exp(np.clip(x @ beta, -6.0, 5.0))
        n = len(y) / 2
        phi = float((wts * (y - mu) ** 2 / mu).sum() / wts.sum()) * n / max(n - x.shape[1] / 2.0, 1.0)
        if phi <= 1.15:
            break
        disp = float(mu.mean() / (phi - 1.0))
    return beta, disp


def _count_pmf(mean: float, size: int, dispersion: float | None) -> np.ndarray:
    k = np.arange(size)
    lg = np.array([math.lgamma(i + 1) for i in k])
    if dispersion is None:
        return np.exp(-mean + k * math.log(max(mean, 1e-9)) - lg)
    r = dispersion
    p = r / (r + mean)
    return np.exp(np.array([math.lgamma(i + r) for i in k]) - math.lgamma(r) - lg + r * math.log(p)
                  + k * math.log(max(1 - p, 1e-12)))


def resolve_draws(m: np.ndarray, step: int) -> np.ndarray:
    """Remis po czasie podstawowym rozstrzyga dogrywka: wynik końcowy o `step` wyższy dla zwycięzcy.
    Szansa gospodarzy w dogrywce – w połowie drogi między 50% a ich przewagą w czasie podstawowym."""
    n = m.shape[0]
    home, away = float(np.tril(m, -1).sum()), float(np.triu(m, 1).sum())
    ph = 0.5 + 0.5 * (home / (home + away) - 0.5) if home + away > 0 else 0.5
    out = np.zeros((n + step, n + step))
    out[:n, :n] = m
    idx = np.arange(n)
    draws = np.diag(m).copy()
    out[idx, idx] = 0.0
    out[idx + step, idx] += draws * ph
    out[idx, idx + step] += draws * (1 - ph)
    return out


def _median(dist: np.ndarray) -> float:
    c = np.cumsum(dist) / max(float(dist.sum()), 1e-12)
    return float(np.searchsorted(c, 0.5))
