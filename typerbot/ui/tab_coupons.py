"""Zakładka „Moje kupony” – rejestr postawionych kuponów i ich rozliczenie."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from typerbot.fmt import num, pct, plural, signed_pct
from typerbot.services.coupons import kickoff_local
from typerbot.services.register import LOST, PENDING, STATUS_LABELS, VOID, WON, StoredCoupon
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import NumItem, hbox, label, make_table, profit_color, text_item
from typerbot.ui.workers import run_in_background

STATUS_COLORS = {WON: theme.POSITIVE, LOST: theme.NEGATIVE, VOID: theme.MUTED, PENDING: theme.WARNING}
FILTERS = [("Wszystkie", None), ("W grze", PENDING), ("Wygrane", WON), ("Przegrane", LOST), ("Zwroty", VOID)]
COLUMNS = ["Nr", "Postawiono", "Bukmacher", "Zdarzenia", "Kurs", "Stawka", "Szansa", "Status", "Wypłata", "Zysk"]


class CouponDetailsDialog(QDialog):
    def __init__(self, coupon: StoredCoupon, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Kupon nr {coupon.id}")
        self.resize(760, 380)
        t = make_table(["Data", "Liga", "Mecz", "Typ", "Kurs", "Prognoza", "Wynik meczu", "Rozstrzygnięcie"],
                       stretch=2, sortable=False)
        for leg in coupon.legs:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, text_item(kickoff_local(leg.kickoff) if leg.kickoff else ""))
            t.setItem(r, 1, text_item(leg.league))
            t.setItem(r, 2, text_item(f"{leg.home} – {leg.away}"))
            t.setItem(r, 3, text_item(leg.label))
            t.setItem(r, 4, NumItem(num(leg.odds), leg.odds))
            t.setItem(r, 5, NumItem(pct(leg.probability), leg.probability))
            t.setItem(r, 6, text_item(leg.score or "–"))
            t.setItem(r, 7, text_item(STATUS_LABELS.get(leg.result, leg.result), STATUS_COLORS.get(leg.result), True))
        info = (f"Postawiono {coupon.placed_at[:16].replace('T', ' ')} · {coupon.bookmaker or 'bukmacher'} · "
                f"stawka {num(coupon.stake)} zł · kurs {num(coupon.odds)}"
                + (f" · szansa wg prognozy {pct(coupon.probability, 1)}" if coupon.probability else "")
                + (f" · EV {signed_pct(coupon.ev)}" if coupon.ev is not None else ""))
        result = (f"Status: {coupon.status_label}" + (f" · wypłata {num(coupon.payout)} zł" if coupon.payout is not None
                                                        else "") + (" (rozliczony ręcznie)" if coupon.manual else ""))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Zamknij")
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(label(info, "muted", wrap=True))
        lay.addWidget(t)
        lay.addWidget(label(result, "title"))
        if coupon.note:
            lay.addWidget(label(f"Notatka: {coupon.note}", "muted"))
        lay.addWidget(buttons)


class ManualResultDialog(QDialog):
    def __init__(self, coupon: StoredCoupon, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Rozlicz ręcznie – kupon nr {coupon.id}")
        self.status = QComboBox()
        for key in (WON, LOST, VOID, PENDING):
            self.status.addItem(STATUS_LABELS[key], key)
        self.payout = QDoubleSpinBox()
        self.payout.setRange(0, 10_000_000)
        self.payout.setDecimals(2)
        self.payout.setSuffix(" zł")
        self.payout.setValue(coupon.payout or 0.0)
        self.auto = QComboBox()
        self.auto.addItem("Wylicz wypłatę automatycznie", True)
        self.auto.addItem("Wpisz wypłatę (np. wcześniejsza wypłata)", False)
        self.note = QLineEdit(coupon.note)
        form = QFormLayout()
        form.addRow("Status", self.status)
        form.addRow("Wypłata", self.auto)
        form.addRow("Kwota", self.payout)
        form.addRow("Notatka", self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Zapisz")
        buttons.button(QDialogButtonBox.Cancel).setText("Anuluj")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.auto.currentIndexChanged.connect(lambda: self.payout.setEnabled(not self.auto.currentData()))
        self.payout.setEnabled(False)

    def values(self) -> tuple[str, float | None, str]:
        return self.status.currentData(), None if self.auto.currentData() else self.payout.value(), self.note.text()


class CouponsTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.coupons: list[StoredCoupon] = []
        self.filter = QComboBox()
        for text, _ in FILTERS:
            self.filter.addItem(text)
        self.summary = label("", "muted")
        self.settle_btn = QPushButton("Rozlicz teraz")
        self.details_btn = QPushButton("Szczegóły")
        self.manual_btn = QPushButton("Rozlicz ręcznie…")
        self.delete_btn = QPushButton("Usuń")
        self.table = make_table(COLUMNS, stretch=3)
        lay = QVBoxLayout(self)
        lay.addLayout(hbox(label("Pokaż:"), self.filter, None, self.summary))
        lay.addWidget(self.table, 1)
        lay.addLayout(hbox(self.details_btn, self.manual_btn, self.delete_btn, None, self.settle_btn))
        lay.addWidget(label("Kupony rozliczają się same po zakończeniu meczów (wynik po 90 minutach; mecz odwołany "
                            "lub przełożony o ponad 48 h liczony po kursie 1,00).", "muted", wrap=True))
        self.filter.currentIndexChanged.connect(self.reload)
        self.settle_btn.clicked.connect(self.settle)
        self.details_btn.clicked.connect(self.details)
        self.manual_btn.clicked.connect(self.manual)
        self.delete_btn.clicked.connect(self.delete)
        self.table.doubleClicked.connect(lambda _: self.details())
        ctx.hub.coupons_changed.connect(self.reload)
        ctx.hub.data_changed.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        status = FILTERS[self.filter.currentIndex()][1]
        self.coupons = self.ctx.register.list(status=status)
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for c in self.coupons:
            r = t.rowCount()
            t.insertRow(r)
            nr = NumItem(str(c.id), c.id, Qt.AlignCenter)
            nr.setData(Qt.UserRole + 10, c.id)
            t.setItem(r, 0, nr)
            t.setItem(r, 1, text_item(c.placed_at[:16].replace("T", " ")))
            t.setItem(r, 2, text_item(c.bookmaker))
            names = " · ".join(f"{leg.home} – {leg.away} ({leg.label})" for leg in c.legs)
            t.setItem(r, 3, text_item(f"{len(c.legs)}: {names}", tooltip=names.replace(" · ", "\n")))
            t.setItem(r, 4, NumItem(num(c.odds), c.odds))
            t.setItem(r, 5, NumItem(f"{num(c.stake)} zł", c.stake))
            t.setItem(r, 6, NumItem(pct(c.probability, 1), c.probability))
            done = sum(leg.result != PENDING for leg in c.legs)
            status_text = c.status_label + (f" ({done}/{len(c.legs)})" if c.status == PENDING else "")
            t.setItem(r, 7, text_item(status_text, STATUS_COLORS.get(c.status), True))
            t.setItem(r, 8, NumItem(f"{num(c.payout)} zł" if c.payout is not None else "–", c.payout))
            profit = c.profit
            t.setItem(r, 9, NumItem(f"{profit:+.2f} zł".replace(".", ",") if profit is not None else "–", profit))
            color = profit_color(profit)
            if color:
                t.item(r, 9).setForeground(QBrush(QColor(color)))
        t.setSortingEnabled(True)
        totals = self.ctx.stats.totals()
        self.summary.setText(f"Kuponów: {totals.coupons} · w grze: {totals.pending} ({num(totals.pending_stake)} zł) · "
                             f"bilans rozliczonych: {totals.profit:+.2f} zł".replace(".", ","))

    def selected(self) -> StoredCoupon | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        cid = self.table.item(rows[0].row(), 0).data(Qt.UserRole + 10)
        return self.ctx.register.get(cid)

    def details(self) -> None:
        c = self.selected()
        if c:
            CouponDetailsDialog(c, self).exec()

    def manual(self) -> None:
        c = self.selected()
        if not c:
            return
        dlg = ManualResultDialog(c, self)
        if dlg.exec() == QDialog.Accepted:
            status, value, note = dlg.values()
            self.ctx.register.set_manual_result(c.id, status, value, note)
            self.ctx.hub.coupons_changed.emit()

    def delete(self) -> None:
        c = self.selected()
        if not c:
            return
        if QMessageBox.question(self, "Usuń kupon", f"Usunąć kupon nr {c.id} z rejestru?") == QMessageBox.Yes:
            self.ctx.register.delete(c.id)
            self.ctx.hub.coupons_changed.emit()

    def settle(self) -> None:
        """Pobiera wyniki (synchronizacja terminarza) i rozlicza kupony – w tle."""
        self.settle_btn.setEnabled(False)
        self.ctx.hub.busy.emit("rozliczanie", True)
        ctx = self.ctx

        def work():
            ctx.sync.run(lambda rep: ctx.sync.sync_fixtures(rep))
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
