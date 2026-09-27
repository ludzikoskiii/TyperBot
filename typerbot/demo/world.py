"""Syntetyczny „świat” meczów do trybu demo i testów.

Generuje dwie ligi (Premier League, Ekstraklasa) z ukrytą prawdziwą siłą
drużyn, wynikami z rozkładu Poissona i kursami kilku bukmacherów.
Każde źródło dostaje inne warianty nazw drużyn – tak jak w rzeczywistości –
co pozwala sprawdzić dopasowanie nazw. Znana „prawda” przyda się też
w etapie 2 do testu, czy model odzyskuje parametry.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

UK = ZoneInfo("Europe/London")
WARSAW = ZoneInfo("Europe/Warsaw")

# nazwa kanoniczna: (football-data.co.uk, football-data.org, nazwa alternatywna (OddsPapi), The Odds API)
PL_TEAMS: dict[str, tuple[str, str | None, str, str]] = {
    "Arsenal": ("Arsenal", "Arsenal FC", "Arsenal", "Arsenal"),
    "Aston Villa": ("Aston Villa", "Aston Villa FC", "Aston Villa", "Aston Villa"),
    "Bournemouth": ("Bournemouth", "AFC Bournemouth", "Bournemouth", "Bournemouth"),
    "Brentford": ("Brentford", "Brentford FC", "Brentford", "Brentford"),
    "Brighton": ("Brighton", "Brighton & Hove Albion FC", "Brighton", "Brighton and Hove Albion"),
    "Burnley": ("Burnley", "Burnley FC", "Burnley", "Burnley"),
    "Chelsea": ("Chelsea", "Chelsea FC", "Chelsea", "Chelsea"),
    "Crystal Palace": ("Crystal Palace", "Crystal Palace FC", "Crystal Palace", "Crystal Palace"),
    "Everton": ("Everton", "Everton FC", "Everton", "Everton"),
    "Fulham": ("Fulham", "Fulham FC", "Fulham", "Fulham"),
    "Leeds": ("Leeds", "Leeds United FC", "Leeds", "Leeds United"),
    "Liverpool": ("Liverpool", "Liverpool FC", "Liverpool", "Liverpool"),
    "Man City": ("Man City", "Manchester City FC", "Manchester City", "Manchester City"),
    "Man United": ("Man United", "Manchester United FC", "Manchester United", "Manchester United"),
    "Newcastle": ("Newcastle", "Newcastle United FC", "Newcastle", "Newcastle United"),
    "Nottingham Forest": ("Nott'm Forest", "Nottingham Forest FC", "Nottingham Forest", "Nottingham Forest"),
    "Tottenham": ("Tottenham", "Tottenham Hotspur FC", "Tottenham", "Tottenham Hotspur"),
    "West Ham": ("West Ham", "West Ham United FC", "West Ham", "West Ham United"),
    "Wolves": ("Wolves", "Wolverhampton Wanderers FC", "Wolves", "Wolverhampton Wanderers"),
    "Southampton": ("Southampton", "Southampton FC", "Southampton", "Southampton"),
    "Sunderland": ("Sunderland", "Sunderland AFC", "Sunderland", "Sunderland"),
}
PL_PAST_ONLY = {"Southampton"}
PL_PROMOTED = {"Sunderland"}

EKS_TEAMS: dict[str, tuple[str, str | None, str, str]] = {
    "Legia": ("Legia", None, "Legia Warszawa", "Legia Warsaw"),
    "Lech": ("Lech", None, "Lech Poznan", "Lech Poznań"),
    "Rakow": ("Rakow", None, "Rakow Czestochowa", "Raków Częstochowa"),
    "Jagiellonia": ("Jagiellonia", None, "Jagiellonia", "Jagiellonia Białystok"),
    "Pogon": ("Pogon Szczecin", None, "Pogon Szczecin", "Pogoń Szczecin"),
    "Gornik": ("Gornik Z.", None, "Gornik Zabrze", "Górnik Zabrze"),
    "Zaglebie": ("Zaglebie", None, "Zaglebie Lubin", "Zagłębie Lubin"),
    "Cracovia": ("Cracovia", None, "Cracovia Krakow", "Cracovia"),
    "Piast": ("Piast Gliwice", None, "Piast Gliwice", "Piast Gliwice"),
    "Korona": ("Korona Kielce", None, "Korona Kielce", "Korona Kielce"),
    "Radomiak": ("Radomiak Radom", None, "Radomiak Radom", "Radomiak Radom"),
    "Widzew": ("Widzew Lodz", None, "Widzew Lodz", "Widzew Łódź"),
    "Katowice": ("Katowice", None, "GKS Katowice", "GKS Katowice"),
    "Lechia": ("Lechia", None, "Lechia Gdansk", "Lechia Gdańsk"),
    "Motor": ("Motor Lublin", None, "Motor Lublin", "Motor Lublin"),
    "Stal Mielec": ("Stal Mielec", None, "Stal Mielec", "Stal Mielec"),
    "Puszcza": ("Puszcza", None, "Puszcza Niepolomice", "Puszcza Niepołomice"),
    "Slask": ("Slask Wroclaw", None, "Slask Wroclaw", "Śląsk Wrocław"),
    "Arka": ("Arka", None, "Arka Gdynia", "Arka Gdynia"),
    "Termalica": ("Termalica", None, "Bruk-Bet Termalica Nieciecza", "Termalica Nieciecza"),
    "Wisla Plock": ("Plock", None, "Wisla Plock", "Wisła Płock"),
}
EKS_PAST_ONLY = {"Stal Mielec", "Puszcza", "Slask"}
EKS_PROMOTED = {"Arka", "Termalica", "Wisla Plock"}

ODDS_API_BOOKS = {"unibet_eu": 0.060, "pinnacle": 0.025, "betsson": 0.055, "williamhill": 0.065, "marathonbet": 0.045}
CSV_BOOKS_1X2 = {"B365": 0.065, "PS": 0.025, "Max": 0.010, "Avg": 0.055}


@dataclass
class DemoTeam:
    key: str
    names: tuple[str, str | None, str, str]
    attack: float
    defence: float
    fdorg_id: int
    alt_id: int

    @property
    def csv(self) -> str:
        return self.names[0]

    @property
    def fdorg(self) -> str | None:
        return self.names[1]

    @property
    def alt(self) -> str:
        return self.names[2]

    @property
    def odds(self) -> str:
        return self.names[3]


@dataclass
class DemoMatch:
    league: str
    season: int
    round: int
    kickoff: datetime
    home: DemoTeam
    away: DemoTeam
    lam_home: float
    lam_away: float
    home_goals: int
    away_goals: int
    home_xg: float
    away_xg: float
    home_shots: int
    away_shots: int
    home_sot: int
    away_sot: int
    seq: int
    noise: dict[str, float] = field(default_factory=dict)

    @property
    def fdorg_id(self) -> int:
        return 500000 + self.seq

    @property
    def odds_id(self) -> str:
        return hashlib.md5(f"demo-{self.seq}".encode()).hexdigest()


def _poisson_pmf(lam: float, n: int = 11) -> np.ndarray:
    k = np.arange(n)
    return np.exp(-lam + k * math.log(lam) - np.array([math.lgamma(i + 1) for i in k]))


def true_probabilities(lam_h: float, lam_a: float) -> dict[str, float]:
    m = np.outer(_poisson_pmf(lam_h), _poisson_pmf(lam_a))
    m /= m.sum()
    home = float(np.tril(m, -1).sum())
    draw = float(np.trace(m))
    away = float(np.triu(m, 1).sum())
    goals = np.add.outer(np.arange(11), np.arange(11))
    over = float(m[goals > 2.5].sum())
    btts = float(m[1:, 1:].sum())
    return {"H": home, "D": draw, "A": away, "O": over, "U": 1 - over, "Y": btts, "N": 1 - btts}


def price(p: float, margin: float, noise: float = 0.0) -> float:
    return round(max(1.01, 1.0 / (p * math.exp(noise) * (1 + margin))), 2)


class DemoWorld:
    MU = 0.22
    HOME_ADV = 0.25

    def __init__(self, now: datetime | None = None, seasons: int = 7, seed: int = 7):
        self.now = now or datetime.now(timezone.utc)
        current = self.now.year if self.now.month >= 7 else self.now.year - 1
        self.current_season = current
        self.seasons = list(range(current - seasons + 1, current + 1))
        rng = np.random.default_rng(seed)
        self.teams: dict[str, dict[str, DemoTeam]] = {}
        for league, table in (("PL", PL_TEAMS), ("EKS", EKS_TEAMS)):
            spread = 0.30 if league == "PL" else 0.18
            self.teams[league] = {
                key: DemoTeam(key, names, float(rng.normal(0, spread)), float(rng.normal(0, spread)),
                              fdorg_id=100 + i + (0 if league == "PL" else 1000), alt_id=30 + i + (0 if league == "PL" else 3000))
                for i, (key, names) in enumerate(table.items())
            }
        self.matches: list[DemoMatch] = []
        seq = 0
        for league in ("PL", "EKS"):
            for season in self.seasons:
                for m in self._season(league, season, rng, seq):
                    self.matches.append(m)
                    seq += 1
        self.matches.sort(key=lambda m: m.kickoff)

    # -- generowanie ---------------------------------------------------------------
    def _participants(self, league: str, season: int) -> list[DemoTeam]:
        teams = self.teams[league]
        past_only = PL_PAST_ONLY if league == "PL" else EKS_PAST_ONLY
        promoted = PL_PROMOTED if league == "PL" else EKS_PROMOTED
        exclude = past_only if season == self.current_season else promoted
        return [t for k, t in teams.items() if k not in exclude]

    def _season(self, league: str, season: int, rng: np.random.Generator, seq0: int) -> list[DemoMatch]:
        teams = self._participants(league, season)
        rounds = round_robin(len(teams))
        if league == "PL":
            first = _nth_weekday(date(season, 8, 1), 5, 2)          # druga sobota sierpnia
            slots = [(0, time(12, 30)), (0, time(15, 0)), (0, time(17, 30)), (1, time(14, 0)), (1, time(16, 30))]
            tz = UK
        else:
            first = _nth_weekday(date(season, 7, 18), 4, 1)         # piątek w drugiej połowie lipca
            slots = [(0, time(18, 0)), (0, time(20, 30)), (1, time(15, 0)), (1, time(17, 30)), (2, time(17, 30))]
            tz = WARSAW
        drift = {t.key: (rng.normal(0, 0.05), rng.normal(0, 0.05)) for t in teams}
        out = []
        seq = seq0
        for r, pairs in enumerate(rounds):
            day0 = first + timedelta(weeks=r + (2 if r >= len(rounds) // 2 and league == "EKS" else 0))
            for i, (hi, ai) in enumerate(pairs):
                home, away = teams[hi], teams[ai]
                offset, clock = slots[i % len(slots)]
                kickoff = datetime.combine(day0 + timedelta(days=offset), clock, tzinfo=tz).astimezone(timezone.utc)
                ah, dh = home.attack + drift[home.key][0], home.defence + drift[home.key][1]
                aa, da = away.attack + drift[away.key][0], away.defence + drift[away.key][1]
                lam_h = math.exp(self.MU + self.HOME_ADV + ah - da)
                lam_a = math.exp(self.MU + aa - dh)
                hg, ag = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
                xh = round(max(0.05, lam_h * math.exp(rng.normal(0, 0.25))), 2)
                xa = round(max(0.05, lam_a * math.exp(rng.normal(0, 0.25))), 2)
                sh, sa = int(rng.poisson(lam_h * 8)), int(rng.poisson(lam_a * 8))
                noise = {k: float(rng.normal(0, 0.05)) for k in ("H", "D", "A", "O", "U", "Y", "N")}
                out.append(DemoMatch(
                    league, season, r + 1, kickoff, home, away, lam_h, lam_a, hg, ag, xh, xa, sh, sa,
                    max(hg, int(rng.binomial(sh, 0.35))), max(ag, int(rng.binomial(sa, 0.35))), seq, noise,
                ))
                seq += 1
        return out

    # -- zapytania -------------------------------------------------------------------
    def is_finished(self, m: DemoMatch) -> bool:
        return m.kickoff + timedelta(hours=2) <= self.now

    def is_live(self, m: DemoMatch) -> bool:
        return m.kickoff <= self.now < m.kickoff + timedelta(hours=2)

    def league_matches(self, league: str) -> list[DemoMatch]:
        return [m for m in self.matches if m.league == league]

    def upcoming(self, league: str, days: int = 10) -> list[DemoMatch]:
        end = self.now + timedelta(days=days)
        return [m for m in self.league_matches(league) if self.now < m.kickoff <= end]

    def odds_for(self, m: DemoMatch, margin: float, sharp: float = 1.0) -> dict[str, float]:
        p = true_probabilities(m.lam_home, m.lam_away)
        return {k: price(v, margin, m.noise[k] * sharp) for k, v in p.items()}


def round_robin(n: int) -> list[list[tuple[int, int]]]:
    """Terminarz „każdy z każdym”, mecz i rewanż (metoda koła)."""
    idx = list(range(n))
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = idx[i], idx[n - 1 - i]
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        idx = [idx[0], idx[-1], *idx[1:-1]]
    return rounds + [[(b, a) for a, b in pairs] for pairs in rounds]


def _nth_weekday(start: date, weekday: int, n: int) -> date:
    d = start
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)
