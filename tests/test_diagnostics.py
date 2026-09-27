"""Diagnostyka generatora: generator zawsze zwraca kupon albo konkretny powód (z podpowiedzią)."""

import random
from dataclasses import replace

import pytest

from tests.conftest import NOW
from tests.test_coupons import demo_cfg
from tests.test_sync import make_service
from typerbot.config.secrets import KEYED_SOURCES, MemorySecretStore
from typerbot.config.settings import MARKETS
from typerbot.demo.world import DemoWorld
from typerbot.services.coupons import CouponService
from typerbot.services.diagnostics import sync_problems


@pytest.fixture(scope="module")
def world():
    return DemoWorld(NOW)


def build(db, world, clock, *, missing_keys=(), fail=()):
    secrets = MemorySecretStore({s: f"key-{s}" for s in KEYED_SOURCES if s not in missing_keys})
    sync, _ = make_service(db, secrets, world, clock, fail=set(fail))
    report = sync.run_all()
    return sync, report, CouponService(db, now=lambda: NOW), secrets


def test_no_fixtures_gives_concrete_reason_with_sources(db, world, clock):
    """Objaw z pytania: terminarz się nie pobrał -> brak meczów; powód wskazuje źródła i co zrobić."""
    sync, report, service, secrets = build(db, world, clock, missing_keys=("football_data_org",),
                                           fail=("oddspapi", "the_odds_api"))
    result = service.run(demo_cfg(), secrets=secrets)
    diag = result.diagnosis
    assert result.coupons == [] and diag.stages[0].matches == 0
    reason = diag.reasons[0]
    assert "terminarz nie został pobrany" in reason
    assert "football-data.org" in reason and "OddsPapi" in reason
    assert any("wpisz klucz" in h for h in diag.hints)
    fd = next(s for s in diag.sources if s.source == "football_data_org")
    assert fd.state == "no_key"
    # lista problemów: ten sam problem w kilku ligach to jeden wpis
    problems = sync_problems(sync.last_report())
    assert problems == diag.problems
    papi = [p for p in problems if p.source == "oddspapi" and p.step == "fixtures"]
    assert len(papi) == 1 and set(papi[0].leagues) == {"PL", "EKS"}


def test_odds_source_fills_fixtures_when_fixture_sources_fail(db, world, clock):
    """Naprawa: gdy terminarz nie spłynie, kursy (i mecze) bierzemy z The Odds API zamiast nie pytać wcale."""
    sync, report, service, secrets = build(db, world, clock, missing_keys=("football_data_org",), fail=("oddspapi",))
    assert any(s.source == "the_odds_api" and s.step == "odds" and s.state == "ok" for s in report.steps)
    result = service.run(demo_cfg(), secrets=secrets)
    assert result.coupons, result.diagnosis.to_text()
    odds_api = next(s for s in result.diagnosis.sources if s.source == "the_odds_api")
    assert odds_api.matches > 0 and odds_api.with_odds > 0


def test_missing_odds_reason_names_source(db, world, clock):
    sync, report, service, secrets = build(db, world, clock, fail=("oddspapi", "the_odds_api"))
    diag = service.run(demo_cfg(), secrets=secrets).diagnosis
    assert diag.stages[2].matches > 0                      # mecze i prognozy są
    assert diag.reasons and diag.reasons[0].startswith("Brak kursów dla")
    assert "The Odds API" in diag.reasons[0]


@pytest.fixture
def ready(db, world, clock):
    sync, report, service, secrets = build(db, world, clock)
    return service, secrets


@pytest.mark.parametrize("change,expected", [
    ({"min_probability": 0.97}, "szansy co najmniej 97%"),
    ({"target_odds": 5000.0, "max_events": 3}, "najwyższy możliwy kurs"),
    ({"target_odds": 1.05, "min_events": 3}, "najniższy możliwy kurs"),
    ({"min_events": 30, "max_events": 30}, "minimalna liczba zdarzeń to 30"),
    ({"leagues": ["XX"]}, "W wybranych ligach brak meczów"),
    ({"date_range": "custom", "date_from": "2027-06-20", "date_to": "2027-06-21"}, "Brak meczów na"),
])
def test_reason_for_each_filter(ready, change, expected):
    service, secrets = ready
    result = service.run(demo_cfg(**change), secrets=secrets)
    assert result.coupons == []
    assert expected in result.diagnosis.reasons[0], result.diagnosis.reasons
    assert result.diagnosis.hints


def test_generator_always_returns_coupon_or_reason(ready):
    """Losowe ustawienia na dostępnych danych: zawsze kupon w zakresie albo powód i podpowiedź."""
    service, secrets = ready
    service.evaluate(demo_cfg(days_ahead=14))
    rng = random.Random(3)
    seen = {"ok": 0, "reason": 0}
    for _ in range(60):
        lo = rng.randint(1, 6)
        cfg = demo_cfg(
            days_ahead=rng.choice([1, 2, 3, 7, 14]), target_odds=round(rng.choice([1.3, 2, 3, 5, 10, 25, 80]), 2),
            tolerance=rng.choice([0.03, 0.1, 0.2]), min_events=lo, max_events=lo + rng.randint(0, 5),
            min_probability=rng.choice([0.3, 0.45, 0.55, 0.7, 0.85]),
            markets=rng.sample(list(MARKETS), rng.randint(1, 4)), include_low_data=rng.random() < 0.3,
            leagues=rng.choice([[], ["PL"], ["EKS"]]), alternatives=3)
        result = service.run(cfg, secrets=secrets)
        if result.coupons:
            seen["ok"] += 1
            assert all(c.in_range and cfg.min_events <= len(c.legs) <= cfg.max_events for c in result.coupons)
            assert result.diagnosis.ok
        else:
            seen["reason"] += 1
            assert result.diagnosis.reasons and result.diagnosis.hints, cfg
            assert not result.diagnosis.ok
    assert seen["ok"] >= 5 and seen["reason"] >= 5, seen


def test_diagnosis_counts_are_monotonic_and_text(ready):
    service, secrets = ready
    diag = service.run(demo_cfg(), secrets=secrets).diagnosis
    counts = [st.matches for st in diag.stages[:-1]]
    assert counts == sorted(counts, reverse=True) and counts[0] > 0
    text = diag.to_text()
    assert "Po kolejnych filtrach" in text and "football-data.co.uk" in text


def test_last_report_roundtrip(ready, db):
    from typerbot.services.sync import load_last_report

    report = load_last_report(db)
    assert report is not None and report.steps and report.finished
    assert sync_problems(report) == []                        # wszystkie źródła działają


def test_partial_alternatives_are_explained(ready):
    service, secrets = ready
    result = service.run(replace(demo_cfg(), leagues=["EKS"], days_ahead=1, target_odds=2.0, min_events=1,
                                 max_events=2, alternatives=3), secrets=secrets)
    if 0 < len(result.coupons) < 3:
        assert any("Ułożono" in n for n in result.diagnosis.notes)
