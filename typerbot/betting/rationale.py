"""Uzasadnienie typu: forma, statystyki, bilans bezpośrednich meczów, liczby modelu."""

from __future__ import annotations

import math
from dataclasses import dataclass

from typerbot.betting.evaluation import SelectionEval
from typerbot.config.settings import Settings
from typerbot.data.db import Database
from typerbot.fmt import num as _fnum, pct as _pct, plural, signed_pct


@dataclass
class TeamForm:
    team_id: int
    results: list[str]          # Z / R / P, od najnowszego
    goals_for: float
    goals_against: float
    xg_for: float | None
    xg_against: float | None
    over25: float               # odsetek meczów z ponad 2,5 bramki
    btts: float                 # odsetek meczów, w których strzeliły obie drużyny
    n: int

    @property
    def form(self) -> str:
        return " ".join(self.results) if self.results else "brak meczów"


def team_form(db: Database, team_id: int, before: str, venue: str | None = None, limit: int = 5) -> TeamForm:
    """Ostatnie mecze drużyny przed daną chwilą (venue: 'home', 'away' albo None – wszystkie)."""
    where = {"home": "home_team_id = ?", "away": "away_team_id = ?"}.get(
        venue or "", "(home_team_id = ? OR away_team_id = ?)")
    params: tuple = (team_id,) if venue in ("home", "away") else (team_id, team_id)
    rows = db.query(
        f"SELECT home_team_id, home_goals, away_goals, home_xg, away_xg FROM matches WHERE {where} "
        "AND status = 'FINISHED' AND home_goals IS NOT NULL AND kickoff < ? ORDER BY kickoff DESC LIMIT ?",
        (*params, before, limit),
    )
    results, gf, ga, xf, xa, over, both = [], [], [], [], [], [], []
    for r in rows:
        home = r["home_team_id"] == team_id
        f, a = (r["home_goals"], r["away_goals"]) if home else (r["away_goals"], r["home_goals"])
        results.append("Z" if f > a else ("R" if f == a else "P"))
        gf.append(f)
        ga.append(a)
        over.append(f + a > 2.5)
        both.append(f > 0 and a > 0)
        if r["home_xg"] is not None and r["away_xg"] is not None:
            xf.append(r["home_xg"] if home else r["away_xg"])
            xa.append(r["away_xg"] if home else r["home_xg"])
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return TeamForm(team_id, results, mean(gf), mean(ga), mean(xf) if xf else None, mean(xa) if xa else None,
                    mean(over), mean(both), len(rows))


@dataclass
class HeadToHead:
    home_wins: int      # z perspektywy obecnego gospodarza
    draws: int
    away_wins: int
    games: list[str]    # np. "2025-03-02 2:1"

    @property
    def n(self) -> int:
        return self.home_wins + self.draws + self.away_wins


def head_to_head(db: Database, home_id: int, away_id: int, before: str, limit: int = 5) -> HeadToHead:
    rows = db.query(
        "SELECT kickoff, home_team_id, home_goals, away_goals FROM matches WHERE status = 'FINISHED' "
        "AND home_goals IS NOT NULL AND kickoff < ? AND ((home_team_id = ? AND away_team_id = ?) OR "
        "(home_team_id = ? AND away_team_id = ?)) ORDER BY kickoff DESC LIMIT ?",
        (before, home_id, away_id, away_id, home_id, limit),
    )
    hw = d = aw = 0
    games = []
    for r in rows:
        f, a = (r["home_goals"], r["away_goals"]) if r["home_team_id"] == home_id else (r["away_goals"], r["home_goals"])
        hw += f > a
        d += f == a
        aw += f < a
        games.append(f"{r['kickoff'][:10]} {r['home_goals']}:{r['away_goals']}")
    return HeadToHead(hw, d, aw, games)


def _num(x: float) -> str:
    return _fnum(x, 1)


def build_rationale(db: Database, sel: SelectionEval, match: dict, lam_home: float, lam_away: float,
                    settings: Settings, flags: list[str] | None = None) -> list[str]:
    """Kilka zdań uzasadnienia dla typu na kuponie.

    `match` – słownik z kluczami: home_id, away_id, home, away, kickoff.
    """
    home, away, before = match["home"], match["away"], match["kickoff"]
    lines: list[str] = []
    ev = sel.ev
    parts = [f"prognoza {_pct(sel.probability)}"]
    if sel.p_market is not None:
        parts[0] += f" (model {_pct(sel.p_model)}, rynek {_pct(sel.p_market)})"
    else:
        parts[0] += " (tylko model – brak kursów rynku)"
    if sel.odds:
        parts.append(f"kurs {_fnum(sel.odds)} {sel.source_label}".rstrip()
                     + (f" → implikowane {_pct(sel.implied)}" if sel.implied else ""))
    if ev is not None:
        after = sel.ev_after_tax(settings)
        parts.append(f"EV {signed_pct(ev)}" + (" (value)" if ev > 0 else "")
                     + (f", jako pojedynczy zakład po podatku {signed_pct(after)}" if after is not None else ""))
    lines.append(" · ".join(parts))

    hf = team_form(db, match["home_id"], before, "home")
    af = team_form(db, match["away_id"], before, "away")
    lines.append(f"Forma: {home} u siebie {hf.form} (śr. {_num(hf.goals_for)}:{_num(hf.goals_against)}), "
                 f"{away} na wyjeździe {af.form} (śr. {_num(af.goals_for)}:{_num(af.goals_against)})")

    market = sel.key[0]
    if market in ("1X2", "DC"):
        h2h = head_to_head(db, match["home_id"], match["away_id"], before)
        h2h_txt = (f"bilans ostatnich spotkań ({h2h.n}): {h2h.home_wins} zw. {home}, "
                   f"{plural(h2h.draws, 'remis', 'remisy', 'remisów')}, {h2h.away_wins} zw. {away}"
                   ) if h2h.n else "brak wcześniejszych spotkań w bazie"
        lines.append(f"Oczekiwane gole modelu {_num(lam_home)} : {_num(lam_away)} · {h2h_txt}")
    elif market == "OU":
        h10 = team_form(db, match["home_id"], before, None, 10)
        a10 = team_form(db, match["away_id"], before, None, 10)
        lines.append(f"Oczekiwane gole łącznie {_num(lam_home + lam_away)} · ponad 2,5 bramki w ostatnich 10 meczach: "
                     f"{_pct(h10.over25)} ({home}), {_pct(a10.over25)} ({away})")
    elif market == "BTTS":
        h10 = team_form(db, match["home_id"], before, None, 10)
        a10 = team_form(db, match["away_id"], before, None, 10)
        p_home_scores, p_away_scores = 1 - math.exp(-lam_home), 1 - math.exp(-lam_away)
        lines.append(f"Obie drużyny strzelały w {_pct(h10.btts)} meczów {home} i {_pct(a10.btts)} meczów {away} "
                     f"(ostatnie 10) · szansa gola wg modelu: {home} {_pct(p_home_scores)}, {away} {_pct(p_away_scores)}")
    xg_parts = []
    if hf.xg_for is not None:
        xg_parts.append(f"{home} xG {_num(hf.xg_for)}:{_num(hf.xg_against or 0)}")
    if af.xg_for is not None:
        xg_parts.append(f"{away} xG {_num(af.xg_for)}:{_num(af.xg_against or 0)}")
    if xg_parts:
        lines.append("Średnie xG w ostatnich meczach: " + ", ".join(xg_parts))
    notes = list(flags or [])
    if sel.odds_source == "estimated":
        notes.append("kurs szacowany z 1X2 – sprawdź ofertę bukmachera")
    if notes:
        lines.append("Uwaga: " + "; ".join(notes))
    return lines
