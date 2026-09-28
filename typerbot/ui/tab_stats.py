"""Backtest i strojenie modelu (wyniki w jednostkach: 1 zakład / kupon = 1 jednostka)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from typerbot.config.settings import CouponSettings
from typerbot.fmt import num, pct, signed_pct
from typerbot.model.backtest import BacktestConfig, BacktestResult, default_seasons, run_backtest, save_run
from typerbot.model.tuning import tune
from typerbot.ui import charts, theme
from typerbot.ui.context import AppContext
from typerbot.ui.widgets import NumItem, hbox, label, make_table, profit_color, text_item
from typerbot.ui.workers import run_in_background

MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej 2,5", "BTTS": "Obie strzelą"}


def _units(x: float) -> str:
    return f"{x:+,.2f}".replace(",", " ").replace(".", ",") + " j."


def _brush(color: str):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(color))


class BacktestView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.result: BacktestResult | None = None
        self.tuning = []
        self.leagues = QListWidget()
        self.leagues.setMaximumHeight(140)
        self.seasons = QLineEdit()
        self.seasons.setPlaceholderText("np. 2023, 2024, 2025")
        self.mode = QComboBox()
        self.mode.addItem("Kupony: najwyższa szansa trafienia", "probability")
        self.mode.addItem("Kupony: tylko typy z przewagą (value)", "value")
        self.target = QDoubleSpinBox()
        self.target.setRange(1.2, 1000)
        self.target.setDecimals(2)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0, 50)
        self.threshold.setSuffix(" %")
        self.haircut = QDoubleSpinBox()
        self.haircut.setRange(0, 20)
        self.haircut.setSuffix(" %")
        self.low_data = QCheckBox("Dopuść „mało danych”")
        self.run_btn = QPushButton("Uruchom backtest")
        self.run_btn.setProperty("role", "primary")
        self.tune_btn = QPushButton("Strojenie parametrów (ok. minuty)")
        self.save_tune_btn = QPushButton("Zapisz najlepsze ustawienia")
        self.save_tune_btn.setEnabled(False)
        self.status = label("", "muted", wrap=True)

        box = QGroupBox("Backtest")
        blay = QVBoxLayout(box)
        blay.addWidget(label("Ligi", "muted"))
        blay.addWidget(self.leagues)
        blay.addWidget(label("Sezony (rok rozpoczęcia)", "muted"))
        blay.addWidget(self.seasons)
        blay.addWidget(self.mode)
        blay.addLayout(hbox(label("Kurs kuponu"), self.target))
        blay.addLayout(hbox(label("Próg value"), self.threshold))
        blay.addLayout(hbox(label("Obniżka kursów"), self.haircut))
        blay.addWidget(self.low_data)
        blay.addWidget(self.run_btn)
        blay.addWidget(self.tune_btn)
        blay.addWidget(self.save_tune_btn)
        blay.addWidget(self.status)
        blay.addStretch(1)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setWidget(box)
        left.setMaximumWidth(340)

        self.metrics = make_table(["Rynek", "Mecze", "Trafność", "Log-loss", "Brier", "Log-loss rynku",
                                   "Trafność rynku", "Model lepszy?"], stretch=0, sortable=False)
        self.finance = label("", wrap=True)
        self.cal_market = QComboBox()
        for code in ("1X2", "OU", "BTTS", "DC"):
            self.cal_market.addItem(MARKET_NAMES[code], code)
        self.chart_row = QSplitter(Qt.Horizontal)
        self.tune_table = make_table(["Meczów", "Półokres", "Regularyzacja", "Log-loss 1X2", "Brier",
                                      "Kalibracja", "Udział modelu", "Log-loss mieszanki"], stretch=None, sortable=False)
        results = QWidget()
        rlay = QVBoxLayout(results)
        rlay.addWidget(label("1) Skuteczność vs rynek (kursy zamknięcia bez marży) – niższy log-loss = lepiej", "muted"))
        rlay.addWidget(self.metrics)
        rlay.addLayout(hbox(label("2) Kalibracja i 4) mieszanka model + rynek", "muted"), None, self.cal_market))
        rlay.addWidget(self.chart_row, 1)
        rlay.addWidget(label("3) Wynik w jednostkach – 1 zakład / kupon = 1 j. (po podatku)", "muted"))
        rlay.addWidget(self.finance)
        rlay.addWidget(label("Strojenie parametrów", "muted"))
        rlay.addWidget(self.tune_table)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(results)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(scroll)
        split.setSizes([320, 900])
        lay = QVBoxLayout(self)
        lay.addWidget(split)

        self.run_btn.clicked.connect(self.run)
        self.tune_btn.clicked.connect(self.run_tuning)
        self.save_tune_btn.clicked.connect(self.save_tuning)
        self.cal_market.currentIndexChanged.connect(self._draw_charts)
        ctx.hub.settings_changed.connect(self.load)
        ctx.hub.data_changed.connect(self._fill_seasons)
        self.load()

    def _fill_seasons(self) -> None:
        codes = self._leagues()
        if codes and not self.seasons.text().strip():
            seasons = default_seasons(self.ctx.db, codes, self.ctx.sync.current_season())
            self.seasons.setText(", ".join(map(str, seasons)))

    def load(self) -> None:
        settings = self.ctx.settings()
        self.leagues.clear()
        for lg in self.ctx.sync.leagues.all(enabled_only=True):
            if lg.is_cup or lg.sport != "football":      # backtest modelu piłkarskiego (Dixon-Coles + Elo)
                continue
            item = QListWidgetItem(lg.name)
            item.setData(Qt.UserRole, lg.code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.leagues.addItem(item)
        self._fill_seasons()
        self.target.setValue(settings.coupon.target_odds)
        self.mode.setCurrentIndex(0 if settings.coupon.mode == "probability" else 1)

    def _leagues(self) -> list[str]:
        return [self.leagues.item(i).data(Qt.UserRole) for i in range(self.leagues.count())
                if self.leagues.item(i).checkState() == Qt.Checked]

    def _config(self) -> BacktestConfig | None:
        try:
            seasons = [int(x) for x in self.seasons.text().replace(";", ",").split(",") if x.strip()]
        except ValueError:
            self.status.setText("Sezony wpisz jako lata oddzielone przecinkami, np. 2023, 2024.")
            return None
        leagues = self._leagues()
        if not seasons or not leagues:
            self.status.setText("Wybierz ligi i sezony (w bazie musi być historia – odśwież dane).")
            return None
        return BacktestConfig(leagues=leagues, seasons=seasons, value_threshold=self.threshold.value() / 100,
                              odds_haircut=self.haircut.value() / 100, include_low_data=self.low_data.isChecked())

    def _coupon_cfg(self) -> CouponSettings:
        from dataclasses import replace
        base = self.ctx.settings().coupon
        return replace(base, target_odds=self.target.value(), mode=self.mode.currentData(), markets=list(base.markets))

    def run(self) -> None:
        cfg = self._config()
        if cfg is None:
            return
        settings = self.ctx.settings()
        coupon = self._coupon_cfg()
        self._busy(True, "Liczę backtest – model uczy się tydzień po tygodniu…")
        db = self.ctx.db

        def work():
            res = run_backtest(db, cfg, settings.model, coupon, settings.tax)
            save_run(db, res)
            return res

        run_in_background(work, self._done, self._failed)

    def _busy(self, on: bool, text: str = "") -> None:
        self.run_btn.setEnabled(not on)
        self.tune_btn.setEnabled(not on)
        self.ctx.hub.busy.emit("backtest", on)
        if text:
            self.status.setText(text)

    def _failed(self, msg: str) -> None:
        self._busy(False, f"Błąd: {msg}")

    def _done(self, res: BacktestResult) -> None:
        self._busy(False, f"Gotowe: {len(res.rows)} meczów, {res.fits} dopasowań modelu, {res.seconds:.1f} s.")
        self.result = res
        t = self.metrics
        t.setRowCount(0)
        for code, m in res.metrics.items():
            if not m.n:
                continue
            r = t.rowCount()
            t.insertRow(r)
            verdict = {True: "tak", False: "nie", None: "brak kursów"}[m.beats_market]
            t.setItem(r, 0, text_item(MARKET_NAMES[code], bold=True))
            t.setItem(r, 1, NumItem(str(m.n), m.n))
            t.setItem(r, 2, NumItem(pct(m.accuracy, 1), m.accuracy))
            t.setItem(r, 3, NumItem(num(m.log_loss, 3), m.log_loss))
            t.setItem(r, 4, NumItem(num(m.brier, 3), m.brier))
            t.setItem(r, 5, NumItem(num(m.market_log_loss, 3) if m.market_log_loss is not None else "–", m.market_log_loss))
            t.setItem(r, 6, NumItem(pct(m.market_accuracy, 1) if m.market_accuracy is not None else "–", m.market_accuracy))
            t.setItem(r, 7, text_item(verdict, theme.POSITIVE if m.beats_market else None))
        s, c = res.singles, res.coupons
        recs = res.coupon_records
        avg_p = sum(r.probability for r in recs) / len(recs) if recs else 0.0
        mk = [r.market_probability for r in recs if r.market_probability is not None]
        lines = [
            f"<b>Pojedyncze typy value</b>: {s.bets} zakładów, trafność {pct(s.hit_rate, 1)}, śr. kurs {num(s.avg_odds)}, "
            f"wynik <span style='color:{profit_color(s.profit) or theme.TEXT}'>{_units(s.profit)}</span>, ROI {signed_pct(s.roi)}",
            "według przewagi: " + " · ".join(f"{k}: {v.bets} zakł., ROI {signed_pct(v.roi)}" for k, v in res.singles_by_edge.items()),
            f"<b>Kupony</b> (kurs {num(self.target.value())}): {c.bets}, trafione {c.hits} ({pct(c.hit_rate, 1)}), "
            f"wynik <span style='color:{profit_color(c.profit) or theme.TEXT}'>{_units(c.profit)}</span>, ROI {signed_pct(c.roi)}"
            + (f" · szansa trafienia: prognoza {pct(avg_p, 1)}" + (f", rynek {pct(sum(mk) / len(mk), 1)}" if mk else "")
               + f", faktycznie {pct(c.hit_rate, 1)}" if recs else ""),
        ]
        if res.blend:
            best_w, best_ll = min(res.blend, key=lambda x: x[1])
            lines.append(f"<b>Mieszanka model + rynek</b>: najlepiej przy {pct(best_w)} modelu (log-loss {num(best_ll, 4)}); "
                         + ("model nie wnosi informacji ponad kursy." if best_w == 0 else "model wnosi część informacji."))
        lines += [f"<i>{n}</i>" for n in res.notes]
        self.finance.setText("<br>".join(lines))
        self._draw_charts()

    def _draw_charts(self) -> None:
        while self.chart_row.count():
            self.chart_row.widget(0).setParent(None)
        res = self.result
        if res is None:
            return
        code = self.cal_market.currentData()
        bins = [(b.predicted, b.observed, b.n) for b in res.calibration.get(code, [])]
        ece = res.ece.get(code)
        title = f"Kalibracja – {MARKET_NAMES[code]}" + (f" (ECE {100 * ece:.1f} pkt proc.)" if ece == ece else "")
        title = title.replace(".", ",")
        self.chart_row.addWidget(charts.calibration_chart(bins, title))
        if res.blend:
            best = min(res.blend, key=lambda x: x[1])
            self.chart_row.addWidget(charts.xy_line_chart(res.blend, "Log-loss 1X2 a udział modelu", "udział modelu %",
                                                          "log-loss", highlight=best))
        if res.equity_coupons:
            self.chart_row.addWidget(charts.equity_chart(res.equity_coupons, "Symulowane kupony – wynik (jednostki)"))

    def run_tuning(self) -> None:
        cfg = self._config()
        if cfg is None:
            return
        base = self.ctx.settings().model
        self._busy(True, "Strojenie: sprawdzam 36 zestawów ustawień…")
        db = self.ctx.db
        run_in_background(lambda: tune(db, cfg, base), self._tuned, self._failed)

    def _tuned(self, results) -> None:
        self.tuning = results
        self._busy(False, "Strojenie zakończone – najlepsze ustawienia na górze.")
        t = self.tune_table
        t.setRowCount(0)
        for res in results[:10]:
            m = res.settings
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, NumItem(str(m.last_matches), m.last_matches))
            t.setItem(r, 1, NumItem(f"{m.half_life_days:g} dni", m.half_life_days))
            t.setItem(r, 2, NumItem(f"{m.regularization:g}", m.regularization))
            t.setItem(r, 3, NumItem(num(res.log_loss, 4), res.log_loss))
            t.setItem(r, 4, NumItem(num(res.brier, 4), res.brier))
            t.setItem(r, 5, NumItem(f"{num(100 * res.ece, 1)} pp", res.ece))
            t.setItem(r, 6, NumItem(pct(res.best_model_weight) if res.best_model_weight is not None else "–",
                                    res.best_model_weight))
            t.setItem(r, 7, NumItem(num(res.blend_log_loss, 4) if res.blend_log_loss else "–", res.blend_log_loss))
        self.save_tune_btn.setEnabled(bool(results))

    def save_tuning(self) -> None:
        if not self.tuning:
            return
        best = self.tuning[0]
        settings = self.ctx.settings()
        settings.model.last_matches = best.settings.last_matches
        settings.model.half_life_days = best.settings.half_life_days
        settings.model.regularization = best.settings.regularization
        if best.best_model_weight is not None:
            settings.model.model_weight = best.best_model_weight
        self.ctx.save_settings(settings)
        self.status.setText("Zapisano najlepsze ustawienia modelu.")
