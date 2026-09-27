"""Wykresy (QtCharts) w ciemnym motywie.

Zasady: jedna oś wartości, cienkie znaczniki (linie 2 px), dyskretna siatka,
podpowiedź po najechaniu, bez legendy dla pojedynczej serii (tytuł mówi, co to).
Zysk/strata na słupkach: kierunek od osi zera koduje znak niezależnie od koloru.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCharts import (
    QBarCategoryAxis, QBarSet, QChart, QChartView, QDateTimeAxis, QLineSeries, QScatterSeries, QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, QMargins, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import QToolTip

from typerbot.ui import theme

MIN_BIN = 20


def _base_chart(title: str) -> QChart:
    chart = QChart()
    chart.setTitle(title)
    chart.setTitleBrush(QColor(theme.TEXT))
    f = QFont()
    f.setPointSize(10)
    f.setBold(True)
    chart.setTitleFont(f)
    chart.setBackgroundBrush(QColor(theme.SURFACE))
    chart.setPlotAreaBackgroundBrush(QColor(theme.SURFACE))
    chart.setPlotAreaBackgroundVisible(True)
    chart.setBackgroundRoundness(6)
    chart.setMargins(QMargins(6, 6, 6, 6))
    chart.legend().setVisible(False)
    chart.setAnimationOptions(QChart.NoAnimation)
    return chart


def _style_axis(axis) -> None:
    axis.setLabelsColor(QColor(theme.MUTED))
    axis.setGridLineColor(QColor(theme.BORDER))
    axis.setLinePenColor(QColor(theme.BORDER))
    pen = QPen(QColor(theme.BORDER))
    pen.setWidth(1)
    axis.setGridLinePen(pen)
    f = QFont()
    f.setPointSize(8)
    axis.setLabelsFont(f)


def view(chart: QChart) -> QChartView:
    v = QChartView(chart)
    v.setRenderHint(QPainter.Antialiasing)
    v.setMinimumHeight(230)
    v.setStyleSheet(f"background: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 6px;")
    return v


def _tooltip(text: str) -> None:
    QToolTip.showText(QCursor.pos(), text)


def equity_chart(points: list[tuple[str, float]], title: str = "Wynik narastająco (jednostki)",
                 unit: str = "j.") -> QChartView:
    """Linia skumulowanego wyniku w czasie (daty RRRR-MM-DD); 1 kupon = 1 jednostka."""
    chart = _base_chart(title)
    series = QLineSeries()
    pen = QPen(QColor(theme.ACCENT))
    pen.setWidth(2)
    pen.setCapStyle(Qt.RoundCap)
    series.setPen(pen)
    series.setPointsVisible(len(points) < 40)
    ys = [0.0]
    for day, value in points:
        ms = QDateTime(datetime.fromisoformat(day)).toMSecsSinceEpoch()
        series.append(float(ms), value)
        ys.append(value)
    chart.addSeries(series)
    x = QDateTimeAxis()
    span = 0
    if len(points) > 1:
        span = (datetime.fromisoformat(points[-1][0]) - datetime.fromisoformat(points[0][0])).days
    x.setFormat("MM.yyyy" if span > 150 else "dd.MM")
    x.setTickCount(4)
    y = QValueAxis()
    lo, hi = min(ys), max(ys)
    pad = max(1.0, (hi - lo) * 0.1)
    y.setRange(lo - pad, hi + pad)
    y.applyNiceNumbers()
    y.setLabelFormat("%.0f" if hi - lo > 8 else "%.1f")
    for axis in (x, y):
        _style_axis(axis)
    chart.addAxis(x, Qt.AlignBottom)
    chart.addAxis(y, Qt.AlignLeft)
    series.attachAxis(x)
    series.attachAxis(y)
    zero = QLineSeries()
    zpen = QPen(QColor(theme.MUTED))
    zpen.setWidth(1)
    zero.setPen(zpen)
    if points:
        first = QDateTime(datetime.fromisoformat(points[0][0])).toMSecsSinceEpoch()
        last = QDateTime(datetime.fromisoformat(points[-1][0])).toMSecsSinceEpoch()
        zero.append(float(first), 0.0)
        zero.append(float(last if last > first else first + 86400000), 0.0)
    chart.addSeries(zero)
    zero.attachAxis(x)
    zero.attachAxis(y)
    series.hovered.connect(lambda p, state: _tooltip(
        QDateTime.fromMSecsSinceEpoch(int(p.x())).toString("dd.MM.yyyy") + ": "
        + f"{p.y():+.2f}".replace(".", ",") + f" {unit}")
        if state else QToolTip.hideText())
    return view(chart)


def profit_bars(labels: list[str], values: list[float], title: str = "Wynik w miesiącach (jednostki)",
                unit: str = "j.") -> QChartView:
    """Słupki zysku (w górę) i straty (w dół) – po jednym na miesiąc."""
    chart = _base_chart(title)
    gain, loss = QBarSet("zysk"), QBarSet("strata")
    gain.setColor(QColor(theme.POSITIVE))
    loss.setColor(QColor(theme.NEGATIVE))
    for s in (gain, loss):
        s.setBorderColor(QColor(theme.SURFACE))
    for v in values:
        gain.append(max(v, 0.0))
        loss.append(min(v, 0.0))
    series = QStackedBarSeries()
    series.append(gain)
    series.append(loss)
    series.setBarWidth(min(0.5, 24 * max(1, len(values)) / 600))   # słupki najwyżej ~24 px
    chart.addSeries(series)
    x = QBarCategoryAxis()
    x.append(labels or ["–"])
    y = QValueAxis()
    lo, hi = min([0.0, *values]), max([0.0, *values])
    pad = max(1.0, (hi - lo) * 0.1)
    y.setRange(lo - pad, hi + pad)
    y.applyNiceNumbers()
    y.setLabelFormat("%.0f")
    for axis in (x, y):
        _style_axis(axis)
    chart.addAxis(x, Qt.AlignBottom)
    chart.addAxis(y, Qt.AlignLeft)
    series.attachAxis(x)
    series.attachAxis(y)
    series.hovered.connect(lambda state, idx, _set: _tooltip(
        f"{labels[idx]}: " + f"{values[idx]:+.2f}".replace(".", ",") + f" {unit}")
        if state and idx < len(values) else QToolTip.hideText())
    return view(chart)


def calibration_chart(bins: list[tuple[float, float, int]], title: str) -> QChartView:
    """Kalibracja: przewidywane (oś X) vs faktyczne (oś Y); przekątna = idealna kalibracja."""
    chart = _base_chart(title)
    diag = QLineSeries()
    dpen = QPen(QColor(theme.MUTED))
    dpen.setWidth(1)
    diag.setPen(dpen)
    diag.append(0, 0)
    diag.append(100, 100)
    line = QLineSeries()
    lpen = QPen(QColor(theme.ACCENT))
    lpen.setWidth(2)
    line.setPen(lpen)
    dots = QScatterSeries()
    dots.setColor(QColor(theme.ACCENT))
    dots.setBorderColor(QColor(theme.SURFACE))
    dots.setMarkerSize(10)
    for predicted, observed, n in bins:
        if n < MIN_BIN:   # przedziały z kilkoma typami to szum – pomijamy na wykresie (są w tabeli)
            continue
        line.append(100 * predicted, 100 * observed)
        dots.append(100 * predicted, 100 * observed)
    for s in (diag, line, dots):
        chart.addSeries(s)
    x, y = QValueAxis(), QValueAxis()
    for axis, name in ((x, "przewidywane %"), (y, "faktycznie %")):
        axis.setRange(0, 100)
        axis.setTickCount(6)
        axis.setLabelFormat("%.0f")
        axis.setTitleText(name)
        axis.setTitleBrush(QColor(theme.MUTED))
        _style_axis(axis)
    chart.addAxis(x, Qt.AlignBottom)
    chart.addAxis(y, Qt.AlignLeft)
    for s in (diag, line, dots):
        s.attachAxis(x)
        s.attachAxis(y)
    counts = {round(100 * p, 3): n for p, _, n in bins}
    dots.hovered.connect(lambda p, state: _tooltip(
        f"model {p.x():.0f}% → faktycznie {p.y():.0f}% (n={counts.get(round(p.x(), 3), '?')})")
        if state else QToolTip.hideText())
    return view(chart)


def xy_line_chart(points: list[tuple[float, float]], title: str, x_title: str, y_title: str,
                  x_percent: bool = True, highlight: tuple[float, float] | None = None) -> QChartView:
    chart = _base_chart(title)
    line = QLineSeries()
    pen = QPen(QColor(theme.ACCENT))
    pen.setWidth(2)
    line.setPen(pen)
    for xv, yv in points:
        line.append(QPointF(100 * xv if x_percent else xv, yv))
    chart.addSeries(line)
    x, y = QValueAxis(), QValueAxis()
    if points:
        xs = [100 * p[0] if x_percent else p[0] for p in points]
        ys = [p[1] for p in points]
        x.setRange(min(xs), max(xs))
        pad = max(1e-4, (max(ys) - min(ys)) * 0.15)
        y.setRange(min(ys) - pad, max(ys) + pad)
    y.setLabelFormat("%.3f")
    x.setLabelFormat("%.0f")
    for axis, name in ((x, x_title), (y, y_title)):
        axis.setTitleText(name)
        axis.setTitleBrush(QColor(theme.MUTED))
        _style_axis(axis)
    chart.addAxis(x, Qt.AlignBottom)
    chart.addAxis(y, Qt.AlignLeft)
    line.attachAxis(x)
    line.attachAxis(y)
    if highlight:
        dot = QScatterSeries()
        dot.setColor(QColor(theme.POSITIVE))
        dot.setBorderColor(QColor(theme.SURFACE))
        dot.setMarkerSize(12)
        dot.append(100 * highlight[0] if x_percent else highlight[0], highlight[1])
        chart.addSeries(dot)
        dot.attachAxis(x)
        dot.attachAxis(y)
    line.hovered.connect(lambda p, state: _tooltip(f"{p.x():.0f}% → {p.y():.4f}") if state else QToolTip.hideText())
    return view(chart)
