"""Zakładka „Kupony” – prosty ekran główny: kurs docelowy, zakres dat, „Generuj kupony” i 3 kupony
w stylu kuponu bukmacherskiego. Pozostałe opcje są w zwiniętej sekcji „Zaawansowane”,
a lista wszystkich meczów z ocenami typów – pod przyciskiem „Wszystkie mecze”."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QDate, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLayout, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from typerbot.betting.evaluation import ESTIMATED_NOTE
from typerbot.config.settings import MARKETS, CouponSettings
from typerbot.fmt import num, pct, plural, signed_pct
from typerbot.services.coupons import Coupon, CouponLeg, CouponService, GenerationResult, SwapOption, kickoff_local
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.diagnostics_view import DiagnosisDialog, reason_html
from typerbot.ui.league_tree import LeagueTree
from typerbot.ui.tab_matches import MatchesTab
from typerbot.ui.widgets import NumItem, ProbabilityDelegate, hbox, label, make_table, prob_item, set_role, text_item
from typerbot.ui.workers import run_in_background

MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej 2,5", "BTTS": "Obie strzelą"}
LETTERS = "ABC"
WEEKDAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


def when(iso: str) -> str:
    """„sob 27.09 15:00” – dzień tygodnia i godzina w czasie polskim."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Warsaw"))
    return f"{WEEKDAYS[dt.weekday()]} {dt.strftime('%d.%m %H:%M')}"


def coupon_text(coupon: Coupon, letter: str, league_names: dict[str, str]) -> str:
    """Kupon jako tekst do wklejenia (np. w notatkę albo do bukmachera)."""
    lines = [f"TyperBot – kupon {letter}"]
    for i, leg in enumerate(coupon.legs, 1):
        s, m = leg.selection, leg.match
        est = f" ({ESTIMATED_NOTE})" if s.estimated else ""
        lines.append(f"{i}. {m.home} – {m.away} ({league_names.get(m.league, m.league)}, {when(m.kickoff)}): "
                     f"{s.label} @ {num(s.odds or 1.0)}{est}")
    lines.append(f"Kurs łączny: {num(coupon.odds)} (po podatku {num(coupon.odds_after_tax)})")
    lines.append(f"Szansa trafienia: {pct(coupon.probability, 1)}")
    if coupon.estimated_legs:
        lines.insert(1, f"UWAGA: kupon z kursami szacunkowymi ({coupon.estimated_legs} z {len(coupon.legs)}) – "
                        "sprawdź kursy u bukmachera, kurs łączny może być inny.")
    return "\n".join(lines)


# -- elementy kuponu ---------------------------------------------------------------------------------
class ProbBar(QWidget):
    """Cienki pasek szansy trafienia."""

    def __init__(self, value: float, parent=None):
        super().__init__(parent)
        self.value = value
        self.setFixedHeight(6)

    def paintEvent(self, _event) -> None:  # noqa: N802 (nazwa z Qt)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.BORDER))
            r = self.rect()
            p.drawRoundedRect(r, 3, 3)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRect(r.x(), r.y(), int(r.width() * max(0.0, min(1.0, self.value))), r.height()), 3, 3)
        finally:
            p.end()


class LegRow(QFrame):
    """Jedno zdarzenie na kuponie: mecz, typ, kurs, szansa i jedno zdanie uzasadnienia."""
    action = Signal(str, int)      # (akcja, id meczu)

    def __init__(self, leg: CouponLeg, league_name: str):
        super().__init__()
        self.setProperty("role", "leg")
        s, m = leg.selection, leg.match
        estimated = s.estimated
        head = label(f"{league_name} · {when(m.kickoff)}", "muted")
        menu_btn = QToolButton()
        menu_btn.setText("⋯")
        menu_btn.setToolTip("Wymień zdarzenie, zmień kurs, usuń, szczegóły")
        menu_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(menu_btn)
        for text, key in (("Wymień zdarzenie…", "swap"), ("Zmień kurs (z oferty)…", "odds"),
                          ("Usuń zdarzenie", "remove"), ("Szczegóły typu", "details")):
            menu.addAction(text, lambda k=key: self.action.emit(k, m.match_id))
        menu_btn.setMenu(menu)
        teams = label(f"{m.home} – {m.away}", wrap=True)     # długie nazwy klubów zawijają się zamiast poszerzać kartę
        teams.setStyleSheet("font-weight: 600;")
        pick = label(f"Typ: <b>{s.label}</b>")
        odds = label(("≈ " if estimated else "") + num(s.odds or 1.0), "odds")
        if estimated:
            set_role(odds, "odds")
            odds.setStyleSheet(f"color: {theme.WARNING};")
            odds.setToolTip("Kurs szacunkowy – źródła nie podają kursu, więc wyliczono go z prognozy (albo z innych "
                            "kursów meczu) z typową marżą bukmachera. Sprawdź u bukmachera.")
        chance = label(f"szansa {pct(s.probability)}", "muted")
        est_note = label("≈ " + ESTIMATED_NOTE, "warning", wrap=True) if estimated else None
        reason = label(leg.summary or "", "muted", wrap=True)
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setVerticalSpacing(3)
        lay.setColumnStretch(0, 1)
        lay.addLayout(hbox(head, None, menu_btn, spacing=4), 0, 0, 1, 2)
        lay.addWidget(teams, 1, 0, 1, 2)
        lay.addWidget(pick, 2, 0)
        lay.addWidget(odds, 2, 1, Qt.AlignRight)
        lay.addWidget(ProbBar(s.probability), 3, 0)
        lay.addWidget(chance, 3, 1, Qt.AlignRight)
        lay.addWidget(reason, 4, 0, 1, 2)
        if est_note is not None:
            lay.addWidget(est_note, 5, 0, 1, 2)
        if m.flags:
            lay.addWidget(label("Uwaga: " + "; ".join(m.flags), "warning", wrap=True), 6, 0, 1, 2)


class SlipCard(QFrame):
    """Kupon w stylu bukmacherskim."""
    changed = Signal()

    def __init__(self, ctx: AppContext, service: CouponService, cfg: CouponSettings, coupon: Coupon, letter: str,
                 league_names: dict[str, str]):
        super().__init__()
        self.setProperty("role", "card")
        self.ctx, self.service, self.cfg, self.coupon, self.letter = ctx, service, cfg, coupon, letter
        self.league_names = league_names
        self.setMinimumWidth(330)
        self.setMaximumWidth(460)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(8)
        self.copy_btn = QPushButton("Kopiuj kupon")
        self.copy_btn.setProperty("role", "primary")
        self.copy_btn.clicked.connect(self.copy)
        self.copied = label("", "positive")
        self.refresh()

    def _clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None and w not in (self.copy_btn, self.copied):
                w.deleteLater()
            elif item.layout() is not None:
                _drop_layout(item.layout(), keep=(self.copy_btn, self.copied))

    def refresh(self) -> None:
        self._clear()
        c = self.coupon
        subtitle = "największa szansa trafienia" if self.letter == "A" else "alternatywa – inne mecze"
        self.body.addWidget(label(f"Kupon {self.letter}", "title"))
        self.body.addWidget(label(f"{subtitle} · {plural(len(c.legs), 'zdarzenie', 'zdarzenia', 'zdarzeń')}", "muted"))
        if c.estimated_legs:
            banner = QFrame()
            banner.setProperty("role", "banner")
            bl = QVBoxLayout(banner)
            bl.setContentsMargins(8, 6, 8, 6)
            bl.addWidget(label(f"≈ Kupon z kursami szacunkowymi ({c.estimated_legs} z {len(c.legs)}) – sprawdź kursy "
                               "u bukmachera; kurs łączny może być inny.", "warning", wrap=True))
            self.body.addWidget(banner)
        for leg in c.legs:
            row = LegRow(leg, self.league_names.get(leg.match.league, leg.match.league))
            row.action.connect(self._leg_action)
            self.body.addWidget(row)
        totals = QGridLayout()
        totals.addWidget(label("Kurs łączny", "section"), 0, 0)
        totals.addWidget(label("Po podatku (−12%)", "section"), 0, 1)
        odds = label(num(c.odds), "big")
        if not c.in_range:
            odds.setStyleSheet(f"color: {theme.WARNING};")
            odds.setToolTip(f"Poza zakresem {num(c.target[0])}–{num(c.target[1])}")
        totals.addWidget(odds, 1, 0)
        totals.addWidget(label(num(c.odds_after_tax), "big"), 1, 1)
        totals.addWidget(label("Szansa trafienia", "section"), 2, 0, 1, 2)
        totals.addWidget(label(pct(c.probability, 1), "big"), 3, 0)
        market = c.probability_market
        detail = f"model {pct(c.probability_model, 1)}" + (f" · rynek {pct(market, 1)}" if market is not None else "")
        totals.addWidget(label(detail, "muted"), 3, 1)
        self.body.addLayout(totals)
        if not c.in_range:
            self.body.addWidget(label(f"Kurs łączny poza zakresem {num(c.target[0])}–{num(c.target[1])}.",
                                      "warning", wrap=True))
        self.body.addLayout(hbox(self.copy_btn, self.copied, None))
        self.body.addWidget(label(f"W historii jako kupon nr {c.history_id}" if c.history_id else "", "muted"))
        self.body.addStretch(1)

    def text(self) -> str:
        return coupon_text(self.coupon, self.letter, self.league_names)

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text())
        if self.coupon.history_id is None:
            self.service.update_record(self.coupon)
        if self.coupon.history_id is not None:
            self.ctx.register.mark_copied(self.coupon.history_id)
            self.ctx.hub.coupons_changed.emit()
        self.copied.setText("Skopiowano ✓")
        self.ctx.hub.message.emit(f"Skopiowano kupon {self.letter} do schowka.")

    # -- zmiany kuponu ------------------------------------------------------------------------------
    def _leg_action(self, action: str, match_id: int) -> None:
        {"swap": self.swap, "odds": self.change_odds, "remove": self.remove, "details": self.details}[action](match_id)

    def _leg(self, match_id: int) -> CouponLeg | None:
        return next((x for x in self.coupon.legs if x.match.match_id == match_id), None)

    def _updated(self) -> None:
        """Po ręcznej zmianie: przeliczenie, aktualizacja wpisu w historii i odświeżenie kuponu."""
        self.service.update_record(self.coupon)
        self.copied.setText("")
        self.refresh()
        self.changed.emit()
        self.ctx.hub.coupons_changed.emit()

    def swap(self, match_id: int) -> None:
        leg = self._leg(match_id)
        if leg is None:
            return
        options = self.service.swap_options(self.coupon, match_id, self.cfg, limit=25)
        if not options:
            QMessageBox.information(self, "Wymień zdarzenie", "Brak zamienników spełniających warunki.")
            return
        dlg = SwapDialog(options, f"{leg.match.home} – {leg.match.away}: {leg.selection.label}", self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen():
            self.coupon = self.service.swap(self.coupon, match_id, dlg.chosen())
            self._updated()

    def change_odds(self, match_id: int) -> None:
        leg = self._leg(match_id)
        if leg is None:
            return
        value, ok = QInputDialog.getDouble(self, "Zmień kurs", f"Kurs z oferty bukmachera – {leg.selection.label}:",
                                           leg.selection.odds or 1.5, 1.01, 1000.0, 2)
        if ok:
            self.coupon = self.service.set_manual_odds(self.coupon, match_id, value)
            self._updated()

    def remove(self, match_id: int) -> None:
        if len(self.coupon.legs) <= 1:
            return
        self.coupon = self.service.remove(self.coupon, match_id)
        self._updated()

    def details(self, match_id: int) -> None:
        leg = self._leg(match_id)
        if leg is None:
            return
        s = leg.selection
        info = (f"<b>{leg.match.home} – {leg.match.away}: {s.label}</b><br>"
                f"Kurs {num(s.odds or 1.0)} ({s.source_label or 'bukmacher'}) · prognoza {pct(s.probability)} · "
                f"model {pct(s.p_model)} · rynek {pct(s.p_market)} · EV {signed_pct(s.ev)}<br><br>"
                + "<br>".join(leg.rationale))
        QMessageBox.information(self, "Szczegóły typu", info)


class CardGrid(QWidget):
    """Kupony obok siebie – a gdy ekran jest węższy, w dwóch kolumnach albo jeden pod drugim."""
    CARD_MIN = 330          # = minimalna szerokość SlipCard
    SPACING = 12

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setSpacing(self.SPACING)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)   # szerokość wyznacza okno, nie bieżący układ
        self.items: list[QWidget] = []
        self.fill = False
        self.columns = 0

    def set_items(self, items: list[QWidget], fill: bool = False) -> None:
        """`fill` – jeden element na całą szerokość (np. komunikat zamiast kuponów)."""
        for w in self.items:
            self.grid.removeWidget(w)
        self.items, self.fill, self.columns = list(items), fill, 0
        self._relayout()

    def _fit(self) -> int:
        if self.fill:
            return 1
        m = self.grid.contentsMargins()
        width = self.width() - m.left() - m.right()
        return max(1, min(max(len(self.items), 1), (width + self.SPACING) // (self.CARD_MIN + self.SPACING)))

    def _relayout(self) -> None:
        cols = self._fit()
        if cols == self.columns:
            return
        self.columns = cols
        for w in self.items:
            self.grid.removeWidget(w)
        for c in range(self.grid.columnCount()):
            self.grid.setColumnStretch(c, 0)
        for r in range(self.grid.rowCount()):
            self.grid.setRowStretch(r, 0)
        for i, w in enumerate(self.items):
            self.grid.addWidget(w, i // cols, i % cols)
        rows = (len(self.items) + cols - 1) // cols
        self.grid.setRowStretch(rows, 1)
        self.grid.setColumnStretch(0 if self.fill else cols, 1)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (nazwa z Qt)
        """Minimum to jedna kolumna – inaczej przewijanie w poziomie nie pozwoliłoby zmniejszyć liczby kolumn."""
        hint = self.grid.minimumSize()
        m = self.grid.contentsMargins()
        return QSize(min(hint.width(), self.CARD_MIN + m.left() + m.right()), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (nazwa z Qt)
        super().resizeEvent(event)
        self._relayout()


def _drop_layout(layout, keep=()) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None and w not in keep:
            w.deleteLater()
        elif w is not None:
            w.setParent(None)
        elif item.layout() is not None:
            _drop_layout(item.layout(), keep)


class SwapDialog(QDialog):
    def __init__(self, options: list[SwapOption], title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wymień zdarzenie")
        self.resize(820, 440)
        self.options = options
        self.table = make_table(["Data", "Mecz", "Typ", "Kurs", "Szansa", "Kurs kuponu", "Zakres"], stretch=1,
                                sortable=False)
        self.table.setItemDelegateForColumn(4, ProbabilityDelegate(self.table))
        for o in options:
            r = self.table.rowCount()
            self.table.insertRow(r)
            s, m = o.leg.selection, o.leg.match
            self.table.setItem(r, 0, text_item(kickoff_local(m.kickoff)))
            self.table.setItem(r, 1, text_item(f"{m.home} – {m.away}"))
            self.table.setItem(r, 2, text_item(s.label))
            est = s.odds_source == "estimated"
            self.table.setItem(r, 3, NumItem(("≈ " if est else "") + num(s.odds), s.odds))
            self.table.setItem(r, 4, prob_item(s.probability))
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


# -- zakładka ------------------------------------------------------------------------------------------
class CouponsTab(QWidget):
    RANGE_BUTTONS = [("Dziś", "today"), ("Jutro", "tomorrow"), ("Najbliższe 3 dni", "days"), ("Własny", "custom")]

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.service = CouponService(ctx.db, now=ctx.now)
        self.busy = False
        self.diagnosis = None

        # -- proste opcje -------------------------------------------------------------------------------
        self.target = QDoubleSpinBox()
        self.target.setProperty("role", "target")
        self.target.setRange(1.2, 10000)
        self.target.setDecimals(2)
        self.target.setSingleStep(0.5)
        self.range_group = QButtonGroup(self)
        self.range_buttons: dict[str, QPushButton] = {}
        range_grid = QGridLayout()
        for i, (text, mode) in enumerate(self.RANGE_BUTTONS):
            btn = QPushButton(text)
            btn.setProperty("role", "segment")
            btn.setCheckable(True)
            self.range_group.addButton(btn, i)
            self.range_buttons[mode] = btn
            range_grid.addWidget(btn, i // 2, i % 2)
        self.date_from = QDateEdit(QDate.currentDate())
        self.date_to = QDateEdit(QDate.currentDate().addDays(3))
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd.MM.yyyy")
        self.custom_row = QWidget()
        cr = QHBoxLayout(self.custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(label("od"))
        cr.addWidget(self.date_from, 1)
        cr.addWidget(label("do"))
        cr.addWidget(self.date_to, 1)
        self.generate_btn = QPushButton("Generuj kupony")
        self.generate_btn.setProperty("role", "big")
        self.status = label("", "muted", wrap=True)

        # -- zaawansowane (zwinięte) ------------------------------------------------------------------------
        self.expander = QToolButton()
        self.expander.setProperty("role", "expander")
        self.expander.setText("Zaawansowane")
        self.expander.setCheckable(True)
        self.expander.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.expander.setArrowType(Qt.RightArrow)
        self.advanced = QWidget()
        self.tolerance = QSpinBox()
        self.tolerance.setRange(1, 50)
        self.tolerance.setSuffix(" %")
        self.days = QSpinBox()
        self.days.setRange(1, 14)
        self.days.setSuffix(" dni")
        self.min_events = QSpinBox()
        self.min_events.setRange(1, 20)
        self.max_events = QSpinBox()
        self.max_events.setRange(1, 20)
        self.min_prob = QSpinBox()
        self.min_prob.setRange(1, 99)
        self.min_prob.setSuffix(" %")
        self.mode = QComboBox()
        self.mode.addItem("Najwyższa szansa trafienia", "probability")
        self.mode.addItem("Tylko typy z przewagą (value)", "value")
        self.divergence = QSpinBox()
        self.divergence.setRange(1, 50)
        self.divergence.setSuffix(" pkt proc.")
        self.divergence.setToolTip("Tryb „najwyższa szansa”: pomijamy typy, w których model i rynek różnią się bardziej")
        self.leagues = LeagueTree()
        self.leagues.setMinimumHeight(240)
        self.markets = {m: QCheckBox(MARKET_NAMES[m]) for m in MARKETS}
        self.low_data = QCheckBox("Dopuść drużyny z małą liczbą danych")
        self.estimated = QComboBox()
        self.estimated.addItem("gdy brak innych", "fallback")
        self.estimated.addItem("zawsze dopuszczaj", "always")
        self.estimated.addItem("nigdy", "never")
        self.estimated.setToolTip("Kurs szacunkowy (≈) – z prognozy modelu z typową marżą bukmachera, gdy źródła nie "
                                  "podają kursu. Kupon z takim kursem jest wyraźnie oznaczony – sprawdź kurs u "
                                  "bukmachera.")
        self.save_defaults_btn = QPushButton("Zapisz jako domyślne")
        events = QWidget()
        el = QHBoxLayout(events)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(self.min_events)
        el.addWidget(label("–"))
        el.addWidget(self.max_events)
        for w in (self.mode, self.estimated):
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(12)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.addRow("Tolerancja kursu ±", self.tolerance)
        form.addRow("„Najbliższe dni”", self.days)
        form.addRow("Liczba zdarzeń", events)
        form.addRow("Min. szansa typu", self.min_prob)
        form.addRow("Tryb", self.mode)
        form.addRow("Różnica model–rynek", self.divergence)
        form.addRow("Kursy szacunkowe (≈)", self.estimated)
        al = QVBoxLayout(self.advanced)
        al.setContentsMargins(0, 0, 0, 0)
        al.addLayout(form)
        al.addWidget(label("Kraje i ligi (zaznacz kraj, aby wybrać wszystkie jego ligi)", "section", wrap=True))
        al.addWidget(self.leagues)
        al.addWidget(label("Rynki", "section"))
        for cb in self.markets.values():
            al.addWidget(cb)
        al.addWidget(self.low_data)
        al.addWidget(self.save_defaults_btn)
        self.advanced.setVisible(False)

        self.diag_btn = QPushButton("Diagnostyka")
        self.diag_btn.setToolTip("Ile meczów i kursów przyszło z każdego źródła i ile zostaje po każdym filtrze")
        self.diag_btn.setEnabled(False)

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setSpacing(8)
        pl.addWidget(label("Kurs docelowy", "section"))
        pl.addWidget(self.target)
        pl.addWidget(label("Mecze", "section"))
        pl.addLayout(range_grid)
        pl.addWidget(self.custom_row)
        pl.addSpacing(6)
        pl.addWidget(self.generate_btn)
        pl.addWidget(self.status)
        pl.addSpacing(6)
        pl.addWidget(self.expander)
        pl.addWidget(self.advanced)
        pl.addStretch(1)
        pl.addWidget(self.diag_btn)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setWidget(panel)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.setFixedWidth(350)
        panel.setMaximumWidth(334)

        # -- wyniki i wszystkie mecze ----------------------------------------------------------------------
        self.view_group = QButtonGroup(self)
        self.view_coupons = QPushButton("Kupony")
        self.view_matches = QPushButton("Wszystkie mecze")
        for i, b in enumerate((self.view_coupons, self.view_matches)):
            b.setProperty("role", "segment")
            b.setCheckable(True)
            self.view_group.addButton(b, i)
        self.view_coupons.setChecked(True)
        self.results = CardGrid()
        self.placeholder = label("Ustaw kurs docelowy, wybierz mecze i kliknij „Generuj kupony”. Aplikacja ułoży do 3 "
                                 "kuponów – od najwyższej szansy trafienia, każdy z inną połową meczów.", "muted",
                                 wrap=True)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.results.set_items([self.placeholder], fill=True)
        results_scroll = QScrollArea()
        results_scroll.setWidgetResizable(True)
        results_scroll.setWidget(self.results)
        self.matches = MatchesTab(ctx)
        self.stack = QStackedWidget()
        self.stack.addWidget(results_scroll)
        self.stack.addWidget(self.matches)
        note = label("Szansa trafienia zakłada niezależność meczów (na kuponie jest najwyżej jeden typ z meczu i żadna "
                     "drużyna dwa razy). Prognoza to kursy bez marży połączone z modelem w proporcji dobranej "
                     "backtestem (udział modelu: Ustawienia → Model i backtest); na kupon trafiają tylko typy, "
                     "w których model i rynek są zgodni.", "muted", wrap=True)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(hbox(self.view_coupons, self.view_matches, None))
        rl.addWidget(self.stack, 1)
        rl.addWidget(note)

        lay = QHBoxLayout(self)
        lay.addWidget(left)
        lay.addWidget(right, 1)

        self.range_group.idClicked.connect(self._range_changed)
        self.view_group.idClicked.connect(self.stack.setCurrentIndex)
        self.expander.toggled.connect(self._toggle_advanced)
        self.generate_btn.clicked.connect(self.generate)
        self.save_defaults_btn.clicked.connect(self.save_defaults)
        self.diag_btn.clicked.connect(self.show_diagnosis)
        self.days.valueChanged.connect(self._update_days_label)
        ctx.hub.settings_changed.connect(self.load_settings)
        ctx.hub.data_changed.connect(lambda: self.load_leagues())    # nowe ligi i mecze po pobraniu danych
        self.load_settings()

    # -- ustawienia -------------------------------------------------------------------------------------
    def _toggle_advanced(self, on: bool) -> None:
        self.advanced.setVisible(on)
        self.expander.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def _update_days_label(self) -> None:
        self.range_buttons["days"].setText(f"Najbliższe {plural(self.days.value(), 'dzień', 'dni', 'dni')}")

    def range_mode(self) -> str:
        return self.RANGE_BUTTONS[max(0, self.range_group.checkedId())][1]

    def set_range(self, mode: str) -> None:
        self.range_buttons.get(mode, self.range_buttons["days"]).setChecked(True)
        self._range_changed()

    def _range_changed(self, *_args) -> None:
        self.custom_row.setVisible(self.range_mode() == "custom")

    def load_settings(self) -> None:
        settings = self.ctx.settings()
        c = settings.coupon
        self.target.setValue(c.target_odds)
        self.tolerance.setValue(round(c.tolerance * 100))
        self.days.setValue(c.days_ahead)
        self._update_days_label()
        if c.date_from:
            self.date_from.setDate(QDate.fromString(c.date_from, "yyyy-MM-dd"))
        if c.date_to:
            self.date_to.setDate(QDate.fromString(c.date_to, "yyyy-MM-dd"))
        self.set_range(c.date_range)
        self.min_events.setValue(c.min_events)
        self.max_events.setValue(c.max_events)
        self.min_prob.setValue(round(c.min_probability * 100))
        self.mode.setCurrentIndex(0 if c.mode == "probability" else 1)
        self.divergence.setValue(round(c.max_divergence * 100))
        self.low_data.setChecked(c.include_low_data)
        self.estimated.setCurrentIndex(max(0, self.estimated.findData(c.estimated_odds)))
        self.load_leagues(c.leagues)
        for m, cb in self.markets.items():
            cb.setChecked(m in c.markets)
            cb.setEnabled(m in settings.markets_enabled)

    def load_leagues(self, selected: list[str] | None = None) -> None:
        """Ligi z danymi (aktywne w ustawieniach) – lista budowana z bazy przy każdym odświeżeniu."""
        keep = selected if selected is not None else (None if self.leagues.all_checked() else
                                                      self.leagues.checked_codes())
        rows = [r for r in self.ctx.sync.leagues.with_counts(self.ctx.now()) if r.league.enabled]
        self.leagues.set_leagues(rows, keep or None)

    def current_cfg(self) -> CouponSettings:
        base = self.ctx.settings().coupon
        leagues = self.leagues.checked_codes()
        all_leagues = self.leagues.all_checked()
        return replace(
            base,
            target_odds=self.target.value(), tolerance=self.tolerance.value() / 100,
            date_range=self.range_mode(), days_ahead=self.days.value(),
            date_from=self.date_from.date().toString("yyyy-MM-dd"), date_to=self.date_to.date().toString("yyyy-MM-dd"),
            min_events=min(self.min_events.value(), self.max_events.value()),
            max_events=max(self.min_events.value(), self.max_events.value()),
            min_probability=self.min_prob.value() / 100, mode=self.mode.currentData(),
            max_divergence=self.divergence.value() / 100,
            leagues=[] if all_leagues else leagues, markets=[m for m, cb in self.markets.items() if cb.isChecked()],
            include_low_data=self.low_data.isChecked(), estimated_odds=self.estimated.currentData(),
        )

    def save_defaults(self) -> None:
        settings = self.ctx.settings()
        settings.coupon = self.current_cfg()
        self.ctx.save_settings(settings)
        self.status.setText("Zapisano ustawienia kuponu jako domyślne.")

    # -- generowanie ------------------------------------------------------------------------------------
    def generate(self) -> None:
        if self.busy:
            return
        cfg = self.current_cfg()
        if not cfg.markets:
            self.status.setText("Zaznacz co najmniej jeden rynek (Zaawansowane).")
            return
        self.busy = True
        self.generate_btn.setEnabled(False)
        self.status.setText("Liczę prognozy i szukam najlepszych kombinacji…")
        self.ctx.hub.busy.emit("generator", True)
        self.view_coupons.setChecked(True)
        self.stack.setCurrentIndex(0)
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

    def _clear_results(self) -> None:
        old = self.results.items
        self.results.set_items([])
        for w in old:
            if w is self.placeholder:
                w.setParent(None)
            else:
                w.deleteLater()

    def _generated(self, cfg: CouponSettings, result: GenerationResult) -> None:
        self.busy = False
        self.generate_btn.setEnabled(True)
        self.ctx.hub.busy.emit("generator", False)
        self.diagnosis = diag = result.diagnosis
        self.diag_btn.setEnabled(True)
        self._clear_results()
        if not result.coupons:
            self.placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            set_role(self.placeholder, None)
            self.placeholder.setText(reason_html(diag) + "<p style='color:#9aa1ad'>Szczegóły: przycisk "
                                     "„Diagnostyka” – ile meczów i kursów przyszło z każdego źródła i ile zostaje "
                                     "po każdym filtrze.</p>")
            self.results.set_items([self.placeholder], fill=True)
            self.status.setText(diag.headline())
            return
        names = {lg.code: lg.name for lg in self.ctx.sync.leagues.all()}
        self.results.set_items([SlipCard(self.ctx, self.service, cfg, coupon, letter, names)
                                for letter, coupon in zip(LETTERS, result.coupons)])
        n_matches = diag.stages[2].matches if len(diag.stages) > 2 else 0
        msg = (f"Ułożono {plural(len(result.coupons), 'kupon', 'kupony', 'kuponów')} z "
               f"{plural(n_matches, 'meczu', 'meczów', 'meczów')} – zapisane w Historii.")
        if diag.notes:
            msg += " " + " ".join(diag.notes)
        self.status.setText(msg)
        self.ctx.hub.coupons_changed.emit()

    def cards(self) -> list[SlipCard]:
        return [w for w in self.results.items if isinstance(w, SlipCard)]

    def show_diagnosis(self) -> None:
        if self.diagnosis is not None:
            DiagnosisDialog(self.diagnosis, self).exec()
