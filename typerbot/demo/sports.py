"""Inne dyscypliny w świecie demo: NFL (nflverse), MLB (MLB Stats API), piłka ręczna i hokej (OpenLigaDB).

Wyniki z ukrytej siły drużyn: futbol amerykański i piłka ręczna – rozkład normalny punktów, hokej –
Poisson, baseball – rozkład ujemny dwumianowy (większy rozrzut runów). W dyscyplinach bez remisów
remis rozstrzyga dogrywka. Kursy NFL (zwycięzca, handicap, suma) – z prawdziwych prawdopodobieństw
z marżą bukmachera, jak w pliku nflverse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

NFL_CODES = ("ARI", "BAL", "BUF", "CHI", "DAL", "DEN", "DET", "GB", "KC", "LV", "MIA", "NE", "NYG", "PHI", "SEA", "SF")
MLB_TEAMS = ("New York Yankees", "Boston Red Sox", "Toronto Blue Jays", "Houston Astros", "Seattle Mariners",
             "Los Angeles Dodgers", "San Diego Padres", "Atlanta Braves", "Philadelphia Phillies", "Chicago Cubs")
HBL_TEAMS = ("THW Kiel", "SG Flensburg-Handewitt", "SC Magdeburg", "Füchse Berlin", "Rhein-Neckar Löwen",
             "MT Melsungen", "TSV Hannover-Burgdorf", "HSG Wetzlar", "Frisch Auf! Göppingen", "HC Erlangen",
             "TBV Lemgo Lippe", "Bergischer HC", "ThSV Eisenach", "VfL Gummersbach")
DEL_TEAMS = ("Eisbären Berlin", "Adler Mannheim", "EHC Red Bull München", "Kölner Haie", "ERC Ingolstadt",
             "Grizzlys Wolfsburg", "Pinguins Bremerhaven", "Straubing Tigers", "Nürnberg Ice Tigers",
             "Schwenninger Wild Wings", "Löwen Frankfurt", "Iserlohn Roosters", "Augsburger Panther",
             "Düsseldorfer EG")


@dataclass(frozen=True)
class SportSpec:
    code: str                    # kod ligi w aplikacji
    sport: str
    name: str
    teams: tuple[str, ...]
    tz: str
    start: tuple[int, int]       # (miesiąc, dzień) – początek sezonu
    style: str                   # 'split' | 'calendar'
    model: str                   # 'normal' | 'poisson' | 'negbin'
    mean: float                  # średnio punktów (goli, runów) drużyny
    home_adv: float              # przewaga gospodarzy (normal: w punktach; poisson/negbin: w logarytmie)
    spread: float                # rozrzut siły drużyn
    sd: float = 0.0              # rozrzut punktów drużyny (rozkład normalny)
    draws: bool = False
    ot_step: int = 1
    shortcut: str | None = None  # OpenLigaDB
    rounds: int = 0              # kolejek w sezonie (0 – mecz i rewanż z każdym)
    per_week: int = 1            # kolejek w tygodniu
    round_days: tuple[int, ...] = ()   # przy kilku kolejkach w tygodniu – dzień każdej z nich (godziny ze `slots`)
    slots: tuple[tuple[int, time], ...] = ()   # (dzień tygodnia kolejki, godzina lokalna) dla kolejnych meczów
    series_days: int = 1         # baseball: kolejka to seria meczów tych samych drużyn dzień po dniu


def _slots(*items: tuple[int, int, int]) -> tuple[tuple[int, time], ...]:
    return tuple((d, time(h, m)) for d, h, m in items)


SPORT_LEAGUES: tuple[SportSpec, ...] = (
    SportSpec("NFL", "american_football", "NFL", NFL_CODES, "America/New_York", (9, 10), "split", "normal",
              22.0, 1.8, 4.0, sd=9.8, ot_step=3, rounds=17,
              slots=_slots((3, 20, 15), (6, 13, 0), (6, 13, 0), (6, 13, 0), (6, 16, 25), (6, 16, 25), (6, 20, 20),
                           (7, 20, 15))),
    SportSpec("MLB", "baseball", "MLB", MLB_TEAMS, "America/New_York", (4, 1), "calendar", "negbin",
              4.5, 0.08, 0.12, rounds=60, series_days=2,
              slots=_slots((0, 19, 5), (0, 19, 10), (0, 19, 40), (0, 22, 10), (0, 22, 40))),
    SportSpec("OL-HBL", "handball", "Handball-Bundesliga", HBL_TEAMS, "Europe/Berlin", (9, 3), "split", "normal",
              29.0, 1.8, 2.2, sd=4.4, draws=True, shortcut="hbl",
              slots=_slots((3, 19, 0), (3, 19, 0), (3, 20, 30), (6, 13, 30), (6, 16, 0), (6, 16, 0), (6, 18, 0))),
    SportSpec("OL-DEL", "hockey", "DEL", DEL_TEAMS, "Europe/Berlin", (9, 11), "split", "poisson",
              2.9, 0.12, 0.18, shortcut="del", per_week=2, round_days=(4, 6), rounds=52,
              slots=_slots((0, 19, 30), (0, 19, 30), (0, 19, 30), (0, 14, 0), (0, 16, 30), (0, 16, 30), (0, 19, 0))),
)
SPORT_BY_CODE = {s.code: s for s in SPORT_LEAGUES}
SPORT_BY_SHORTCUT = {s.shortcut: s for s in SPORT_LEAGUES if s.shortcut}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def sample_score(spec: SportSpec, mh: float, ma: float, rng: np.random.Generator) -> tuple[int, int]:
    if spec.model == "normal":
        h, a = (max(0, int(round(rng.normal(m, spec.sd)))) for m in (mh, ma))
    elif spec.model == "negbin":
        h, a = (int(rng.poisson(rng.gamma(5.0, m / 5.0))) for m in (mh, ma))
    else:
        h, a = int(rng.poisson(mh)), int(rng.poisson(ma))
    if h == a and not spec.draws:        # dogrywka
        if rng.random() < 0.5 + 0.25 * math.tanh(mh - ma):
            h += spec.ot_step
        else:
            a += spec.ot_step
    return h, a


def expected_scores(spec: SportSpec, home: tuple[float, float], away: tuple[float, float],
                    neutral: bool = False) -> tuple[float, float]:
    adv = 0.0 if neutral else spec.home_adv
    if spec.model == "normal":
        return (spec.mean + adv / 2 + home[0] - away[1], spec.mean - adv / 2 + away[0] - home[1])
    return (spec.mean * math.exp(adv + home[0] - away[1]), spec.mean * math.exp(away[0] - home[1]))


def nfl_lines(mh: float, ma: float, sd: float) -> dict[str, float]:
    """Prawdziwe prawdopodobieństwa NFL i linie bliskie 50% (jak u bukmachera)."""
    s_d = s_t = sd * math.sqrt(2)
    mu_d, mu_t = mh - ma, mh + ma
    spread = round(mu_d * 2) / 2 or 0.5
    total = math.floor(mu_t) + 0.5
    return {"H": _norm_cdf(mu_d / s_d), "spread": spread, "cover": 1 - _norm_cdf((spread - mu_d) / s_d),
            "total": total, "over": 1 - _norm_cdf((total - mu_t) / s_t)}


def american(p: float, margin: float) -> int:
    dec = max(1.01, 1.0 / (p * (1 + margin)))
    return int(round((dec - 1) * 100)) if dec >= 2 else -int(round(100 / (dec - 1)))


def round_robin(n: int) -> list[list[tuple[int, int]]]:
    from typerbot.demo.world import round_robin as rr
    return rr(n)


def season_matches(spec: SportSpec, season: int, strengths: dict[str, tuple[float, float]],
                   rng: np.random.Generator, now: datetime) -> list[tuple]:
    """Mecze sezonu: (kolejka, początek UTC, gospodarze, goście, oczek. gosp., oczek. gości, wynik gosp., wynik gości)."""
    teams = list(spec.teams)
    base = round_robin(len(teams))
    n_rounds = spec.rounds or len(base)
    tz = ZoneInfo(spec.tz)
    first = date(season, *spec.start)
    week = first - timedelta(days=first.weekday())
    out = []
    for r in range(n_rounds):
        pairs = base[r % len(base)]
        if spec.series_days > 1:
            day0 = first + timedelta(days=r * (spec.series_days + 1))
            days = [day0 + timedelta(days=k) for k in range(spec.series_days)]
        else:
            days = None
        for i, (hi, ai) in enumerate(pairs):
            offset, clock = spec.slots[i % len(spec.slots)]
            if spec.round_days:
                offset = spec.round_days[r % spec.per_week]
            for d in days or [week + timedelta(days=offset)]:
                kickoff = datetime.combine(d, clock, tzinfo=tz).astimezone(timezone.utc)
                home, away = teams[hi], teams[ai]
                mh, ma = expected_scores(spec, strengths[home], strengths[away])
                hs, as_ = sample_score(spec, mh, ma, rng)
                out.append((r + 1, kickoff, home, away, mh, ma, hs, as_))
        if days is None and (r + 1) % spec.per_week == 0:
            week += timedelta(weeks=1)
    if spec.code == "MLB":                 # play-off w październiku: najlepsze drużyny, mecz dziennie
        top = sorted(teams, key=lambda t: -(strengths[t][0] + strengths[t][1]))[:4]
        day = date(season, 9, 30)
        for k in range(25):
            h, a = top[k % 4], top[(k + 1 + k // 4) % 4]
            if h == a:
                a = top[(k + 2) % 4]
            kickoff = datetime.combine(day + timedelta(days=k), time(20, 8), tzinfo=tz).astimezone(timezone.utc)
            mh, ma = expected_scores(spec, strengths[h], strengths[a])
            hs, as_ = sample_score(spec, mh, ma, rng)
            out.append((n_rounds + 1 + k, kickoff, h, a, mh, ma, hs, as_))
    return out
