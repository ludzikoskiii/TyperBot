"""Dopasowany model i prognozy dla pojedynczych meczów."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from typerbot.config.settings import ModelSettings
from typerbot.model import dixon_coles as dc
from typerbot.model.data import OTHER_GROUP, MatchTable, TeamInfo, Window, build_window
from typerbot.model.markets import Key, market_probabilities, most_likely_score


@dataclass
class Prediction:
    home_id: int
    away_id: int
    league: str
    lam_home: float
    lam_away: float
    rho: float
    probs: dict[Key, float]
    low_data_home: bool
    low_data_away: bool
    new_home: bool = False
    new_away: bool = False
    cross_league: bool = False       # drużyny z różnych lig – siła przeliczona współczynnikiem ligi
    likely_score: tuple[int, int, float] = (0, 0, 0.0)
    matrix: np.ndarray | None = field(default=None, repr=False)

    @property
    def low_data(self) -> bool:
        return self.low_data_home or self.low_data_away


class FittedModel:
    def __init__(self, window: Window, params: dc.DCParams, settings: ModelSettings):
        self.window = window
        self.params = params
        self.settings = settings
        self.team_index = window.team_index
        self.ctx_index = {c: i for i, c in enumerate(window.ctx_codes)}
        self.group_index = {g: i for i, g in enumerate(window.group_codes)}

    @classmethod
    def fit(cls, table: MatchTable, cutoff: datetime | float, settings: ModelSettings,
            previous: "FittedModel | None" = None) -> "FittedModel | None":
        window = build_window(table, cutoff, settings, new_team_prior=settings.new_team_prior)
        if window is None or len(window.rows) < 20:
            return None
        init = previous._warm_start(window) if previous is not None else None
        params = dc.fit(window.fit, reg=settings.regularization, use_rho=settings.dixon_coles, init=init)
        return cls(window, params, settings)

    def _warm_start(self, window: Window) -> np.ndarray:
        """Parametry poprzedniego dopasowania jako punkt startowy (szybszy backtest)."""
        d = window.fit
        vec = np.zeros(2 * d.n_teams + 2 * d.n_ctx + d.n_groups)
        vec[:d.n_teams] = d.prior_attack
        vec[d.n_teams:2 * d.n_teams] = d.prior_defence
        for i, tid in enumerate(window.team_ids):
            j = self.team_index.get(tid)
            if j is not None:
                vec[i] = self.params.attack[j]
                vec[d.n_teams + i] = self.params.defence[j]
        mean_mu, mean_home = float(np.mean(self.params.mu)), float(np.mean(self.params.home))
        for i, code in enumerate(window.ctx_codes):
            j = self.ctx_index.get(code)
            vec[2 * d.n_teams + i] = self.params.mu[j] if j is not None else mean_mu
            vec[2 * d.n_teams + d.n_ctx + i] = self.params.home[j] if j is not None else mean_home
        for i, g in enumerate(window.group_codes):
            j = self.group_index.get(g)
            vec[2 * d.n_teams + 2 * d.n_ctx + i] = self.params.group[j] if j is not None else 0.0
        return vec

    # -- siła drużyn ---------------------------------------------------------------
    def team_info(self, team_id: int) -> TeamInfo | None:
        return self.window.teams.get(team_id)

    def _strength(self, team_id: int, league: str) -> tuple[float, float, float, bool, bool, str]:
        """(atak, obrona, siła ligi, mało danych, beniaminek, liga krajowa)."""
        i = self.team_index.get(team_id)
        if i is None:
            # Drużyna bez żadnych meczów w oknie – siła beniaminka w lidze meczu.
            g = self.group_index.get(league)
            s = float(self.params.group[g]) if g is not None else 0.0
            prior = self.settings.new_team_prior
            return prior, prior, s, True, True, league
        info = self.window.teams[team_id]
        g = int(self.window.fit.team_group[i])
        return (float(self.params.attack[i]), float(self.params.defence[i]), float(self.params.group[g]),
                info.low_data, info.new_in_league, info.group)

    def expected_goals(self, home_id: int, away_id: int, league: str) -> tuple[float, float]:
        ah, dh, sh, *_ = self._strength(home_id, league)
        aa, da, sa, *_ = self._strength(away_id, league)
        c = self.ctx_index.get(league)
        mu = float(self.params.mu[c]) if c is not None else float(np.mean(self.params.mu))
        home = float(self.params.home[c]) if c is not None else float(np.mean(self.params.home))
        lh = np.exp(np.clip(mu + home + ah + sh - da - sa, *dc.LOG_LAMBDA_BOUNDS))
        la = np.exp(np.clip(mu + aa + sa - dh - sh, *dc.LOG_LAMBDA_BOUNDS))
        return float(lh), float(la)

    def predict(self, home_id: int, away_id: int, league: str,
                ou_lines: tuple[float, ...] = (2.5,), keep_matrix: bool = False) -> Prediction:
        _, _, _, low_h, new_h, group_h = self._strength(home_id, league)
        _, _, _, low_a, new_a, group_a = self._strength(away_id, league)
        lh, la = self.expected_goals(home_id, away_id, league)
        m = dc.score_matrix(lh, la, self.params.rho, self.settings.max_goals)
        cross = group_h != group_a or OTHER_GROUP in (group_h, group_a)
        return Prediction(
            home_id=home_id, away_id=away_id, league=league, lam_home=lh, lam_away=la, rho=self.params.rho,
            probs=market_probabilities(m, ou_lines), low_data_home=low_h, low_data_away=low_a,
            new_home=new_h, new_away=new_a, cross_league=cross, likely_score=most_likely_score(m),
            matrix=m if keep_matrix else None,
        )

    def ratings(self) -> list[dict]:
        """Tabela siły drużyn (do wyświetlenia)."""
        out = []
        for tid, i in self.team_index.items():
            info = self.window.teams[tid]
            g = int(self.window.fit.team_group[i])
            out.append({
                "team_id": tid, "group": info.group, "attack": float(self.params.attack[i] + self.params.group[g]),
                "defence": float(self.params.defence[i] + self.params.group[g]), "matches": info.n_window,
                "recent": info.n_recent, "low_data": info.low_data, "new": info.new_in_league,
            })
        return sorted(out, key=lambda r: r["attack"] + r["defence"], reverse=True)

    def summary(self) -> dict:
        return {
            "matches": int(len(self.window.rows)),
            "teams": len(self.window.team_ids),
            "rho": round(self.params.rho, 4),
            "home_advantage": {c: round(float(self.params.home[i]), 3) for c, i in self.ctx_index.items()},
            "league_strength": {g: round(float(self.params.group[i]), 3) for g, i in self.group_index.items()},
            "converged": self.params.converged,
        }
