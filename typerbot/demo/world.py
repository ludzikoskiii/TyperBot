"""Syntetyczny „świat” meczów do trybu demo i testów.

Kilka lig z różnych krajów (Anglia, Polska, Niemcy, Brazylia, Japonia, USA) i reprezentacje – z ukrytą
prawdziwą siłą drużyn, wynikami z rozkładu Poissona i kursami kilku bukmacherów. Ligi grają
w różne dni tygodnia (także we wtorek i środę), a czołowe ligi mają przerwy reprezentacyjne
(jak w kalendarzu FIFA 2026: 21.09–06.10) – wtedy grają tylko niższe ligi i ligi spoza Europy.

Każde źródło dostaje inne warianty nazw drużyn – tak jak w rzeczywistości (np. „Man United”
w football-data.co.uk, „Manchester United FC” w openfootball) – co sprawdza ujednolicanie nazw.
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

# nazwa kanoniczna: (football-data.co.uk, pełna nazwa – openfootball)
PL_TEAMS: dict[str, tuple[str, str]] = {
    "Arsenal": ("Arsenal", "Arsenal FC"),
    "Aston Villa": ("Aston Villa", "Aston Villa FC"),
    "Bournemouth": ("Bournemouth", "AFC Bournemouth"),
    "Brentford": ("Brentford", "Brentford FC"),
    "Brighton": ("Brighton", "Brighton & Hove Albion FC"),
    "Burnley": ("Burnley", "Burnley FC"),
    "Chelsea": ("Chelsea", "Chelsea FC"),
    "Crystal Palace": ("Crystal Palace", "Crystal Palace FC"),
    "Everton": ("Everton", "Everton FC"),
    "Fulham": ("Fulham", "Fulham FC"),
    "Leeds": ("Leeds", "Leeds United FC"),
    "Liverpool": ("Liverpool", "Liverpool FC"),
    "Man City": ("Man City", "Manchester City FC"),
    "Man United": ("Man United", "Manchester United FC"),
    "Newcastle": ("Newcastle", "Newcastle United FC"),
    "Nottingham Forest": ("Nott'm Forest", "Nottingham Forest FC"),
    "Tottenham": ("Tottenham", "Tottenham Hotspur FC"),
    "West Ham": ("West Ham", "West Ham United FC"),
    "Wolves": ("Wolves", "Wolverhampton Wanderers FC"),
    "Southampton": ("Southampton", "Southampton FC"),
    "Sunderland": ("Sunderland", "Sunderland AFC"),
}
EKS_TEAMS: dict[str, tuple[str, str]] = {
    "Legia": ("Legia", "Legia Warszawa"), "Lech": ("Lech", "Lech Poznań"), "Rakow": ("Rakow", "Raków Częstochowa"),
    "Jagiellonia": ("Jagiellonia", "Jagiellonia Białystok"), "Pogon": ("Pogon Szczecin", "Pogoń Szczecin"),
    "Gornik": ("Gornik Z.", "Górnik Zabrze"), "Zaglebie": ("Zaglebie", "Zagłębie Lubin"),
    "Cracovia": ("Cracovia", "Cracovia"), "Piast": ("Piast Gliwice", "Piast Gliwice"),
    "Korona": ("Korona Kielce", "Korona Kielce"), "Radomiak": ("Radomiak Radom", "Radomiak Radom"),
    "Widzew": ("Widzew Lodz", "Widzew Łódź"), "Katowice": ("Katowice", "GKS Katowice"),
    "Lechia": ("Lechia", "Lechia Gdańsk"), "Motor": ("Motor Lublin", "Motor Lublin"),
    "Stal Mielec": ("Stal Mielec", "Stal Mielec"), "Puszcza": ("Puszcza", "Puszcza Niepołomice"),
    "Slask": ("Slask Wroclaw", "Śląsk Wrocław"), "Arka": ("Arka", "Arka Gdynia"),
    "Termalica": ("Termalica", "Bruk-Bet Termalica Nieciecza"), "Wisla Plock": ("Plock", "Wisła Płock"),
}
E2_TEAMS: dict[str, tuple[str, str]] = {
    "Barnsley": ("Barnsley", "Barnsley FC"), "Bolton": ("Bolton", "Bolton Wanderers FC"),
    "Blackpool": ("Blackpool", "Blackpool FC"), "Bristol Rovers": ("Bristol Rvs", "Bristol Rovers FC"),
    "Burton": ("Burton", "Burton Albion FC"), "Cambridge": ("Cambridge", "Cambridge United FC"),
    "Charlton": ("Charlton", "Charlton Athletic FC"), "Exeter": ("Exeter", "Exeter City FC"),
    "Leyton Orient": ("Leyton Orient", "Leyton Orient FC"), "Lincoln": ("Lincoln", "Lincoln City FC"),
    "Peterborough": ("Peterborough", "Peterborough United FC"), "Reading": ("Reading", "Reading FC"),
    "Rotherham": ("Rotherham", "Rotherham United FC"), "Stevenage": ("Stevenage", "Stevenage FC"),
    "Wigan": ("Wigan", "Wigan Athletic FC"), "Wycombe": ("Wycombe", "Wycombe Wanderers FC"),
}
# 3. Liga: nazwy z OpenLigaDB i z openfootball (bez football-data.co.uk)
BL3_TEAMS: dict[str, tuple[str, str]] = {
    "1860": ("TSV 1860 München", "TSV 1860 München"), "Ingolstadt": ("FC Ingolstadt 04", "FC Ingolstadt 04"),
    "Verl": ("SC Verl", "SC Verl"), "Essen": ("Rot-Weiss Essen", "Rot-Weiss Essen"),
    "Rostock": ("FC Hansa Rostock", "F.C. Hansa Rostock"), "Aue": ("FC Erzgebirge Aue", "FC Erzgebirge Aue"),
    "Mannheim": ("SV Waldhof Mannheim", "SV Waldhof Mannheim 07"), "Saarbruecken": ("1. FC Saarbrücken", "1. FC Saarbrücken"),
    "Osnabrueck": ("VfL Osnabrück", "VfL Osnabrück"), "Wiesbaden": ("SV Wehen Wiesbaden", "SV Wehen Wiesbaden"),
    "Cottbus": ("FC Energie Cottbus", "FC Energie Cottbus"), "Viktoria": ("FC Viktoria Köln", "FC Viktoria Köln 1904"),
}
# Brazylia: football-data.co.uk ma skróty, openfootball – pełne nazwy (łączy je lista wariantów nazw klubów)
BRA_TEAMS: dict[str, tuple[str, str]] = {
    "Flamengo": ("Flamengo RJ", "CR Flamengo"), "Palmeiras": ("Palmeiras", "SE Palmeiras"),
    "Corinthians": ("Corinthians", "SC Corinthians Paulista"), "Sao Paulo": ("Sao Paulo", "São Paulo FC"),
    "Fluminense": ("Fluminense", "Fluminense FC"), "Botafogo": ("Botafogo RJ", "Botafogo FR"),
    "Gremio": ("Gremio", "Grêmio FBPA"), "Internacional": ("Internacional", "SC Internacional"),
    "Atletico MG": ("Atletico-MG", "CA Mineiro"), "Cruzeiro": ("Cruzeiro", "Cruzeiro EC"),
    "Santos": ("Santos", "Santos FC"), "Bahia": ("Bahia", "EC Bahia"),
}
USA_TEAMS: dict[str, tuple[str, str]] = {
    k: (k, k) for k in ("Atlanta Utd", "Austin FC", "Charlotte", "Chicago Fire", "Columbus Crew", "DC United",
                        "Inter Miami", "LA Galaxy", "Los Angeles FC", "New York City", "NY Red Bulls",
                        "Seattle Sounders")
}
JPN_TEAMS: dict[str, tuple[str, str]] = {
    "Kashima": ("Kashima", "Kashima Antlers"), "Urawa": ("Urawa", "Urawa Red Diamonds"),
    "Kawasaki": ("Kawasaki Frontale", "Kawasaki Frontale"), "Yokohama": ("Yokohama M.", "Yokohama F. Marinos"),
    "Gamba": ("G-Osaka", "Gamba Osaka"), "Cerezo": ("C-Osaka", "Cerezo Osaka"), "Nagoya": ("Nagoya", "Nagoya Grampus"),
    "Hiroshima": ("Hiroshima", "Sanfrecce Hiroshima"), "Kobe": ("Kobe", "Vissel Kobe"),
    "Kashiwa": ("Kashiwa", "Kashiwa Reysol"), "Tokyo": ("FC Tokyo", "FC Tokyo"), "Tosu": ("Tosu", "Sagan Tosu"),
    "Shimizu": ("Shimizu", "Shimizu S-Pulse"), "Sapporo": ("Sapporo", "Hokkaido Consadole Sapporo"),
}
NATIONS = ["Poland", "Germany", "England", "Spain", "France", "Italy", "Netherlands", "Portugal", "Brazil",
           "Argentina", "United States", "Japan", "Norway", "Sweden", "Denmark", "Croatia"]
# Warianty nazw (jak w openfootball/clubs) – bez nich „Atletico-MG” i „CA Mineiro” to dwie drużyny.
CLUB_ALIASES = {
    "Brazil": [("CA Mineiro", ["Atletico-MG", "Atlético Mineiro", "Atletico Mineiro"]),
               ("CR Flamengo", ["Flamengo RJ", "Flamengo"]), ("Botafogo FR", ["Botafogo RJ", "Botafogo"]),
               ("SC Corinthians Paulista", ["Corinthians"]), ("Grêmio FBPA", ["Gremio", "Grêmio"]),
               ("SC Internacional", ["Internacional"]), ("São Paulo FC", ["Sao Paulo"])],
    "England": [("Manchester United FC", ["Man United", "Man Utd"]), ("Nottingham Forest FC", ["Nott'm Forest"]),
                ("Bristol Rovers FC", ["Bristol Rvs", "Bristol Rovers"])],
    "Japan": [(full, [short]) for short, full in JPN_TEAMS.values() if short != full],
}
CSV_BOOKS_1X2 = {"B365": 0.065, "PS": 0.025, "Max": 0.010, "Avg": 0.055}
OU_BOOKS = {"B365": 0.065, "P": 0.025, "Max": 0.010, "Avg": 0.055}
# Okna FIFA (miesiąc, dzień) – czołowe ligi wtedy pauzują (2026: jedno długie okno 21.09–06.10).
FIFA_WINDOWS = [((9, 21), (10, 6)), ((11, 9), (11, 17)), ((3, 22), (3, 31))]


@dataclass(frozen=True)
class LeagueSpec:
    code: str
    country: str                 # kraj w pliku football-data.co.uk (kolumna Country)
    name: str
    teams: dict[str, tuple[str, str]]
    tz: str
    slots: tuple[tuple[int, time], ...]     # (dzień od poniedziałku tygodnia kolejki, godzina lokalna)
    start: tuple[int, int]       # (miesiąc, dzień) – pierwszy tydzień sezonu
    style: str = "split"         # 'split' | 'calendar'
    spread: float = 0.25
    breaks: bool = True          # pauza w oknach FIFA
    csv_main: str | None = None
    csv_extra: str | None = None
    openfootball: str | None = None
    openligadb: str | None = None
    past_only: frozenset[str] = frozenset()
    promoted: frozenset[str] = frozenset()


def _slots(*items: tuple[int, int, int]) -> tuple[tuple[int, time], ...]:
    return tuple((d, time(h, m)) for d, h, m in items)


LEAGUES: tuple[LeagueSpec, ...] = (
    LeagueSpec("PL", "England", "Premier League", PL_TEAMS, "Europe/London",
               _slots((4, 20, 0), (5, 12, 30), (5, 15, 0), (5, 15, 0), (5, 17, 30), (6, 14, 0), (6, 16, 30),
                      (7, 20, 0)), (8, 10), spread=0.30, csv_main="E0", openfootball="en.1",
               past_only=frozenset({"Southampton"}), promoted=frozenset({"Sunderland"})),
    LeagueSpec("EKS", "Poland", "Ekstraklasa", EKS_TEAMS, "Europe/Warsaw",
               _slots((4, 18, 0), (4, 20, 30), (5, 15, 0), (5, 17, 30), (6, 15, 0), (6, 17, 30), (7, 19, 0)),
               (7, 20), spread=0.18, csv_extra="POL",
               past_only=frozenset({"Stal Mielec", "Puszcza", "Slask"}),
               promoted=frozenset({"Arka", "Termalica", "Wisla Plock"})),
    LeagueSpec("E2", "England", "League One", E2_TEAMS, "Europe/London",
               _slots((5, 15, 0), (5, 15, 0), (5, 15, 0), (5, 15, 0), (1, 19, 45), (1, 19, 45), (2, 19, 45),
                      (2, 19, 45)), (8, 3), spread=0.20, breaks=False, csv_main="E2"),
    LeagueSpec("BL3", "Germany", "3. Liga", BL3_TEAMS, "Europe/Berlin",
               _slots((4, 19, 0), (5, 14, 0), (5, 14, 0), (6, 13, 30), (1, 19, 0), (1, 19, 0)), (7, 27),
               spread=0.18, openfootball="de.3", openligadb="bl3"),
    LeagueSpec("BRA", "Brazil", "Serie A", BRA_TEAMS, "America/Sao_Paulo",
               _slots((2, 16, 0), (2, 21, 30), (3, 16, 30), (5, 18, 30), (6, 16, 0), (6, 18, 30)), (5, 11),
               style="calendar", spread=0.22, csv_extra="BRA", openfootball="br.1"),
    LeagueSpec("JPN", "Japan", "J1 League", JPN_TEAMS, "Asia/Tokyo",
               _slots((2, 19, 0), (3, 19, 0), (5, 14, 0), (5, 16, 0), (6, 15, 0), (6, 17, 0), (6, 18, 0)), (5, 4),
               style="calendar", spread=0.18, breaks=False, csv_extra="JPN", openfootball="jp.1"),
    LeagueSpec("USA", "USA", "MLS", USA_TEAMS, "America/New_York",
               _slots((2, 19, 30), (3, 20, 0), (5, 19, 30), (5, 19, 30), (5, 22, 30), (6, 18, 0)), (6, 1),
               style="calendar", spread=0.18, breaks=False, csv_extra="USA"),
)
LEAGUE_BY_CODE = {spec.code: spec for spec in LEAGUES}


@dataclass
class DemoTeam:
    key: str
    names: tuple[str, str]       # (football-data.co.uk / OpenLigaDB, openfootball)
    attack: float
    defence: float

    @property
    def csv(self) -> str:
        return self.names[0]

    @property
    def full(self) -> str:
        return self.names[1]


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
    home_shots: int
    away_shots: int
    home_sot: int
    away_sot: int
    seq: int
    noise: dict[str, float] = field(default_factory=dict)
    neutral: bool = False

    @property
    def uid(self) -> str:
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


def in_fifa_window(day: date) -> bool:
    for (m1, d1), (m2, d2) in FIFA_WINDOWS:
        if date(day.year, m1, d1) <= day <= date(day.year, m2, d2):
            return True
    return False


def season_of_spec(spec: LeagueSpec, when: datetime) -> int:
    if spec.style == "calendar":
        return when.year
    return when.year if when.month >= 7 else when.year - 1


class DemoWorld:
    MU = 0.22
    HOME_ADV = 0.25

    def __init__(self, now: datetime | None = None, seasons: int = 7, seed: int = 7,
                 international_fixtures: bool = False):
        self.now = now or datetime.now(timezone.utc)
        self.international_fixtures = international_fixtures
        self._probs: dict[int, dict[str, float]] = {}
        rng = np.random.default_rng(seed)
        self.teams: dict[str, dict[str, DemoTeam]] = {}
        for spec in LEAGUES:
            self.teams[spec.code] = {key: DemoTeam(key, names, float(rng.normal(0, spec.spread)),
                                                   float(rng.normal(0, spec.spread)))
                                     for key, names in spec.teams.items()}
        self.nations = {n: DemoTeam(n, (n, n), float(rng.normal(0, 0.35)), float(rng.normal(0, 0.35)))
                        for n in NATIONS}
        self.matches: list[DemoMatch] = []
        seq = 0
        for spec in LEAGUES:
            current = season_of_spec(spec, self.now)
            for season in range(current - seasons + 1, current + 1):
                for m in self._season(spec, season, rng, seq):
                    self.matches.append(m)
                    seq += 1
        for m in self._internationals(rng, seq, seasons):
            self.matches.append(m)
            seq += 1
        self.matches.sort(key=lambda m: m.kickoff)

    # -- generowanie ---------------------------------------------------------------
    def current_season(self, code: str = "PL") -> int:
        return season_of_spec(LEAGUE_BY_CODE[code], self.now)

    def _participants(self, spec: LeagueSpec, season: int) -> list[DemoTeam]:
        exclude = spec.past_only if season == season_of_spec(spec, self.now) else spec.promoted
        return [t for k, t in self.teams[spec.code].items() if k not in exclude]

    def _match(self, spec_code: str, season: int, rnd: int, kickoff: datetime, home: DemoTeam, away: DemoTeam,
               rng: np.random.Generator, seq: int, drift=(0.0, 0.0, 0.0, 0.0), neutral: bool = False) -> DemoMatch:
        adv = 0.0 if neutral else self.HOME_ADV
        lam_h = math.exp(self.MU + adv + home.attack + drift[0] - away.defence - drift[3])
        lam_a = math.exp(self.MU + away.attack + drift[2] - home.defence - drift[1])
        hg, ag = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
        sh, sa = int(rng.poisson(lam_h * 8)), int(rng.poisson(lam_a * 8))
        noise = {k: float(rng.normal(0, 0.05)) for k in ("H", "D", "A", "O", "U", "Y", "N")}
        return DemoMatch(spec_code, season, rnd, kickoff, home, away, lam_h, lam_a, hg, ag, sh, sa,
                         max(hg, int(rng.binomial(sh, 0.35))), max(ag, int(rng.binomial(sa, 0.35))), seq, noise,
                         neutral)

    def _season(self, spec: LeagueSpec, season: int, rng: np.random.Generator, seq0: int) -> list[DemoMatch]:
        teams = self._participants(spec, season)
        rounds = round_robin(len(teams))
        year = season + (1 if spec.style == "split" and spec.start[0] < 7 else 0)
        week = _monday(date(year, *spec.start))
        tz = ZoneInfo(spec.tz)
        drift = {t.key: (rng.normal(0, 0.05), rng.normal(0, 0.05)) for t in teams}
        out, seq = [], seq0
        for r, pairs in enumerate(rounds):
            while spec.breaks and any(in_fifa_window(week + timedelta(days=d)) for d in range(0, 8)):
                week += timedelta(weeks=1)
            for i, (hi, ai) in enumerate(pairs):
                offset, clock = spec.slots[i % len(spec.slots)]
                kickoff = datetime.combine(week + timedelta(days=offset), clock, tzinfo=tz).astimezone(timezone.utc)
                home, away = teams[hi], teams[ai]
                d = (*drift[home.key], *drift[away.key])
                out.append(self._match(spec.code, season, r + 1, kickoff, home, away, rng, seq, d))
                seq += 1
            week += timedelta(weeks=1)
        return out

    def _internationals(self, rng: np.random.Generator, seq0: int, seasons: int) -> list[DemoMatch]:
        """Mecze reprezentacji w oknach FIFA (trzy kolejki na okno)."""
        out, seq = [], seq0
        nations = list(self.nations.values())
        for year in range(self.now.year - seasons, self.now.year + 1):
            for (m1, d1), _ in FIFA_WINDOWS:
                start = date(year, m1, d1)
                for k, day in enumerate((start + timedelta(days=3), start + timedelta(days=6),
                                         start + timedelta(days=9))):
                    order = rng.permutation(len(nations))
                    for j in range(0, len(order) - 1, 2):
                        home, away = nations[order[j]], nations[order[j + 1]]
                        kickoff = datetime.combine(day, time(18, 45), tzinfo=timezone.utc)
                        out.append(self._match("INT", year, k + 1, kickoff, home, away, rng, seq,
                                               neutral=bool(rng.random() < 0.2)))
                        seq += 1
        return out

    # -- zapytania -------------------------------------------------------------------
    def is_finished(self, m: DemoMatch) -> bool:
        return m.kickoff + timedelta(hours=2) <= self.now

    def league_matches(self, league: str) -> list[DemoMatch]:
        return [m for m in self.matches if m.league == league]

    def upcoming(self, league: str, days: int = 10) -> list[DemoMatch]:
        end = self.now + timedelta(days=days)
        return [m for m in self.league_matches(league) if self.now < m.kickoff <= end]

    def odds_for(self, m: DemoMatch, margin: float, sharp: float = 1.0) -> dict[str, float]:
        p = self._probs.get(m.seq)
        if p is None:                  # prawdziwe prawdopodobieństwa liczymy raz na mecz (pliki mają wiele kolumn)
            p = self._probs[m.seq] = true_probabilities(m.lam_home, m.lam_away)
        return {k: price(v, margin, m.noise[k] * sharp) for k, v in p.items()}

    def enable_leagues(self, leagues) -> None:
        """Aktywne tylko ligi świata demo (+ reprezentacje) – reszta katalogu nie ma danych."""
        wanted = {spec.code for spec in LEAGUES} | {"INT"}
        for league in leagues.all():
            leagues.set_enabled(league.code, league.code in wanted)

    def clubs_txt(self, country: str) -> str | None:
        clubs = CLUB_ALIASES.get(country)
        if not clubs:
            return None
        lines = [f"= {country}", ""]
        for name, variants in clubs:
            lines += [f"{name}, 1900", "  | " + " | ".join(variants)]
        return "\n".join(lines) + "\n"


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


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())
