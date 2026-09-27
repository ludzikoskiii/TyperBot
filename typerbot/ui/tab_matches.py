"""Zakładka „Mecze” – nadchodzące mecze z prognozami i oceną typów."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHeaderView, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from typerbot.betting.rationale import match_summary
from typerbot.fmt import num, pct, signed_pct
from typerbot.services.coupons import CouponService, kickoff_local
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import (
    NumItem, ProbabilityDelegate, fill_row_background, hbox, label, make_table, prob_item, text_item,
)
from typerbot.ui.workers import run_in_background

RANGES = [("Dziś", "today", 1), ("Jutro", "tomorrow", 1), ("Najbliższe 3 dni", "days", 3),
          ("Najbliższe 7 dni", "days", 7)]
MARKET_FILTER = [("Wszystkie rynki", None), ("1X2", "1X2"), ("Podwójna szansa", "DC"),
                 ("Powyżej/poniżej", "OU"), ("Obie strzelą", "BTTS")]
MATCH_COLUMNS = ["Data", "Liga", "Mecz", "Oczek. gole", "1", "X", "2", ">2,5", "BTTS", "Value", "Uwagi"]
SEL_COLUMNS = ["Typ", "Prognoza", "Model", "Rynek", "Kurs", "Źródło", "Implik.", "EV", "EV po podatku"]


class MatchesTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.service = CouponService(ctx.db, now=ctx.now)
        self.evaluated: dict = {}
        self.loading = False

        self.range_box = QComboBox()
        for text, *_ in RANGES:
            self.range_box.addItem(text)
        self.range_box.setCurrentIndex(2)
        self.league_box = QComboBox()
        self.market_box = QComboBox()
        for text, _ in MARKET_FILTER:
            self.market_box.addItem(text)
        self.value_only = QCheckBox("Tylko mecze z typami value")
        self.refresh_btn = QPushButton("Przelicz prognozy")
        self.info = label("", "muted")
        self._fill_leagues()

        self.table = make_table(MATCH_COLUMNS, stretch=2)
        delegate = ProbabilityDelegate(self.table)
        for col in range(4, 9):
            self.table.setItemDelegateForColumn(col, delegate)
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, 78)
        self.sel_table = make_table(SEL_COLUMNS, stretch=0, sortable=False)
        self.sel_table.setItemDelegateForColumn(1, ProbabilityDelegate(self.sel_table))
        self.details = label("Wybierz mecz, aby zobaczyć wszystkie typy i uzasadnienie.", "muted", wrap=True)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        box = QWidget()
        lay = QVBoxLayout(box)
        self.detail_title = label("", "title")
        lay.addWidget(self.detail_title)
        lay.addWidget(self.details)
        lay.addStretch(1)
        scroll.setWidget(box)

        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(self.sel_table)
        bottom.addWidget(scroll)
        bottom.setSizes([640, 460])
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(bottom)
        split.setSizes([420, 300])

        root = QVBoxLayout(self)
        root.addLayout(hbox(label("Zakres:"), self.range_box, label("Liga:"), self.league_box, self.market_box,
                            self.value_only, None, self.info, self.refresh_btn))
        root.addWidget(split, 1)

        self.refresh_btn.clicked.connect(self.reload)
        for w in (self.range_box, self.league_box):
            w.currentIndexChanged.connect(self.reload)
        self.market_box.currentIndexChanged.connect(self._show_selected)
        self.value_only.toggled.connect(self._populate)
        self.table.itemSelectionChanged.connect(self._show_selected)
        ctx.hub.data_changed.connect(self.reload)
        ctx.hub.settings_changed.connect(self._on_settings)

    def _fill_leagues(self) -> None:
        current = self.league_box.currentData()
        self.league_box.blockSignals(True)
        self.league_box.clear()
        self.league_box.addItem("Wszystkie ligi", None)
        for lg in self.ctx.sync.leagues.all(enabled_only=True):
            self.league_box.addItem(lg.name, lg.code)
        idx = self.league_box.findData(current)
        self.league_box.setCurrentIndex(max(0, idx))
        self.league_box.blockSignals(False)

    def _on_settings(self) -> None:
        self._fill_leagues()
        self.reload()

    def _cfg(self):
        _, mode, days = RANGES[self.range_box.currentIndex()]
        base = self.ctx.settings().coupon
        league = self.league_box.currentData()
        return replace(base, date_range=mode, days_ahead=days, leagues=[league] if league else [],
                       markets=list(base.markets))

    def reload(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.info.setText("liczę prognozy…")
        self.service = CouponService(self.ctx.db, now=self.ctx.now)
        cfg = self._cfg()
        service = self.service
        self.ctx.hub.busy.emit("prognozy", True)
        run_in_background(lambda: service.evaluate(cfg), self._loaded, self._failed)

    def _loaded(self, evaluated: dict) -> None:
        self.loading = False
        self.ctx.hub.busy.emit("prognozy", False)
        self.evaluated = evaluated
        w = self.ctx.settings().model.model_weight
        self.info.setText(f"{len(evaluated)} meczów · prognoza = {w:.0%} model + {1 - w:.0%} rynek")
        self._populate()

    def _failed(self, message: str) -> None:
        self.loading = False
        self.ctx.hub.busy.emit("prognozy", False)
        self.info.setText("błąd obliczeń")
        self.ctx.hub.message.emit(f"Nie udało się policzyć prognoz: {message}")

    def _populate(self) -> None:
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        rows = sorted(self.evaluated.values(), key=lambda x: x[0].kickoff)
        for info, evals in rows:
            n_value = sum(e.is_value for e in evals)
            if self.value_only.isChecked() and not n_value:
                continue
            by_key = {e.key: e for e in evals}
            r = t.rowCount()
            t.insertRow(r)
            when = NumItem(kickoff_local(info.kickoff), None, Qt.AlignLeft | Qt.AlignVCenter)
            when.setData(Qt.UserRole, info.kickoff)
            when.setData(Qt.UserRole + 10, info.match_id)
            t.setItem(r, 0, when)
            t.setItem(r, 1, text_item(info.league))
            t.setItem(r, 2, text_item(f"{info.home} – {info.away}", bold=True))
            t.setItem(r, 3, NumItem(f"{num(info.lam_home, 1)} : {num(info.lam_away, 1)}", info.lam_home + info.lam_away))
            for c, key in enumerate([("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0), ("OU", "O", 2.5),
                                     ("BTTS", "Y", 0.0)], start=4):
                e = by_key.get(key)
                t.setItem(r, c, prob_item(e.probability if e else None, bool(e and e.is_value)))
            t.setItem(r, 9, NumItem(f"★ {n_value}" if n_value else "", n_value, Qt.AlignCenter))
            if n_value:
                t.item(r, 9).setForeground(QBrush(QColor(theme.POSITIVE)))
            t.setItem(r, 10, text_item("; ".join(info.flags), theme.WARNING if info.flags else None))
        t.setSortingEnabled(True)
        if t.rowCount():
            t.selectRow(0)
        else:
            self._clear_details()

    def _clear_details(self) -> None:
        self.sel_table.setRowCount(0)
        self.detail_title.setText("")
        self.details.setText("Brak meczów w wybranym zakresie. Odśwież dane lub zmień filtry.")

    def selected_match_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole + 10)

    def _show_selected(self) -> None:
        mid = self.selected_match_id()
        if mid is None or mid not in self.evaluated:
            return
        info, evals = self.evaluated[mid]
        market = MARKET_FILTER[self.market_box.currentIndex()][1]
        settings = self.ctx.settings()
        t = self.sel_table
        t.setRowCount(0)
        for e in evals:
            if market and e.key[0] != market:
                continue
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(e.label + (" ★" if e.is_value else ""), theme.POSITIVE if e.is_value else None,
                                      bold=e.is_value))
            t.setItem(r, 1, prob_item(e.probability, e.is_value))
            t.setItem(r, 2, NumItem(pct(e.p_model), e.p_model))
            t.setItem(r, 3, NumItem(pct(e.p_market), e.p_market))
            t.setItem(r, 4, NumItem(num(e.odds) if e.odds else "–", e.odds))
            t.setItem(r, 5, text_item(e.source_label, theme.WARNING if e.odds_source == "estimated" else None))
            t.setItem(r, 6, NumItem(pct(e.implied), e.implied))
            ev_after = e.ev_after_tax(settings)
            t.setItem(r, 7, text_item(signed_pct(e.ev), theme.POSITIVE if e.is_value else None))
            t.setItem(r, 8, text_item(signed_pct(ev_after), theme.POSITIVE if ev_after and ev_after > 0 else None))
            if e.is_value:
                fill_row_background(t, r, theme.VALUE_BG)
        self.detail_title.setText(f"{info.home} – {info.away}")
        lines = match_summary(self.ctx.db, info.as_dict(), info.lam_home, info.lam_away)
        if info.flags:
            lines.append("Uwagi: " + "; ".join(info.flags))
        lines.append("★ value = prognoza × kurs > 1 (przed podatkiem). EV po podatku dotyczy gry pojedynczej.")
        self.details.setText("\n\n".join(lines))
