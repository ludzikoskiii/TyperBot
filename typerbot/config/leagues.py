"""Domyślny katalog lig z identyfikatorami w każdym źródle danych.

Lista jest kopiowana do tabeli `leagues` przy pierwszym uruchomieniu;
potem użytkownik może ją edytować w ustawieniach (włączać, wyłączać, dodawać).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    code: str
    name: str
    country: str
    is_cup: bool = False
    fd_org_code: str | None = None       # football-data.org
    api_football_id: int | None = None   # API-Football
    odds_api_key: str | None = None      # The Odds API
    oddspapi_id: int | None = None       # OddsPapi (tournamentId)
    fdcuk_code: str | None = None        # football-data.co.uk (opcjonalny import CSV)
    fdcuk_format: str | None = None      # 'main' (plik na sezon) lub 'extra' (jeden plik)
    enabled: bool = True
    sort_order: int = 0
    oddspapi_slug: str = ""              # 'kraj/liga' do weryfikacji tournamentId przez /tournaments


DEFAULT_LEAGUES: list[League] = [
    League("EKS", "Ekstraklasa", "Polska", api_football_id=106, odds_api_key="soccer_poland_ekstraklasa",
           oddspapi_id=202, fdcuk_code="POL", fdcuk_format="extra", sort_order=1, oddspapi_slug="poland/ekstraklasa"),
    League("PL", "Premier League", "Anglia", fd_org_code="PL", api_football_id=39, odds_api_key="soccer_epl",
           oddspapi_id=17, fdcuk_code="E0", fdcuk_format="main", sort_order=2, oddspapi_slug="england/premier-league"),
    League("PD", "La Liga", "Hiszpania", fd_org_code="PD", api_football_id=140, odds_api_key="soccer_spain_la_liga",
           oddspapi_id=8, fdcuk_code="SP1", fdcuk_format="main", sort_order=3, oddspapi_slug="spain/laliga"),
    League("BL1", "Bundesliga", "Niemcy", fd_org_code="BL1", api_football_id=78,
           odds_api_key="soccer_germany_bundesliga", oddspapi_id=35, fdcuk_code="D1", fdcuk_format="main",
           sort_order=4, oddspapi_slug="germany/bundesliga"),
    League("SA", "Serie A", "Włochy", fd_org_code="SA", api_football_id=135, odds_api_key="soccer_italy_serie_a",
           oddspapi_id=23, fdcuk_code="I1", fdcuk_format="main", sort_order=5, oddspapi_slug="italy/serie-a"),
    League("FL1", "Ligue 1", "Francja", fd_org_code="FL1", api_football_id=61, odds_api_key="soccer_france_ligue_one",
           oddspapi_id=34, fdcuk_code="F1", fdcuk_format="main", sort_order=6, oddspapi_slug="france/ligue-1"),
    League("CL", "Liga Mistrzów", "Europa", is_cup=True, fd_org_code="CL", api_football_id=2,
           odds_api_key="soccer_uefa_champs_league", oddspapi_id=7, sort_order=7,
           oddspapi_slug="europe/uefa-champions-league"),
    # Dodatkowe ligi – domyślnie wyłączone, do włączenia w ustawieniach.
    League("ELC", "Championship", "Anglia", fd_org_code="ELC", api_football_id=40, odds_api_key="soccer_efl_champ",
           oddspapi_id=18, fdcuk_code="E1", fdcuk_format="main", enabled=False, sort_order=8,
           oddspapi_slug="england/championship"),
    League("DED", "Eredivisie", "Holandia", fd_org_code="DED", api_football_id=88,
           odds_api_key="soccer_netherlands_eredivisie", oddspapi_id=37, fdcuk_code="N1", fdcuk_format="main",
           enabled=False, sort_order=9, oddspapi_slug="netherlands/eredivisie"),
    League("PPL", "Primeira Liga", "Portugalia", fd_org_code="PPL", api_football_id=94,
           odds_api_key="soccer_portugal_primeira_liga", oddspapi_id=238, fdcuk_code="P1", fdcuk_format="main",
           enabled=False, sort_order=10, oddspapi_slug="portugal/liga-portugal"),
]


def season_of(year: int, month: int) -> int:
    """Rok rozpoczęcia sezonu (sezon 2026/27 -> 2026). Sezon startuje w lipcu."""
    return year if month >= 7 else year - 1
