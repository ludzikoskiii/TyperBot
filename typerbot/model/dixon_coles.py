"""Model Dixona-Colesa: bramki jako rozkład Poissona z korektą niskich wyników.

Oczekiwane bramki w meczu gospodarz i – gość j w rozgrywkach c:
    λ_dom  = exp(μ_c + h_c + (a_i + s_Gi) − (d_j + s_Gj))
    λ_wyj  = exp(μ_c + (a_j + s_Gj) − (d_i + s_Gi))
  a – siła ataku, d – siła obrony (większa = lepsza obrona), μ – poziom bramek
  w rozgrywkach, h – przewaga własnego boiska, s_G – siła ligi krajowej drużyny.
Siła ligi znosi się w meczach ligowych, a działa w pucharach (Liga Mistrzów),
gdzie grają drużyny z różnych lig – to wariant (a) z planu.

Dopasowanie w dwóch krokach:
  1. parametry Poissona – ważona wiarygodność z łagodną regularyzacją (ściąganie
     do średniej ligi, co stabilizuje wynik przy 10–20 meczach na drużynę;
     przewaga boiska ściągana do typowej wartości 0,2),
     analityczny gradient + L-BFGS-B,
  2. parametr ρ korekty Dixona-Colesa – optymalizacja jednowymiarowa.
Cel dopasowania może być mieszanką bramek i xG (quasi-wiarygodność Poissona).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln

LOG_LAMBDA_BOUNDS = (-4.0, 2.5)
RHO_BOUNDS = (-0.25, 0.25)


@dataclass
class FitData:
    home: np.ndarray        # indeks drużyny gospodarzy
    away: np.ndarray
    hg: np.ndarray          # bramki (do korekty ρ)
    ag: np.ndarray
    yh: np.ndarray          # cel dopasowania (bramki lub mieszanka z xG)
    ya: np.ndarray
    weight: np.ndarray
    ctx: np.ndarray         # indeks rozgrywek meczu
    team_group: np.ndarray  # indeks ligi krajowej drużyny
    n_teams: int
    n_ctx: int
    n_groups: int
    prior_attack: np.ndarray
    prior_defence: np.ndarray


@dataclass
class DCParams:
    attack: np.ndarray
    defence: np.ndarray
    mu: np.ndarray
    home: np.ndarray
    group: np.ndarray
    rho: float
    converged: bool = True

    def vector(self) -> np.ndarray:
        return np.concatenate([self.attack, self.defence, self.mu, self.home, self.group])


def _unpack(theta: np.ndarray, d: FitData):
    t, c, g = d.n_teams, d.n_ctx, d.n_groups
    att = theta[:t]
    dfn = theta[t:2 * t]
    mu = theta[2 * t:2 * t + c]
    home = theta[2 * t + c:2 * t + 2 * c]
    grp = theta[2 * t + 2 * c:2 * t + 2 * c + g]
    return att, dfn, mu, home, grp


def log_rates(theta: np.ndarray, d: FitData) -> tuple[np.ndarray, np.ndarray]:
    att, dfn, mu, home, grp = _unpack(theta, d)
    sh, sa = grp[d.team_group[d.home]], grp[d.team_group[d.away]]
    lh = mu[d.ctx] + home[d.ctx] + att[d.home] + sh - dfn[d.away] - sa
    la = mu[d.ctx] + att[d.away] + sa - dfn[d.home] - sh
    return np.clip(lh, *LOG_LAMBDA_BOUNDS), np.clip(la, *LOG_LAMBDA_BOUNDS)


HOME_PRIOR = 0.2      # typowa przewaga własnego boiska (log), do której łagodnie ściągamy
REG_HOME = 30.0


def _objective(theta: np.ndarray, d: FitData, reg: float, reg_group: float):
    att, dfn, mu, home, grp = _unpack(theta, d)
    lh, la = log_rates(theta, d)
    eh, ea = np.exp(lh), np.exp(la)
    ll = np.sum(d.weight * (d.yh * lh - eh + d.ya * la - ea))
    da, dd = att - d.prior_attack, dfn - d.prior_defence
    dh = home - HOME_PRIOR
    penalty = reg * (np.dot(da, da) + np.dot(dd, dd)) + reg_group * np.dot(grp, grp) + REG_HOME * np.dot(dh, dh)

    rh = d.weight * (d.yh - eh)   # pochodna wiarygodności po log λ_dom
    ra = d.weight * (d.ya - ea)
    t, c, g = d.n_teams, d.n_ctx, d.n_groups
    g_att = np.bincount(d.home, rh, t) + np.bincount(d.away, ra, t)
    g_def = -np.bincount(d.away, rh, t) - np.bincount(d.home, ra, t)
    g_mu = np.bincount(d.ctx, rh + ra, c)
    g_home = np.bincount(d.ctx, rh, c)
    gh, ga = d.team_group[d.home], d.team_group[d.away]
    g_grp = np.bincount(gh, rh - ra, g) + np.bincount(ga, ra - rh, g)
    grad = np.concatenate([g_att, g_def, g_mu, g_home, g_grp])
    grad[:t] -= 2 * reg * da
    grad[t:2 * t] -= 2 * reg * dd
    grad[2 * t + c:2 * t + 2 * c] -= 2 * REG_HOME * dh
    grad[2 * t + 2 * c:] -= 2 * reg_group * grp
    return -(ll - penalty), -grad


def tau(hg: np.ndarray, ag: np.ndarray, lh: np.ndarray, la: np.ndarray, rho: float) -> np.ndarray:
    """Korekta Dixona-Colesa dla wyników 0:0, 1:0, 0:1 i 1:1."""
    out = np.ones_like(lh, dtype=float)
    m00 = (hg == 0) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m10 = (hg == 1) & (ag == 0)
    m11 = (hg == 1) & (ag == 1)
    out[m00] = 1 - lh[m00] * la[m00] * rho
    out[m01] = 1 + lh[m01] * rho
    out[m10] = 1 + la[m10] * rho
    out[m11] = 1 - rho
    return out


def fit_rho(d: FitData, lh: np.ndarray, la: np.ndarray) -> float:
    low = (d.hg <= 1) & (d.ag <= 1)
    if low.sum() < 10:
        return 0.0
    w, hg, ag, eh, ea = d.weight[low], d.hg[low], d.ag[low], np.exp(lh[low]), np.exp(la[low])

    def neg(rho: float) -> float:
        return -float(np.sum(w * np.log(np.maximum(tau(hg, ag, eh, ea, rho), 1e-10))))

    res = minimize_scalar(neg, bounds=RHO_BOUNDS, method="bounded", options={"xatol": 1e-4})
    return float(res.x)


def fit(d: FitData, *, reg: float = 1.0, reg_group: float = 5.0, use_rho: bool = True,
        init: np.ndarray | None = None) -> DCParams:
    size = 2 * d.n_teams + 2 * d.n_ctx + d.n_groups
    if init is None or init.shape != (size,):
        init = np.zeros(size)
        init[:d.n_teams] = d.prior_attack
        init[d.n_teams:2 * d.n_teams] = d.prior_defence
        total = d.weight.sum()
        mean_goals = (np.sum(d.weight * (d.yh + d.ya)) / (2 * total)) if total > 0 else 1.3
        init[2 * d.n_teams:2 * d.n_teams + d.n_ctx] = np.log(max(mean_goals, 0.2))
        init[2 * d.n_teams + d.n_ctx:2 * d.n_teams + 2 * d.n_ctx] = 0.2
    res = minimize(_objective, init, args=(d, reg, reg_group), jac=True, method="L-BFGS-B",
                   options={"maxiter": 500, "gtol": 1e-6})
    att, dfn, mu, home, grp = (x.copy() for x in _unpack(res.x, d))
    rho = 0.0
    if use_rho:
        lh, la = log_rates(res.x, d)
        rho = fit_rho(d, lh, la)
    return DCParams(att, dfn, mu, home, grp, rho, converged=bool(res.success))


def poisson_pmf(lam: float, max_goals: int) -> np.ndarray:
    k = np.arange(max_goals + 1)
    return np.exp(k * np.log(max(lam, 1e-9)) - lam - gammaln(k + 1))


def score_matrix(lh: float, la: float, rho: float = 0.0, max_goals: int = 10) -> np.ndarray:
    """Macierz P(gospodarze = i, goście = j) z korektą ρ, znormalizowana do 1."""
    m = np.outer(poisson_pmf(lh, max_goals), poisson_pmf(la, max_goals))
    if rho:
        m[0, 0] *= 1 - lh * la * rho
        m[0, 1] *= 1 + lh * rho
        m[1, 0] *= 1 + la * rho
        m[1, 1] *= 1 - rho
        m = np.maximum(m, 0.0)
    return m / m.sum()


def match_log_likelihood(hg: int, ag: int, lh: float, la: float, rho: float) -> float:
    """Log-wiarygodność pojedynczego wyniku (do testów i diagnostyki)."""
    base = hg * np.log(lh) - lh - gammaln(hg + 1) + ag * np.log(la) - la - gammaln(ag + 1)
    t = tau(np.array([hg]), np.array([ag]), np.array([lh]), np.array([la]), rho)[0]
    return float(base + np.log(max(t, 1e-12)))
