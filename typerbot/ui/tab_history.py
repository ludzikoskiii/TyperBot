"""Zakładka „Historia” – wygenerowane kupony z wynikiem i statystyki (bez kwot, w jednostkach)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QMessageBox, QPushButton, QSplitter, QTabWidget, QVBoxLayout,
    QWidget,
)

from typerbot.fmt import num, pct, plural, signed_pct
from typerbot.services.coupons import kickoff_local
from typerbot.services.register import LOST, PENDING, VOID, WON, StoredCoupon, export_csv
from typerbot.ui import charts, theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import KpiTile, NumItem, hbox, label, make_table, profit_color, profit_role, text_item
from typerbot.ui.workers import run_in_background

STATUS_COLORS = {WON: theme.POSITIVE, LOST: theme.NEGATIVE, VOID: theme.MUTED, PENDING: theme.WARNING}
FILTERS = [("Wszystkie", None), ("W trakcie", PENDING), ("Trafione", WON), ("Nietrafione", LOST), ("Zwroty", VOID)]
COLUMNS = ["Nr", "Wygenerowano", "Zdarzenia", "Kurs", "Po podatku", "Szansa", "Status", "Wynik", "Skopiowany"]
LEG_COLUMNS = ["Data", "Liga", "Mecz", "Typ", "Kurs", "Prognoza", "Wynik meczu", "Rozstrzygnięcie"]


def units(x: float | None) -> str:
    return "–" if x is None else f"{x:+.2f}".replace(".", ",") + " j."


def _brush(color: str) -> QBrush:
    return QBrush(QColor(color))


class HistoryListView(QWidget):
    def __init__(self, ctx: AppContext, owner: "HistoryTab"):
        super().__init__()
        self.ctx, self.owner = ctx, owner
        self.coupons: list[StoredCoupon] = []
        self.filter = QComboBox()
        for text, _ in FILTERS:
            self.filter.addItem(text)
        self.summary = label("", "muted")
        self.table = make_table(COLUMNS, stretch=2)
        self.legs = make_table(LEG_COLUMNS, stretch=2, sortable=False)
        self.delete_btn = QPushButton("Usuń z historii")
        self.export_btn = QPushButton("Eksportuj do CSV…")
        self.settle_btn = QPushButton("Rozlicz teraz")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.legs)
        split.setSizes([360, 200])
        lay = QVBoxLayout(self)
        lay.addLayout(hbox(label("Pokaż:"), self.filter, None, self.summary))
        lay.addWidget(split, 1)
        lay.addLayout(hbox(self.delete_btn, self.export_btn, None, self.settle_btn))
        lay.addWidget(label("Każdy ułożony kupon trafia tu sam (ten sam zestaw typów tylko raz) i rozlicza się "
                            "automatycznie po meczach: wynik po 90 minutach, mecz odwołany lub przełożony o ponad 48 h "
                            "liczony po kursie 1,00. Wynik w jednostkach: 1 kupon = 1 jednostka, trafiony zwraca "
                            "kurs × 0,88 (podatek od stawki).", "muted", wrap=True))
        self.filter.currentIndexChanged.connect(self.reload)
        self.table.itemSelectionChanged.connect(self._show_legs)
        self.delete_btn.clicked.connect(self.delete)
        self.export_btn.clicked.connect(self.export)
        self.settle_btn.clicked.connect(self.settle)

    def reload(self) -> None:
        status = FILTERS[self.filter.currentIndex()][1]
        tax = self.ctx.settings().tax
        factor = 1.0 if tax.bookmaker_pays_tax else 1.0 - tax.stake_tax
        self.coupons = self.ctx.register.list(status=status, copied_only=self.owner.copied_only())
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for c in self.coupons:
            r = t.rowCount()
            t.insertRow(r)
            nr = NumItem(str(c.id), c.id, Qt.AlignCenter)
            nr.setData(Qt.UserRole + 10, c.id)
            t.setItem(r, 0, nr)
            t.setItem(r, 1, text_item(kickoff_local(c.created_at)))
            names = " · ".join(f"{leg.home} – {leg.away} ({leg.label})" for leg in c.legs)
            t.setItem(r, 2, text_item(f"{len(c.legs)}: {names}", tooltip=names.replace(" · ", "\n")))
            t.setItem(r, 3, NumItem(num(c.odds), c.odds))
            t.setItem(r, 4, NumItem(num(c.odds * factor), c.odds * factor))
            t.setItem(r, 5, NumItem(pct(c.probability, 1), c.probability))
            status_text = c.status_label + (f" ({c.decided_legs}/{len(c.legs)})" if c.status == PENDING else "")
            t.setItem(r, 6, text_item(status_text, STATUS_COLORS.get(c.status), True))
            t.setItem(r, 7, NumItem(units(c.profit), c.profit))
            if profit_color(c.profit):
                t.item(r, 7).setForeground(_brush(profit_color(c.profit)))
            t.setItem(r, 8, text_item("tak" if c.copied else "", theme.MUTED))
        t.setSortingEnabled(True)
        if self.coupons:
            t.selectRow(0)
        else:
            self.legs.setRowCount(0)
        totals = self.ctx.stats.totals(self.coupons)
        self.summary.setText(f"Kuponów: {totals.coupons} · w trakcie: {totals.pending} · trafione: {totals.won} · "
                             f"nietrafione: {totals.lost} · wynik: {units(totals.profit)}")

    def selected(self) -> StoredCoupon | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        cid = self.table.item(rows[0].row(), 0).data(Qt.UserRole + 10)
        return next((c for c in self.coupons if c.id == cid), None)

    def _show_legs(self) -> None:
        c = self.selected()
        t = self.legs
        t.setRowCount(0)
        for leg in c.legs if c else []:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(kickoff_local(leg.kickoff) if leg.kickoff else ""))
            t.setItem(r, 1, text_item(leg.league))
            t.setItem(r, 2, text_item(f"{leg.home} – {leg.away}"))
            t.setItem(r, 3, text_item(leg.label))
            t.setItem(r, 4, NumItem(("≈ " if leg.estimated else "") + num(leg.odds), leg.odds))
            t.setItem(r, 5, NumItem(pct(leg.probability), leg.probability))
            t.setItem(r, 6, text_item(leg.score or "–"))
            t.setItem(r, 7, text_item(leg.result_label, STATUS_COLORS.get(leg.result), True))

    def delete(self) -> None:
        c = self.selected()
        if c and QMessageBox.question(self, "Usuń kupon", f"Usunąć kupon nr {c.id} z historii?") == QMessageBox.Yes:
            self.ctx.register.delete(c.id)
            self.ctx.hub.coupons_changed.emit()

    def export(self, path: str | None = None) -> int:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Eksport historii", "historia_kuponow.csv", "CSV (*.csv)")
            if not path:
                return 0
        rows = export_csv(self.coupons, path)
        self.ctx.hub.message.emit(f"Wyeksportowano {plural(rows, 'zdarzenie', 'zdarzenia', 'zdarzeń')} do {path}")
        return rows

    def settle(self) -> None:
        """Pobiera wyniki (źródła bez limitu + brakujące wyniki meczów z kuponów) i rozlicza – w tle."""
        self.settle_btn.setEnabled(False)
        self.ctx.hub.busy.emit("rozliczanie", True)
        ctx = self.ctx

        def work():
            ctx.sync.run_all(odds=False)
            return ctx.register.settle_pending()

        def done(settled):
            self.settle_btn.setEnabled(True)
            ctx.hub.busy.emit("rozliczanie", False)
            ctx.hub.message.emit(f"Rozliczono {plural(len(settled), 'kupon', 'kupony', 'kuponów')}.")
            ctx.hub.coupons_changed.emit()

        def failed(msg):
            self.settle_btn.setEnabled(True)
            ctx.hub.busy.emit("rozliczanie", False)
            ctx.hub.message.emit(f"Błąd rozliczania: {msg}")

        run_in_background(work, done, failed)


class HistoryStatsView(QWidget):
    def __init__(self, ctx: AppContext, owner: "HistoryTab"):
        super().__init__()
        self.ctx, self.owner = ctx, owner
        self.tiles = {k: KpiTile(k) for k in ("Kupony", "Trafność kuponów", "Wynik", "Zwrot (ROI)",
                                              "Trafność typów")}
        grid = QGridLayout()
        for i, tile in enumerate(self.tiles.values()):
            grid.addWidget(tile, 0, i)
        self.charts_row = QSplitter(Qt.Horizontal)
        group_cols = ["Typów", "Trafione", "Nietrafione", "Trafność", "Śr. prognoza", "Śr. kurs",
                      "Wynik pojedynczo", "Zwrot pojedynczo"]
        self.market_table = make_table(["Rynek", *group_cols], stretch=0)
        self.league_table = make_table(["Liga", *group_cols], stretch=0)
        self.month_table = make_table(["Miesiąc", "Kupony", "Rozliczone", "Trafione", "Trafność", "Wynik"], stretch=0)
        tables = QTabWidget()
        tables.addTab(self.market_table, "Typy według rynków")
        tables.addTab(self.league_table, "Typy według lig")
        tables.addTab(self.month_table, "Kupony według miesięcy")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.charts_row)
        split.addWidget(tables)
        split.setSizes([280, 260])
        lay = QVBoxLayout(self)
        lay.addLayout(grid)
        lay.addWidget(split, 1)
        lay.addWidget(label("Trafność typów liczymy na pojedynczych zdarzeniach – porównaj ją ze średnią prognozą "
                            "(test kalibracji). „Wynik pojedynczo” pokazuje, jak wyszłoby granie każdego typu osobno "
                            "za 1 jednostkę (po podatku). Wynik kuponów: 1 kupon = 1 jednostka.", "muted", wrap=True))

    def reload(self) -> None:
        stats = self.ctx.stats
        coupons = stats.coupons(copied_only=self.owner.copied_only())
        t = stats.totals(coupons)
        legs = stats.legs_total(coupons)
        self.tiles["Kupony"].set(str(t.coupons), f"rozliczone {t.settled} · w trakcie {t.pending}")
        expected = t.expected_hit_rate
        self.tiles["Trafność kuponów"].set(
            pct(t.hit_rate, 1) if t.won + t.lost else "–",
            f"{t.won} z {t.won + t.lost}" + (f" · oczekiwana {pct(expected, 1)}" if expected is not None else ""))
        self.tiles["Wynik"].set(units(t.profit) if t.settled else "–", f"zwrot {num(t.returned)} j. z {t.settled} j.",
                                profit_role(t.profit))
        self.tiles["Zwrot (ROI)"].set(signed_pct(t.roi) if t.settled else "–", "wynik / liczba kuponów",
                                      profit_role(t.roi))
        self.tiles["Trafność typów"].set(pct(legs.hit_rate, 1) if legs.hit_rate is not None else "–",
                                         f"{legs.won} z {legs.won + legs.lost}"
                                         + (f" · prognoza {pct(legs.avg_probability, 1)}"
                                            if legs.avg_probability is not None else ""))
        while self.charts_row.count():
            self.charts_row.widget(0).setParent(None)
        months = list(reversed(stats.by_month(coupons)))
        self.charts_row.addWidget(charts.equity_chart(stats.equity(coupons)))
        self.charts_row.addWidget(charts.profit_bars([m.month for m in months], [round(m.profit, 2) for m in months]))

        for table, rows in ((self.market_table, stats.by_market(coupons)), (self.league_table, stats.by_league(coupons))):
            table.setSortingEnabled(False)
            table.setRowCount(0)
            for g in rows:
                r = table.rowCount()
                table.insertRow(r)
                table.setItem(r, 0, text_item(g.name, bold=True))
                table.setItem(r, 1, NumItem(str(g.legs), g.legs))
                table.setItem(r, 2, NumItem(str(g.won), g.won))
                table.setItem(r, 3, NumItem(str(g.lost), g.lost))
                table.setItem(r, 4, NumItem(pct(g.hit_rate, 1) if g.hit_rate is not None else "–", g.hit_rate))
                table.setItem(r, 5, NumItem(pct(g.avg_probability, 1), g.avg_probability))
                table.setItem(r, 6, NumItem(num(g.avg_odds), g.avg_odds))
                table.setItem(r, 7, NumItem(units(g.singles_profit) if g.singles_settled else "–", g.singles_profit))
                roi = g.singles_roi
                table.setItem(r, 8, NumItem(signed_pct(roi) if roi is not None else "–", roi))
                for col, value in ((7, g.singles_profit if g.singles_settled else None), (8, roi)):
                    if profit_color(value):
                        table.item(r, col).setForeground(_brush(profit_color(value)))
            table.setSortingEnabled(True)
        mt = self.month_table
        mt.setRowCount(0)
        for m in stats.by_month(coupons):
            r = mt.rowCount()
            mt.insertRow(r)
            mt.setItem(r, 0, text_item(m.month, bold=True))
            mt.setItem(r, 1, NumItem(str(m.coupons), m.coupons))
            mt.setItem(r, 2, NumItem(str(m.settled), m.settled))
            mt.setItem(r, 3, NumItem(str(m.won), m.won))
            mt.setItem(r, 4, NumItem(pct(m.hit_rate, 1) if m.hit_rate is not None else "–", m.hit_rate))
            mt.setItem(r, 5, NumItem(units(m.profit) if m.settled else "–", m.profit))
            if m.settled and profit_color(m.profit):
                mt.item(r, 5).setForeground(_brush(profit_color(m.profit)))


class HistoryTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.copied = QCheckBox("Tylko skopiowane kupony (zagrane)")
        self.copied.setToolTip("Kupony, które skopiowałeś przyciskiem „Kopiuj kupon” – zwykle te, które zagrałeś")
        self.list = HistoryListView(ctx, self)
        self.stats = HistoryStatsView(ctx, self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.list, "Kupony")
        self.tabs.addTab(self.stats, "Statystyki")
        lay = QVBoxLayout(self)
        lay.addLayout(hbox(self.copied, None))
        lay.addWidget(self.tabs, 1)
        self.copied.toggled.connect(self.reload)
        ctx.hub.coupons_changed.connect(self.reload)
        ctx.hub.data_changed.connect(self.reload)
        self.reload()

    def copied_only(self) -> bool:
        return self.copied.isChecked()

    def reload(self) -> None:
        self.list.reload()
        self.stats.reload()
