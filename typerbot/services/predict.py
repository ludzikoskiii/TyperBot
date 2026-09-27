"""Prognozy dla nadchodzących meczów.

Model (Dixon-Coles + ranking Elo) jest dopasowywany do wszystkich lig z bazy naraz;
drużyny z samymi wynikami (bez długiej historii w oknie modelu) i reprezentacje dostają
prognozę z rankingu Elo. Wyniki trafiają do tabeli `predictions` – z niej korzysta
interfejs i generator.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from typerbot.config.settings import ModelSettings, SettingsStore
from typerbot.data.db import Database
from typerbot.data.records import to_iso
from typerbot.model.data import load_matches
from typerbot.model.markets import Key
from typerbot.model.predictor import HybridModel, Prediction

_MODEL_CACHE: dict[tuple, HybridModel | None] = {}


@dataclass
class MatchPrediction:
    match_id: int
    league: str
    kickoff: str
    home: str
    away: str
    prediction: Prediction
    home_matches: int
    away_matches: int


def key_to_str(key: Key) -> str:
    return f"{key[0]}|{key[1]}|{key[2]:g}"


def str_to_key(text: str) -> Key:
    market, sel, line = text.split("|")
    return market, sel, float(line)


class PredictionService:
    def __init__(self, db: Database, now=None):
        self.db = db
        self.settings_store = SettingsStore(db)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.model: HybridModel | None = None

    def fit(self, at: datetime | None = None, settings: ModelSettings | None = None) -> HybridModel | None:
        """Dopasowanie modelu na danych do chwili `at`. Wynik jest zapamiętywany, dopóki nie zmienią się
        dane, ustawienia ani (w przybliżeniu do 10 minut) chwila dopasowania – kolejne generowania są szybkie."""
        settings = settings or self.settings_store.load().model
        at = at or self._now()
        sig = self.db.query_one("SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM matches WHERE status = 'FINISHED'")
        key = (self.db.path, int(at.timestamp() // 600), json.dumps(asdict(settings), sort_keys=True),
               sig["n"] if sig else 0, sig["u"] if sig else None)
        if key in _MODEL_CACHE:
            self.model = _MODEL_CACHE[key]
            return self.model
        table = load_matches(self.db)
        self.model = HybridModel.fit(table, at, settings) if len(table) else None
        if len(_MODEL_CACHE) >= 4:
            _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
        _MODEL_CACHE[key] = self.model
        return self.model

    def predict_between(self, start: datetime, end: datetime, *, save: bool = True) -> list[MatchPrediction]:
        model = self.model or self.fit(start)
        if model is None:
            return []
        rows = self.db.query(
            "SELECT m.id, m.league_code, m.kickoff, m.home_team_id, m.away_team_id, m.neutral, h.name AS home, "
            "a.name AS away "
            "FROM matches m JOIN teams h ON h.id = m.home_team_id JOIN teams a ON a.id = m.away_team_id "
            "WHERE m.status = 'SCHEDULED' AND m.kickoff >= ? AND m.kickoff < ? ORDER BY m.kickoff",
            (to_iso(start), to_iso(end)),
        )
        out = []
        for r in rows:
            pred = model.predict(r["home_team_id"], r["away_team_id"], r["league_code"], neutral=bool(r["neutral"]))
            out.append(MatchPrediction(r["id"], r["league_code"], r["kickoff"], r["home"], r["away"], pred,
                                       model.team_matches(r["home_team_id"]), model.team_matches(r["away_team_id"])))
        if save:
            self._save(out)
        return out

    def _save(self, preds: list[MatchPrediction]) -> None:
        created = to_iso(self._now())
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO predictions(match_id, created_at, lam_home, lam_away, rho, probs, low_data_home, "
                "low_data_away, new_home, new_away, cross_league, home_matches, away_matches, method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET "
                "created_at = excluded.created_at, lam_home = excluded.lam_home, lam_away = excluded.lam_away, "
                "rho = excluded.rho, probs = excluded.probs, low_data_home = excluded.low_data_home, "
                "low_data_away = excluded.low_data_away, new_home = excluded.new_home, new_away = excluded.new_away, "
                "cross_league = excluded.cross_league, home_matches = excluded.home_matches, "
                "away_matches = excluded.away_matches, method = excluded.method",
                [(p.match_id, created, p.prediction.lam_home, p.prediction.lam_away, p.prediction.rho,
                  json.dumps({key_to_str(k): round(v, 5) for k, v in p.prediction.probs.items()}),
                  int(p.prediction.low_data_home), int(p.prediction.low_data_away), int(p.prediction.new_home),
                  int(p.prediction.new_away), int(p.prediction.cross_league), p.home_matches, p.away_matches,
                  p.prediction.method)
                 for p in preds],
            )

    def stored(self, match_id: int) -> dict[Key, float] | None:
        row = self.db.query_one("SELECT probs FROM predictions WHERE match_id = ?", (match_id,))
        return {str_to_key(k): v for k, v in json.loads(row["probs"]).items()} if row else None
