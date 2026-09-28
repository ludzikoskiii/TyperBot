"""Model dla wszystkich dyscyplin: piłka nożna – Dixon-Coles z rankingiem Elo (HybridModel),
pozostałe dyscypliny – model wyników (ScoreModel), osobno dla każdej dyscypliny."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from typerbot.config.settings import ModelSettings
from typerbot.config.sports import SPORTS, football_like
from typerbot.model.data import MatchTable
from typerbot.model.predictor import HybridModel, Prediction
from typerbot.model.scores import ScoreModel


@dataclass
class MultiSportModel:
    football: HybridModel | None
    others: dict[str, ScoreModel] = field(default_factory=dict)
    league_sport: dict[str, str] = field(default_factory=dict)

    @classmethod
    def fit(cls, football_table: MatchTable, other_tables: dict[str, MatchTable], cutoff: datetime | float,
            settings: ModelSettings, league_sport: dict[str, str]) -> "MultiSportModel | None":
        football = HybridModel.fit(football_table, cutoff, settings) if len(football_table) else None
        others = {}
        for sport, table in other_tables.items():
            model = ScoreModel.fit(table, cutoff, SPORTS[sport]) if sport in SPORTS and len(table) else None
            if model is not None:
                others[sport] = model
        if football is None and not others:
            return None
        return cls(football, others, dict(league_sport))

    def sport(self, league: str) -> str:
        return self.league_sport.get(league, "football")

    def predict(self, home_id: int, away_id: int, league: str, *, neutral: bool = False,
                ou_lines=(), hcp_lines=(), keep_matrix: bool = False) -> Prediction | None:
        sport = self.sport(league)
        if football_like(sport):
            if self.football is None:
                return None
            return self.football.predict(home_id, away_id, league, keep_matrix=keep_matrix, neutral=neutral)
        model = self.others.get(sport)
        if model is None:
            return None
        return model.predict(home_id, away_id, league, neutral=neutral, ou_lines=tuple(ou_lines),
                             hcp_lines=tuple(hcp_lines), keep_matrix=keep_matrix)

    def team_matches(self, team_id: int, league: str) -> int:
        sport = self.sport(league)
        if football_like(sport):
            return self.football.team_matches(team_id) if self.football else 0
        model = self.others.get(sport)
        return model.team_matches(team_id) if model else 0

    def summary(self) -> dict:
        out = self.football.summary() if self.football else {}
        for sport, model in self.others.items():
            out[f"sport:{sport}"] = model.summary()
        return out
