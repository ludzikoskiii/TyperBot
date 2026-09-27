"""Okno główne: zakładki, pasek stanu ze źródłami danych, odświeżanie w tle."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMainWindow, QPushButton, QTabWidget, QVBoxLayout, QWidget

from typerbot import __version__
from typerbot.data.errors import STATE_LABELS
from typerbot.fmt import plural
from typerbot.services.sync import SyncReport
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.tab_coupons import CouponsTab
from typerbot.ui.tab_generator import GeneratorTab
from typerbot.ui.tab_matches import MatchesTab
from typerbot.ui.tab_settings import SettingsTab
from typerbot.ui.tab_stats import StatsTab
from typerbot.ui.widgets import label
from typerbot.ui.workers import run_in_background

STATE_COLORS = {"ok": theme.POSITIVE, "idle": theme.MUTED, "offline": theme.WARNING, "plan": theme.WARNING,
                "quota": theme.NEGATIVE, "no_key": theme.MUTED, "auth": theme.NEGATIVE, "error": theme.NEGATIVE}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.busy: set[str] = set()
        self.syncing = False
        self.setWindowTitle(f"TyperBot {__version__}" + (" – tryb demo (dane syntetyczne)" if ctx.demo else ""))
        self.resize(1360, 860)

        self.tabs = QTabWidget()
        self.matches = MatchesTab(ctx)
        self.generator = GeneratorTab(ctx)
        self.coupons = CouponsTab(ctx)
        self.stats = StatsTab(ctx)
        self.settings = SettingsTab(ctx)
        for widget, title in ((self.matches, "Mecze"), (self.generator, "Generator kuponu"),
                              (self.coupons, "Moje kupony"), (self.stats, "Statystyki"),
                              (self.settings, "Ustawienia")):
            self.tabs.addTab(widget, title)

        self.banner = QFrame()
        self.banner.setProperty("role", "banner")
        bl = QHBoxLayout(self.banner)
        self.banner_text = label("", "warning", wrap=True)
        bl.addWidget(self.banner_text)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(self.banner)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._update_banner()

        sb = self.statusBar()
        self.sources_label = label("")
        self.sync_label = label("")
        self.busy_label = label("")
        self.message_label = label("")
        self.refresh_btn = QPushButton("Odśwież dane")
        sb.addWidget(self.sources_label)
        sb.addWidget(self.message_label, 1)
        sb.addPermanentWidget(self.busy_label)
        sb.addPermanentWidget(self.sync_label)
        sb.addPermanentWidget(self.refresh_btn)
        self.refresh_btn.clicked.connect(lambda: self.refresh(force=True))

        ctx.hub.message.connect(self.message_label.setText)
        ctx.hub.busy.connect(self._on_busy)
        ctx.hub.settings_changed.connect(self._on_settings)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self._on_settings()
        self._update_sources()

    def start(self) -> None:
        """Pierwsze odświeżenie po otwarciu okna (w tle)."""
        if self.ctx.demo or self.ctx.has_any_key():
            self.refresh()
        else:
            self.matches.reload()

    def _update_banner(self) -> None:
        if self.ctx.demo:
            self.banner_text.setText("Tryb demo: dane syntetyczne i tymczasowa baza. Kupony zapisane w tym trybie "
                                     "znikną po zamknięciu. Prawdziwe dane: uruchom bez --demo i wpisz klucze API.")
            self.banner.show()
        elif not self.ctx.has_any_key():
            self.banner_text.setText("Brak kluczy API. Wpisz je w zakładce „Ustawienia” (opis zdobycia kluczy w "
                                     "README), a potem kliknij „Odśwież dane”.")
            self.banner.show()
        else:
            self.banner.hide()

    def _on_settings(self) -> None:
        hours = self.ctx.settings().sync.fixtures_every_hours
        self.timer.start(int(max(0.5, hours) * 3600 * 1000))
        self._update_banner()

    def _on_busy(self, name: str, on: bool) -> None:
        (self.busy.add if on else self.busy.discard)(name)
        names = {"sync": "pobieram dane", "prognozy": "liczę prognozy", "generator": "układam kupony",
                 "backtest": "backtest", "rozliczanie": "rozliczam kupony"}
        self.busy_label.setText(("⏳ " + ", ".join(names.get(n, n) for n in sorted(self.busy))) if self.busy else "")

    def _update_sources(self) -> None:
        parts, tips = [], []
        for q in self.ctx.sync.quota_rows():
            color = STATE_COLORS.get(q.state, theme.MUTED)
            limit = f"{q.remaining}/{q.limit}" if q.limit and q.remaining is not None else ""
            parts.append(f"<span style='color:{color}'>●</span> {q.label}" + (f" <small>{limit}</small>" if limit else ""))
            tips.append(f"{q.label}: {STATE_LABELS.get(q.state, q.state)}"
                        + (f" – {q.message}" if q.message else "")
                        + (f" · zostało {q.remaining} z {q.limit} ({q.period})" if q.limit else ""))
        self.sources_label.setText("  ".join(parts))
        self.sources_label.setToolTip("\n".join(tips))

    def refresh(self, force: bool = False) -> None:
        if self.syncing:
            return
        if not (self.ctx.demo or self.ctx.has_any_key()):
            self.ctx.hub.message.emit("Brak kluczy API – wpisz je w Ustawieniach.")
            self._update_banner()
            return
        self.syncing = True
        self.refresh_btn.setEnabled(False)
        self.ctx.hub.busy.emit("sync", True)
        ctx = self.ctx
        run_in_background(lambda: ctx.refresh(force=force), self._refreshed, self._refresh_failed)

    def _refreshed(self, result: tuple[SyncReport, list[int]]) -> None:
        report, settled = result
        self.syncing = False
        self.refresh_btn.setEnabled(True)
        self.ctx.hub.busy.emit("sync", False)
        records = sum(s.records for s in report.steps if s.state == "ok")
        errors = [s for s in report.errors]
        msg = f"Zaktualizowano dane ({records} rekordów)"
        if settled:
            msg += f", rozliczono {plural(len(settled), 'kupon', 'kupony', 'kuponów')}"
        if errors:
            msg += f"; problemy: {len(errors)} (szczegóły w podpowiedzi źródeł)"
        self.ctx.hub.message.emit(msg)
        self.sync_label.setText(f"ostatnio: {datetime.now().strftime('%H:%M')}")
        self._update_sources()
        self._update_banner()
        self.ctx.hub.data_changed.emit()
        if settled:
            self.ctx.hub.coupons_changed.emit()

    def _refresh_failed(self, message: str) -> None:
        self.syncing = False
        self.refresh_btn.setEnabled(True)
        self.ctx.hub.busy.emit("sync", False)
        self.ctx.hub.message.emit(f"Błąd odświeżania: {message}")
        self._update_sources()
