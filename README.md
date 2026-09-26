# TyperBot

Aplikacja desktopowa (Python + PySide6 + SQLite), która analizuje nadchodzące mecze piłkarskie,
szacuje prawdopodobieństwa modelem Poissona / Dixona-Colesa i układa kupon o zadanym kursie.

> **To narzędzie analityczne, nie gwarancja wygranej.** Bukmacher ma marżę, a w Polsce dochodzi
> 12% podatku od stawki. Większość kuponów ma ujemną wartość oczekiwaną i aplikacja pokazuje to
> wprost. Graj tylko za kwoty, których stratę akceptujesz. Pomoc przy problemach z hazardem:
> telefon zaufania 801 889 880, [Krajowe Centrum Przeciwdziałania Uzależnieniom](https://www.kcpu.gov.pl/).

## Stan prac

| Etap | Zakres | Stan |
|---|---|---|
| 1 | Źródła danych, pobieranie, baza SQLite, cache, limity API | **gotowy** |
| 2 | Model prognoz (Dixon-Coles) i backtest | następny |
| 3 | Ocena typów (marża, podatek, EV) i generator kuponu | – |
| 4 | Interfejs (5 zakładek), rejestr kuponów, statystyki | – |
| 5 | Kontrola budżetu, dopracowanie, plik .exe | – |

Do czasu interfejsu graficznego (etap 4) aplikację obsługuje się z wiersza poleceń.

## Uruchomienie na Windows

1. Zainstaluj **Python 3.12** z [python.org](https://www.python.org/downloads/windows/).
   W instalatorze zaznacz **„Add python.exe to PATH”**.
2. Otwórz **PowerShell** w folderze projektu i wykonaj:

   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   ```

   Jeśli PowerShell zablokuje aktywację skryptu, wykonaj raz:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
3. Sprawdź, że wszystko działa – **tryb demo** nie potrzebuje kluczy ani internetu
   (dane syntetyczne, tymczasowa baza):

   ```powershell
   python -m typerbot demo
   python -m typerbot demo --awaria the_odds_api   # symulacja awarii jednego źródła
   python -m typerbot demo --csv                   # z opcjonalnym importem CSV
   ```

4. Uruchom testy: `python -m pytest`.

## Klucze API

Wszystkie źródła mają darmowe plany. Klucz wpisujesz poleceniem `klucz` – trafia do
**Menedżera poświadczeń Windows** (biblioteka `keyring`), nigdy do bazy ani plików projektu.
Wpisywany klucz nie jest wyświetlany.

| Źródło | Jak zdobyć klucz | Polecenie |
|---|---|---|
| football-data.org | Rejestracja na <https://www.football-data.org/client/register>, klucz przychodzi mailem | `python -m typerbot klucz football_data_org` |
| API-Football | Rejestracja na <https://dashboard.api-football.com/register>, klucz w panelu („Account”) | `python -m typerbot klucz api_football` |
| The Odds API | Na <https://the-odds-api.com> wybierz darmowy plan „Starter”, klucz przychodzi mailem | `python -m typerbot klucz the_odds_api` |
| OddsPapi | Rejestracja na <https://oddspapi.io>, klucz w panelu po zalogowaniu | `python -m typerbot klucz oddspapi` |

Usunięcie klucza: `python -m typerbot klucz <źródło> --usun`.

## Praca z prawdziwymi danymi

```powershell
python -m typerbot sync            # pobranie danych (kolejne uruchomienia korzystają z cache)
python -m typerbot sync --force    # pominięcie cache
python -m typerbot status          # zużycie limitów każdego API i stan źródeł
python -m typerbot mecze --dni 3   # nadchodzące mecze z kursami Superbet (w nawiasie średnia rynkowa)
python -m typerbot druzyny --liga EKS   # jak nazwy drużyn z różnych źródeł zostały połączone
```

Baza i logi: `%LOCALAPPDATA%\TyperBot\` (`typerbot.db`, `typerbot.log`).

## Źródła danych i limity (plany darmowe)

| Źródło | Limit | Do czego służy |
|---|---|---|
| **API-Football** | 100 zapytań/dzień; plan darmowy obejmuje tylko sezony **2022–2024** | historia wyników wszystkich lig (1 zapytanie = cały sezon ligi), xG do backtestu |
| **football-data.org** | 10 zapytań/min; tylko **bieżący sezon** | terminarz i wyniki: Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Liga Mistrzów |
| **OddsPapi** | 250 zapytań/miesiąc | kursy **Superbet** (1 zapytanie na wiele lig), terminarz i wyniki Ekstraklasy, historia kursów |
| **The Odds API** | 500 kredytów/miesiąc (koszt = rynki × regiony) | kursy wielu bukmacherów → **średnia rynkowa**; BTTS i podwójna szansa dla kandydatów na kupon |
| football-data.co.uk (CSV) | bez limitu | **opcjonalnie, domyślnie wyłączone** – uzupełnienie historii |

Jak oszczędzamy limity:
- wszystko trafia do SQLite, zakończone sezony pobierane są tylko raz, odpowiedzi API są w cache;
- kursy pobierane są tylko dla lig z meczami w wybranym zakresie dat;
- budżety ustawiasz w opcjach: xG – 50 zapytań/dzień, wyniki z OddsPapi – 60/miesiąc,
  BTTS/podwójna szansa – 10 meczów/dzień;
- limity odczytywane są z nagłówków odpowiedzi API (`[nagł.]` w statusie), a gdy ich brak – liczone lokalnie (`[lok.]`).

Każde źródło to osobny moduł (`typerbot/data/sources/`). Awaria jednego źródła jest zapisywana
w jego statusie, a reszta synchronizacji działa dalej. Gdy źródło jest niedostępne, używane są
dane z cache.

### Znane ograniczenie planów darmowych

Żadne darmowe API nie udostępnia hurtowo **sezonu 2025/26**: API-Football kończy się na 2024/25,
a football-data.org ma tylko sezon bieżący. W efekcie na początku sezonu (do ok. listopada)
model ma mniej „ostatnich meczów” na drużynę. Historię kursów do backtestu da się pobrać
za darmo tylko w małych porcjach (OddsPapi: 1 zapytanie na mecz). Dlatego aplikacja zapisuje
kursy każdego obserwowanego meczu i z czasem sama buduje własną historię.

Jeśli chcesz uzupełnić lukę od razu, możesz włączyć import plików CSV z football-data.co.uk
(wyniki, strzały i kursy zamknięcia z wielu sezonów, bez klucza):
`python -m typerbot csv wlacz`.

## Struktura projektu

```
typerbot/
├── config/        ustawienia (SQLite), klucze (keyring), katalog lig
├── data/
│   ├── db.py, schema.py        SQLite (WAL) i migracje
│   ├── http.py, ratelimit.py   cache HTTP, ograniczanie zapytań
│   ├── quota.py                zużycie limitów i status źródeł
│   ├── teams.py, team_seeds.py dopasowanie nazw drużyn między źródłami
│   ├── repository.py           zapis meczów i kursów, łączenie meczów z kilku źródeł
│   └── sources/                football_data_org, api_football, oddspapi, the_odds_api, football_data_csv
├── services/sync.py            synchronizacja z izolacją błędów źródeł
├── demo/                       syntetyczny świat meczów i transport udający API
└── cli.py                      polecenia wiersza poleceń
tests/                          testy jednostkowe i integracyjne (pytest)
```
