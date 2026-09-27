"""Wybór lig: wyszukiwarka i drzewo „kraj → ligi” z zaznaczaniem całych krajów.

Lista jest budowana z danych: pokazujemy ligi, dla których są mecze w bazie (liczba nadchodzących
meczów obok nazwy). Przed pierwszym pobraniem danych – wszystkie ligi z katalogu.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from typerbot.data.repository import LeagueCounts
from typerbot.data.teams import normalize

CODE_ROLE = Qt.UserRole


class LeagueTree(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, counts_label: str = "Mecze"):
        super().__init__(parent)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Szukaj ligi lub kraju…")
        self.search.setClearButtonEnabled(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Kraj / liga", counts_label])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        self._leagues: dict[str, QTreeWidgetItem] = {}
        self._updating = False
        self.search.textChanged.connect(self._filter)
        self.tree.itemChanged.connect(self._item_changed)

    # -- dane ---------------------------------------------------------------------------------
    def set_leagues(self, rows: Iterable[LeagueCounts], checked: Iterable[str] | None = None,
                    only_with_data: bool = True) -> None:
        """`checked` = None – wszystkie zaznaczone."""
        rows = list(rows)
        with_data = [r for r in rows if r.upcoming or r.finished]
        shown = with_data if only_with_data and with_data else rows
        wanted = None if checked is None else set(checked)
        self._updating = True
        self.tree.clear()
        self._leagues.clear()
        countries: dict[str, QTreeWidgetItem] = {}
        for r in sorted(shown, key=lambda x: (x.league.country != "Polska", x.league.country, x.league.tier,
                                              x.league.name)):
            lg = r.league
            parent = countries.get(lg.country)
            if parent is None:
                parent = QTreeWidgetItem([lg.country, ""])
                parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                parent.setCheckState(0, Qt.Unchecked)
                countries[lg.country] = parent
                self.tree.addTopLevelItem(parent)
            item = QTreeWidgetItem([lg.name, str(r.upcoming) if r.upcoming else "–"])
            item.setData(0, CODE_ROLE, lg.code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if wanted is None or lg.code in wanted else Qt.Unchecked)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(0, f"{lg.country} – {lg.name} ({lg.code}); nadchodzące mecze: {r.upcoming}, "
                               f"zakończone w bazie: {r.finished}")
            parent.addChild(item)
            self._leagues[lg.code] = item
        for parent in countries.values():
            total = sum(int(parent.child(i).text(1)) for i in range(parent.childCount())
                        if parent.child(i).text(1).isdigit())
            parent.setText(1, str(total) if total else "–")
            parent.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self._updating = False
        self._filter(self.search.text())

    def codes(self) -> list[str]:
        return list(self._leagues)

    def checked_codes(self) -> list[str]:
        return [code for code, item in self._leagues.items() if item.checkState(0) == Qt.Checked]

    def all_checked(self) -> bool:
        return all(item.checkState(0) == Qt.Checked for item in self._leagues.values())

    def set_checked(self, codes: Iterable[str] | None) -> None:
        wanted = None if codes is None else set(codes)
        self._updating = True
        for code, item in self._leagues.items():
            item.setCheckState(0, Qt.Checked if wanted is None or code in wanted else Qt.Unchecked)
        self._updating = False
        self.changed.emit()

    def set_country(self, country: str, checked: bool) -> None:
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent.text(0) == country:
                parent.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)

    # -- zdarzenia -------------------------------------------------------------------------------
    def _item_changed(self, *_args) -> None:
        if not self._updating:
            self.changed.emit()

    def _filter(self, text: str) -> None:
        query = normalize(text) if text.strip() else ""
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            country_hit = not query or query in normalize(parent.text(0))
            visible = 0
            for j in range(parent.childCount()):
                child = parent.child(j)
                hit = country_hit or query in normalize(child.text(0)) or query in normalize(
                    child.data(0, CODE_ROLE) or "")
                child.setHidden(not hit)
                visible += hit
            parent.setHidden(visible == 0)
            parent.setExpanded(bool(query) and visible > 0)
