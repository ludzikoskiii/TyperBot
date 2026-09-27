"""Testy kontroli budżetu i eksportu kuponów."""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from typerbot.config.settings import SettingsStore
from typerbot.data.records import MatchRecord
from typerbot.data.repository import LeagueRepository, MatchRepository
from typerbot.services.budget import EXCEEDED, NO_LIMIT, OK, WARNING, BudgetService, month_label
from typerbot.services.register import CouponRegister, LegInput, export_csv
from typerbot.services.stats import StatsService, local_month

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(db):
    LeagueRepository(db).ensure_defaults()
    repo = MatchRepository(db)
    ids = [repo.save_records([MatchRecord("api_football", str(i), "PL", 2026, NOW - timedelta(days=1), f"Home {i}",
                                          f"Away {i}")])[0].match_id for i in range(4)]
    store = SettingsStore(db)
    settings = store.load()
    settings.budget.monthly_limit = 100.0
    store.save(settings)
    return CouponRegister(db, now=lambda: NOW), BudgetService(db, now=lambda: NOW), ids, repo


def leg(mid, odds=2.0):
    return LegInput(mid, "PL", "1X2", "H", 0.0, odds, 0.5)


def test_local_month_boundary():
    assert local_month("2026-09-30T22:30:00Z") == "2026-10"     # 1.10 00:30 w Polsce
    assert local_month("2026-09-30T21:30:00Z") == "2026-09"
    assert month_label("2026-10") == "październik 2026"


def test_budget_levels_and_cashflow(env):
    register, budget, ids, repo = env
    assert budget.status().level == OK and budget.status().staked == 0
    register.save([leg(ids[0])], 30, placed_at=NOW - timedelta(days=40))           # wrzesień – nie liczy się
    register.save([leg(ids[1])], 50, placed_at=NOW - timedelta(days=2))
    st = budget.status()
    assert st.month == "2026-10" and st.staked == 50 and st.pending_stake == 50 and st.level == OK
    register.save([leg(ids[2])], 35, placed_at=NOW - timedelta(days=1))
    assert budget.status().level == WARNING                                          # 85% limitu
    repo.set_score(ids[1], 2, 0)
    register.settle_pending()
    st = budget.status()
    assert st.payouts == pytest.approx(50 * 0.88 * 2.0) and st.balance == pytest.approx(88.0 - 85.0)
    ok, msg = budget.check_stake(20)
    assert not ok and "przekroczy" in msg
    register.save([leg(ids[3])], 20)
    st = budget.status()
    assert st.level == EXCEEDED and "Przekroczono" in st.warning()
    assert "październik 2026" in st.summary().lower()


def test_no_limit(env, db):
    register, budget, ids, _ = env
    store = SettingsStore(db)
    s = store.load()
    s.budget.monthly_limit = 0
    store.save(s)
    register.save([leg(ids[0])], 1000)
    st = budget.status()
    assert st.level == NO_LIMIT and st.warning() is None and budget.check_stake(5000) == (True, None)


def test_stats_month_uses_local_time(env, db):
    register, _, ids, _ = env
    register.save([leg(ids[0])], 10, placed_at=datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc))
    months = StatsService(db, now=lambda: NOW).by_month()
    assert [m.month for m in months] == ["2026-10"]


def test_export_csv(env, tmp_path):
    register, _, ids, _ = env
    register.save([leg(ids[0], 1.8), leg(ids[1], 2.2)], 10, bookmaker="Superbet", note="test")
    path = tmp_path / "kupony.csv"
    assert export_csv(register.list(), str(path)) == 2
    with open(path, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0][0] == "Nr kuponu" and len(rows) == 3
    assert rows[1][3] == "10,00" and rows[1][4] == "3,96" and rows[1][13] == "1" and rows[1][18] == "test"
