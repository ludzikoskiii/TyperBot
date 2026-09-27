"""Ranking Elo drużyn liczony z samych wyników i zamiana na prawdopodobieństwa modelem Poissona.

Działa dla każdej drużyny, która ma choć kilka meczów w bazie – także w ligach, o których nie ma
szczegółowych statystyk (tylko wyniki), i dla reprezentacji.

Ranking (jak w klasycznym Elo dla piłki nożnej):
    oczekiwany wynik  E = 1 / (1 + 10^(−(R_dom − R_wyj + H) / 400)),  H – przewaga boiska,
    po meczu          R ← R + K · G · (wynik − E),  G rośnie z różnicą bramek (1; 1,5; (11 + różnica)/8).
Nowa drużyna startuje z oceną nieco poniżej średniej swojej ligi (beniaminek); drużyna, która zmienia
ligę (awans, spadek), zachowuje swoją ocenę – dzięki temu ligi jednego kraju są porównywalne.

Zamiana na gole (model Poissona):
    λ_dom = exp(c_L + h_L + β·d),   λ_wyj = exp(c_L − β·d),   d = (R_dom − R_wyj) / 400,
gdzie c_L, h_L to średni poziom bramek gości i przewaga gospodarzy w lidze (z ostatnich 2 lat),
a β (jedno dla wszystkich lig) dopasowujemy metodą największej wiarygodności na historii sprzed
daty prognozy. Z oczekiwanych goli – macierz wyników z korektą Dixona-Colesa i wszystkie rynki.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from typerbot.model.data import MatchTable

INIT = 1500.0
NEW_TEAM_OFFSET = -60.0       # nowa drużyna w lidze: średnia ligi − 60 (beniaminek zwykle słabszy)
HOME_CLUBS = 60.0             # przewaga boiska w punktach Elo (kluby)
HOME_NATIONAL = 100.0         # reprezentacje (poza terenem neutralnym)
K_NATIONAL_FACTOR = 1.5       # mecze reprezentacji są rzadkie – szybsze zmiany oceny
LEAGUE_WINDOW_DAYS = 730.0
BETA_WINDOW_DAYS = 3 * 365.0
MIN_PRIOR_MATCHES = 10        # do dopasowania β bierzemy mecze drużyn z ustaloną oceną
DEFAULT_BETA = 0.75
DEFAULT_RHO = -0.05


def _goal_factor(diff: int) -> float:
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    return (11.0 + diff) / 8.0


@dataclass
class EloTable:
    """Oceny przed i po każdym meczu tabeli (jedno przejście w kolejności czasu)."""
    pre_h: np.ndarray
    pre_a: np.ndarray
    post_h: np.ndarray
    post_a: np.ndarray
    n_h: np.ndarray           # liczba wcześniejszych meczów gospodarzy
    n_a: np.ndarray

    @classmethod
    def build(cls, table: MatchTable, k: float = 20.0) -> "EloTable":
        n = len(table)
        pre_h, pre_a, post_h, post_a = (np.empty(n) for _ in range(4))
        n_h, n_a = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
        rating: dict[int, float] = {}
        played: dict[int, int] = {}
        members: dict[str, dict[int, float]] = {}        # liga -> {drużyna: ostatnia ocena}
        home_ids, away_ids = table.home_id.tolist(), table.away_id.tolist()
        leagues, hgs, ags = table.league.tolist(), table.hg.tolist(), table.ag.tolist()
        neutral = table.neutral.tolist() if table.neutral is not None else [False] * n
        national = table.national.tolist() if table.national is not None else [False] * n
        for i in range(n):
            h, a, lg = home_ids[i], away_ids[i], leagues[i]
            league_teams = members.setdefault(lg, {})
            for team in (h, a):
                if team not in rating:
                    others = [r for t, r in league_teams.items() if t != team]
                    rating[team] = (sum(others) / len(others) + NEW_TEAM_OFFSET) if len(others) >= 4 else INIT
            rh, ra = rating[h], rating[a]
            home_adv = 0.0 if neutral[i] else (HOME_NATIONAL if national[i] else HOME_CLUBS)
            expected = 1.0 / (1.0 + 10.0 ** (-(rh - ra + home_adv) / 400.0))
            hg, ag = hgs[i], ags[i]
            score = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
            delta = k * (K_NATIONAL_FACTOR if national[i] else 1.0) * _goal_factor(abs(hg - ag)) * (score - expected)
            pre_h[i], pre_a[i] = rh, ra
            n_h[i], n_a[i] = played.get(h, 0), played.get(a, 0)
            rating[h], rating[a] = rh + delta, ra - delta
            post_h[i], post_a[i] = rating[h], rating[a]
            played[h] = played.get(h, 0) + 1
            played[a] = played.get(a, 0) + 1
            if not table.is_cup[i]:
                league_teams[h], league_teams[a] = rating[h], rating[a]
        return cls(pre_h, pre_a, post_h, post_a, n_h, n_a)


@dataclass
class TeamElo:
    rating: float
    matches: int              # mecze w historii przed datą prognozy
    known: bool               # drużyna ma choć jeden mecz w bazie


@dataclass
class LeagueGoals:
    away: float               # średnie gole gości
    home: float               # średnie gole gospodarzy
    matches: int


class EloModel:
    """Prognozy z rankingu Elo na daną chwilę (bez „podglądania przyszłości”)."""

    def __init__(self, table: MatchTable, elo: EloTable, cutoff_days: float, beta: float,
                 leagues: dict[str, LeagueGoals], overall: LeagueGoals, rho: float = DEFAULT_RHO):
        self.table, self.elo, self.cutoff = table, elo, cutoff_days
        self.end = int(np.searchsorted(table.t, cutoff_days, side="left"))
        self.beta, self.leagues, self.overall, self.rho = beta, leagues, overall, rho

    @classmethod
    def fit(cls, table: MatchTable, elo: EloTable, cutoff_days: float, rho: float = DEFAULT_RHO) -> "EloModel":
        end = int(np.searchsorted(table.t, cutoff_days, side="left"))
        start = int(np.searchsorted(table.t, cutoff_days - LEAGUE_WINDOW_DAYS, side="left"))
        hg, ag = table.hg[start:end].astype(float), table.ag[start:end].astype(float)
        home_games = ~(table.neutral[start:end] if table.neutral is not None else np.zeros(end - start, bool))
        overall = LeagueGoals(float(ag.mean()) if len(ag) else 1.15, float(hg.mean()) if len(hg) else 1.45, len(hg))
        leagues: dict[str, LeagueGoals] = {}
        codes = table.league[start:end]
        for code in set(codes.tolist()):
            m = (codes == code) & home_games
            if m.sum() >= 30:
                leagues[code] = LeagueGoals(float(ag[m].mean()), float(hg[m].mean()), int(m.sum()))
        beta = _fit_beta(table, elo, cutoff_days, leagues, overall)
        return cls(table, elo, cutoff_days, beta, leagues, overall, rho)

    def team(self, team_id: int) -> TeamElo:
        rows = self.table.team_rows(team_id)
        k = int(np.searchsorted(rows, self.end, side="left"))
        if k == 0:
            return TeamElo(INIT, 0, False)
        last = int(rows[k - 1])
        home = int(self.table.home_id[last]) == team_id
        return TeamElo(float(self.elo.post_h[last] if home else self.elo.post_a[last]), k, True)

    def league_goals(self, league: str) -> LeagueGoals:
        return self.leagues.get(league, self.overall)

    def expected_goals(self, home: TeamElo, away: TeamElo, league: str, neutral: bool = False
                       ) -> tuple[float, float]:
        g = self.league_goals(league)
        d = (home.rating - away.rating) / 400.0
        if neutral:
            base = math.sqrt(g.home * g.away)
            return base * math.exp(self.beta * d), base * math.exp(-self.beta * d)
        return g.home * math.exp(self.beta * d), g.away * math.exp(-self.beta * d)


def _fit_beta(table: MatchTable, elo: EloTable, cutoff_days: float, leagues: dict[str, LeagueGoals],
              overall: LeagueGoals) -> float:
    """β metodą Newtona (log-wiarygodność Poissona jest wklęsła w β)."""
    end = int(np.searchsorted(table.t, cutoff_days, side="left"))
    start = int(np.searchsorted(table.t, cutoff_days - BETA_WINDOW_DAYS, side="left"))
    sl = slice(start, end)
    ok = (elo.n_h[sl] >= MIN_PRIOR_MATCHES) & (elo.n_a[sl] >= MIN_PRIOR_MATCHES)
    if table.neutral is not None:
        ok &= ~table.neutral[sl]
    if ok.sum() < 200:
        return DEFAULT_BETA
    d = ((elo.pre_h[sl] - elo.pre_a[sl]) / 400.0)[ok]
    hg, ag = table.hg[sl][ok].astype(float), table.ag[sl][ok].astype(float)
    codes = table.league[sl][ok]
    mh = np.array([leagues.get(c, overall).home for c in codes.tolist()])
    ma = np.array([leagues.get(c, overall).away for c in codes.tolist()])
    beta = DEFAULT_BETA
    for _ in range(25):
        eh, ea = mh * np.exp(beta * d), ma * np.exp(-beta * d)
        grad = float(np.sum(d * (hg - eh) - d * (ag - ea)))
        hess = -float(np.sum(d * d * (eh + ea)))
        if hess >= 0:
            break
        step = grad / hess
        beta = min(max(beta - step, 0.05), 3.0)
        if abs(step) < 1e-6:
            break
    return beta
