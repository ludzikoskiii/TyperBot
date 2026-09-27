"""Stałe podsumowanie budżetu bieżącego miesiąca (róg paska zakładek)."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QProgressBar

from typerbot.fmt import num
from typerbot.services.budget import EXCEEDED, NO_LIMIT, WARNING, BudgetService, BudgetStatus
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import label

HELP = ("Graj tylko za kwoty, których stratę akceptujesz. Pomoc przy problemach z hazardem: "
        "telefon zaufania 801 889 880 (Krajowe Centrum Przeciwdziałania Uzależnieniom).")


class BudgetWidget(QFrame):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.text = label("")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(110, 8)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.addWidget(self.text)
        lay.addWidget(self.bar)
        for signal in (ctx.hub.coupons_changed, ctx.hub.settings_changed, ctx.hub.data_changed):
            signal.connect(self.refresh)
        self.status: BudgetStatus | None = None
        self.refresh()

    def refresh(self) -> None:
        st = self.status = BudgetService(self.ctx.db, now=self.ctx.now).status()
        color = {EXCEEDED: theme.NEGATIVE, WARNING: theme.WARNING}.get(st.level, theme.POSITIVE)
        spent = f"{num(st.staked)} / {num(st.limit)} zł" if st.level != NO_LIMIT else f"{num(st.staked)} zł"
        balance_color = theme.POSITIVE if st.balance > 0 else (theme.NEGATIVE if st.balance < 0 else theme.MUTED)
        self.text.setText(f"<span style='color:{theme.MUTED}'>{st.label.capitalize()}:</span> wydano "
                          f"<b style='color:{color}'>{spent}</b> · bilans "
                          f"<b style='color:{balance_color}'>{st.balance:+.2f} zł</b>".replace(".", ","))
        self.bar.setVisible(st.level != NO_LIMIT)
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(min(1.0, st.fraction or 0.0) * 1000))
        self.bar.setStyleSheet(f"QProgressBar {{ background: {theme.SURFACE_2}; border: none; border-radius: 4px; }}"
                               f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}")
        tip = [st.summary(), f"w grze: {num(st.pending_stake)} zł · kuponów w miesiącu: {st.coupons}"]
        if st.level != NO_LIMIT:
            tip.append(f"zostało do limitu: {num(max(0.0, st.remaining))} zł")
        if st.warning():
            tip.append(st.warning())
        tip.append(HELP)
        self.setToolTip("\n".join(tip))
