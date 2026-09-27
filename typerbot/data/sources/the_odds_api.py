"""The Odds API (v4) – uzupełnienie brakujących kursów i terminarza.

Plan darmowy (bez karty): 500 kredytów miesięcznie. Koszt zapytania o kursy =
liczba rynków × liczba regionów. Lista meczów (/events) i lista rozgrywek (/sports)
są bezpłatne – z nich bierzemy terminarz lig, których nie obejmują inne źródła.
Kursy pobieramy tylko dla lig z brakującymi kursami 1X2 / powyżej-poniżej,
najwyżej raz dziennie i w ramach budżetu (patrz services/sync.py).
Dokumentacja: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

from typing import Any

from typerbot.config.leagues import League, season_of
from typerbot.data.errors import AuthError, QuotaExceededError, SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import (
    FINISHED, MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SCHEDULED, MatchRecord, OddsQuote, parse_iso,
)
from typerbot.data.sources.base import ApiSource
from typerbot.data.teams import normalize

BULK_MARKETS = ("h2h", "totals")
MARKET_KEYS = {MARKET_1X2: "h2h", MARKET_OU: "totals"}   # rynki, które uzupełniamy z tego źródła
MARKET_MAP = {"h2h": MARKET_1X2, "totals": MARKET_OU, "btts": MARKET_BTTS, "double_chance": MARKET_DC}


class TheOddsApi(ApiSource):
    name = "the_odds_api"
    label = "The Odds API"
    base_url = "https://api.the-odds-api.com/v4"
    per_minute = 30
    secret_params = ("apiKey",)
    quota_period = "month"
    quota_limit = 500

    def auth(self, key, params, headers) -> None:
        params["apiKey"] = key or ""

    def check_budget(self, cost: int) -> None:
        info = self.quota_info()
        if cost and info and info.remaining is not None and info.remaining < cost:
            raise QuotaExceededError(
                f"{self.label}: za mało kredytów (potrzeba {cost}, zostało {info.remaining})", source=self.name
            )

    def update_quota(self, resp: HttpResponse) -> None:
        remaining = resp.header_int("x-requests-remaining")
        used = resp.header_int("x-requests-used")
        if remaining is not None:
            self.quota.update(self.name, "month", remaining=remaining, used=used)

    def actual_cost(self, resp: HttpResponse, expected: int) -> int:
        last = resp.header_int("x-requests-last")
        return last if last is not None else expected

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status < 400:
            return self.decode(resp)
        try:
            message = str(resp.json().get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        lowered = message.lower()
        if resp.status == 401 and "quota" in lowered:
            raise QuotaExceededError(f"{self.label}: wykorzystano miesięczny limit kredytów", source=self.name)
        if resp.status == 401:
            raise AuthError(f"{self.label}: nieprawidłowy klucz API", source=self.name)
        if resp.status == 429:
            self.limiter.block_for(5)
            raise QuotaExceededError(f"{self.label}: zbyt częste zapytania", source=self.name)
        raise SourceError(f"{self.label}: HTTP {resp.status} {message}".strip(), source=self.name)

    # -- dane --------------------------------------------------------------------
    def sports(self) -> list[dict]:
        """Lista aktywnych rozgrywek (zapytanie bezpłatne)."""
        return self.request("/sports", ttl=24 * 3600, cost=0)

    def odds(self, league: League, *, region: str, ttl: float,
             markets: tuple[str, ...] = BULK_MARKETS) -> list[MatchRecord]:
        if not league.odds_api_key:
            return []
        data = self.request(
            f"/sports/{league.odds_api_key}/odds",
            {"regions": region, "markets": ",".join(markets), "oddsFormat": "decimal", "dateFormat": "iso"},
            ttl=ttl,
            cost=len(markets) * len(region.split(",")),
        )
        return [rec for rec in (self.parse_event(e, league) for e in data or []) if rec is not None]

    def events(self, league: League, *, ttl: float) -> list[MatchRecord]:
        """Nadchodzące mecze ligi bez kursów – zapytanie bezpłatne (nie zużywa kredytów)."""
        if not league.odds_api_key:
            return []
        data = self.request(f"/sports/{league.odds_api_key}/events", {"dateFormat": "iso"}, ttl=ttl, cost=0)
        return [rec for rec in (self.parse_event(e, league) for e in data or []) if rec is not None]

    def scores(self, league: League, days_from: int = 3, *, ttl: float) -> list[MatchRecord]:
        """Wyniki zakończonych meczów – zapasowe źródło do rozliczania kuponów."""
        if not league.odds_api_key:
            return []
        data = self.request(f"/sports/{league.odds_api_key}/scores", {"daysFrom": days_from, "dateFormat": "iso"},
                            ttl=ttl, cost=2)
        out = []
        for event in data or []:
            rec = self.parse_event(event, league)
            if rec is None or not event.get("completed"):
                continue
            scores = {s.get("name"): s.get("score") for s in event.get("scores") or []}
            try:
                rec.home_goals = int(scores[rec.home])
                rec.away_goals = int(scores[rec.away])
            except (KeyError, TypeError, ValueError):
                continue
            rec.status = FINISHED
            out.append(rec)
        return out

    def parse_event(self, event: dict, league: League) -> MatchRecord | None:
        try:
            kickoff = parse_iso(event["commence_time"])
            home, away = event["home_team"], event["away_team"]
        except (KeyError, TypeError, ValueError):
            return None
        rec = MatchRecord(
            source=self.name,
            external_id=str(event.get("id")),
            league_code=league.code,
            season=season_of(kickoff.year, kickoff.month),
            kickoff=kickoff,
            home=home,
            away=away,
            status=SCHEDULED,
        )
        for book in event.get("bookmakers") or []:
            bookmaker = book.get("key") or book.get("title") or "?"
            for market in book.get("markets") or []:
                code = MARKET_MAP.get(market.get("key"))
                if code is None:
                    continue
                for outcome in market.get("outcomes") or []:
                    quote = parse_outcome(code, outcome, home, away, bookmaker)
                    if quote is not None:
                        rec.odds.append(quote)
        return rec


def parse_outcome(market: str, outcome: dict, home: str, away: str, bookmaker: str) -> OddsQuote | None:
    try:
        price = float(outcome["price"])
    except (KeyError, TypeError, ValueError):
        return None
    name = str(outcome.get("name", ""))
    selection: str | None = None
    line = 0.0
    if market == MARKET_1X2:
        selection = {home: "H", away: "A"}.get(name) or ("D" if name.lower() == "draw" else None)
    elif market == MARKET_OU:
        selection = {"over": "O", "under": "U"}.get(name.lower())
        try:
            line = float(outcome.get("point"))
        except (TypeError, ValueError):
            return None
    elif market == MARKET_BTTS:
        selection = {"yes": "Y", "no": "N"}.get(name.lower())
    elif market == MARKET_DC:
        selection = parse_double_chance(name, home, away)
    if selection is None:
        return None
    return OddsQuote(bookmaker=bookmaker, market=market, selection=selection, price=price, line=line)


def parse_double_chance(name: str, home: str, away: str) -> str | None:
    """Rozpoznaje nazwy typu 'Legia Warsaw/Draw', 'Draw or Away', 'Home/Away'."""
    text = name.replace(" or ", "/").replace(" Or ", "/")
    parts = [p.strip() for p in text.split("/") if p.strip()]
    if len(parts) != 2:
        return None
    home_n, away_n = normalize(home), normalize(away)
    symbols = set()
    for part in parts:
        low = part.lower()
        norm = normalize(part)
        if low in ("draw", "x", "tie"):
            symbols.add("X")
        elif low in ("home", "1") or norm == home_n:
            symbols.add("1")
        elif low in ("away", "2") or norm == away_n:
            symbols.add("2")
        else:
            return None
    return {frozenset("1X"): "1X", frozenset("12"): "12", frozenset("X2"): "X2"}.get(frozenset(symbols))
