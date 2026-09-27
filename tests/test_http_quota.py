import json

import pytest

from typerbot.data.errors import QuotaExceededError, SourceUnavailableError
from typerbot.data.http import HttpClient, HttpResponse
from typerbot.data.quota import QuotaTracker, StatusBoard
from typerbot.data.ratelimit import RateLimiter
from typerbot.data.sources import OpenLigaDb


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
    src = OpenLigaDb(http, QuotaTracker(db, clock), limiter=RateLimiter(None), clock=clock)
    assert src.request("/sports", ttl=60) == {"sports": 1}
    assert src.request("/sports", ttl=60) == {"sports": 1}
    assert len(transport.calls) == 1
    clock.advance(61)
    assert src.request("/sports", ttl=60) == {"sports": 2}
    assert len(transport.calls) == 2


def test_offline_falls_back_to_stale_cache(db, clock, secrets):
    transport = ScriptedTransport(ok({"v": 1}), SourceUnavailableError("offline"))
    http = HttpClient(db, transport, clock)
    src = OpenLigaDb(http, QuotaTracker(db, clock), limiter=RateLimiter(None), clock=clock)
    src.request("/sports", ttl=10)
    clock.advance(20)
    assert src.request("/sports", ttl=10) == {"v": 1}
    assert src.last_stale


def test_offline_without_cache_raises(db, clock, secrets):
    http = HttpClient(db, ScriptedTransport(SourceUnavailableError("offline")), clock)
    src = OpenLigaDb(http, QuotaTracker(db, clock), limiter=RateLimiter(None), clock=clock)
    with pytest.raises(SourceUnavailableError):
        src.request("/sports", ttl=10)


# -- stan źródeł ---------------------------------------------------------------------------
def test_status_board_keeps_last_successful_update(db, clock):
    board = StatusBoard(db, clock)
    board.set("openfootball", "ok", "120 rekordów")
    good = clock()
    clock.advance(3600)
    board.set("openfootball", "offline", "brak połączenia")
    st = board.get("openfootball")
    assert st.state == "offline" and st.last_ok == good and st.updated_at == good + 3600


def test_calls_are_counted_per_day(db, clock):
    transport = ScriptedTransport(ok([]), ok([]))
    src = OpenLigaDb(HttpClient(db, transport, clock), QuotaTracker(db, clock), limiter=RateLimiter(None),
                     clock=clock)
    src.request("/getmatchdata/bl3/2026")
    src.request("/getmatchdata/bl1/2026")
    assert QuotaTracker(db, clock).calls_today("openligadb") == 2
