"""Testy interfejsu (bez ekranu – platforma offscreen, zadania w tle wykonywane od razu)."""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from tests.conftest import NOW  # noqa: E402
from typerbot.config.settings import Settings  # noqa: E402
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


def generate(window, days=7, target=4.0, tolerance=15, min_prob=40):
    tab = window.coupons
    tab.days.setValue(days)
    tab.set_range("days")
    tab.target.setValue(target)
    tab.tolerance.setValue(tolerance)
    tab.min_prob.setValue(min_prob)
    tab.generate()
    return tab


def test_three_tabs_and_simple_screen(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Kupony", "Historia", "Ustawienia"]
    tab = window.coupons
    assert not tab.advanced.isVisible() and not tab.expander.isChecked()      # zaawansowane zwinięte
    assert [b.text() for b in tab.range_buttons.values()][:2] == ["Dziś", "Jutro"]
    tab.range_buttons["custom"].click()
    assert tab.range_mode() == "custom" and tab.custom_row.isVisibleTo(tab)
    tab.range_buttons["tomorrow"].click()
    assert tab.current_cfg().date_range == "tomorrow" and not tab.custom_row.isVisibleTo(tab)
    tab.expander.setChecked(True)
    assert tab.advanced.isVisibleTo(tab)
    # „Wszystkie mecze” – dawna zakładka „Mecze” jest w zakładce „Kupony”
    tab.view_matches.click()
    assert tab.stack.currentWidget() is window.matches


def test_slip_cards_copy_swap_and_history(window, monkeypatch):
    tab = generate(window)
    cards = tab.cards()
    assert len(cards) == 3
    probs = [c.coupon.probability for c in cards]
    assert probs == sorted(probs, reverse=True)                  # od najwyższej szansy trafienia
    card = cards[0]
    assert card.coupon.in_range and card.coupon.history_id
    assert all(leg.summary.endswith(".") for leg in card.coupon.legs)   # jedno zdanie uzasadnienia
    history = window.history.list
    assert history.table.rowCount() == 3

    # kopiowanie – tekst kuponu w schowku, kupon oznaczony w historii jako skopiowany
    card.copy()
    text = QtWidgets.QApplication.clipboard().text()
    assert text.startswith("TyperBot – kupon A") and "Kurs łączny" in text and "Szansa trafienia" in text
    assert window.ctx.register.get(card.coupon.history_id).copied and "Skopiowano" in card.copied.text()
    window.history.copied.setChecked(True)
    assert window.history.list.table.rowCount() == 1
    window.history.copied.setChecked(False)

    # zmiana kursu (okno dialogowe zastąpione odpowiedzią) – aktualizuje wpis w historii
    monkeypatch.setattr(QtWidgets.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (2.5, True)))
    first = card.coupon.legs[0].match.match_id
    card.change_odds(first)
    assert card.coupon.legs[0].selection.odds == 2.5
    stored = window.ctx.register.get(card.coupon.history_id)
    assert stored.odds == pytest.approx(card.coupon.odds, abs=1e-3)

    # wymiana zdarzenia
    options = card.service.swap_options(card.coupon, first, card.cfg)
    card.coupon = card.service.swap(card.coupon, first, options[0])
    card._updated()
    assert options[0].leg.match.match_id in {x.match.match_id for x in card.coupon.legs}
    stored = window.ctx.register.get(card.coupon.history_id)
    assert {x.match_id for x in stored.legs} == card.coupon.match_ids and stored.status == PENDING
    assert "w trakcie" in window.history.list.summary.text()


def test_no_coupon_shows_reason(window):
    tab = generate(window, target=5000.0)
    assert tab.cards() == [] and "Żadna kombinacja" in tab.placeholder.text()
    assert tab.diag_btn.isEnabled() and tab.diagnosis and not tab.diagnosis.ok


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
    bt = window.settings.backtest
    assert bt.seasons.text()                                  # sezony podpowiedziane po synchronizacji
    bt.run()
    assert bt.result is not None and bt.metrics.rowCount() >= 3
    assert "Kupony" in bt.finance.text()
    assert bt.chart_row.count() >= 2


def test_settings_screens_keep_defaults(window):
    """Otwarcie i zapis ustawień bez zmian nie może obciąć wartości domyślnych (np. zakresem pola)."""
    defaults = Settings()
    s = window.settings.collect()
    assert (s.model, s.odds, s.tax, s.sync) == (defaults.model, defaults.odds, defaults.tax, defaults.sync)
    cfg = window.coupons.current_cfg()
    assert replace(cfg, date_from="", date_to="") == defaults.coupon


def test_settings_roundtrip(window):
    tab = window.settings
    tab.model_weight.setValue(45)
    tab.market_checks["BTTS"].setChecked(False)
    tab.save()
    s = window.ctx.settings()
    assert s.model.model_weight == pytest.approx(0.45)
    assert "BTTS" not in s.markets_enabled and "BTTS" not in s.coupon.markets
    assert not window.coupons.markets["BTTS"].isEnabled()     # generator reaguje na zmianę ustawień
    tab.key_edits["oddspapi"].setText("nowy-klucz-9999")
    tab.save_key("oddspapi")
    assert window.ctx.secrets.get("oddspapi") == "nowy-klucz-9999"
    assert tab.quota_table.rowCount() >= 4


def test_no_money_anywhere_and_export(window, tmp_path):
    assert not hasattr(window, "budget") and not hasattr(window, "budget_banner")
    generate(window)
    rows = window.history.list.export(str(tmp_path / "k.csv"))
    assert rows > 0 and "zł" not in (tmp_path / "k.csv").read_text(encoding="utf-8-sig")
