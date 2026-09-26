import json

import pytest

from typerbot.data.errors import AuthError, PlanRestrictionError, QuotaExceededError, SourceUnavailableError
from typerbot.data.http import HttpClient, HttpResponse
from typerbot.data.quota import QuotaTracker
from typerbot.data.ratelimit import RateLimiter
from typerbot.data.sources import ApiFootball, TheOddsApi


class ScriptedTransport:
    """Zwraca kolejne przygotowane odpowiedzi i zapisuje wywołania."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params, headers, timeout):
        self.calls.append((url, dict(params), dict(headers)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(payload, status=200, **headers):
    return HttpResponse(status, {k.replace("_", "-"): str(v) for k, v in headers.items()},
                        json.dumps(payload).encode())


# -- ogranicznik zapytań ---------------------------------------------------------
def test_rate_limiter_waits_when_window_full(clock):
    lim = RateLimiter(2, clock=clock, sleep=clock.sleep)
    lim.acquire()
    lim.acquire()
    lim.acquire()  # trzecie w tej samej minucie musi poczekać
    assert clock.slept and pytest.approx(clock.slept[0]) == 60.0


def test_rate_limiter_block_and_max_wait(clock):
    lim = RateLimiter(None, clock=clock, sleep=clock.sleep, max_wait=30)
    lim.block_for(10)
    lim.acquire()
    assert clock.slept == [10]
    lim.block_for(100)
    with pytest.raises(QuotaExceededError):
        lim.acquire()


# -- cache ----------------------------------------------------------------------------
def test_cache_hit_avoids_network_and_expires(db, clock, secrets):
    transport = ScriptedTransport(ok({"sports": 1}), ok({"sports": 2}))
    http = HttpClient(db, transport, clock)
    src = TheOddsApi(http, QuotaTracker(db, clock), secrets, limiter=RateLimiter(None), clock=clock)
    assert src.request("/sports", ttl=60, cost=0) == {"sports": 1}
    assert src.request("/sports", ttl=60, cost=0) == {"sports": 1}
    assert len(transport.calls) == 1
    clock.advance(61)
    assert src.request("/sports", ttl=60, cost=0) == {"sports": 2}
    assert len(transport.calls) == 2


def test_api_key_not_in_cache_key_or_stored_url(db, clock, secrets):
    transport = ScriptedTransport(ok([]))
    http = HttpClient(db, transport, clock)
    src = TheOddsApi(http, QuotaTracker(db, clock), secrets, limiter=RateLimiter(None), clock=clock)
    src.request("/sports", ttl=60, cost=0)
    assert transport.calls[0][1]["apiKey"] == "key-the_odds_api"
    row = db.query_one("SELECT url FROM http_cache")
    assert "key-the_odds_api" not in row["url"]


def test_offline_falls_back_to_stale_cache(db, clock, secrets):
    transport = ScriptedTransport(ok({"v": 1}), SourceUnavailableError("offline"))
    http = HttpClient(db, transport, clock)
    src = TheOddsApi(http, QuotaTracker(db, clock), secrets, limiter=RateLimiter(None), clock=clock)
    src.request("/sports", ttl=10, cost=0)
    clock.advance(20)
    assert src.request("/sports", ttl=10, cost=0) == {"v": 1}
    assert src.last_stale


def test_offline_without_cache_raises(db, clock, secrets):
    http = HttpClient(db, ScriptedTransport(SourceUnavailableError("offline")), clock)
    src = TheOddsApi(http, QuotaTracker(db, clock), secrets, limiter=RateLimiter(None), clock=clock)
    with pytest.raises(SourceUnavailableError):
        src.request("/sports", ttl=10, cost=0)


# -- limity z nagłówków ------------------------------------------------------------------
def test_odds_api_quota_from_headers_and_cost(db, clock, secrets):
    quota = QuotaTracker(db, clock)
    transport = ScriptedTransport(ok([], x_requests_remaining=480, x_requests_used=20, x_requests_last=2))
    src = TheOddsApi(HttpClient(db, transport, clock), quota, secrets, limiter=RateLimiter(None), clock=clock)
    src.request("/sports/soccer_epl/odds", {"regions": "eu"}, cost=2)
    info = quota.get("the_odds_api", "month", 500)
    assert (info.remaining, info.used, info.limit, info.from_headers) == (480, 20, 500, True)
    assert quota.local_used("the_odds_api", "month") == 2


def test_odds_api_budget_blocks_expensive_call(db, clock, secrets):
    quota = QuotaTracker(db, clock)
    quota.update("the_odds_api", "month", remaining=1, used=499)
    transport = ScriptedTransport()
    src = TheOddsApi(HttpClient(db, transport, clock), quota, secrets, limiter=RateLimiter(None), clock=clock)
    with pytest.raises(QuotaExceededError):
        src.request("/sports/soccer_epl/odds", cost=2)
    assert transport.calls == []  # nie wysłano zapytania


def test_api_football_errors_in_200_body_are_typed_and_not_cached(db, clock, secrets):
    quota = QuotaTracker(db, clock)
    plan = ok({"errors": {"plan": "Free plans do not have access to this season, try from 2022 to 2024."},
               "response": []})
    token = ok({"errors": {"token": "Error/Missing application key."}, "response": []})
    limit = ok({"errors": {"requests": "You have reached the request limit for the day"}, "response": []})
    transport = ScriptedTransport(plan, token, limit)
    src = ApiFootball(HttpClient(db, transport, clock), quota, secrets, limiter=RateLimiter(None), clock=clock)
    with pytest.raises(PlanRestrictionError):
        src.request("/fixtures", {"league": 39, "season": 2026}, ttl=3600)
    with pytest.raises(AuthError):
        src.request("/fixtures", {"league": 39, "season": 2025}, ttl=3600)
    with pytest.raises(QuotaExceededError):
        src.request("/fixtures", {"league": 39, "season": 2024}, ttl=3600)
    assert db.query_one("SELECT COUNT(*) FROM http_cache")[0] == 0


def test_api_football_daily_budget_from_headers(db, clock, secrets):
    quota = QuotaTracker(db, clock)
    transport = ScriptedTransport(ok({"errors": [], "response": []}, x_ratelimit_requests_limit=100,
                                     x_ratelimit_requests_remaining=0))
    src = ApiFootball(HttpClient(db, transport, clock), quota, secrets, limiter=RateLimiter(None), clock=clock)
    src.request("/fixtures", {"league": 39, "season": 2024})
    with pytest.raises(QuotaExceededError):
        src.request("/fixtures", {"league": 39, "season": 2023})


def test_quota_header_info_expires_with_period(db, clock):
    quota = QuotaTracker(db, clock)
    quota.update("api_football", "day", limit=100, remaining=5)
    assert quota.get("api_football", "day", 100).remaining == 5
    clock.advance(24 * 3600)  # następny dzień – limit dzienny się odnawia
    info = quota.get("api_football", "day", 100)
    assert info.remaining == 100 and not info.from_headers
