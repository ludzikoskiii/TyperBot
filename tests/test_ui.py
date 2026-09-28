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
    assert "czekiwan" in tab.details.text()          # „Oczekiwane gole” / „oczekiwany wynik” (inne dyscypliny)
    tab.value_only.setChecked(True)
    assert tab.table.rowCount() <= len(tab.evaluated)


def test_status_bar_reports_sources_and_data_date(window):
    text = window.sources_label.text()
    for name in ("football-data.co.uk", "openfootball", "OpenLigaDB", "Reprezentacje"):
        assert name in text
    for gone in ("football-data.org", "OddsPapi", "The Odds API", "zużyto"):
        assert gone not in text
    assert "dane z:" in text and "nigdy" not in text
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
    assert len(cards) == tab.count.value() == 5
    probs = [c.coupon.probability for c in cards]
    assert probs == sorted(probs, reverse=True)                  # od najwyższej szansy trafienia
    card = cards[0]
    assert card.coupon.in_range and card.coupon.history_id
    assert all(leg.summary.endswith(".") for leg in card.coupon.legs)   # jedno zdanie uzasadnienia
    history = window.history.list
    assert history.table.rowCount() == len(cards)

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
    assert not hasattr(tab, "key_edits") and not hasattr(tab, "quota_table")      # bez kluczy i limitów API
    assert tab.sources_table.rowCount() == 7          # 5 źródeł piłkarskich + NFL i MLB
    tab.model_weight.setValue(45)
    tab.elo_weight.setValue(30)
    tab.estimated_margin.setValue(8)
    tab.market_checks["BTTS"].setChecked(False)
    tab.source_checks["openligadb"].setChecked(False)
    tab.leagues.set_country("Japonia", False)
    tab.save()
    s = window.ctx.settings()
    assert s.model.model_weight == pytest.approx(0.45) and s.model.elo_weight == pytest.approx(0.30)
    assert s.odds.estimated_margin == pytest.approx(0.08) and not s.sync.openligadb
    assert "BTTS" not in s.markets_enabled and "BTTS" not in s.coupon.markets
    assert not window.coupons.markets["BTTS"].isEnabled()     # generator reaguje na zmianę ustawień
    assert not window.ctx.sync.leagues.get("JPN").enabled
    assert "JPN" not in window.coupons.leagues.codes()        # liga wyłączona znika z generatora


def test_league_tree_search_and_countries(window):
    tree = window.coupons.leagues
    assert {"PL", "E2", "EKS", "BL3", "BRA", "JPN", "USA"} <= set(tree.codes())
    assert tree.all_checked() and window.coupons.current_cfg().leagues == []
    tree.set_country("Anglia", False)
    cfg = window.coupons.current_cfg()
    assert "PL" not in cfg.leagues and "E2" not in cfg.leagues and "BRA" in cfg.leagues
    tree.search.setText("brazy")
    visible = [tree.tree.topLevelItem(i).text(0) for i in range(tree.tree.topLevelItemCount())
               if not tree.tree.topLevelItem(i).isHidden()]
    assert visible == ["Brazylia"]
    tree.search.setText("")


def test_estimated_odds_coupon_is_clearly_marked(window):
    tab = window.coupons
    tab.leagues.set_checked(["BL3"])                   # 3. Liga – tylko terminarz, bez kursów bukmacherów
    tab.set_range("custom")
    from PySide6.QtCore import QDate
    tab.date_from.setDate(QDate(2026, 10, 13))
    tab.date_to.setDate(QDate(2026, 10, 18))
    tab.target.setValue(3.0)
    tab.tolerance.setValue(20)
    tab.min_prob.setValue(35)
    tab.generate()
    cards = tab.cards()
    assert cards and all(c.coupon.estimated_legs == len(c.coupon.legs) for c in cards)
    texts = [w.text() for w in cards[0].findChildren(QtWidgets.QLabel)]
    assert any("Kupon z kursami szacunkowymi" in t for t in texts)
    assert any("kurs szacunkowy – sprawdź u bukmachera" in t for t in texts)
    assert "UWAGA: kupon z kursami szacunkowymi" in cards[0].text()
    assert "kursami szacunkowymi" in tab.status.text()


def test_no_money_anywhere_and_export(window, tmp_path):
    assert not hasattr(window, "budget") and not hasattr(window, "budget_banner")
    generate(window)
    rows = window.history.list.export(str(tmp_path / "k.csv"))
    assert rows > 0 and "zł" not in (tmp_path / "k.csv").read_text(encoding="utf-8-sig")


def test_coupon_count_setting_and_letters(window):
    tab = window.coupons
    assert tab.count.value() == 5                           # domyślnie 5 kuponów
    tab.count.setValue(7)
    tab.difference.setCurrentIndex(1)                       # wystarczy 1 inny mecz
    cfg = tab.current_cfg()
    assert cfg.alternatives == 7 and cfg.min_difference == 0.0
    generate(window, days=14, target=3.0, min_prob=35)
    cards = tab.cards()
    assert 5 < len(cards) <= 7
    assert [c.letter for c in cards] == list("ABCDEFG"[:len(cards)])


def test_sport_checkboxes_filter_leagues_and_coupons(window, app):
    tab = window.coupons
    assert {"football", "american_football", "handball", "hockey", "baseball"} <= set(tab.sport_checks)
    assert tab.current_cfg().sports == []                          # domyślnie wszystkie dyscypliny
    for code, cb in tab.sport_checks.items():
        cb.setChecked(code == "hockey")
    assert tab.current_cfg().sports == ["hockey"]
    visible = [code for code, item in tab.leagues._leagues.items() if not item.isHidden()]
    assert visible and all(code.startswith("OL-") for code in visible)   # w drzewie tylko ligi hokejowe
    tab.target.setValue(3.0)
    tab.generate()
    app.processEvents()
    cards = tab.cards()
    assert cards and all(leg.match.sport == "hockey" for c in cards for leg in c.coupon.legs)
    for cb in tab.sport_checks.values():
        cb.setChecked(False)
    tab.generate()
    assert "dyscyplinę" in tab.status.text()
