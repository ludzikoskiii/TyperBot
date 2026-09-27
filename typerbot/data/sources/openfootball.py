"""openfootball (football.json) – terminarz całego sezonu z wyprzedzeniem i wyniki.

Dane w domenie publicznej (CC0), bez klucza i rejestracji, udostępniane jako pliki na GitHubie:
  https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json
  (sezon jesień–wiosna w folderze '2026-27', sezon kalendarzowy w folderze '2026').
Pliki są generowane raz dziennie; wyniki bywają dopisywane z opóźnieniem (do kilku dni),
dlatego źródło służy głównie do terminarza – kursy i szybkie wyniki dają inne źródła.
Godziny w plikach są czasem lokalnym ligi; mecz bez godziny ma godzinę przybliżoną.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from typerbot.config.leagues import League
from typerbot.data.errors import SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import FINISHED, SCHEDULED, MatchRecord
from typerbot.data.sources.base import ApiSource

DEFAULT_TIME = time(15, 0)      # mecz bez godziny w terminarzu – godzina przybliżona


def season_folder(league: League, season: int) -> str:
    return str(season) if league.season_style == "calendar" else f"{season}-{(season + 1) % 100:02d}"


class OpenFootball(ApiSource):
    name = "openfootball"
    label = "openfootball"
    base_url = "https://raw.githubusercontent.com/openfootball/football.json/master"
    requires_key = False
    per_minute = 30

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status == 404:
            raise SourceError(f"{self.label}: brak pliku (404)", source=self.name)
        return super().check_response(resp)

    def season(self, league: League, season: int, *, ttl: float) -> list[MatchRecord]:
        if not league.openfootball:
            return []
        data = self.request(f"/{season_folder(league, season)}/{league.openfootball}.json", ttl=ttl)
        return parse_season(data, league, season, self.name)


def _matches(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("matches"), list):
        return [m for m in data["matches"] if isinstance(m, dict)]
    out = []
    for rnd in data.get("rounds") or []:          # starszy format: kolejki z listą meczów
        out.extend(m for m in rnd.get("matches") or [] if isinstance(m, dict))
    return out


def _score(m: dict) -> tuple[int, int] | None:
    score = m.get("score")
    ft = score.get("ft") if isinstance(score, dict) else score
    if isinstance(ft, list) and len(ft) == 2 and all(isinstance(x, int) for x in ft):
        return ft[0], ft[1]
    if isinstance(m.get("score1"), int) and isinstance(m.get("score2"), int):
        return m["score1"], m["score2"]
    return None


def _kickoff(m: dict, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    try:
        day = date.fromisoformat(str(m.get("date", ""))[:10])
    except ValueError:
        return None
    clock, exact = DEFAULT_TIME, False
    raw = str(m.get("time") or "").strip()
    if raw:
        try:
            hh, mm = (int(x) for x in raw.split()[0].split(":")[:2])
            clock, exact = time(hh, mm), True
        except ValueError:
            pass
    return datetime.combine(day, clock, tzinfo=tz).astimezone(timezone.utc), exact


def _team(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def parse_season(data: Any, league: League, season: int, source: str = "openfootball") -> list[MatchRecord]:
    try:
        tz = ZoneInfo(league.timezone or "UTC")
    except Exception:  # nieznana strefa w bazie
        tz = ZoneInfo("UTC")
    out = []
    for m in _matches(data):
        home, away = _team(m.get("team1")), _team(m.get("team2"))
        when = _kickoff(m, tz)
        if not home or not away or when is None:
            continue
        kickoff, exact = when
        score = _score(m)
        # Kolejka + drużyny nie zmieniają się po przełożeniu meczu (data – tak).
        rnd = str(m.get("round") or "").strip() or kickoff.date().isoformat()
        out.append(MatchRecord(
            source=source,
            external_id=f"{league.code}:{season}:{rnd}:{home}:{away}",
            league_code=league.code, season=season, kickoff=kickoff, home=home, away=away,
            status=FINISHED if score else SCHEDULED,
            home_goals=score[0] if score else None, away_goals=score[1] if score else None,
            kickoff_exact=exact,
        ))
    return out
