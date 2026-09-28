"""Zakładka „Ustawienia”: Ogólne (ligi, rynki, kursy, podatek), Źródła danych, Model i backtest."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QInputDialog, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from typerbot.config.settings import MARKETS, Settings
from typerbot.data.errors import STATE_LABELS
from typerbot.data.sources import SOURCE_LABELS
from typerbot.model.markets import MARKET_HINTS, MARKET_NAMES
from typerbot.services.diagnostics import when_label
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.league_tree import LeagueTree
from typerbot.ui.widgets import NumItem, hbox, label, make_table, text_item

SOURCE_LINKS = {
    "football_data_csv": "https://www.football-data.co.uk",
    "openfootball": "https://github.com/openfootball/football.json",
    "openligadb": "https://www.openligadb.de",
    "international": "https://github.com/martj42/international_results",
    "club_names": "https://github.com/openfootball/clubs",
    "nflverse": "https://github.com/nflverse/nfldata",
    "mlb": "https://statsapi.mlb.com",
}
# Ustawienie → opis pola wyboru (źródła, które można wyłączyć).
OPTIONAL_SOURCES = {
    "openfootball": f"Używaj: {SOURCE_LABELS['openfootball']}",
    "openligadb": f"Używaj: {SOURCE_LABELS['openligadb']}",
    "openligadb_more": "Więcej lig i dyscyplin z OpenLigaDB (piłka ręczna, hokej, piłka nożna kobiet, niższe ligi)",
    "international": f"Używaj: {SOURCE_LABELS['international']}",
    "nflverse": f"Futbol amerykański – {SOURCE_LABELS['nflverse']}",
    "mlb": f"Baseball – {SOURCE_LABELS['mlb']}",
}


def _dspin(lo, hi, dec=2, step=0.1, suffix=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    return s


def _spin(lo, hi, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    if suffix:
        s.setSuffix(suffix)
    return s


class SettingsTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

        # -- Ogólne: ligi, rynki, kursy, podatek --------------------------------------------------------------
        leagues_box = QGroupBox("Ligi używane w aplikacji (pobierane dane i generator)")
        self.leagues = LeagueTree(counts_label="Nadchodzące")
        self.leagues.setMinimumHeight(360)
        ll = QVBoxLayout(leagues_box)
        ll.addWidget(label("Lista pochodzi z danych: 38 lig z football-data.co.uk, ligi z openfootball i "
                           "OpenLigaDB oraz inne dyscypliny (NFL, MLB, piłka ręczna, hokej); nowe ligi dopisują się "
                           "same. Zaznacz kraj, aby wybrać wszystkie jego ligi.", "muted", wrap=True))
        ll.addWidget(self.leagues)

        markets_box = QGroupBox("Rynki")
        self.market_checks = {m: QCheckBox(MARKET_NAMES[m]) for m in MARKETS}
        for m, cb in self.market_checks.items():
            cb.setToolTip(MARKET_HINTS[m])
        ml = QVBoxLayout(markets_box)
        for cb in self.market_checks.values():
            ml.addWidget(cb)

        odds_box = QGroupBox("Kursy")
        self.reference = QComboBox()
        self.reference.addItem("Średnia rynkowa", "average")
        self.reference.addItem("Najwyższy kurs", "best")
        self.reference.addItem("Pinnacle (brak → średnia)", "pinnacle")
        self.reference.addItem("Bet365 (brak → średnia)", "bet365")
        self.margin = QComboBox()
        self.margin.addItem("proporcjonalnie", "proportional")
        self.margin.addItem("metoda Shina", "shin")
        self.estimated_margin = _dspin(0, 30, 1, 0.5, " %")
        of = QFormLayout(odds_box)
        of.addRow("Kurs do oceny typów", self.reference)
        of.addRow("Usuwanie marży", self.margin)
        of.addRow("Marża kursu szacunkowego", self.estimated_margin)
        of.addRow(label("Kurs szacunkowy (≈) dostają typy bez kursu w źródłach: 1 / (prawdopodobieństwo × "
                        "(1 + marża)). Jest wyraźnie oznaczony – sprawdź go u bukmachera.", "muted", wrap=True))

        tax_box = QGroupBox("Podatek")
        self.stake_tax = _dspin(0, 50, 1, 1, " %")
        self.pays_tax = QCheckBox("Bukmacher pokrywa podatek od stawki")
        tf = QFormLayout(tax_box)
        tf.addRow("Podatek od stawki", self.stake_tax)
        tf.addRow("", self.pays_tax)
        tf.addRow(label("Kurs po podatku = kurs × 0,88. Wyniki w historii liczone są w jednostkach "
                        "(1 kupon = 1 jednostka) – aplikacja nie używa kwot.", "muted", wrap=True))

        # -- Źródła danych ------------------------------------------------------------------------------------
        sources_box = QGroupBox("Źródła danych – wszystkie darmowe, bez klucza i rejestracji")
        self.sources_table = make_table(["Źródło", "Co daje", "Stan", "Dane z", "Zapytań dziś"], stretch=1,
                                        sortable=False)
        self.sources_table.setMinimumHeight(190)
        self.source_checks = {key: QCheckBox(text) for key, text in OPTIONAL_SOURCES.items()}
        sl = QVBoxLayout(sources_box)
        sl.addWidget(self.sources_table)
        for cb in self.source_checks.values():
            sl.addWidget(cb)
        links = " · ".join(f"<a href='{url}' style='color:{theme.ACCENT}'>{SOURCE_LABELS[name]}</a>"
                           for name, url in SOURCE_LINKS.items())
        link_label = label(links)
        link_label.setOpenExternalLinks(True)
        sl.addWidget(link_label)
        sl.addWidget(label("Wszystko, co pobrane, zostaje w bazie: bez internetu albo przy awarii źródła aplikacja "
                           "działa na ostatnich danych (kolumna „Dane z”). Awaria jednego źródła nie blokuje "
                           "pozostałych.", "muted", wrap=True))

        sync_box = QGroupBox("Pobieranie danych")
        self.fixtures_hours = _dspin(0.5, 48, 1, 0.5, " h")
        self.csv_seasons = _spin(2, 25, " sezonów")
        sf = QFormLayout(sync_box)
        sf.addRow("Odświeżanie automatyczne co", self.fixtures_hours)
        sf.addRow("Historia z football-data.co.uk", self.csv_seasons)
        sf.addRow(label("Pierwsze pobranie historii wszystkich lig trwa kilka minut (pliki są pobierane raz, "
                        "potem tylko bieżący sezon). Kursy na weekend pojawiają się w piątek, na środek tygodnia – "
                        "we wtorek; wcześniej terminarz daje openfootball.", "muted", wrap=True))

        teams_box = QGroupBox("Dopasowanie nazw drużyn do sprawdzenia")
        self.teams_table = make_table(["Liga", "Źródło", "Nazwa w źródle", "Połączona z", "Metoda", "Zgodność"],
                                      stretch=3, sortable=False)
        self.teams_table.setMinimumHeight(160)
        confirm = QPushButton("Potwierdź dopasowanie")
        merge = QPushButton("Połącz z inną drużyną…")
        confirm.clicked.connect(self.confirm_alias)
        merge.clicked.connect(self.merge_alias)
        tl = QVBoxLayout(teams_box)
        tl.addWidget(label("Nazwy z różnych źródeł (np. „Man United” i „Manchester United FC”) są łączone "
                           "automatycznie – także na podstawie listy wariantów nazw klubów z openfootball. "
                           "Tu są tylko dopasowania niepewne.", "muted", wrap=True))
        tl.addWidget(self.teams_table)
        tl.addLayout(hbox(confirm, merge, None))

        # -- Model ------------------------------------------------------------------------------------------
        model_box = QGroupBox("Model prognoz")
        self.last_matches = _spin(5, 200, " meczów")
        self.half_life = _dspin(10, 2000, 0, 10, " dni")
        self.min_matches = _spin(1, 30, " meczów")
        self.regularization = _dspin(0, 200, 1, 1)
        self.model_weight = _spin(0, 100, " %")
        self.elo_weight = _spin(0, 100, " %")
        self.elo_k = _dspin(5, 60, 0, 1)
        self.dixon_coles = QCheckBox("Korekta Dixona-Colesa (0:0, 1:0, 0:1, 1:1)")
        mf = QFormLayout(model_box)
        mf.addRow("Ostatnie mecze drużyny", self.last_matches)
        mf.addRow("Półokres wygaszania", self.half_life)
        mf.addRow("„Mało danych” poniżej", self.min_matches)
        mf.addRow("Regularyzacja", self.regularization)
        mf.addRow("Udział rankingu Elo w modelu", self.elo_weight)
        mf.addRow("Szybkość zmian Elo (K)", self.elo_k)
        mf.addRow("Udział modelu w prognozie", self.model_weight)
        mf.addRow("", self.dixon_coles)
        mf.addRow(label("Model = Dixon-Coles połączony z rankingiem Elo (z samych wyników – działa też dla lig "
                        "bez szczegółowych statystyk i dla reprezentacji). Resztę prognozy stanowi rynek (kursy bez "
                        "marży), gdy są kursy. Najlepsze wartości dobierzesz poniżej: „Strojenie parametrów”. "
                        "Te ustawienia dotyczą piłki nożnej; inne dyscypliny mają model wyników z parametrami "
                        "dobranymi backtestem (README, „Inne dyscypliny”).", "muted", wrap=True))

        self.save_btn = QPushButton("Zapisz ustawienia")
        self.save_btn.setProperty("role", "primary")
        self.defaults_btn = QPushButton("Przywróć domyślne")
        self.status = label("", "muted")

        def page(*rows) -> QScrollArea:
            content = QWidget()
            grid = QGridLayout(content)
            for r, widgets in enumerate(rows):
                for c, w in enumerate(widgets):
                    grid.addWidget(w, r, c, 1, 2 if len(widgets) == 1 else 1)
            grid.setRowStretch(len(rows), 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            return scroll

        # Model i backtest – strojenie zmienia ustawienia modelu, więc są w jednym miejscu.
        from typerbot.ui.tab_stats import BacktestView

        self.backtest = BacktestView(ctx)
        model_page = QWidget()
        mpl = QVBoxLayout(model_page)
        mpl.setContentsMargins(0, 0, 0, 0)
        model_box.setMaximumWidth(760)
        mpl.addWidget(model_box)
        mpl.addWidget(self.backtest, 1)
        side = QWidget()
        sdl = QVBoxLayout(side)
        sdl.setContentsMargins(0, 0, 0, 0)
        for box in (markets_box, odds_box, tax_box):
            sdl.addWidget(box)
        sdl.addStretch(1)
        self.pages = QTabWidget()
        self.pages.addTab(page((leagues_box, side)), "Ogólne")
        self.pages.addTab(page((sources_box,), (sync_box,), (teams_box,)), "Źródła danych")
        self.pages.addTab(model_page, "Model i backtest")
        lay = QVBoxLayout(self)
        lay.addWidget(self.pages, 1)
        lay.addLayout(hbox(self.status, None, self.defaults_btn, self.save_btn))

        self.save_btn.clicked.connect(self.save)
        self.defaults_btn.clicked.connect(self.restore_defaults)
        ctx.hub.data_changed.connect(self.refresh_live)
        self.load(ctx.settings())
        self.refresh_live()

    # -- wczytanie / zapis ------------------------------------------------------------------------
    def load_leagues(self, keep_checks: bool = False) -> None:
        """Ligi z bazy; `keep_checks` – zachowaj niezapisane zaznaczenia (nowe ligi według „aktywna”)."""
        rows = self.ctx.sync.leagues.with_counts(self.ctx.now())
        shown = set(self.leagues.codes())
        current = set(self.leagues.checked_codes()) if keep_checks else set()
        checked = []
        for r in rows:
            known = keep_checks and r.league.code in shown
            if (r.league.code in current) if known else r.league.enabled:
                checked.append(r.league.code)
        self.leagues.set_leagues(rows, checked, only_with_data=False)

    def load(self, s: Settings) -> None:
        self.load_leagues()
        for m, cb in self.market_checks.items():
            cb.setChecked(m in s.markets_enabled)
        m = s.model
        self.last_matches.setValue(m.last_matches)
        self.half_life.setValue(m.half_life_days)
        self.min_matches.setValue(m.min_matches)
        self.regularization.setValue(m.regularization)
        self.model_weight.setValue(round(m.model_weight * 100))
        self.elo_weight.setValue(round(m.elo_weight * 100))
        self.elo_k.setValue(m.elo_k)
        self.dixon_coles.setChecked(m.dixon_coles)
        for box, value in ((self.reference, s.odds.reference), (self.margin, s.odds.margin_method)):
            box.setCurrentIndex(max(0, box.findData(value)))
        self.estimated_margin.setValue(s.odds.estimated_margin * 100)
        self.stake_tax.setValue(s.tax.stake_tax * 100)
        self.pays_tax.setChecked(s.tax.bookmaker_pays_tax)
        self.fixtures_hours.setValue(s.sync.fixtures_every_hours)
        self.csv_seasons.setValue(s.sync.csv_seasons)
        for key, cb in self.source_checks.items():
            cb.setChecked(getattr(s.sync, key))

    def collect(self) -> Settings:
        s = self.ctx.settings()
        s.markets_enabled = [m for m, cb in self.market_checks.items() if cb.isChecked()]
        s.model.last_matches = self.last_matches.value()
        s.model.half_life_days = self.half_life.value()
        s.model.min_matches = self.min_matches.value()
        s.model.regularization = self.regularization.value()
        s.model.model_weight = self.model_weight.value() / 100
        s.model.elo_weight = self.elo_weight.value() / 100
        s.model.elo_k = self.elo_k.value()
        s.model.dixon_coles = self.dixon_coles.isChecked()
        s.odds.reference = self.reference.currentData()
        s.odds.margin_method = self.margin.currentData()
        s.odds.estimated_margin = round(self.estimated_margin.value() / 100, 4)
        s.tax.stake_tax = self.stake_tax.value() / 100
        s.tax.bookmaker_pays_tax = self.pays_tax.isChecked()
        s.sync.fixtures_every_hours = self.fixtures_hours.value()
        s.sync.csv_seasons = self.csv_seasons.value()
        for key, cb in self.source_checks.items():
            setattr(s.sync, key, cb.isChecked())
        s.coupon.markets = [m for m in s.coupon.markets if m in s.markets_enabled] or list(s.markets_enabled)
        return s

    def save(self) -> None:
        settings = self.collect()
        if not settings.markets_enabled:
            QMessageBox.warning(self, "Ustawienia", "Włącz co najmniej jeden rynek.")
            return
        if not self.leagues.checked_codes():
            QMessageBox.warning(self, "Ustawienia", "Zaznacz co najmniej jedną ligę.")
            return
        checked = set(self.leagues.checked_codes())
        for code in self.leagues.codes():
            self.ctx.sync.leagues.set_enabled(code, code in checked)
        self.ctx.save_settings(settings)
        self.status.setText("Zapisano ustawienia.")

    def restore_defaults(self) -> None:
        if QMessageBox.question(self, "Ustawienia", "Przywrócić domyślne ustawienia (bez lig)?") != QMessageBox.Yes:
            return
        self.load(Settings())
        self.status.setText("Wczytano wartości domyślne – kliknij „Zapisz ustawienia”, aby je zastosować.")

    # -- dane na żywo -------------------------------------------------------------------------------
    def refresh_live(self) -> None:
        self.load_leagues(keep_checks=True)
        t = self.sources_table
        t.setRowCount(0)
        for row in self.ctx.sync.source_rows():
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(row.label, bold=True))
            t.setItem(r, 1, text_item(row.role, theme.MUTED, tooltip=row.role))
            color = theme.POSITIVE if row.state == "ok" else (
                theme.MUTED if row.state in ("idle", "disabled") else theme.WARNING)
            text = STATE_LABELS.get(row.state, row.state) + (f" – {row.message}" if row.message and row.state != "ok"
                                                              else "")
            t.setItem(r, 2, text_item(text, color, tooltip=text))
            t.setItem(r, 3, text_item(when_label(row.last_ok)))
            t.setItem(r, 4, NumItem(str(row.calls_today), row.calls_today))
        tt = self.teams_table
        tt.setRowCount(0)
        for row in self.ctx.sync.matches.matcher.review_list():
            r = tt.rowCount()
            tt.insertRow(r)
            item = text_item(row["league_code"])
            item.setData(Qt.UserRole, (row["source"], row["league_code"], row["name"], row["team_id"]))
            tt.setItem(r, 0, item)
            tt.setItem(r, 1, text_item(SOURCE_LABELS.get(row["source"], row["source"])))
            tt.setItem(r, 2, text_item(row["name"], bold=True))
            tt.setItem(r, 3, text_item(row["team_name"]))
            tt.setItem(r, 4, text_item(row["method"]))
            tt.setItem(r, 5, NumItem("–" if row["score"] is None else f"{row['score']:.0f}", row["score"]))

    def _selected_alias(self):
        rows = self.teams_table.selectionModel().selectedRows()
        return self.teams_table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def confirm_alias(self) -> None:
        sel = self._selected_alias()
        if sel:
            self.ctx.sync.matches.matcher.confirm_alias(*sel[:3])
            self.refresh_live()

    def merge_alias(self) -> None:
        sel = self._selected_alias()
        if not sel:
            return
        source, league, name, team_id = sel
        teams = self.ctx.db.query(
            "SELECT DISTINCT t.id, t.name FROM teams t JOIN team_aliases a ON a.team_id = t.id "
            "WHERE a.league_code = ? AND t.id != ? ORDER BY t.name", (league, team_id))
        if not teams:
            return
        choice, ok = QInputDialog.getItem(self, "Połącz drużyny", f"„{name}” to ta sama drużyna co:",
                                          [t["name"] for t in teams], 0, False)
        if ok:
            keep = next(t["id"] for t in teams if t["name"] == choice)
            merged = self.ctx.sync.matches.merge_teams(keep, team_id)
            self.status.setText(f"Połączono drużyny (scalone mecze: {merged}).")
            self.refresh_live()
            self.ctx.hub.data_changed.emit()
