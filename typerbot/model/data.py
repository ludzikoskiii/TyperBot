"""Przygotowanie danych do modelu: tabela meczów, okno ostatnich meczów, wagi.

Tabela jest wczytywana z bazy raz (np. na cały backtest), a okno do dopasowania
wycinane w pamięci dla dowolnej daty – bez „podglądania przyszłości”: do
dopasowania trafiają wyłącznie mecze rozpoczęte przed datą odcięcia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from typerbot.config.leagues import season_of
from typerbot.config.settings import ModelSettings
from typerbot.data.db import Database
from typerbot.model.dixon_coles import FitData

DAY_SECONDS = 86400.0
OTHER_GROUP = "_INNE"   # drużyny znane tylko z pucharów (liga krajowa spoza bazy)


def to_days(dt: datetime) -> float:
    return dt.timestamp() / DAY_SECONDS


@dataclass
class MatchTable:
    match_id: np.ndarray
    t: np.ndarray            # czas rozpoczęcia w dniach (epoch)
    season: np.ndarray
    home_id: np.ndarray
    away_id: np.ndarray
    hg: np.ndarray
    ag: np.ndarray
    hxg: np.ndarray          # NaN, gdy brak xG
    axg: np.ndarray
    league: np.ndarray       # kod rozgrywek (str)
    is_cup: np.ndarray
    _team_rows: dict[int, np.ndarray] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.match_id)

    def team_rows(self, team_id: int) -> np.ndarray:
        """Indeksy (rosnąco w czasie) meczów danej drużyny."""
        if not self._team_rows:
            order: dict[int, list[int]] = {}
            for i, (h, a) in enumerate(zip(self.home_id.tolist(), self.away_id.tolist())):
                order.setdefault(h, []).append(i)
                order.setdefault(a, []).append(i)
            self._team_rows = {k: np.array(v, dtype=np.int64) for k, v in order.items()}
        return self._team_rows.get(team_id, np.empty(0, dtype=np.int64))

    def subset(self, mask: np.ndarray) -> "MatchTable":
        return MatchTable(*(getattr(self, f)[mask] for f in (
            "match_id", "t", "season", "home_id", "away_id", "hg", "ag", "hxg", "axg", "league", "is_cup")))


def load_matches(db: Database, leagues: list[str] | None = None) -> MatchTable:
    """Wszystkie zakończone mecze z wynikiem, posortowane według czasu."""
    sql = ("SELECT m.id, m.kickoff, m.season, m.home_team_id, m.away_team_id, m.home_goals, m.away_goals, "
           "m.home_xg, m.away_xg, m.league_code, l.is_cup FROM matches m JOIN leagues l ON l.code = m.league_code "
           "WHERE m.status = 'FINISHED' AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL")
    params: tuple = ()
    if leagues:
        sql += f" AND m.league_code IN ({','.join('?' * len(leagues))})"
        params = tuple(leagues)
    rows = db.query(sql + " ORDER BY m.kickoff, m.id", params)
    t = np.array([to_days(datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))) for r in rows], dtype=float)

    def col(name, dtype):
        return np.array([r[name] for r in rows], dtype=dtype)

    return MatchTable(
        match_id=col("id", np.int64), t=t, season=col("season", np.int64),
        home_id=col("home_team_id", np.int64), away_id=col("away_team_id", np.int64),
        hg=col("home_goals", np.int64), ag=col("away_goals", np.int64),
        hxg=np.array([np.nan if r["home_xg"] is None else r["home_xg"] for r in rows], dtype=float),
        axg=np.array([np.nan if r["away_xg"] is None else r["away_xg"] for r in rows], dtype=float),
        league=col("league_code", object), is_cup=col("is_cup", bool),
    )


@dataclass
class TeamInfo:
    team_id: int
    group: str                 # liga krajowa
    n_window: int              # mecze w oknie
    n_recent: int              # mecze w oknie z ostatnich 365 dni
    new_in_league: bool        # beniaminek / brak historii w lidze
    low_data: bool


@dataclass
class Window:
    """Dane gotowe do dopasowania modelu na daną datę."""
    cutoff: float
    rows: np.ndarray
    fit: FitData
    team_ids: list[int]
    ctx_codes: list[str]
    group_codes: list[str]
    teams: dict[int, TeamInfo]

    @property
    def team_index(self) -> dict[int, int]:
        return {tid: i for i, tid in enumerate(self.team_ids)}


def build_window(table: MatchTable, cutoff: datetime | float, settings: ModelSettings,
                 *, max_age_days: float = 730.0, new_team_prior: float = -0.15) -> Window | None:
    """Okno: N ostatnich meczów każdej drużyny przed datą odcięcia (wagi wygasają wykładniczo)."""
    cut = cutoff if isinstance(cutoff, float) else to_days(cutoff)
    end = int(np.searchsorted(table.t, cut, side="left"))       # tylko mecze przed odcięciem
    if end == 0:
        return None
    start = int(np.searchsorted(table.t, cut - max_age_days, side="left"))
    mask = np.zeros(len(table), dtype=bool)
    teams_in_range = np.unique(np.concatenate([table.home_id[start:end], table.away_id[start:end]]))
    n = max(1, settings.last_matches)
    for tid in teams_in_range.tolist():
        rows = table.team_rows(tid)
        k = int(np.searchsorted(rows, end, side="left"))
        chosen = rows[max(0, k - n):k]
        mask[chosen[chosen >= start]] = True
    rows = np.flatnonzero(mask)
    if rows.size == 0:
        return None

    home, away = table.home_id[rows], table.away_id[rows]
    team_ids = sorted(set(home.tolist()) | set(away.tolist()))
    tindex = {tid: i for i, tid in enumerate(team_ids)}
    leagues = table.league[rows]
    ctx_codes = sorted(set(leagues.tolist()))
    cindex = {c: i for i, c in enumerate(ctx_codes)}

    # Liga krajowa drużyny = rozgrywki ligowe, w których ma najwięcej meczów w oknie.
    counts: dict[int, dict[str, int]] = {}
    for h, a, lg, cup in zip(home.tolist(), away.tolist(), leagues.tolist(), table.is_cup[rows].tolist()):
        if cup:
            continue
        for tid in (h, a):
            counts.setdefault(tid, {}).setdefault(lg, 0)
            counts[tid][lg] += 1
    group_of = {tid: (max(counts[tid], key=counts[tid].get) if tid in counts else OTHER_GROUP) for tid in team_ids}
    group_codes = sorted(set(group_of.values()))
    gindex = {g: i for i, g in enumerate(group_codes)}

    # Beniaminek: brak meczów w swojej lidze w poprzednim sezonie, choć liga je rozegrała.
    prev = table.season[start:end] == _season_at(cut) - 1
    seg_l = table.league[start:end][prev].tolist()
    league_prev = set(seg_l)
    prev_team_league = set(zip(table.home_id[start:end][prev].tolist(), seg_l)) | set(
        zip(table.away_id[start:end][prev].tolist(), seg_l))

    t_rows = table.t[rows]
    teams: dict[int, TeamInfo] = {}
    prior_att = np.zeros(len(team_ids))
    prior_def = np.zeros(len(team_ids))
    for tid in team_ids:
        involved = (home == tid) | (away == tid)
        n_window = int(involved.sum())
        n_recent = int((involved & (t_rows >= cut - 365)).sum())
        group = group_of[tid]
        new = group != OTHER_GROUP and group in league_prev and (tid, group) not in prev_team_league
        # Drużyny spoza lig w bazie (znane tylko z pucharów) zawsze mają niższą pewność.
        low = n_recent < settings.min_matches or group == OTHER_GROUP
        teams[tid] = TeamInfo(tid, group, n_window, n_recent, new, low)
        if new:
            prior_att[tindex[tid]] = new_team_prior
            prior_def[tindex[tid]] = new_team_prior

    hl = max(settings.half_life_days, 1.0)
    weight = np.power(0.5, (cut - t_rows) / hl)
    hg, ag = table.hg[rows].astype(float), table.ag[rows].astype(float)
    yh, ya = hg.copy(), ag.copy()
    xw = min(max(settings.xg_weight, 0.0), 1.0)
    if xw > 0:
        hx, ax = table.hxg[rows], table.axg[rows]
        has = ~np.isnan(hx) & ~np.isnan(ax)
        yh[has] = (1 - xw) * hg[has] + xw * hx[has]
        ya[has] = (1 - xw) * ag[has] + xw * ax[has]

    fit = FitData(
        home=np.array([tindex[x] for x in home.tolist()], dtype=np.int64),
        away=np.array([tindex[x] for x in away.tolist()], dtype=np.int64),
        hg=table.hg[rows], ag=table.ag[rows], yh=yh, ya=ya, weight=weight,
        ctx=np.array([cindex[x] for x in leagues.tolist()], dtype=np.int64),
        team_group=np.array([gindex[group_of[t]] for t in team_ids], dtype=np.int64),
        n_teams=len(team_ids), n_ctx=len(ctx_codes), n_groups=len(group_codes),
        prior_attack=prior_att, prior_defence=prior_def,
    )
    return Window(cut, rows, fit, team_ids, ctx_codes, group_codes, teams)


def _season_at(days: float) -> int:
    dt = datetime.fromtimestamp(days * DAY_SECONDS, tz=timezone.utc)
    return season_of(dt.year, dt.month)
