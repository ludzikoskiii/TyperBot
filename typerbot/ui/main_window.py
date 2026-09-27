"""Okno główne: zakładki, pasek stanu ze źródłami danych, odświeżanie w tle."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMainWindow, QPushButton, QTabWidget, QVBoxLayout, QWidget

from typerbot import __version__
from typerbot.data.errors import STATE_LABELS
from typerbot.fmt import plural
from typerbot.services.diagnostics import sync_problems
from typerbot.services.sync import SyncReport
from typerbot.ui import theme
from typerbot.ui.context import AppContext
from typerbot.ui.diagnostics_view import ProblemsDialog
from typerbot.ui.tab_generator import GeneratorTab
from typerbot.ui.tab_history import HistoryTab
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
        self.history = HistoryTab(ctx)
        self.stats = StatsTab(ctx)
        self.settings = SettingsTab(ctx)
        for widget, title in ((self.matches, "Mecze"), (self.generator, "Generator kuponu"),
                              (self.history, "Historia"), (self.stats, "Model (backtest)"),
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
        self.problems_btn = QPushButton("")
        self.problems_btn.setFlat(True)
        self.problems_btn.setCursor(Qt.PointingHandCursor)
        self.problems_btn.setStyleSheet(f"color: {theme.WARNING}; border: none; text-decoration: underline;")
        self.problems_btn.setToolTip("Pokaż listę problemów ze źródłami z ostatniej synchronizacji")
        self.problems_btn.clicked.connect(self.show_problems)
        self.problems_btn.hide()
        sb.addWidget(self.sources_label)
        sb.addWidget(self.problems_btn)
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
        self._update_problems()

    def start(self) -> None:
        """Pierwsze odświeżenie po otwarciu okna (w tle) – działa bez kluczy (football-data.co.uk)."""
        self.refresh()

    def _update_banner(self) -> None:
        if self.ctx.demo:
            self.banner_text.setText("Tryb demo: dane syntetyczne i tymczasowa baza. Kupony zapisane w tym trybie "
                                     "znikną po zamknięciu. Prawdziwe dane: uruchom bez --demo (klucze API są opcjonalne).")
            self.banner.show()
        elif not self.ctx.has_any_key() and not self.ctx.sync.matches.counts()["matches"]:
            self.banner_text.setText("Kliknij „Odśwież dane” – główne źródło (football-data.co.uk) nie wymaga klucza. "
                                     "Darmowe klucze w „Ustawieniach” są opcjonalne i uzupełniają dane.")
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
        periods = {"day": "dziś", "month": "w tym mies."}
        for q in self.ctx.sync.quota_rows():
            color = STATE_COLORS.get(q.state, theme.MUTED)
            # Pokazujemy zużycie („zużyto 12/500”) – limity minutowe pomijamy, bo nic nie mówią.
            used = q.used if q.used is not None else (q.limit - q.remaining if q.limit and q.remaining is not None
                                                      else None)
            limit = (f"zużyto {used}/{q.limit} {periods[q.period]}"
                     if q.limit and used is not None and q.period in periods else "")
            parts.append(f"<span style='color:{color}'>●</span> {q.label}" + (f" <small>{limit}</small>" if limit else ""))
            tips.append(f"{q.label}: {STATE_LABELS.get(q.state, q.state)}"
                        + (f" – {q.message}" if q.message else "")
                        + (f" · zużyto {used} z {q.limit}, zostało {q.remaining} ({periods.get(q.period, q.period)})"
                           if q.limit and used is not None else ""))
        self.sources_label.setText("  ".join(parts))
        self.sources_label.setToolTip("\n".join(tips))

    def _update_problems(self) -> None:
        problems = sync_problems(self.ctx.sync.last_report())
        self.problems = problems
        self.problems_btn.setText(f"⚠ problemy ze źródeł: {len(problems)}")
        self.problems_btn.setVisible(bool(problems))

    def show_problems(self) -> None:
        ProblemsDialog(self.problems, self).exec()

    def refresh(self, force: bool = False) -> None:
        if self.syncing:
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
        msg = f"Zaktualizowano dane ({records} rekordów)"
        if settled:
            msg += f", rozliczono {plural(len(settled), 'kupon', 'kupony', 'kuponów')}"
        self.ctx.hub.message.emit(msg)
        self._update_problems()
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
