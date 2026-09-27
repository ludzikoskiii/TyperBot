"""Warianty nazw klubów z openfootball/clubs (domena publiczna, CC0) – do ujednolicania nazw.

Plik kraju (np. europe/england/eng.clubs.txt) ma format Football.TXT:

    Manchester United FC, 1878, @ Old Trafford, Manchester
      | Man United | Man Utd | Manchester United

Pierwsza nazwa w wierszu to nazwa główna, wiersze z „|” to warianty. Dzięki temu
„Man United” (football-data.co.uk) i „Manchester United FC” (openfootball) to ta sama drużyna.
"""

from __future__ import annotations

from typerbot.data.http import HttpResponse
from typerbot.data.sources.base import ApiSource

# Kraj (jak w katalogu lig) → plik w repozytorium openfootball/clubs.
CLUB_FILES: dict[str, str | tuple[str, ...]] = {
    "Anglia": ("europe/england/eng", "europe/wales/wal"),   # kluby walijskie grają w ligach angielskich
    "Szkocja": "europe/scotland/sco", "Niemcy": "europe/germany/de",
    "Hiszpania": "europe/spain/es", "Włochy": "europe/italy/it", "Francja": "europe/france/fr",
    "Holandia": "europe/netherlands/nl", "Belgia": "europe/belgium/be", "Portugalia": "europe/portugal/pt",
    "Turcja": "europe/turkey/tr", "Grecja": "europe/greece/gr", "Polska": "europe/poland/pl",
    "Austria": "europe/austria/at", "Szwajcaria": "europe/switzerland/ch", "Dania": "europe/denmark/dk",
    "Norwegia": "europe/norway/no", "Szwecja": "europe/sweden/se", "Finlandia": "europe/finland/fi",
    "Irlandia": "europe/ireland/ie", "Rumunia": "europe/romania/ro", "Rosja": "europe/russia/ru",
    "Argentyna": "south-america/argentina/ar", "Brazylia": "south-america/brazil/br",
    "Kolumbia": "south-america/colombia/co", "Meksyk": "north-america/mexico/mx",
    "USA": "north-america/united-states/us", "Japonia": "asia/japan/jp", "Chiny": "asia/china/cn",
}


class ClubNames(ApiSource):
    name = "club_names"
    label = "openfootball/clubs"
    base_url = "https://raw.githubusercontent.com/openfootball/clubs/master"
    requires_key = False
    per_minute = 30

    def decode(self, resp: HttpResponse) -> str:
        return resp.text()

    def check_response(self, resp: HttpResponse) -> str:
        if resp.status == 404:        # brak listy dla kraju – nic nie szkodzi (dopasowanie działa i bez niej)
            return ""
        return super().check_response(resp)

    def country(self, country: str, *, ttl: float) -> list[tuple[str, list[str]]]:
        paths = CLUB_FILES.get(country) or ()
        out: list[tuple[str, list[str]]] = []
        for path in (paths,) if isinstance(paths, str) else paths:
            out += parse_clubs(self.request(f"/{path}.clubs.txt", ttl=ttl))
        return out


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_clubs(text: str) -> list[tuple[str, list[str]]]:
    """[(nazwa główna, [warianty])] – warianty bez nazwy głównej."""
    clubs: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line or line.startswith("="):
            continue
        if line.startswith("|"):
            if clubs:
                clubs[-1][1].extend(v.strip() for v in line.strip("|").split("|") if v.strip())
            continue
        if raw[:1].isspace():         # wcięty wiersz bez „|” (np. adres) – pomijamy
            continue
        name = line.split(",", 1)[0].strip()
        if name and not name.startswith("@"):
            clubs.append((name, []))
    return clubs
