"""Dopasowany model i prognozy dla pojedynczych meczów.

  * FittedModel – Dixon-Coles (siła ataku i obrony z bramek, okno ostatnich meczów),
  * HybridModel – Dixon-Coles połączony z rankingiem Elo: gdy obie drużyny mają pełne dane,
    prognoza to mieszanka obu modeli (udział Elo w ustawieniach, dobrany backtestem); gdy drużyna
    ma mało meczów w oknie Dixona-Colesa, a dłuższą historię wyników – sam Elo; reprezentacje – Elo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from typerbot.config.settings import ModelSettings
from typerbot.model import dixon_coles as dc
from typerbot.model.data import OTHER_GROUP, MatchTable, TeamInfo, Window, build_window, to_days
from typerbot.model.elo import EloModel, EloTable
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
    method: str = "Dixon-Coles"      # 'Dixon-Coles' | 'Elo' | 'Dixon-Coles + Elo'
    elo_home: float | None = None
    elo_away: float | None = None

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


ELO_MIN_MATCHES = 10          # tyle meczów w historii wystarczy, żeby ranking Elo był wiarygodny
LEAGUE_MIN_MATCHES = 60       # liga z mniejszą liczbą meczów w historii – „mało danych”


class HybridModel:
    """Dixon-Coles + ranking Elo (także dla drużyn i lig, o których są tylko wyniki)."""

    def __init__(self, dc_model: FittedModel | None, elo: EloModel, settings: ModelSettings,
                 national: set[str] = frozenset(), league_matches: dict[str, int] | None = None):
        self.dc, self.elo, self.settings = dc_model, elo, settings
        self.national = set(national)
        self.league_matches = league_matches or {}

    @classmethod
    def fit(cls, table: MatchTable, cutoff: datetime | float, settings: ModelSettings,
            elo_table: EloTable | None = None, previous: "HybridModel | None" = None) -> "HybridModel | None":
        if len(table) == 0:
            return None
        cut = cutoff if isinstance(cutoff, float) else to_days(cutoff)
        elo_table = elo_table or EloTable.build(table, settings.elo_k)
        clubs = table if table.national is None else table.subset(~table.national)
        dc_model = FittedModel.fit(clubs, cut, settings, previous=previous.dc if previous else None) \
            if len(clubs) else None
        rho = dc_model.params.rho if dc_model is not None and settings.dixon_coles else 0.0
        elo = EloModel.fit(table, elo_table, cut, rho=rho)
        end = int(np.searchsorted(table.t, cut, side="left"))
        codes, counts = np.unique(table.league[:end].astype(str), return_counts=True) if end else ([], [])
        national = set(table.league[table.national].tolist()) if table.national is not None else set()
        return cls(dc_model, elo, settings, national, dict(zip(codes, (int(c) for c in counts))))

    @property
    def window(self) -> Window | None:
        return self.dc.window if self.dc else None

    def team_info(self, team_id: int) -> TeamInfo | None:
        return self.dc.team_info(team_id) if self.dc else None

    def team_matches(self, team_id: int) -> int:
        info = self.team_info(team_id)
        return max(info.n_window if info else 0, self.elo.team(team_id).matches)

    def predict(self, home_id: int, away_id: int, league: str, ou_lines: tuple[float, ...] = (2.5,),
                keep_matrix: bool = False, neutral: bool = False) -> Prediction:
        eh, ea = self.elo.team(home_id), self.elo.team(away_id)
        lh_e, la_e = self.elo.expected_goals(eh, ea, league, neutral)
        rho = self.elo.rho if self.settings.dixon_coles else 0.0
        m_elo = dc.score_matrix(lh_e, la_e, rho, self.settings.max_goals)
        thin_league = self.league_matches.get(league, 0) < LEAGUE_MIN_MATCHES
        elo_low_h, elo_low_a = eh.matches < ELO_MIN_MATCHES, ea.matches < ELO_MIN_MATCHES

        dc_pred = None
        if self.dc is not None and league not in self.national and not neutral:
            dc_pred = self.dc.predict(home_id, away_id, league, ou_lines, keep_matrix=True)
        w = min(max(self.settings.elo_weight, 0.0), 1.0)
        if dc_pred is None:
            method, m, lh, la = "Elo", m_elo, lh_e, la_e
            low_h, low_a = elo_low_h, elo_low_a
            new_h = new_a = False
            cross = False
        elif dc_pred.low_data and (not elo_low_h and not elo_low_a):
            # W oknie Dixona-Colesa za mało meczów, ale ranking Elo ma dłuższą historię – prognoza z Elo.
            method, m, lh, la = "Elo", m_elo, lh_e, la_e
            low_h = low_a = False
            new_h, new_a, cross = dc_pred.new_home, dc_pred.new_away, dc_pred.cross_league
        else:
            method = "Dixon-Coles + Elo" if w > 0 else "Dixon-Coles"
            m = (1 - w) * dc_pred.matrix + w * m_elo
            lh, la = (1 - w) * dc_pred.lam_home + w * lh_e, (1 - w) * dc_pred.lam_away + w * la_e
            low_h, low_a = dc_pred.low_data_home and elo_low_h, dc_pred.low_data_away and elo_low_a
            new_h, new_a, cross = dc_pred.new_home, dc_pred.new_away, dc_pred.cross_league
        if thin_league:
            low_h = low_a = True
        m = m / m.sum()
        return Prediction(
            home_id=home_id, away_id=away_id, league=league, lam_home=lh, lam_away=la, rho=rho,
            probs=market_probabilities(m, ou_lines), low_data_home=low_h, low_data_away=low_a,
            new_home=new_h, new_away=new_a, cross_league=cross, likely_score=most_likely_score(m),
            matrix=m if keep_matrix else None, method=method, elo_home=eh.rating, elo_away=ea.rating,
        )

    def summary(self) -> dict:
        out = self.dc.summary() if self.dc else {}
        out["elo_beta"] = round(self.elo.beta, 3)
        return out
