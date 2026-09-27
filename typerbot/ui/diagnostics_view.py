"""Okna diagnostyki: lejek generatora (źródła → filtry → kupony) i lista problemów ze źródeł."""

from __future__ import annotations

import html

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from typerbot.data.errors import STATE_LABELS
from typerbot.fmt import plural
from typerbot.services.diagnostics import STEP_LABELS, Diagnosis, Problem, _date, day_label, when_label
from typerbot.ui import theme
from typerbot.ui.widgets import NumItem, ProbabilityDelegate, hbox, label, make_table, prob_item, text_item

STATE_COLORS = {"ok": theme.POSITIVE, "idle": theme.MUTED, "offline": theme.WARNING, "error": theme.NEGATIVE,
                "disabled": theme.MUTED}


def reason_html(diag: Diagnosis) -> str:
    """Powód braku kuponu i podpowiedzi – do wyświetlenia w miejscu kuponów."""
    parts = [f"<p style='font-size:12pt'><b>{html.escape(r)}</b></p>" for r in diag.reasons]
    if diag.hints:
        parts.append("<p>Co zmienić:</p><ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in diag.hints) + "</ul>")
    return "".join(parts)


class DiagnosisPanel(QWidget):
    def __init__(self, diag: Diagnosis, parent=None):
        super().__init__(parent)
        self.diag = diag
        head = label(diag.headline(), "title" if diag.ok else "negative", wrap=True)
        period = label(f"Zakres dat: {diag.period()} · dane z: {when_label(diag.data_as_of)}", "muted")

        self.sources = make_table(["Źródło", "Mecze w zakresie", "Z kursami", "Dni (mecze)", "Wszystkie nadchodzące",
                                   "Dane z", "Stan"], stretch=3, sortable=False)
        for s in diag.sources:
            r = self.sources.rowCount()
            self.sources.insertRow(r)
            self.sources.setItem(r, 0, text_item(s.label))
            self.sources.setItem(r, 1, NumItem(str(s.matches), s.matches))
            self.sources.setItem(r, 2, NumItem(str(s.with_odds), s.with_odds))
            days = ", ".join(f"{day_label(_date(d))}: {n}" for d, n in sorted(s.days.items())) or "–"
            self.sources.setItem(r, 3, text_item(days, tooltip=days))
            self.sources.setItem(r, 4, NumItem(str(s.upcoming), s.upcoming))
            self.sources.setItem(r, 5, text_item(when_label(s.last_ok)))
            state = STATE_LABELS.get(s.state, s.state)
            if s.message and s.state != "ok" and s.message != state:
                state += f" – {s.message}"
            self.sources.setItem(r, 6, text_item(state, STATE_COLORS.get(s.state), tooltip=s.message))
        self.sources.setFixedHeight(34 + 30 * max(1, len(diag.sources)))

        self.days = make_table(["Dzień", "Mecze (wszystkie ligi)", "W wybranych ligach", "Z kursem bukmachera",
                                "Ligi z meczami"], stretch=0, sortable=False)
        for d in diag.days:
            r = self.days.rowCount()
            self.days.insertRow(r)
            self.days.setItem(r, 0, text_item(day_label(_date(d.day))))
            for c, v in enumerate((d.matches, d.selected, d.with_odds, d.leagues), start=1):
                self.days.setItem(r, c, NumItem(str(v), v))
        self.days.setFixedHeight(34 + 30 * max(1, len(diag.days)))

        self.stages = make_table(["Filtr", "Mecze", "Typy", "Zostało"], stretch=0, sortable=False)
        first = max((st.matches for st in diag.stages), default=0)
        for st in diag.stages:
            r = self.stages.rowCount()
            self.stages.insertRow(r)
            self.stages.setItem(r, 0, text_item(st.label))
            self.stages.setItem(r, 1, NumItem(str(st.matches), st.matches))
            self.stages.setItem(r, 2, NumItem("" if st.selections is None else str(st.selections), st.selections))
            share = prob_item(st.matches / first if first else 0.0)
            self.stages.setItem(r, 3, share)
        self.stages.setItemDelegateForColumn(3, ProbabilityDelegate(self.stages))
        self.stages.setColumnWidth(3, 160)
        self.stages.setFixedHeight(34 + 30 * max(1, len(diag.stages)))

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(period)
        if diag.hints:
            lay.addWidget(label("Co zmienić:\n" + "\n".join(f"• {h}" for h in diag.hints), wrap=True))
        lay.addWidget(label("Ile meczów przyszło z każdego źródła (wybrane ligi) i z których dni", "muted"))
        lay.addWidget(self.sources)
        lay.addWidget(label("Mecze w kolejnych dniach zakresu", "muted"))
        lay.addWidget(self.days)
        lay.addWidget(label("Ile zostaje po kolejnych filtrach generatora", "muted"))
        lay.addWidget(self.stages)
        if diag.notes:
            lay.addWidget(label("\n".join(f"• {n}" for n in diag.notes), "muted", wrap=True))
        if diag.problems:
            lay.addWidget(label(f"Problemy ze źródeł przy ostatniej synchronizacji ({len(diag.problems)}):", "warning"))
            lay.addWidget(label("\n".join(f"• {p.text()}" + (f" → {p.hint}" if p.hint else "")
                                          for p in diag.problems), wrap=True))
        lay.addStretch(1)


class DiagnosisDialog(QDialog):
    def __init__(self, diag: Diagnosis, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostyka generatora")
        self.resize(900, 720)
        self.diag = diag
        copy = QPushButton("Kopiuj jako tekst")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(diag.to_text()))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Zamknij")
        buttons.rejected.connect(self.reject)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(DiagnosisPanel(diag))
        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addLayout(hbox(copy, None, buttons))


class ProblemsDialog(QDialog):
    """Lista problemów z ostatniej synchronizacji z podpowiedzią, co zrobić."""

    def __init__(self, problems: list[Problem], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Problemy ze źródłami danych")
        self.resize(980, 420)
        self.table = make_table(["Źródło", "Co pobierano", "Ligi", "Stan", "Szczegóły", "Co zrobić"], stretch=4,
                                sortable=False)
        for p in problems:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, text_item(p.label))
            self.table.setItem(r, 1, text_item(STEP_LABELS.get(p.step, p.step)))
            self.table.setItem(r, 2, text_item(", ".join(p.leagues) or "wszystkie"))
            self.table.setItem(r, 3, text_item(STATE_LABELS.get(p.state, p.state), STATE_COLORS.get(p.state)))
            self.table.setItem(r, 4, text_item(p.message, tooltip=p.message))
            self.table.setItem(r, 5, text_item(p.hint, tooltip=p.hint))
        text = "\n".join(f"• {p.text()}" + (f" → {p.hint}" if p.hint else "") for p in problems)
        copy = QPushButton("Kopiuj jako tekst")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Zamknij")
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{plural(len(problems), 'problem', 'problemy', 'problemów')} przy ostatniej "
                            "synchronizacji. Jedno źródło z problemem nie blokuje pozostałych.", "muted", wrap=True))
        lay.addWidget(self.table, 1)
        lay.addLayout(hbox(copy, None, buttons))
        self.table.setWordWrap(True)
        self.table.resizeRowsToContents()
