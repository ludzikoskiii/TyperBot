"""Katalog lig i ich identyfikatory w źródłach danych (wszystkie bez klucza i rejestracji).

  * football-data.co.uk – 22 ligi 'main' (plik na sezon: wyniki, kursy, statystyki; nadchodzące
    mecze z kursami w fixtures.csv) i 16 lig 'extra' (jeden plik ze wszystkimi sezonami: wyniki
    i kursy 1X2; nadchodzące mecze w new_league_fixtures.csv);
  * openfootball (football.json, domena publiczna) – terminarz całego sezonu z wyprzedzeniem
    i wyniki; kod pliku, np. 'en.1';
  * OpenLigaDB – ligi niemieckie (terminarz i wyniki na bieżąco); skrót, np. 'bl3';
  * international_results – mecze reprezentacji (liga 'INT');
  * inne dyscypliny: nflverse (NFL – terminarz i kursy), MLB Stats API (baseball), OpenLigaDB
    (piłka ręczna, hokej i inne ligi dopisywane automatycznie, gdy są w serwisie).

Katalog jest kopiowany do tabeli `leagues`; ligi, które pojawią się w danych, a nie ma ich
w katalogu (np. nowa liga w pliku football-data.co.uk), są dopisywane automatycznie.
Lista w interfejsie pokazuje ligi, dla których są dane.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class League:
    code: str
    name: str
    country: str
    is_cup: bool = False
    fdcuk_code: str | None = None        # football-data.co.uk: kod pliku (np. E0, POL)
    fdcuk_format: str | None = None      # 'main' (plik na sezon) lub 'extra' (jeden plik)
    enabled: bool = True
    sort_order: int = 0
    openfootball: str | None = None      # football.json: kod pliku (np. 'en.1')
    openligadb: str | None = None        # OpenLigaDB: skrót ligi (np. 'bl3')
    season_style: str = "split"          # 'split' (sezon 2026/27) | 'calendar' (sezon = rok)
    timezone: str = "Europe/London"      # strefa godzin w terminarzu openfootball
    tier: int = 1                        # poziom rozgrywek w kraju (1 = najwyższy)
    national: bool = False               # reprezentacje (mecze często na neutralnym terenie)
    sport: str = "football"              # dyscyplina (typerbot.config.sports)
    feed: str | None = None              # źródło całej ligi spoza piłki nożnej: 'nflverse' | 'mlb'

    def season_at(self, when: datetime) -> int:
        """Sezon ligi dla daty: rok rozpoczęcia (split) albo rok kalendarzowy."""
        return when.year if self.season_style == "calendar" else season_of(when.year, when.month)

    def season_label(self, season: int) -> str:
        return str(season) if self.season_style == "calendar" else f"{season}/{(season + 1) % 100:02d}"


def _main(code, name, country, fd, of=None, tz="Europe/London", tier=1, **kw) -> League:
    return League(code, name, country, fdcuk_code=fd, fdcuk_format="main", openfootball=of, timezone=tz,
                  tier=tier, **kw)


def _extra(code, name, country, fd, of=None, tz="UTC", style="split", **kw) -> League:
    return League(code, name, country, fdcuk_code=fd, fdcuk_format="extra", openfootball=of, timezone=tz,
                  season_style=style, **kw)


LON, BER, MAD, ROM, PAR = "Europe/London", "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Paris"

DEFAULT_LEAGUES: list[League] = [
    # --- Polska ---
    _extra("EKS", "Ekstraklasa", "Polska", "POL", tz="Europe/Warsaw"),
    # --- Anglia ---
    _main("PL", "Premier League", "Anglia", "E0", "en.1", LON),
    _main("ELC", "Championship", "Anglia", "E1", "en.2", LON, tier=2),
    _main("E2", "League One", "Anglia", "E2", "en.3", LON, tier=3),
    _main("E3", "League Two", "Anglia", "E3", "en.4", LON, tier=4),
    _main("EC", "National League", "Anglia", "EC", None, LON, tier=5),
    # --- Szkocja ---
    _main("SC0", "Premiership", "Szkocja", "SC0", "sco.1", LON),
    _main("SC1", "Championship", "Szkocja", "SC1", None, LON, tier=2),
    _main("SC2", "League One", "Szkocja", "SC2", None, LON, tier=3),
    _main("SC3", "League Two", "Szkocja", "SC3", None, LON, tier=4),
    # --- Niemcy ---
    _main("BL1", "Bundesliga", "Niemcy", "D1", "de.1", BER, openligadb="bl1"),
    _main("D2", "2. Bundesliga", "Niemcy", "D2", "de.2", BER, tier=2, openligadb="bl2"),
    League("BL3", "3. Liga", "Niemcy", openfootball="de.3", openligadb="bl3", timezone=BER, tier=3),
    League("DFB", "Puchar Niemiec", "Niemcy", is_cup=True, openligadb="dfb", timezone=BER),
    # --- Hiszpania, Włochy, Francja ---
    _main("PD", "La Liga", "Hiszpania", "SP1", "es.1", MAD),
    _main("SP2", "Segunda División", "Hiszpania", "SP2", "es.2", MAD, tier=2),
    _main("SA", "Serie A", "Włochy", "I1", "it.1", ROM),
    _main("I2", "Serie B", "Włochy", "I2", "it.2", ROM, tier=2),
    _main("FL1", "Ligue 1", "Francja", "F1", "fr.1", PAR),
    _main("F2", "Ligue 2", "Francja", "F2", "fr.2", PAR, tier=2),
    # --- pozostała Europa ---
    _main("DED", "Eredivisie", "Holandia", "N1", "nl.1", "Europe/Amsterdam"),
    _main("B1", "Pro League", "Belgia", "B1", "be.1", "Europe/Brussels"),
    _main("PPL", "Primeira Liga", "Portugalia", "P1", "pt.1", "Europe/Lisbon"),
    _main("T1", "Süper Lig", "Turcja", "T1", "tr.1", "Europe/Istanbul"),
    _main("G1", "Super League", "Grecja", "G1", "gr.1", "Europe/Athens"),
    _extra("AUT", "Bundesliga", "Austria", "AUT", "at.1", "Europe/Vienna"),
    League("AT2", "2. Liga", "Austria", openfootball="at.2", timezone="Europe/Vienna", tier=2),
    _extra("SWZ", "Super League", "Szwajcaria", "SWZ", "ch.1", "Europe/Zurich"),
    _extra("DNK", "Superliga", "Dania", "DNK", None, "Europe/Copenhagen"),
    _extra("NOR", "Eliteserien", "Norwegia", "NOR", None, "Europe/Oslo", style="calendar"),
    _extra("SWE", "Allsvenskan", "Szwecja", "SWE", None, "Europe/Stockholm", style="calendar"),
    _extra("FIN", "Veikkausliiga", "Finlandia", "FIN", None, "Europe/Helsinki", style="calendar"),
    _extra("IRL", "Premier Division", "Irlandia", "IRL", None, "Europe/Dublin", style="calendar"),
    _extra("ROU", "Liga I", "Rumunia", "ROU", None, "Europe/Bucharest"),
    _extra("RUS", "Priemjer-Liga", "Rosja", "RUS", None, "Europe/Moscow"),
    # --- Ameryki i Azja ---
    _extra("BRA", "Série A", "Brazylia", "BRA", "br.1", "America/Sao_Paulo", style="calendar"),
    League("BR2", "Série B", "Brazylia", openfootball="br.2", timezone="America/Sao_Paulo", tier=2,
           season_style="calendar"),
    _extra("ARG", "Liga Profesional", "Argentyna", "ARG", "ar.1", "America/Argentina/Buenos_Aires",
           style="calendar"),
    League("COL", "Primera A", "Kolumbia", openfootball="co.1", timezone="America/Bogota", season_style="calendar"),
    _extra("MEX", "Liga MX", "Meksyk", "MEX", None, "America/Mexico_City"),
    _extra("USA", "MLS", "USA", "USA", "mls", "America/New_York", style="calendar"),
    _extra("JPN", "J1 League", "Japonia", "JPN", "jp.1", "Asia/Tokyo", style="calendar"),
    _extra("CHN", "Super League", "Chiny", "CHN", "cn.1", "Asia/Shanghai", style="calendar"),
    # --- puchary i reprezentacje ---
    League("LIB", "Copa Libertadores", "Ameryka Płd.", is_cup=True, openfootball="copa.l",
           timezone="America/Sao_Paulo", season_style="calendar"),
    League("CL", "Liga Mistrzów", "Europa", is_cup=True),
    League("INT", "Reprezentacje", "Świat", timezone="UTC", season_style="calendar", national=True),
    # --- inne dyscypliny ---
    League("NFL", "NFL", "USA", timezone="America/New_York", sport="american_football", feed="nflverse"),
    League("MLB", "MLB", "USA", timezone="America/New_York", season_style="calendar", sport="baseball", feed="mlb"),
]
DEFAULT_LEAGUES = [replace(lg, sort_order=i) for i, lg in enumerate(DEFAULT_LEAGUES, start=1)]

# Kraje w plikach football-data.co.uk 'extra' (kolumna Country) → kod pliku.
FDCUK_EXTRA_COUNTRIES = {
    "Poland": "POL", "Argentina": "ARG", "Austria": "AUT", "Brazil": "BRA", "China": "CHN", "Denmark": "DNK",
    "Finland": "FIN", "Ireland": "IRL", "Japan": "JPN", "Mexico": "MEX", "Norway": "NOR", "Romania": "ROU",
    "Russia": "RUS", "Sweden": "SWE", "Switzerland": "SWZ", "USA": "USA",
}


def season_of(year: int, month: int) -> int:
    """Rok rozpoczęcia sezonu (sezon 2026/27 -> 2026). Sezon startuje w lipcu."""
    return year if month >= 7 else year - 1
