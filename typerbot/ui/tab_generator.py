"""Zakładka „Generator kuponu” – ustawienia, 3 alternatywne kupony, wymiana zdarzeń, zapis."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from typerbot.config.settings import MARKETS, CouponSettings
from typerbot.fmt import num, pct, plural, signed_pct
from typerbot.services.coupons import Coupon, CouponService, GenerationResult, SwapOption, kickoff_local
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.diagnostics_view import DiagnosisDialog, reason_html
from typerbot.ui.widgets import (
    KpiTile, NumItem, ProbabilityDelegate, fill_row_background, hbox, label, make_table, prob_item, profit_role,
    set_role, text_item,
)
from typerbot.ui.workers import run_in_background

MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej 2,5", "BTTS": "Obie strzelą"}
RANGE_MODES = [("Dziś", "today"), ("Jutro", "tomorrow"), ("Najbliższe X dni", "days"), ("Własny zakres", "custom")]
LEG_COLUMNS = ["Data", "Liga", "Mecz", "Typ", "Kurs", "Źródło", "Prognoza", "Model", "Rynek", "EV"]


# -- okna dialogowe ------------------------------------------------------------------------------
class SwapDialog(QDialog):
    def __init__(self, options: list[SwapOption], title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wymień zdarzenie")
        self.resize(820, 440)
        self.options = options
        self.table = make_table(["Data", "Mecz", "Typ", "Kurs", "Prognoza", "Kurs kuponu", "Zakres"], stretch=1,
                                sortable=False)
        self.table.setItemDelegateForColumn(4, ProbabilityDelegate(self.table))
        for o in options:
            r = self.table.rowCount()
            self.table.insertRow(r)
            s, m = o.leg.selection, o.leg.match
            self.table.setItem(r, 0, text_item(kickoff_local(m.kickoff)))
            self.table.setItem(r, 1, text_item(f"{m.home} – {m.away}"))
            self.table.setItem(r, 2, text_item(s.label + (" ★" if s.is_value else ""), theme.POSITIVE if s.is_value else None))
            self.table.setItem(r, 3, NumItem(num(s.odds), s.odds))
            self.table.setItem(r, 4, prob_item(s.probability, s.is_value))
            self.table.setItem(r, 5, NumItem(num(o.new_odds), o.new_odds))
            self.table.setItem(r, 6, text_item("tak" if o.in_range else "poza zakresem",
                                               None if o.in_range else theme.WARNING))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Wymień")
        buttons.button(QDialogButtonBox.Cancel).setText("Anuluj")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.table.doubleClicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(label(title, "title"))
        lay.addWidget(label("Zamienniki z tego samego meczu i z meczów spoza kuponu, najlepsze na górze.", "muted"))
        lay.addWidget(self.table)
        lay.addWidget(buttons)
        if options:
            self.table.selectRow(0)

    def chosen(self) -> SwapOption | None:
        rows = self.table.selectionModel().selectedRows()
        return self.options[rows[0].row()] if rows else None


# -- karta kuponu -----------------------------------------------------------------------------------
class CouponCard(QWidget):
    changed = Signal()

    def __init__(self, ctx: AppContext, service: CouponService, cfg: CouponSettings, coupon: Coupon, letter: str):
        super().__init__()
        self.ctx, self.service, self.cfg, self.coupon, self.letter = ctx, service, cfg, coupon, letter
        self.tiles = {name: KpiTile(name) for name in ("Kurs łączny", "Po podatku", "Szansa trafienia",
                                                         "EV kuponu (po podatku)")}
        self.warning = label("", "warning")
        self.table = make_table(LEG_COLUMNS, stretch=2, sortable=False)
        self.table.setItemDelegateForColumn(6, ProbabilityDelegate(self.table))
        self.rationale = label("", wrap=True)
        self.rationale.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rationale)
        scroll.setMinimumHeight(120)
        self.swap_btn = QPushButton("Wymień zdarzenie…")
        self.odds_btn = QPushButton("Zmień kurs…")
        self.remove_btn = QPushButton("Usuń zdarzenie")
        self.history_label = label("", "muted")
        tiles = QGridLayout()
        for i, tile in enumerate(self.tiles.values()):
            tiles.addWidget(tile, 0, i)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(scroll)
        split.setSizes([260, 170])
        lay = QVBoxLayout(self)
        lay.addLayout(tiles)
        lay.addWidget(self.warning)
        lay.addWidget(split, 1)
        lay.addLayout(hbox(self.swap_btn, self.odds_btn, self.remove_btn, None, self.history_label))
        self.swap_btn.clicked.connect(self.swap)
        self.odds_btn.clicked.connect(self.change_odds)
        self.remove_btn.clicked.connect(self.remove)
        self.table.itemSelectionChanged.connect(self._show_rationale)
        self.table.doubleClicked.connect(lambda idx: self.change_odds() if idx.column() == 4 else self.swap())
        self.refresh()

    def selected_leg(self):
        rows = self.table.selectionModel().selectedRows()
        return self.coupon.legs[rows[0].row()] if rows and rows[0].row() < len(self.coupon.legs) else None

    def refresh(self) -> None:
        c = self.coupon
        self.tiles["Kurs łączny"].set(num(c.odds), f"zakres {num(c.target[0])}–{num(c.target[1])}",
                                      None if c.in_range else "warning")
        self.tiles["Po podatku"].set(num(c.odds_after_tax), "kurs × 0,88 (podatek od stawki)")
        market = c.probability_market
        self.tiles["Szansa trafienia"].set(pct(c.probability, 1), f"model {pct(c.probability_model, 1)}"
                                           + (f" · rynek {pct(market, 1)}" if market is not None else ""))
        self.tiles["EV kuponu (po podatku)"].set(signed_pct(c.ev), f"przed podatkiem {signed_pct(c.ev_before_tax)}",
                                                 profit_role(c.ev))
        self.warning.setText("" if c.in_range else "Kurs łączny poza zadanym zakresem – wymień zdarzenie lub zmień kurs.")
        t = self.table
        selected = self.selected_leg()
        t.setRowCount(0)
        for leg in c.legs:
            s, m = leg.selection, leg.match
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(kickoff_local(m.kickoff)))
            t.setItem(r, 1, text_item(m.league))
            t.setItem(r, 2, text_item(f"{m.home} – {m.away}", theme.WARNING if m.flags else None, tooltip="; ".join(m.flags)))
            t.setItem(r, 3, text_item(s.label + (" ★" if s.is_value else ""), theme.POSITIVE if s.is_value else None, True))
            t.setItem(r, 4, NumItem(num(s.odds) if s.odds else "–", s.odds))
            t.setItem(r, 5, text_item(s.source_label, theme.WARNING if s.odds_source in ("estimated", "manual") else None))
            t.setItem(r, 6, prob_item(s.probability, s.is_value))
            t.setItem(r, 7, NumItem(pct(s.p_model), s.p_model))
            t.setItem(r, 8, NumItem(pct(s.p_market), s.p_market))
            t.setItem(r, 9, text_item(signed_pct(s.ev), theme.POSITIVE if s.is_value else None))
            if s.is_value:
                fill_row_background(t, r, theme.VALUE_BG)
        self.history_label.setText(f"W historii jako kupon nr {c.history_id}" if c.history_id else "")
        idx = next((i for i, leg in enumerate(c.legs) if selected and leg.match.match_id == selected.match.match_id), 0)
        if c.legs:
            t.selectRow(idx)
        self._show_rationale()

    def _show_rationale(self) -> None:
        leg = self.selected_leg()
        if leg is None:
            self.rationale.setText("")
            return
        head = f"<b>{leg.match.home} – {leg.match.away}: {leg.selection.label}</b>"
        body = "<br>".join(leg.rationale) if leg.rationale else "Brak uzasadnienia."
        self.rationale.setText(f"{head}<br>{body}")

    def swap(self) -> None:
        leg = self.selected_leg()
        if leg is None:
            return
        options = self.service.swap_options(self.coupon, leg.match.match_id, self.cfg, limit=25)
        if not options:
            QMessageBox.information(self, "Wymień zdarzenie", "Brak zamienników spełniających warunki.")
            return
        dlg = SwapDialog(options, f"{leg.match.home} – {leg.match.away}: {leg.selection.label}", self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen():
            self.coupon = self.service.swap(self.coupon, leg.match.match_id, dlg.chosen())
            self._updated()

    def change_odds(self) -> None:
        leg = self.selected_leg()
        if leg is None:
            return
        value, ok = QInputDialog.getDouble(self, "Zmień kurs", f"Kurs z oferty bukmachera – {leg.selection.label}:",
                                           leg.selection.odds or 1.5, 1.01, 1000.0, 2)
        if ok:
            self.coupon = self.service.set_manual_odds(self.coupon, leg.match.match_id, value)
            self._updated()

    def remove(self) -> None:
        leg = self.selected_leg()
        if leg is None or len(self.coupon.legs) <= 1:
            return
        self.coupon = self.service.remove(self.coupon, leg.match.match_id)
        self._updated()

    def _updated(self) -> None:
        """Po ręcznej zmianie: przeliczenie, aktualizacja wpisu w historii i odświeżenie widoku."""
        self.service.update_record(self.coupon)
        self.refresh()
        self.changed.emit()
        self.ctx.hub.coupons_changed.emit()


# -- zakładka -----------------------------------------------------------------------------------------
class GeneratorTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.service = CouponService(ctx.db, now=ctx.now)
        self.busy = False

        self.target = QDoubleSpinBox()
        self.target.setRange(1.2, 10000)
        self.target.setDecimals(2)
        self.target.setSingleStep(0.5)
        self.tolerance = QSpinBox()
        self.tolerance.setRange(1, 50)
        self.tolerance.setSuffix(" %")
        self.range_mode = QComboBox()
        for text, _ in RANGE_MODES:
            self.range_mode.addItem(text)
        self.days = QSpinBox()
        self.days.setRange(1, 14)
        self.days.setSuffix(" dni")
        self.date_from = QDateEdit(QDate.currentDate())
        self.date_to = QDateEdit(QDate.currentDate().addDays(3))
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd.MM.yyyy")
        self.min_events = QSpinBox()
        self.min_events.setRange(1, 20)
        self.max_events = QSpinBox()
        self.max_events.setRange(1, 20)
        self.min_prob = QSpinBox()
        self.min_prob.setRange(1, 99)
        self.min_prob.setSuffix(" %")
        self.mode = QComboBox()
        self.mode.addItem("Najwyższa szansa trafienia", "probability")
        self.mode.addItem("Najwyższa wartość (EV)", "value")
        self.leagues = QListWidget()
        self.leagues.setMaximumHeight(150)
        self.markets = {m: QCheckBox(MARKET_NAMES[m]) for m in MARKETS}
        self.low_data = QCheckBox("Dopuść drużyny z małą liczbą danych")
        self.generate_btn = QPushButton("Generuj kupony")
        self.generate_btn.setProperty("role", "primary")
        self.save_defaults_btn = QPushButton("Zapisz jako domyślne")
        self.diag_btn = QPushButton("Diagnostyka")
        self.diag_btn.setToolTip("Ile meczów i kursów przyszło z każdego źródła i ile zostaje po każdym filtrze")
        self.diag_btn.setEnabled(False)
        self.diagnosis = None
        self.status = label("", "muted", wrap=True)

        form = QFormLayout()
        form.addRow("Kurs docelowy", self.target)
        form.addRow("Tolerancja ±", self.tolerance)
        form.addRow("Zakres dat", self.range_mode)
        form.addRow("Liczba dni", self.days)
        form.addRow("Od", self.date_from)
        form.addRow("Do", self.date_to)
        form.addRow("Min. zdarzeń", self.min_events)
        form.addRow("Maks. zdarzeń", self.max_events)
        form.addRow("Min. prawdop. typu", self.min_prob)
        form.addRow("Tryb", self.mode)
        box = QGroupBox("Ustawienia kuponu")
        blay = QVBoxLayout(box)
        blay.addLayout(form)
        blay.addWidget(label("Ligi", "muted"))
        blay.addWidget(self.leagues)
        blay.addWidget(label("Rynki", "muted"))
        for cb in self.markets.values():
            blay.addWidget(cb)
        blay.addWidget(self.low_data)
        blay.addWidget(self.generate_btn)
        blay.addLayout(hbox(self.save_defaults_btn, self.diag_btn))
        blay.addWidget(self.status)
        blay.addStretch(1)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setWidget(box)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.setMinimumWidth(360)
        left.setMaximumWidth(420)

        self.tabs = QTabWidget()
        self.placeholder = label("Ustaw parametry i kliknij „Generuj kupony”. Aplikacja ułoży do 3 alternatywnych "
                                 "kuponów – każdy z inną połową meczów.", "muted", wrap=True)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.tabs.addTab(self.placeholder, "Kupony")
        disclaimer = label("Szansa trafienia zakłada niezależność meczów. Model bywa zbyt pewny siebie – dlatego "
                           "prognoza łączy go z rynkiem. EV kuponu uwzględnia podatek.", "muted", wrap=True)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(self.tabs, 1)
        rlay.addWidget(disclaimer)
        root = QSplitter(Qt.Horizontal)
        root.addWidget(left)
        root.addWidget(right)
        root.setSizes([360, 900])
        lay = QVBoxLayout(self)
        lay.addWidget(root)

        self.range_mode.currentIndexChanged.connect(self._range_changed)
        self.generate_btn.clicked.connect(self.generate)
        self.save_defaults_btn.clicked.connect(self.save_defaults)
        self.diag_btn.clicked.connect(self.show_diagnosis)
        ctx.hub.settings_changed.connect(self.load_settings)
        self.load_settings()

    # -- ustawienia -------------------------------------------------------------------------------------
    def load_settings(self) -> None:
        settings = self.ctx.settings()
        c = settings.coupon
        self.target.setValue(c.target_odds)
        self.tolerance.setValue(round(c.tolerance * 100))
        modes = [m for _, m in RANGE_MODES]
        self.range_mode.setCurrentIndex(modes.index(c.date_range) if c.date_range in modes else 2)
        self.days.setValue(c.days_ahead)
        if c.date_from:
            self.date_from.setDate(QDate.fromString(c.date_from, "yyyy-MM-dd"))
        if c.date_to:
            self.date_to.setDate(QDate.fromString(c.date_to, "yyyy-MM-dd"))
        self.min_events.setValue(c.min_events)
        self.max_events.setValue(c.max_events)
        self.min_prob.setValue(round(c.min_probability * 100))
        self.mode.setCurrentIndex(0 if c.mode == "probability" else 1)
        self.low_data.setChecked(c.include_low_data)
        self.leagues.clear()
        for lg in self.ctx.sync.leagues.all(enabled_only=True):
            item = QListWidgetItem(lg.name)
            item.setData(Qt.UserRole, lg.code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if not c.leagues or lg.code in c.leagues else Qt.Unchecked)
            self.leagues.addItem(item)
        for m, cb in self.markets.items():
            cb.setChecked(m in c.markets)
            cb.setEnabled(m in settings.markets_enabled)
        self._range_changed()

    def _range_changed(self) -> None:
        mode = RANGE_MODES[self.range_mode.currentIndex()][1]
        self.days.setEnabled(mode == "days")
        self.date_from.setEnabled(mode == "custom")
        self.date_to.setEnabled(mode == "custom")

    def current_cfg(self) -> CouponSettings:
        base = self.ctx.settings().coupon
        leagues = [self.leagues.item(i).data(Qt.UserRole) for i in range(self.leagues.count())
                   if self.leagues.item(i).checkState() == Qt.Checked]
        all_leagues = len(leagues) == self.leagues.count()
        return replace(
            base,
            target_odds=self.target.value(), tolerance=self.tolerance.value() / 100,
            date_range=RANGE_MODES[self.range_mode.currentIndex()][1], days_ahead=self.days.value(),
            date_from=self.date_from.date().toString("yyyy-MM-dd"), date_to=self.date_to.date().toString("yyyy-MM-dd"),
            min_events=min(self.min_events.value(), self.max_events.value()),
            max_events=max(self.min_events.value(), self.max_events.value()),
            min_probability=self.min_prob.value() / 100, mode=self.mode.currentData(),
            leagues=[] if all_leagues else leagues, markets=[m for m, cb in self.markets.items() if cb.isChecked()],
            include_low_data=self.low_data.isChecked(),
        )

    def save_defaults(self) -> None:
        settings = self.ctx.settings()
        settings.coupon = self.current_cfg()
        self.ctx.save_settings(settings)
        self.status.setText("Zapisano ustawienia kuponu jako domyślne.")

    # -- generowanie ----------------------------------------------------------------------------------
    def generate(self) -> None:
        if self.busy:
            return
        cfg = self.current_cfg()
        if not cfg.markets:
            self.status.setText("Zaznacz co najmniej jeden rynek.")
            return
        self.busy = True
        self.generate_btn.setEnabled(False)
        self.status.setText("Liczę prognozy i szukam najlepszych kombinacji…")
        self.ctx.hub.busy.emit("generator", True)
        service = self.service = CouponService(self.ctx.db, now=self.ctx.now)
        secrets = self.ctx.secrets

        def work():
            result = service.run(cfg, secrets=secrets)
            service.record(result.coupons, cfg.target_odds)       # kupony trafiają do historii
            return result

        run_in_background(work, lambda res: self._generated(cfg, res), self._failed)

    def _failed(self, message: str) -> None:
        self.busy = False
        self.generate_btn.setEnabled(True)
        self.ctx.hub.busy.emit("generator", False)
        self.status.setText(f"Błąd: {message}")

    def _generated(self, cfg: CouponSettings, result: GenerationResult) -> None:
        self.busy = False
        self.generate_btn.setEnabled(True)
        self.ctx.hub.busy.emit("generator", False)
        self.tabs.clear()
        self.diagnosis = diag = result.diagnosis
        self.diag_btn.setEnabled(True)
        coupons = result.coupons
        if not coupons:
            set_role(self.placeholder, None)
            self.placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            self.placeholder.setText(reason_html(diag) + "<p style='color:#8b93a7'>Szczegóły: przycisk "
                                     "„Diagnostyka” – ile meczów i kursów przyszło z każdego źródła i ile zostaje "
                                     "po każdym filtrze.</p>")
            self.tabs.addTab(self.placeholder, "Kupony")
            self.status.setText(diag.headline())
            return
        self.ctx.hub.coupons_changed.emit()
        for letter, coupon in zip("ABC", coupons):
            card = CouponCard(self.ctx, self.service, cfg, coupon, letter)
            idx = self.tabs.addTab(card, f"Kupon {letter} · {num(coupon.odds)}")
            card.changed.connect(lambda i=idx, c=card: self.tabs.setTabText(i, f"Kupon {c.letter} · {num(c.coupon.odds)}"))
        n_matches = diag.stages[2].matches if len(diag.stages) > 2 else 0
        msg = (f"Ułożono {plural(len(coupons), 'kupon', 'kupony', 'kuponów')} z "
               f"{plural(n_matches, 'meczu', 'meczów', 'meczów')} – zapisane w Historii.")
        if diag.notes:
            msg += " " + " ".join(diag.notes)
        self.status.setText(msg)

    def show_diagnosis(self) -> None:
        if self.diagnosis is not None:
            DiagnosisDialog(self.diagnosis, self).exec()

    def cards(self) -> list[CouponCard]:
        return [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i), CouponCard)]

