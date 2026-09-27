"""Testy interfejsu (bez ekranu – platforma offscreen, zadania w tle wykonywane od razu)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from tests.conftest import NOW  # noqa: E402
from typerbot.services.register import PENDING, WON  # noqa: E402
from typerbot.ui import workers  # noqa: E402
from typerbot.ui.context import demo_context  # noqa: E402
from typerbot.ui.main_window import MainWindow  # noqa: E402
from typerbot.ui.tab_generator import SaveCouponDialog  # noqa: E402
from typerbot.ui.theme import apply_theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(application)
    return application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "SYNCHRONOUS", True)
    ctx = demo_context(NOW, folder=tmp_path)
    win = MainWindow(ctx)
    win.start()
    app.processEvents()
    yield win
    win.close()
    ctx.db.close()


def test_matches_tab_shows_predictions(window):
    tab = window.matches
    assert tab.table.rowCount() > 0
    assert tab.selected_match_id() is not None
    assert tab.sel_table.rowCount() >= 8                      # wszystkie typy wybranego meczu
    assert "Oczekiwane gole" in tab.details.text()
    tab.value_only.setChecked(True)
    assert tab.table.rowCount() <= len(tab.evaluated)


def test_status_bar_reports_sources(window):
    text = window.sources_label.text()
    for name in ("football-data.org", "API-Football", "OddsPapi", "The Odds API"):
        assert name in text
    assert "Zaktualizowano" in window.message_label.text()


def test_generator_swap_manual_odds_and_save(window, monkeypatch):
    gen = window.generator
    gen.days.setValue(7)
    gen.target.setValue(4.0)
    gen.tolerance.setValue(15)
    gen.min_prob.setValue(50)
    gen.generate()
    cards = gen.cards()
    assert len(cards) == 3
    card = cards[0]
    assert card.table.rowCount() == len(card.coupon.legs)
    assert card.coupon.in_range
    assert "Forma:" in card.rationale.text()

    # zmiana kursu (okno dialogowe zastąpione odpowiedzią)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (2.5, True)))
    card.table.selectRow(0)
    card.change_odds()
    assert card.coupon.legs[0].selection.odds == 2.5
    assert card.tiles["Kurs łączny"].value.text().replace(",", ".") == f"{card.coupon.odds:.2f}"

    # wymiana zdarzenia
    leg = card.selected_leg()
    options = card.service.swap_options(card.coupon, leg.match.match_id, card.cfg)
    card.coupon = card.service.swap(card.coupon, leg.match.match_id, options[0])
    card.refresh()
    assert options[0].leg.match.match_id in {x.match.match_id for x in card.coupon.legs}

    # zapis jako postawiony
    dlg = SaveCouponDialog(window.ctx, card.coupon)
    dlg.stake.setValue(25)
    coupon_id = dlg.save()
    stored = window.ctx.register.get(coupon_id)
    assert stored.stake == 25 and stored.status == PENDING and len(stored.legs) == len(card.coupon.legs)
    window.ctx.hub.coupons_changed.emit()
    assert window.coupons.table.rowCount() == 1
    assert "w grze" in window.coupons.summary.text()


def test_coupons_tab_manual_result_and_stats(window):
    ctx = window.ctx
    rows = ctx.db.query("SELECT id, league_code FROM matches WHERE status = 'FINISHED' AND home_goals IS NOT NULL "
                        "ORDER BY kickoff DESC LIMIT 2")
    from typerbot.services.register import LegInput
    cid = ctx.register.save([LegInput(r["id"], r["league_code"], "DC", "1X", 0.0, 1.4, 0.7) for r in rows], 10)
    window.coupons.settle()                                   # synchronicznie: pobranie wyników + rozliczenie
    assert ctx.register.get(cid).status != PENDING
    ctx.register.set_manual_result(cid, WON, 30.0)
    ctx.hub.coupons_changed.emit()
    results = window.stats.results
    assert results.tiles["Wypłacono"].value.text().startswith("30,00")
    assert results.market_table.rowCount() == 1
    assert results.charts_row.count() == 2


def test_backtest_view_runs(window):
    bt = window.stats.backtest
    assert bt.seasons.text()                                  # sezony podpowiedziane po synchronizacji
    bt.run()
    assert bt.result is not None and bt.metrics.rowCount() >= 3
    assert "Kupony" in bt.finance.text()
    assert bt.chart_row.count() >= 2


def test_settings_roundtrip(window):
    tab = window.settings
    tab.model_weight.setValue(45)
    tab.monthly_limit.setValue(300)
    tab.market_checks["BTTS"].setChecked(False)
    tab.save()
    s = window.ctx.settings()
    assert s.model.model_weight == pytest.approx(0.45) and s.budget.monthly_limit == 300
    assert "BTTS" not in s.markets_enabled and "BTTS" not in s.coupon.markets
    assert not window.generator.markets["BTTS"].isEnabled()   # generator reaguje na zmianę ustawień
    tab.key_edits["oddspapi"].setText("nowy-klucz-9999")
    tab.save_key("oddspapi")
    assert window.ctx.secrets.get("oddspapi") == "nowy-klucz-9999"
    assert tab.quota_table.rowCount() >= 4
