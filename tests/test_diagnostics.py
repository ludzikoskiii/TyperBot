"""Diagnostyka generatora: generator zawsze zwraca kupon albo konkretny powód (z podpowiedzią)."""

import random
from dataclasses import replace

import pytest

from tests.test_coupons import SEASON_NOW, demo_cfg
from tests.test_sync import ALL_SOURCES, make_service
from typerbot.config.settings import MARKETS
from typerbot.demo.transport import DemoTransport
from typerbot.demo.world import DemoWorld
from typerbot.services.coupons import CouponService
from typerbot.services.diagnostics import sync_problems


@pytest.fixture(scope="module")
def world():
    return DemoWorld(SEASON_NOW)


def build(db, world, clock, *, fail=(), **transport_kw):
    sync, _ = make_service(db, None, world, clock, now=SEASON_NOW, fail=set(fail), **transport_kw)
    report = sync.run_all()
    return sync, report, CouponService(db, now=lambda: SEASON_NOW), None


def test_no_fixtures_gives_concrete_reason_with_sources(db, world, clock):
    """Terminarz się nie pobrał (brak internetu przy pierwszym uruchomieniu) -> powód wskazuje źródła i co zrobić."""
    sync, report, service, secrets = build(db, world, clock, fail=ALL_SOURCES)
    result = service.run(demo_cfg(), secrets=secrets)
    diag = result.diagnosis
    assert result.coupons == [] and diag.stages[0].matches == 0
    reason = diag.reasons[0]
    assert "terminarz nie został pobrany" in reason and "football-data.co.uk" in reason and "openfootball" in reason
    assert any("sprawdź połączenie" in h for h in diag.hints)
    assert next(s for s in diag.sources if s.source == "football_data_csv").state == "offline"
    problems = sync_problems(sync.last_report())
    assert problems == diag.problems
    csv = [p for p in problems if p.source == "football_data_csv" and p.step == "fixtures"]
    assert len(csv) == 1                     # ten sam problem w kilku krokach to jeden wpis


def test_offline_after_successful_sync_works_on_last_data(db, world, clock):
    sync, report, service, secrets = build(db, world, clock)
    as_of = sync.data_as_of()
    clock.advance(6 * 3600)
    sync.http.transport = DemoTransport(world, fail=set(ALL_SOURCES))
    sync.run_all(force=True)
    result = service.run(demo_cfg(), secrets=secrets)
    assert result.coupons                                     # kupony z danych w bazie
    assert result.diagnosis.data_as_of == as_of               # i data tych danych
    assert "Dane z:" in result.diagnosis.to_text()


def test_missing_bookmaker_odds_reason(db, world, clock):
    # pliki z kursami jeszcze nieopublikowane – mecze tylko z terminarza openfootball / OpenLigaDB
    sync, report, service, secrets = build(db, world, clock, fixtures_days=0)
    diag = service.run(demo_cfg(leagues=["PL", "BL3"], estimated_odds="never"), secrets=secrets).diagnosis
    assert diag.stages[2].matches > 0                      # mecze i prognozy są
    assert "nie ma jeszcze kursów bukmacherów" in diag.reasons[0]
    assert any("piątek" in h and "wtorek" in h for h in diag.hints)
    assert any("Kursy szacunkowe" in h for h in diag.hints)


@pytest.fixture
def ready(db, world, clock):
    sync, report, service, secrets = build(db, world, clock)
    return service, secrets


@pytest.mark.parametrize("change,expected", [
    ({"min_probability": 0.97}, "szansy co najmniej 97%"),
    ({"target_odds": 5000.0, "max_events": 3}, "najwyższy możliwy kurs"),
    ({"target_odds": 1.05, "min_events": 3, "sports": ["football"]}, ("najniższy możliwy kurs", "górna granica kuponu")),
    ({"min_events": 30, "max_events": 30}, "minimalna liczba zdarzeń to 30"),
    ({"leagues": ["XX"]}, "W wybranych ligach nie ma meczów"),
    ({"date_range": "custom", "date_from": "2027-08-01", "date_to": "2027-08-02"}, "Brak meczów na"),
])
def test_reason_for_each_filter(ready, change, expected):
    service, secrets = ready
    result = service.run(demo_cfg(**change), secrets=secrets)
    assert result.coupons == []
    options = expected if isinstance(expected, tuple) else (expected,)
    assert any(e in result.diagnosis.reasons[0] for e in options), result.diagnosis.reasons
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
            leagues=rng.choice([[], ["PL"], ["EKS"], ["BL3"], ["JPN", "USA"]]), alternatives=3)
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
    assert "Po kolejnych filtrach" in text and "football-data.co.uk" in text and "Dni w zakresie" in text
    csv = next(s for s in diag.sources if s.source == "football_data_csv")
    assert csv.days and sum(csv.days.values()) == csv.matches


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
