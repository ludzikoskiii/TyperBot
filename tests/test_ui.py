"""Testy interfejsu (bez ekranu – platforma offscreen, zadania w tle wykonywane od razu)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from tests.conftest import NOW  # noqa: E402
from typerbot.services.register import LOST, PENDING, WON  # noqa: E402
from typerbot.ui import workers  # noqa: E402
from typerbot.ui.context import demo_context  # noqa: E402
from typerbot.ui.main_window import MainWindow  # noqa: E402
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
    for name in ("football-data.co.uk", "football-data.org", "OddsPapi", "The Odds API"):
        assert name in text
    assert "Zaktualizowano" in window.message_label.text()


def test_generator_records_history_swap_and_manual_odds(window, monkeypatch):
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
    assert not hasattr(gen, "stake") and "Wygrana" not in card.tiles
    # kupony same trafiają do historii
    history = window.history.list
    assert history.table.rowCount() == 3 and card.coupon.history_id
    assert f"nr {card.coupon.history_id}" in card.history_label.text()

    # zmiana kursu (okno dialogowe zastąpione odpowiedzią) – aktualizuje wpis w historii
    monkeypatch.setattr(QtWidgets.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (2.5, True)))
    card.table.selectRow(0)
    card.change_odds()
    assert card.coupon.legs[0].selection.odds == 2.5
    assert card.tiles["Kurs łączny"].value.text().replace(",", ".") == f"{card.coupon.odds:.2f}"
    stored = window.ctx.register.get(card.coupon.history_id)
    assert stored.odds == pytest.approx(card.coupon.odds, abs=1e-3)

    # wymiana zdarzenia
    leg = card.selected_leg()
    options = card.service.swap_options(card.coupon, leg.match.match_id, card.cfg)
    card.coupon = card.service.swap(card.coupon, leg.match.match_id, options[0])
    card._updated()
    assert options[0].leg.match.match_id in {x.match.match_id for x in card.coupon.legs}
    stored = window.ctx.register.get(card.coupon.history_id)
    assert {x.match_id for x in stored.legs} == card.coupon.match_ids and stored.status == PENDING
    assert "w trakcie" in window.history.list.summary.text()


def test_history_settles_and_shows_units(window):
    ctx = window.ctx
    rows = ctx.db.query("SELECT id, league_code FROM matches WHERE status = 'FINISHED' AND home_goals IS NOT NULL "
                        "ORDER BY kickoff DESC LIMIT 2")
    from typerbot.services.register import LegInput
    cid = ctx.register.save([LegInput(r["id"], r["league_code"], "DC", "1X", 0.0, 1.4, 0.7) for r in rows],
                            probability=0.49)
    window.history.list.settle()                               # synchronicznie: pobranie wyników + rozliczenie
    stored = ctx.register.get(cid)
    assert stored.status in (WON, LOST)
    stats = window.history.stats
    assert stats.tiles["Kupony"].value.text() == "1"
    expected = "+0,72 j." if stored.status == WON else "-1,00 j."     # 1,96 · 0,88 − 1 = 0,72
    assert stats.tiles["Wynik"].value.text() == expected
    assert stats.market_table.rowCount() == 1 and stats.charts_row.count() == 2
    assert "zł" not in stats.tiles["Wynik"].value.text()
    window.history.copied.setChecked(True)                      # filtr skopiowanych – brak kuponów
    assert window.history.list.table.rowCount() == 0
    window.history.copied.setChecked(False)
    assert window.history.list.table.rowCount() == 1


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
    tab.market_checks["BTTS"].setChecked(False)
    tab.save()
    s = window.ctx.settings()
    assert s.model.model_weight == pytest.approx(0.45)
    assert "BTTS" not in s.markets_enabled and "BTTS" not in s.coupon.markets
    assert not window.generator.markets["BTTS"].isEnabled()   # generator reaguje na zmianę ustawień
    tab.key_edits["oddspapi"].setText("nowy-klucz-9999")
    tab.save_key("oddspapi")
    assert window.ctx.secrets.get("oddspapi") == "nowy-klucz-9999"
    assert tab.quota_table.rowCount() >= 4


def test_no_money_anywhere_and_export(window, tmp_path):
    assert not hasattr(window, "budget") and not hasattr(window, "budget_banner")
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Historia" in titles and "Moje kupony" not in titles
    gen = window.generator
    gen.days.setValue(7)
    gen.generate()
    rows = window.history.list.export(str(tmp_path / "k.csv"))
    assert rows > 0 and "zł" not in (tmp_path / "k.csv").read_text(encoding="utf-8-sig")
