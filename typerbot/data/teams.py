"""Dopasowanie nazw drużyn między źródłami.

Kolejność prób dla nowej nazwy:
  1. dokładny alias z tego samego źródła (także z innej ligi),
  2. zgodność po normalizacji z aliasem z innego źródła,
  3. znana grupa wariantów (team_seeds),
  4. dopasowanie przybliżone (rapidfuzz) z progami pewności,
  5. nowa drużyna.

Ważna reguła: jedno źródło używa jednej nazwy dla drużyny w danej lidze, więc
drużyna, która ma już alias z tego samego źródła, nie jest kandydatem. To
chroni przed pomyleniem np. „Lech Poznań” z „Lechia Gdańsk”.
Niepewne dopasowania dostają flagę `needs_review` do ręcznej korekty.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz

from typerbot.data.db import Database
from typerbot.data.team_seeds import SEED_GROUPS

STOPWORDS = frozenset({
    "fc", "cf", "afc", "sc", "ssc", "ac", "acf", "as", "cd", "ud", "sd", "rcd", "sad", "club", "de",
    "calcio", "ks", "mks", "sk", "fk", "sv", "vfb", "vfl", "tsg", "bsc", "ogc", "osc", "rc", "us", "ss",
    "fsv", "tsv", "bv", "bc", "cfc", "hsc", "sco", "aj", "losc", "kv", "spvgg", "dsc", "gnk", "nk",
})

_SPECIAL = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "ß": "ss", "æ": "ae", "Æ": "AE",
    "đ": "d", "Đ": "D", "ı": "i", "œ": "oe", "Œ": "OE",
})

SOURCE_PRIORITY = {
    "manual": 5,
    "the_odds_api": 3,
    "oddspapi": 3,
    "football_data_org": 2,
    "football_data_csv": 1,
}


def normalize(name: str) -> str:
    text = name.translate(_SPECIAL)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    raw = text.split()
    tokens = [t for t in raw if t not in STOPWORDS and not t.isdigit()]
    return " ".join(tokens or raw)


def similarity(a: str, b: str) -> float:
    """Średnia z token_sort i token_set – odporna na dopiski, ale nie
    daje 100 punktów za sam wspólny podzbiór słów (np. „Paris” vs „Paris SG”)."""
    return 0.5 * fuzz.token_sort_ratio(a, b) + 0.5 * fuzz.token_set_ratio(a, b)


_SEED_INDEX: dict[str, int] = {}
for _gid, _group in enumerate(SEED_GROUPS):
    for _variant in _group:
        _SEED_INDEX.setdefault(normalize(_variant), _gid)


def seed_group(name: str) -> int | None:
    return _SEED_INDEX.get(normalize(name))


@dataclass
class Resolution:
    team_id: int
    method: str
    score: float | None = None
    needs_review: bool = False
    created: bool = False


@dataclass
class _Candidate:
    team_id: int
    names: set[str]           # znormalizowane aliasy
    groups: set[int]          # grupy z team_seeds
    sources: set[str]         # źródła mające już alias w tej lidze


class TeamMatcher:
    AUTO = 90.0
    AUTO_CUP = 92.0
    REVIEW = 80.0
    MARGIN_AUTO = 5.0
    MARGIN_REVIEW = 10.0
    POSSIBLE_DUPLICATE = 70.0

    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[tuple[str, str, str], int] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- główne API -----------------------------------------------------------
    def resolve(
        self,
        conn: sqlite3.Connection,
        source: str,
        league_code: str,
        name: str,
        *,
        hints: tuple[str, ...] = (),
        is_cup: bool = False,
        country: str = "",
    ) -> Resolution:
        name = name.strip()
        key = (source, league_code, name)
        if key in self._cache:
            return Resolution(self._cache[key], "cached")

        row = conn.execute(
            "SELECT team_id FROM team_aliases WHERE source = ? AND league_code = ? AND name = ?",
            (source, league_code, name),
        ).fetchone()
        if row:
            self._cache[key] = row["team_id"]
            return Resolution(row["team_id"], "alias")

        row = conn.execute(
            "SELECT team_id FROM team_aliases WHERE source = ? AND name = ? LIMIT 1", (source, name)
        ).fetchone()
        if row:
            return self._link(conn, source, league_code, name, Resolution(row["team_id"], "exact"))

        names = [name, *[h for h in hints if h]]
        norms = [normalize(n) for n in names]
        groups = {g for g in (seed_group(n) for n in names) if g is not None}
        candidates = [c for c in self._candidates(conn, league_code, is_cup) if source not in c.sources]

        for cand in candidates:
            if any(n in cand.names for n in norms):
                return self._link(conn, source, league_code, name, Resolution(cand.team_id, "normalized", 100.0))
        if groups:
            for cand in candidates:
                if groups & cand.groups:
                    return self._link(conn, source, league_code, name, Resolution(cand.team_id, "seed", 100.0))

        scored: list[tuple[float, int]] = []
        for cand in candidates:
            if groups and cand.groups and not (groups & cand.groups):
                continue  # obie nazwy znane i należą do różnych klubów
            best = max((similarity(n, c) for n in norms for c in cand.names), default=0.0)
            scored.append((best, cand.team_id))
        scored.sort(reverse=True)
        best_score, best_team = scored[0] if scored else (0.0, None)
        second = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - second
        auto = self.AUTO_CUP if is_cup else self.AUTO

        if best_team is not None and best_score >= auto and margin >= self.MARGIN_AUTO:
            res = Resolution(best_team, "fuzzy", best_score, needs_review=best_score < 95.0)
            return self._link(conn, source, league_code, name, res)
        if best_team is not None and best_score >= self.REVIEW and margin >= self.MARGIN_REVIEW:
            res = Resolution(best_team, "fuzzy", best_score, needs_review=True)
            return self._link(conn, source, league_code, name, res)

        team_id = self._create_team(conn, name, source, country)
        res = Resolution(team_id, "new", best_score or None,
                         needs_review=best_score >= self.POSSIBLE_DUPLICATE, created=True)
        return self._link(conn, source, league_code, name, res)

    def set_alias(self, source: str, league_code: str, name: str, team_id: int) -> None:
        """Ręczna korekta dopasowania z poziomu ustawień."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_aliases(source, league_code, name, team_id, method, score, needs_review) "
                "VALUES (?, ?, ?, ?, 'manual', NULL, 0) ON CONFLICT(source, league_code, name) DO UPDATE SET "
                "team_id = excluded.team_id, method = 'manual', needs_review = 0",
                (source, league_code, name, team_id),
            )
        self.clear_cache()

    def confirm_alias(self, source: str, league_code: str, name: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE team_aliases SET needs_review = 0 WHERE source = ? AND league_code = ? AND name = ?",
                (source, league_code, name),
            )

    def review_list(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT a.source, a.league_code, a.name, a.method, a.score, t.id AS team_id, t.name AS team_name "
            "FROM team_aliases a JOIN teams t ON t.id = a.team_id WHERE a.needs_review = 1 "
            "ORDER BY a.league_code, a.name"
        )

    # -- pomocnicze -------------------------------------------------------------
    def _candidates(self, conn: sqlite3.Connection, league_code: str, is_cup: bool) -> list[_Candidate]:
        if is_cup:
            rows = conn.execute("SELECT team_id, source, name, league_code FROM team_aliases").fetchall()
        else:
            rows = conn.execute(
                "SELECT team_id, source, name, league_code FROM team_aliases WHERE league_code = ?",
                (league_code,),
            ).fetchall()
        by_team: dict[int, _Candidate] = {}
        for r in rows:
            cand = by_team.setdefault(r["team_id"], _Candidate(r["team_id"], set(), set(), set()))
            cand.names.add(normalize(r["name"]))
            group = seed_group(r["name"])
            if group is not None:
                cand.groups.add(group)
            if r["league_code"] == league_code:
                cand.sources.add(r["source"])
        return list(by_team.values())

    def _create_team(self, conn: sqlite3.Connection, name: str, source: str, country: str) -> int:
        cur = conn.execute(
            "INSERT INTO teams(name, country, name_priority, created_at) VALUES (?, ?, ?, ?)",
            (name, country, SOURCE_PRIORITY.get(source, 0), datetime.now(timezone.utc).isoformat()),
        )
        return int(cur.lastrowid)

    def _link(self, conn: sqlite3.Connection, source: str, league_code: str, name: str,
              res: Resolution) -> Resolution:
        conn.execute(
            "INSERT OR IGNORE INTO team_aliases(source, league_code, name, team_id, method, score, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, league_code, name, res.team_id, res.method, res.score, int(res.needs_review)),
        )
        priority = SOURCE_PRIORITY.get(source, 0)
        conn.execute(
            "UPDATE teams SET name = ?, name_priority = ? WHERE id = ? AND name_priority < ?",
            (name, priority, res.team_id, priority),
        )
        self._cache[(source, league_code, name)] = res.team_id
        return res
