"""OddsPapi (API v4) – kursy Superbet i ponad 300 innych bukmacherów.

Plan darmowy: 250 zapytań miesięcznie (z historią kursów). Oszczędzamy je:
  * kursy jednego bukmachera dla wielu lig naraz: /odds-by-tournaments,
  * terminarz ligi jednym zapytaniem: /fixtures?tournamentId=…,
  * wyniki (/scores) i historia kursów (/historical-odds) – pojedyncze mecze,
    tylko w ramach budżetu ustawionego przez użytkownika.
Identyfikatory rynków pobieramy z /markets (cache 30 dni), a 1X2 ma stałe ID 101.
Dokumentacja: https://oddspapi.io/en/docs
"""

from __future__ import annotations

from datetime import date
from typing import Any

from typerbot.config.leagues import League, season_of
from typerbot.data.errors import AuthError, QuotaExceededError, SourceError
from typerbot.data.http import HttpResponse
from typerbot.data.records import (
    CANCELLED, FINISHED, LIVE, MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SCHEDULED,
    MatchRecord, OddsQuote, parse_iso,
)
from typerbot.data.sources.base import ApiSource
from typerbot.data.sources.the_odds_api import parse_double_chance

DAY = 86400.0
STATUS_MAP = {0: SCHEDULED, 1: LIVE, 2: FINISHED, 3: CANCELLED}
MAX_TOURNAMENTS_PER_CALL = 5

# Rynek 1X2 ma w OddsPapi stałe identyfikatory (dokumentacja i przykłady).
DEFAULT_MARKETS: dict[str, tuple[str, float, dict[str, str]]] = {
    "101": (MARKET_1X2, 0.0, {"101": "H", "102": "D", "103": "A"}),
}


class OddsPapi(ApiSource):
    name = "oddspapi"
    label = "OddsPapi"
    base_url = "https://api.oddspapi.io/v4"
    per_minute = 50              # API wymaga ~1 s przerwy między wywołaniami
    secret_params = ("apiKey",)
    quota_period = "month"
    quota_limit = 250

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._market_map: dict[str, tuple[str, float, dict[str, str]]] | None = None

    def auth(self, key, params, headers) -> None:
        params["apiKey"] = key or ""

    def check_budget(self, cost: int) -> None:
        info = self.quota_info()
        if cost and info and info.remaining is not None and info.remaining < cost:
            raise QuotaExceededError(f"{self.label}: wykorzystano miesięczny limit zapytań", source=self.name)

    def update_quota(self, resp: HttpResponse) -> None:
        remaining = resp.header_int("x-requests-remaining") or resp.header_int("x-ratelimit-remaining")
        if remaining is not None:
            self.quota.update(self.name, "month", limit=self.quota_limit, remaining=remaining)

    def check_response(self, resp: HttpResponse) -> Any:
        if resp.status < 400:
            return self.decode(resp)
        try:
            payload = resp.json()
            message = str(payload.get("message") or payload.get("error") or "") if isinstance(payload, dict) else ""
        except ValueError:
            message = ""
        if resp.status in (401, 403):
            raise AuthError(f"{self.label}: nieprawidłowy klucz API {message}".strip(), source=self.name)
        if resp.status == 429:
            self.limiter.block_for(2)
            raise QuotaExceededError(f"{self.label}: limit zapytań {message}".strip(), source=self.name)
        raise SourceError(f"{self.label}: HTTP {resp.status} {message}".strip(), source=self.name)

    # -- słowniki ----------------------------------------------------------------
    def tournaments(self) -> list[dict]:
        data = self.request("/tournaments", {"sportId": 10}, ttl=30 * DAY)
        return data if isinstance(data, list) else []

    def bookmakers(self) -> list[str]:
        data = self.request("/bookmakers", ttl=30 * DAY)
        out = []
        for item in data if isinstance(data, list) else []:
            slug = item.get("slug") if isinstance(item, dict) else item
            if slug:
                out.append(str(slug))
        return out

    def find_bookmaker(self, wanted: str) -> str:
        """Dopasowuje slug bukmachera (np. 'superbet' -> 'superbet.pl', jeśli taki jest)."""
        slugs = self.bookmakers()
        wanted = wanted.lower()
        exact = [s for s in slugs if s.lower() == wanted]
        if exact:
            return exact[0]
        similar = sorted((s for s in slugs if wanted in s.lower()), key=lambda s: (not s.lower().endswith(".pl"), s))
        return similar[0] if similar else wanted

    def market_map(self) -> dict[str, tuple[str, float, dict[str, str]]]:
        """marketId -> (rynek, linia, {outcomeId: typ}) dla obsługiwanych rynków."""
        if self._market_map is None:
            mapping = dict(DEFAULT_MARKETS)
            try:
                data = self.request("/markets", {"sportId": 10}, ttl=30 * DAY)
            except SourceError:
                data = []
            for m in data if isinstance(data, list) else []:
                parsed = parse_market_definition(m)
                if parsed:
                    mapping[str(m.get("marketId"))] = parsed
            self._market_map = mapping
        return self._market_map

    # -- dane ----------------------------------------------------------------------
    def fixtures(self, league: League, date_from: date, date_to: date, *, ttl: float,
                 status: int | None = None) -> list[MatchRecord]:
        if not league.oddspapi_id:
            return []
        params: dict[str, Any] = {"tournamentId": league.oddspapi_id,
                                  "from": date_from.isoformat(), "to": date_to.isoformat()}
        if status is not None:
            params["statusId"] = status
        data = self.request("/fixtures", params, ttl=ttl)
        return [r for r in (self.parse_fixture(f, league) for f in _as_list(data)) if r is not None]

    def odds_by_tournaments(self, leagues: list[League], bookmaker: str, *, ttl: float) -> dict[str, list[OddsQuote]]:
        """Kursy jednego bukmachera dla wielu lig: {fixtureId: [kursy]}."""
        ids = [str(lg.oddspapi_id) for lg in leagues if lg.oddspapi_id]
        markets = self.market_map()
        out: dict[str, list[OddsQuote]] = {}
        for i in range(0, len(ids), MAX_TOURNAMENTS_PER_CALL):
            data = self.request("/odds-by-tournaments",
                                {"bookmaker": bookmaker, "tournamentIds": ",".join(ids[i:i + MAX_TOURNAMENTS_PER_CALL])},
                                ttl=ttl)
            for item in _as_list(data):
                fid = item.get("fixtureId")
                if fid:
                    out[str(fid)] = parse_bookmaker_odds(item.get("bookmakerOdds") or {}, markets)
        return out

    def historical_odds(self, fixture_id: str, bookmakers: list[str]) -> list[OddsQuote]:
        """Przebieg kursów meczu; zwracamy pierwszy (otwarcie) i ostatni (zamknięcie) kurs."""
        data = self.request("/historical-odds", {"fixtureId": fixture_id, "bookmakers": ",".join(bookmakers[:3])},
                            ttl=365 * DAY)
        if isinstance(data, dict) and fixture_id in data and isinstance(data[fixture_id], dict):
            data = data[fixture_id]
        books = (data or {}).get("bookmakers") or data if isinstance(data, dict) else {}
        return parse_historical(books if isinstance(books, dict) else {}, self.market_map())

    def score(self, fixture_id: str) -> tuple[int, int] | None:
        """Wynik po 90 minutach (suma 1. i 2. połowy, bez dogrywki)."""
        data = self.request("/scores", {"fixtureId": fixture_id}, ttl=365 * DAY)
        return parse_score(data)

    def parse_fixture(self, f: dict, league: League) -> MatchRecord | None:
        try:
            kickoff = parse_iso(str(f["startTime"]))
            home, away = f["participant1Name"], f["participant2Name"]
        except (KeyError, TypeError, ValueError):
            return None
        if not home or not away:
            return None
        status = STATUS_MAP.get(int(f.get("statusId") or 0), SCHEDULED)
        return MatchRecord(
            source=self.name,
            external_id=str(f.get("fixtureId")),
            league_code=league.code,
            season=season_of(kickoff.year, kickoff.month),
            kickoff=kickoff,
            home=str(home),
            away=str(away),
            status=status,  # dla zakończonych meczów wynik przychodzi osobno z /scores
            extra={"home_id": f.get("participant1Id"), "away_id": f.get("participant2Id")},
        )


def _as_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "fixtures", "items"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def parse_market_definition(m: dict) -> tuple[str, float, dict[str, str]] | None:
    name = str(m.get("marketName") or "").lower()
    if "half" in name or "corner" in name or "card" in name or "1st" in name or "2nd" in name:
        return None
    outcomes = {str(o.get("outcomeId")): str(o.get("outcomeName") or "") for o in m.get("outcomes") or []}
    try:
        line = float(m.get("handicap") or 0.0)
    except (TypeError, ValueError):
        line = 0.0
    if "both teams to score" in name:
        sel = {oid: {"yes": "Y", "no": "N"}.get(n.lower()) for oid, n in outcomes.items()}
        return (MARKET_BTTS, 0.0, {k: v for k, v in sel.items() if v}) if all(sel.values()) else None
    if "double chance" in name:
        sel = {oid: parse_double_chance(n.replace("-", "/"), "home", "away") or _dc_symbol(n)
               for oid, n in outcomes.items()}
        return (MARKET_DC, 0.0, {k: v for k, v in sel.items() if v}) if all(sel.values()) else None
    if ("over under" in name or "total" in name) and "team" not in name and "asian" not in name:
        sel = {oid: {"over": "O", "under": "U"}.get(n.lower().split()[0] if n else "") for oid, n in outcomes.items()}
        return (MARKET_OU, line, {k: v for k, v in sel.items() if v}) if all(sel.values()) else None
    return None


def _dc_symbol(name: str) -> str | None:
    text = name.replace(" ", "").upper()
    return {"1X": "1X", "12": "12", "X2": "X2"}.get(text)


def _price(outcome: Any) -> float | None:
    if not isinstance(outcome, dict):
        return None
    players = outcome.get("players")
    entry = players.get("0") if isinstance(players, dict) else outcome
    if not isinstance(entry, dict) or entry.get("active") is False:
        return None
    try:
        price = float(entry.get("price"))
    except (TypeError, ValueError):
        return None
    return price if price > 1.0 else None


def parse_bookmaker_odds(bookmaker_odds: dict, markets: dict[str, tuple[str, float, dict[str, str]]]) -> list[OddsQuote]:
    quotes: list[OddsQuote] = []
    for slug, book in bookmaker_odds.items():
        for mid, market in ((book or {}).get("markets") or {}).items():
            spec = markets.get(str(mid))
            if spec is None:
                continue
            code, line, outcomes = spec
            for oid, outcome in ((market or {}).get("outcomes") or {}).items():
                sel = outcomes.get(str(oid))
                price = _price(outcome)
                if sel and price:
                    quotes.append(OddsQuote(bookmaker=slug, market=code, selection=sel, price=price, line=line))
    return quotes


def parse_historical(books: dict, markets: dict[str, tuple[str, float, dict[str, str]]]) -> list[OddsQuote]:
    """Historia: bookmaker -> markets -> outcomes -> players -> '0' -> [wpisy z createdAt]."""
    quotes: list[OddsQuote] = []
    for slug, book in books.items():
        for mid, market in ((book or {}).get("markets") or {}).items():
            spec = markets.get(str(mid))
            if spec is None:
                continue
            code, line, outcomes = spec
            for oid, outcome in ((market or {}).get("outcomes") or {}).items():
                sel = outcomes.get(str(oid))
                players = (outcome or {}).get("players") if isinstance(outcome, dict) else None
                history = players.get("0") if isinstance(players, dict) else None
                if not sel or not isinstance(history, list) or not history:
                    continue
                ordered = sorted((h for h in history if isinstance(h, dict) and h.get("price")),
                                 key=lambda h: str(h.get("createdAt", "")))
                if not ordered:
                    continue
                for kind, entry in (("pre", ordered[0]), ("close", ordered[-1])):
                    try:
                        price = float(entry["price"])
                    except (TypeError, ValueError):
                        continue
                    if price > 1.0:
                        quotes.append(OddsQuote(bookmaker=slug, market=code, selection=sel, price=price,
                                                line=line, kind=kind))
    return quotes


def parse_score(data: Any) -> tuple[int, int] | None:
    if isinstance(data, list):
        data = data[0] if data else {}
    scores = (data or {}).get("scores") if isinstance(data, dict) else None
    if isinstance(scores, dict) and isinstance(scores.get("periods"), dict):
        scores = scores["periods"]
    if not isinstance(scores, dict):
        return None

    def pair(key: str) -> tuple[int, int] | None:
        p = scores.get(key)
        try:
            return int(p["participant1Score"]), int(p["participant2Score"])
        except (KeyError, TypeError, ValueError):
            return None

    first, second = pair("1"), pair("2")
    if first and second:
        return first[0] + second[0], first[1] + second[1]
    return pair("0")
