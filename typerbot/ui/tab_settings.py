"""Zakładka „Ustawienia”."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QInputDialog, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from typerbot.config.leagues import League
from typerbot.config.secrets import KEYED_SOURCES, SecretStoreError, mask
from typerbot.config.settings import MARKETS, Settings
from typerbot.data.errors import STATE_LABELS
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import NumItem, hbox, label, make_table, text_item

SOURCE_INFO = {
    "football_data_org": ("football-data.org", "https://www.football-data.org/client/register"),
    "api_football": ("API-Football", "https://dashboard.api-football.com/register"),
    "the_odds_api": ("The Odds API", "https://the-odds-api.com"),
    "oddspapi": ("OddsPapi", "https://oddspapi.io"),
}
MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej 2,5", "BTTS": "Obie strzelą"}


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


class LeagueDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dodaj ligę")
        self.code, self.name, self.country = QLineEdit(), QLineEdit(), QLineEdit()
        self.cup = QCheckBox("Rozgrywki pucharowe (drużyny z różnych lig)")
        self.fd, self.apif, self.odds, self.papi = QLineEdit(), _spin(0, 100000), QLineEdit(), _spin(0, 1000000)
        self.csv_code = QLineEdit()
        self.csv_format = QComboBox()
        self.csv_format.addItem("brak", None)
        self.csv_format.addItem("jeden plik na sezon (np. E0)", "main")
        self.csv_format.addItem("jeden plik ze wszystkimi sezonami (np. POL)", "extra")
        form = QFormLayout()
        form.addRow("Kod (np. ELC)", self.code)
        form.addRow("Nazwa", self.name)
        form.addRow("Kraj", self.country)
        form.addRow("", self.cup)
        form.addRow("football-data.org – kod", self.fd)
        form.addRow("API-Football – id ligi", self.apif)
        form.addRow("The Odds API – klucz", self.odds)
        form.addRow("OddsPapi – tournamentId", self.papi)
        form.addRow("football-data.co.uk – kod pliku", self.csv_code)
        form.addRow("football-data.co.uk – format", self.csv_format)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(label("Wystarczy identyfikator w jednym źródle; brakujące zostaw puste.", "muted"))
        lay.addWidget(buttons)

    def league(self) -> League | None:
        code = self.code.text().strip().upper()
        if not code or not self.name.text().strip():
            return None
        return League(code, self.name.text().strip(), self.country.text().strip(), self.cup.isChecked(),
                      self.fd.text().strip() or None, self.apif.value() or None, self.odds.text().strip() or None,
                      self.papi.value() or None, self.csv_code.text().strip() or None, self.csv_format.currentData(),
                      True, 50)


class SettingsTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        # klucze API
        self.key_edits: dict[str, QLineEdit] = {}
        self.key_labels: dict[str, object] = {}
        keys_box = QGroupBox("Klucze API (zapisywane w Menedżerze poświadczeń Windows)")
        kl = QGridLayout(keys_box)
        for i, source in enumerate(KEYED_SOURCES):
            name, url = SOURCE_INFO[source]
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText("wklej nowy klucz")
            btn = QPushButton("Zapisz")
            btn.clicked.connect(lambda _=False, s=source: self.save_key(s))
            current = label("", "muted")
            link = label(f"<a href='{url}' style='color:{theme.ACCENT}'>jak zdobyć</a>")
            link.setOpenExternalLinks(True)
            self.key_edits[source], self.key_labels[source] = edit, current
            kl.addWidget(label(name), i, 0)
            kl.addWidget(edit, i, 1)
            kl.addWidget(btn, i, 2)
            kl.addWidget(current, i, 3)
            kl.addWidget(link, i, 4)
        if ctx.demo:
            kl.addWidget(label("Tryb demo – klucze nie są potrzebne ani zapisywane.", "warning"), len(KEYED_SOURCES), 0, 1, 5)

        # ligi
        leagues_box = QGroupBox("Ligi")
        self.league_table = make_table(["Aktywna", "Kod", "Nazwa", "Kraj", "Źródła"], stretch=4, sortable=False)
        self.league_table.setMinimumHeight(260)
        add_league = QPushButton("Dodaj ligę…")
        add_league.clicked.connect(self.add_league)
        ll = QVBoxLayout(leagues_box)
        ll.addWidget(self.league_table)
        ll.addLayout(hbox(add_league, None))

        # rynki
        markets_box = QGroupBox("Rynki")
        self.market_checks = {m: QCheckBox(MARKET_NAMES[m]) for m in MARKETS}
        ml = QVBoxLayout(markets_box)
        for cb in self.market_checks.values():
            ml.addWidget(cb)

        # model
        model_box = QGroupBox("Model prognoz")
        self.last_matches = _spin(5, 60, " meczów")
        self.half_life = _dspin(10, 2000, 0, 10, " dni")
        self.min_matches = _spin(1, 30, " meczów")
        self.xg_weight = _spin(0, 100, " %")
        self.regularization = _dspin(0, 200, 1, 1)
        self.model_weight = _spin(0, 100, " %")
        self.dixon_coles = QCheckBox("Korekta Dixona-Colesa (0:0, 1:0, 0:1, 1:1)")
        mf = QFormLayout(model_box)
        mf.addRow("Ostatnie mecze drużyny", self.last_matches)
        mf.addRow("Półokres wygaszania", self.half_life)
        mf.addRow("„Mało danych” poniżej", self.min_matches)
        mf.addRow("Udział xG w dopasowaniu", self.xg_weight)
        mf.addRow("Regularyzacja", self.regularization)
        mf.addRow("Udział modelu w prognozie", self.model_weight)
        mf.addRow("", self.dixon_coles)
        mf.addRow(label("Resztę prognozy stanowi rynek (kursy bez marży). Najlepsze wartości dobierzesz w "
                        "„Statystyki → Backtest → Strojenie”.", "muted", wrap=True))

        # kursy i podatek
        odds_box = QGroupBox("Kursy")
        self.bookmaker = QLineEdit()
        self.reference = QComboBox()
        self.reference.addItem("Wybrany bukmacher (brak → średnia)", "bookmaker")
        self.reference.addItem("Średnia rynkowa", "average")
        self.reference.addItem("Najwyższy kurs", "best")
        self.margin = QComboBox()
        self.margin.addItem("proporcjonalnie", "proportional")
        self.margin.addItem("metoda Shina", "shin")
        self.region = QComboBox()
        for r in ("eu", "uk", "us", "au"):
            self.region.addItem(r, r)
        self.cache_hours = _dspin(0.5, 48, 1, 0.5, " h")
        of = QFormLayout(odds_box)
        of.addRow("Bukmacher referencyjny", self.bookmaker)
        of.addRow("Kurs do oceny typów", self.reference)
        of.addRow("Usuwanie marży", self.margin)
        of.addRow("Region (The Odds API)", self.region)
        of.addRow("Ważność kursów w cache", self.cache_hours)

        tax_box = QGroupBox("Podatek")
        self.stake_tax = _dspin(0, 50, 1, 1, " %")
        self.pays_tax = QCheckBox("Bukmacher pokrywa podatek od stawki")
        self.win_tax = QCheckBox("Podatek od wygranej powyżej progu")
        self.win_tax_rate = _dspin(0, 50, 1, 1, " %")
        self.win_tax_threshold = _dspin(0, 1_000_000, 2, 10, " zł")
        tf = QFormLayout(tax_box)
        tf.addRow("Podatek od stawki", self.stake_tax)
        tf.addRow("", self.pays_tax)
        tf.addRow("", self.win_tax)
        tf.addRow("Stawka podatku od wygranej", self.win_tax_rate)
        tf.addRow("Próg wygranej", self.win_tax_threshold)

        sync_box = QGroupBox("Pobieranie danych")
        self.csv = QCheckBox("Import CSV z football-data.co.uk (historia i kursy)")
        self.fixtures_hours = _dspin(0.5, 48, 1, 0.5, " h")
        self.xg_budget = _spin(0, 100, " zapytań/dzień")
        self.event_budget = _spin(0, 100, " meczów/dzień")
        self.papi_budget = _spin(0, 10000, " zapytań/mies.")
        self.papi_scores = _spin(0, 10000, " zapytań/mies.")
        sf = QFormLayout(sync_box)
        sf.addRow("", self.csv)
        sf.addRow("Odświeżanie automatyczne co", self.fixtures_hours)
        sf.addRow("API-Football: xG", self.xg_budget)
        sf.addRow("The Odds API: BTTS/podwójna szansa", self.event_budget)
        sf.addRow("OddsPapi: łączny budżet", self.papi_budget)
        sf.addRow("OddsPapi: wyniki meczów", self.papi_scores)

        budget_box = QGroupBox("Budżet")
        self.monthly_limit = _dspin(0, 1_000_000, 2, 10, " zł")
        self.warn_at = _spin(10, 100, " % limitu")
        bf = QFormLayout(budget_box)
        bf.addRow("Miesięczny limit stawek", self.monthly_limit)
        bf.addRow("Ostrzegaj od", self.warn_at)
        bf.addRow(label("0 zł = bez limitu. Aplikacja pokazuje wydatki i bilans miesiąca w prawym górnym rogu, "
                        "ostrzega przy zapisie kuponu ponad limit i wyświetla czerwony komunikat po jego "
                        "przekroczeniu.", "muted", wrap=True))

        quota_box = QGroupBox("Limity API")
        self.quota_table = make_table(["Źródło", "Okres", "Zużyte", "Limit", "Zostało", "Dziś zapytań", "Stan"],
                                      stretch=6, sortable=False)
        self.quota_table.setMinimumHeight(190)
        ql = QVBoxLayout(quota_box)
        ql.addWidget(self.quota_table)

        teams_box = QGroupBox("Dopasowanie nazw drużyn do sprawdzenia")
        self.teams_table = make_table(["Liga", "Źródło", "Nazwa w źródle", "Połączona z", "Metoda", "Zgodność"],
                                      stretch=3, sortable=False)
        self.teams_table.setMinimumHeight(160)
        confirm = QPushButton("Potwierdź dopasowanie")
        merge = QPushButton("Połącz z inną drużyną…")
        confirm.clicked.connect(self.confirm_alias)
        merge.clicked.connect(self.merge_alias)
        tl = QVBoxLayout(teams_box)
        tl.addWidget(self.teams_table)
        tl.addLayout(hbox(confirm, merge, None))

        self.save_btn = QPushButton("Zapisz ustawienia")
        self.save_btn.setProperty("role", "primary")
        self.defaults_btn = QPushButton("Przywróć domyślne")
        self.status = label("", "muted")

        content = QWidget()
        grid = QGridLayout(content)
        grid.addWidget(keys_box, 0, 0, 1, 2)
        grid.addWidget(leagues_box, 1, 0, 2, 1)
        grid.addWidget(markets_box, 1, 1)
        grid.addWidget(budget_box, 2, 1)
        grid.addWidget(model_box, 3, 0)
        grid.addWidget(odds_box, 3, 1)
        grid.addWidget(tax_box, 4, 0)
        grid.addWidget(sync_box, 4, 1)
        grid.addWidget(quota_box, 5, 0, 1, 2)
        grid.addWidget(teams_box, 6, 0, 1, 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addLayout(hbox(self.status, None, self.defaults_btn, self.save_btn))

        self.save_btn.clicked.connect(self.save)
        self.defaults_btn.clicked.connect(self.restore_defaults)
        ctx.hub.data_changed.connect(self.refresh_live)
        self.load(ctx.settings())
        self.refresh_live()

    # -- wczytanie / zapis ------------------------------------------------------------------------
    def load(self, s: Settings) -> None:
        for source in KEYED_SOURCES:
            self.key_labels[source].setText(f"obecny: {mask(self.ctx.secrets.get(source))}")
        t = self.league_table
        t.setRowCount(0)
        for lg in self.ctx.sync.leagues.all():
            r = t.rowCount()
            t.insertRow(r)
            chk = text_item("")
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if lg.enabled else Qt.Unchecked)
            chk.setData(Qt.UserRole, lg.code)
            t.setItem(r, 0, chk)
            t.setItem(r, 1, text_item(lg.code, bold=True))
            t.setItem(r, 2, text_item(lg.name))
            t.setItem(r, 3, text_item(lg.country))
            sources = [n for n, v in (("football-data.org", lg.fd_org_code), ("API-Football", lg.api_football_id),
                                      ("The Odds API", lg.odds_api_key), ("OddsPapi", lg.oddspapi_id),
                                      ("CSV", lg.fdcuk_code)) if v]
            t.setItem(r, 4, text_item(", ".join(sources), theme.MUTED))
        for m, cb in self.market_checks.items():
            cb.setChecked(m in s.markets_enabled)
        m = s.model
        self.last_matches.setValue(m.last_matches)
        self.half_life.setValue(m.half_life_days)
        self.min_matches.setValue(m.min_matches)
        self.xg_weight.setValue(round(m.xg_weight * 100))
        self.regularization.setValue(m.regularization)
        self.model_weight.setValue(round(m.model_weight * 100))
        self.dixon_coles.setChecked(m.dixon_coles)
        self.bookmaker.setText(s.odds.bookmaker)
        for box, value in ((self.reference, s.odds.reference), (self.margin, s.odds.margin_method),
                           (self.region, s.odds.region)):
            box.setCurrentIndex(max(0, box.findData(value)))
        self.cache_hours.setValue(s.odds.cache_hours)
        self.stake_tax.setValue(s.tax.stake_tax * 100)
        self.pays_tax.setChecked(s.tax.bookmaker_pays_tax)
        self.win_tax.setChecked(s.tax.win_tax_enabled)
        self.win_tax_rate.setValue(s.tax.win_tax_rate * 100)
        self.win_tax_threshold.setValue(s.tax.win_tax_threshold)
        self.csv.setChecked(s.sync.csv_import)
        self.fixtures_hours.setValue(s.sync.fixtures_every_hours)
        self.xg_budget.setValue(s.sync.xg_daily_budget)
        self.event_budget.setValue(s.sync.event_markets_daily_budget)
        self.papi_budget.setValue(s.sync.oddspapi_monthly_budget)
        self.papi_scores.setValue(s.sync.oddspapi_scores_monthly)
        self.monthly_limit.setValue(s.budget.monthly_limit)
        self.warn_at.setValue(round(s.budget.warn_at * 100))

    def collect(self) -> Settings:
        s = self.ctx.settings()
        s.markets_enabled = [m for m, cb in self.market_checks.items() if cb.isChecked()]
        s.model.last_matches = self.last_matches.value()
        s.model.half_life_days = self.half_life.value()
        s.model.min_matches = self.min_matches.value()
        s.model.xg_weight = self.xg_weight.value() / 100
        s.model.regularization = self.regularization.value()
        s.model.model_weight = self.model_weight.value() / 100
        s.model.dixon_coles = self.dixon_coles.isChecked()
        s.odds.bookmaker = self.bookmaker.text().strip().lower()
        s.odds.reference = self.reference.currentData()
        s.odds.margin_method = self.margin.currentData()
        s.odds.region = self.region.currentData()
        s.odds.cache_hours = self.cache_hours.value()
        s.tax.stake_tax = self.stake_tax.value() / 100
        s.tax.bookmaker_pays_tax = self.pays_tax.isChecked()
        s.tax.win_tax_enabled = self.win_tax.isChecked()
        s.tax.win_tax_rate = self.win_tax_rate.value() / 100
        s.tax.win_tax_threshold = self.win_tax_threshold.value()
        s.sync.csv_import = self.csv.isChecked()
        s.sync.fixtures_every_hours = self.fixtures_hours.value()
        s.sync.xg_daily_budget = self.xg_budget.value()
        s.sync.event_markets_daily_budget = self.event_budget.value()
        s.sync.oddspapi_monthly_budget = self.papi_budget.value()
        s.sync.oddspapi_scores_monthly = self.papi_scores.value()
        s.budget.monthly_limit = self.monthly_limit.value()
        s.budget.warn_at = self.warn_at.value() / 100
        s.coupon.markets = [m for m in s.coupon.markets if m in s.markets_enabled] or list(s.markets_enabled)
        return s

    def save(self) -> None:
        settings = self.collect()
        if not settings.markets_enabled:
            QMessageBox.warning(self, "Ustawienia", "Włącz co najmniej jeden rynek.")
            return
        t = self.league_table
        for r in range(t.rowCount()):
            item = t.item(r, 0)
            self.ctx.sync.leagues.set_enabled(item.data(Qt.UserRole), item.checkState() == Qt.Checked)
        self.ctx.save_settings(settings)
        self.status.setText("Zapisano ustawienia.")

    def restore_defaults(self) -> None:
        if QMessageBox.question(self, "Ustawienia", "Przywrócić domyślne ustawienia (bez kluczy i lig)?") != QMessageBox.Yes:
            return
        self.load(Settings())
        self.status.setText("Wczytano wartości domyślne – kliknij „Zapisz ustawienia”, aby je zastosować.")

    def save_key(self, source: str) -> None:
        value = self.key_edits[source].text().strip()
        if not value:
            return
        try:
            self.ctx.secrets.set(source, value)
        except SecretStoreError as exc:
            QMessageBox.critical(self, "Klucz API", str(exc))
            return
        self.key_edits[source].clear()
        self.key_labels[source].setText(f"obecny: {mask(value)}")
        self.status.setText(f"Zapisano klucz {SOURCE_INFO[source][0]}. Odśwież dane, aby z niego skorzystać.")

    def add_league(self) -> None:
        dlg = LeagueDialog(self)
        if dlg.exec() == QDialog.Accepted:
            league = dlg.league()
            if league is None:
                QMessageBox.warning(self, "Dodaj ligę", "Podaj kod i nazwę ligi.")
                return
            self.ctx.sync.leagues.save(league)
            self.load(self.ctx.settings())
            self.ctx.hub.settings_changed.emit()

    # -- dane na żywo -------------------------------------------------------------------------------
    def refresh_live(self) -> None:
        period = {"minute": "minuta", "day": "dzień", "month": "miesiąc", "-": "–"}
        t = self.quota_table
        t.setRowCount(0)
        for q in self.ctx.sync.quota_rows():
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(q.label, bold=True))
            t.setItem(r, 1, text_item(period.get(q.period, q.period)))
            t.setItem(r, 2, NumItem("–" if q.used is None else str(q.used), q.used))
            t.setItem(r, 3, NumItem("–" if q.limit is None else str(q.limit), q.limit))
            t.setItem(r, 4, NumItem("–" if q.remaining is None else str(q.remaining), q.remaining))
            t.setItem(r, 5, NumItem(str(q.calls_today), q.calls_today))
            color = theme.POSITIVE if q.state == "ok" else (theme.MUTED if q.state == "idle" else theme.WARNING)
            text = STATE_LABELS.get(q.state, q.state) + (f" – {q.message}" if q.message and q.state != "ok" else "")
            t.setItem(r, 6, text_item(text, color))
        tt = self.teams_table
        tt.setRowCount(0)
        for row in self.ctx.sync.matches.matcher.review_list():
            r = tt.rowCount()
            tt.insertRow(r)
            item = text_item(row["league_code"])
            item.setData(Qt.UserRole, (row["source"], row["league_code"], row["name"], row["team_id"]))
            tt.setItem(r, 0, item)
            tt.setItem(r, 1, text_item(SOURCE_INFO.get(row["source"], (row["source"],))[0]))
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
