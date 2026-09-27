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
| 2 | Model prognoz (Dixon-Coles), backtest, strojenie parametrów | **gotowy** |
| 3 | Ocena typów (marża, podatek, EV) i generator kuponu | **gotowy** |
| 4 | Interfejs (5 zakładek), rejestr kuponów, statystyki | **gotowy** |
| 5 | Kontrola budżetu, dopracowanie, plik .exe | **gotowy** |

## Uruchomienie na Windows

1. Zainstaluj **Python 3.11 lub nowszy (64-bitowy)** z [python.org](https://www.python.org/downloads/windows/).
   W instalatorze zaznacz **„Add python.exe to PATH”**. Najlepiej trzymaj folder projektu poza OneDrive
   (np. `C:\TyperBot`) – biblioteki zajmują kilkaset MB i OneDrive próbowałby je synchronizować.
2. Dwuklik na **`instaluj.bat`** (jednorazowo, kilka minut) – sam znajdzie Pythona, utworzy środowisko
   `.venv` i zainstaluje biblioteki. To samo ręcznie w **PowerShell** w folderze projektu:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   ```

   Jeśli PowerShell zablokuje aktywację skryptu, wykonaj raz:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
3. Uruchom aplikację dwuklikiem na **`uruchom.bat`** (start bez okna konsoli) albo w PowerShell
   po aktywacji środowiska:

   ```powershell
   python -m typerbot              # interfejs graficzny
   python -m typerbot gui --demo   # interfejs w trybie demo – bez kluczy i internetu (dane syntetyczne)
   ```

4. Wpisz klucze API w zakładce **Ustawienia** (opis niżej) i kliknij **Odśwież dane**.
   **Pierwsza synchronizacja trwa kilka minut** – pobiera 7 sezonów historii (pliki CSV), sezony
   2022–2024 z API-Football i xG w ramach dziennego limitu. Postęp widać w pasku stanu na dole;
   kolejne odświeżenia trwają kilka sekund.
5. W zakładce **Statystyki → Backtest modelu** kliknij **Strojenie parametrów**, a potem
   **Zapisz najlepsze ustawienia** – model i udział modelu w prognozie zostaną dobrane do Twoich danych.

   Tryb demo w wierszu poleceń (raport tekstowy):

   ```powershell
   python -m typerbot demo
   python -m typerbot demo --awaria the_odds_api   # symulacja awarii jednego źródła
   python -m typerbot demo --backtest              # + prognozy i backtest modelu na danych demo
   ```

Testy: `python -m pytest` (ok. 3 minuty; obejmują też interfejs).

## Plik .exe (bez instalowania Pythona)

Na komputerze z Pythonem uruchom **`zbuduj_exe.bat`** – zbuduje folder `dist\TyperBot` z plikiem
`TyperBot.exe` (ok. 300 MB, bo zawiera Pythona, Qt i biblioteki obliczeniowe) i od razu uruchomi
test `TyperBot.exe --self-test`. Folder można skopiować na inny komputer z Windows 10/11.
`TyperBot.exe --demo` otwiera tryb demo. Dane i ustawienia są w `%LOCALAPPDATA%\TyperBot\`
– wspólne dla `.exe` i uruchamiania z Pythona.

## Interfejs

| Zakładka | Co zawiera |
|---|---|
| **Mecze** | nadchodzące mecze z prognozą 1 / X / 2 / >2,5 / BTTS (paski), liczba typów value; po kliknięciu meczu – wszystkie typy (prognoza, model, rynek, kurs, implikowane, EV, EV po podatku; value na zielono) i opis: forma, średnie, xG, bilans spotkań |
| **Generator kuponu** | kurs docelowy i tolerancja, zakres dat (dziś / jutro / X dni / własny), liczba zdarzeń, min. prawdopodobieństwo, tryb, ligi, rynki, stawka → 3 kupony z kursem przed/po podatku, szansą trafienia (prognoza, model, rynek), EV i wygraną; uzasadnienie każdego typu; **Wymień zdarzenie…**, **Zmień kurs…** (kurs z oferty), **Usuń zdarzenie**, **Zapisz jako postawiony…** |
| **Moje kupony** | rejestr postawionych kuponów, automatyczne rozliczanie po meczach, szczegóły z wynikami, ręczne rozliczenie (np. wcześniejsza wypłata) |
| **Statystyki** | bilans, ROI, trafność, krzywa bilansu, wynik w miesiącach, podział na rynki i ligi; podzakładka **Backtest modelu** ze skutecznością, kalibracją (wykres), wynikiem finansowym, mieszanką model + rynek i **strojeniem** parametrów |
| **Ustawienia** | klucze API, ligi (włączanie, dodawanie), rynki, model, kursy, podatek, pobieranie danych, budżet, limity API, dopasowanie nazw drużyn |

Pasek stanu pokazuje każde źródło danych (zielona kropka = OK, w podpowiedzi szczegóły i pozostały limit).
Dane odświeżają się w tle przy starcie i co 3 godziny (przycisk **Odśwież dane** – od razu); przy okazji
rozliczają się zakończone kupony. Wszystkie obliczenia i pobieranie działają w tle – okno się nie zawiesza.
W „Moje kupony” jest **Eksportuj do CSV…** (plik otwiera się w polskim Excelu).

## Kontrola budżetu

- **Miesięczny limit stawek** ustawiasz w *Ustawienia → Budżet* (domyślnie 200 zł, 0 = bez limitu)
  razem z progiem ostrzeżenia (domyślnie 80% limitu).
- W prawym górnym rogu okna stale widać **wydatki i bilans bieżącego miesiąca** z paskiem wykorzystania
  limitu: zielony, pomarańczowy od progu ostrzeżenia, czerwony po przekroczeniu. W podpowiedzi: kwota
  w grze, liczba kuponów, ile zostało do limitu.
- Zapis kuponu, który przekroczy limit, wymaga **potwierdzenia**; po przekroczeniu nad zakładkami
  pojawia się czerwony komunikat.
- Miesiąc liczony jest w czasie polskim: *wydano* = stawki kuponów postawionych w miesiącu (także
  w grze), *wypłaty* = wypłaty rozliczone w miesiącu, *bilans* = wypłaty − wydano.
- W wierszu poleceń: `python -m typerbot budzet` (podsumowanie), `python -m typerbot budzet --limit 300`.

## Klucze API

Wszystkie źródła mają darmowe plany. Klucz wpisujesz w **Ustawieniach** (albo poleceniem `klucz`) – trafia do
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
python -m typerbot prognozy --dni 3     # prognozy modelu dla nadchodzących meczów
python -m typerbot backtest             # test modelu na 3 ostatnich zakończonych sezonach
python -m typerbot backtest --ligi PL,EKS --sezony 2023,2024,2025 --tryb value --kurs 3
python -m typerbot strojenie --zapisz   # dobór parametrów modelu na Twojej historii
python -m typerbot typy --dni 3 --value # ocena typów: prognoza, kurs, implikowane, EV (★ = value)
python -m typerbot kupon                # 3 kupony wg ustawień (domyślnie kurs 5,00 ±10%, 3 dni)
python -m typerbot kupon --jutro --kurs 3 --tryb value --ligi PL,EKS --rynki 1X2,DC --stawka 20
python -m typerbot kupon --wymien A2    # zamienniki dla 2. zdarzenia kuponu A
python -m typerbot kupon --wymien A2 --na 3        # wymiana i przeliczenie kuponu
python -m typerbot kupon --kurs-reczny A2=1,95     # kurs z oferty bukmachera
python -m typerbot kupon --dociagnij    # najpierw kursy BTTS/podwójnej szansy dla najlepszych meczów
```

Baza i logi: `%LOCALAPPDATA%\TyperBot\` (`typerbot.db`, `typerbot.log`).

## Źródła danych i limity (plany darmowe)

| Źródło | Limit | Do czego służy |
|---|---|---|
| **API-Football** | 100 zapytań/dzień; plan darmowy obejmuje tylko sezony **2022–2024** | historia wyników wszystkich lig (1 zapytanie = cały sezon ligi), xG do backtestu |
| **football-data.org** | 10 zapytań/min; tylko **bieżący sezon** | terminarz i wyniki: Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Liga Mistrzów |
| **OddsPapi** | 250 zapytań/miesiąc | kursy **Superbet** (1 zapytanie na wiele lig), terminarz i wyniki Ekstraklasy, historia kursów |
| **The Odds API** | 500 kredytów/miesiąc (koszt = rynki × regiony) | kursy wielu bukmacherów → **średnia rynkowa**; BTTS i podwójna szansa dla kandydatów na kupon |
| **football-data.co.uk** (pliki CSV) | bez limitu | wyniki, strzały i kursy (przedmeczowe i zamknięcia) z 7 sezonów – historia do modelu i **backtestu z kursami**, w tym sezon 2025/26 |

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

Żadne darmowe API nie udostępnia hurtowo **sezonu 2025/26** (API-Football kończy się na 2024/25,
football-data.org ma tylko sezon bieżący), a historię kursów da się pobrać z API tylko w małych
porcjach. Dlatego domyślnie włączony jest import plików CSV z football-data.co.uk – uzupełnia
lukę i daje historyczne kursy do backtestu. Wyłączenie: `python -m typerbot csv wylacz`.
Niezależnie od tego aplikacja zapisuje kursy każdego obserwowanego meczu i z czasem buduje
własną historię.

## Model prognoz

**Dixon-Coles** – liczba goli każdej drużyny ma rozkład Poissona zależny od:
siły ataku i obrony obu drużyn, przewagi własnego boiska i ogólnego poziomu bramek w lidze.
Korekta ρ poprawia prawdopodobieństwa wyników 0:0, 1:0, 0:1 i 1:1.

- **Okno danych:** N ostatnich meczów każdej drużyny (domyślnie 20).
- **Wygaszanie:** mecz sprzed „półokresu” (domyślnie 180 dni) waży o połowę mniej.
- **xG:** gdy jest dostępne, cel dopasowania to mieszanka bramek i xG (domyślnie 50/50).
- **Regularyzacja:** siła drużyn jest łagodnie ściągana do średniej ligi (domyślnie 10).
  Bez tego model „wierzy” w przypadkowe serie i jest zbyt pewny siebie – backtest to pokazał
  (przy słabej regularyzacji typy „70–80%” trafiały w ok. 54%).
- **„Mało danych”:** mniej niż 6 meczów w ostatnim roku albo beniaminek bez historii w lidze;
  beniaminek startuje z siłą nieco poniżej średniej ligi. Takie drużyny domyślnie nie trafiają na kupon.
- **Liga Mistrzów:** wspólny model wszystkich lig z parametrem siły każdej ligi, szacowanym z meczów
  pucharowych. Mecz drużyn z różnych lig jest oznaczony jako „niższa pewność”.

Z macierzy wyników (0–10 bramek) liczone są wszystkie rynki: 1X2, podwójna szansa,
powyżej/poniżej 2,5 i obie strzelą.

## Ocena typów i generator kuponu

Dla każdego typu (1X2, podwójna szansa, powyżej/poniżej 2,5, obie strzelą) aplikacja pokazuje:

| Kolumna | Znaczenie |
|---|---|
| Model | prawdopodobieństwo z modelu Dixona-Colesa |
| Rynek | prawdopodobieństwo ze średnich kursów wielu bukmacherów po usunięciu marży |
| **Prognoza** | `udział modelu × model + reszta × rynek` – tego używa ocena i generator |
| Kurs | Superbet (OddsPapi); gdy brak – średnia rynkowa; „szacowany” = podwójna szansa wyliczona z 1X2 |
| Implikowane | prawdopodobieństwo z kursu Superbet po usunięciu jego marży |
| EV | `prognoza × kurs − 1` (przed podatkiem); **value** (★), gdy EV > 0 |
| EV po podatku | to samo z 12% podatkiem – dla gry pojedynczej |

Dlaczego mieszanka z rynkiem: backtest pokazał, że sam model przy kuponach systematycznie zawyża
szansę trafienia (optymalizator wybiera typy, w których model najbardziej „nie zgadza się” z rynkiem –
często są to jego błędy). Udział modelu dobiera `strojenie --zapisz` na Twoich danych
(domyślnie 30%). Podatek od stawki płaci się raz za kupon, dlatego „value” pojedynczego typu jest
liczone przed podatkiem, a **EV kuponu – po podatku** (12% od stawki i 10% od wygranej powyżej 2280 zł).

Generator:
- bierze mecze z zakresu dat (dziś / jutro / najbliższe X dni / własny zakres), z wybranych lig i rynków;
- pomija typy poniżej minimalnego prawdopodobieństwa i drużyny „mało danych” (chyba że je dopuścisz);
- wybiera **najwyżej jeden typ z meczu** i szuka kombinacji o kursie w zakresie, maksymalizując
  szansę trafienia albo wartość (EV) – dokładnie, programowaniem dynamicznym;
- układa **3 alternatywne kupony**, z których każdy ma co najmniej połowę innych meczów niż poprzednie;
- przy kuponie pokazuje kurs przed i po podatku, szansę trafienia (prognoza, model, rynek), EV
  i wygraną dla stawki, a przy każdym typie uzasadnienie: formę u siebie / na wyjeździe, średnie
  bramek i xG, bilans bezpośrednich meczów, oczekiwane gole modelu oraz uwagi (beniaminek,
  mało danych, różne ligi, kurs szacowany).

Szansa trafienia kuponu zakłada niezależność meczów (jeden typ z meczu ogranicza zależności).

## Backtest – jak czytać wynik

Backtest symuluje używanie aplikacji w przeszłości: **przed każdym tygodniem** model uczy się
tylko na meczach wcześniejszych i prognozuje mecze z tego tygodnia (brak „podglądania przyszłości”
sprawdza test automatyczny). Raport ma cztery części:

1. **Skuteczność** – trafność, log-loss i Brier (niższe = lepiej) w porównaniu z rynkiem
   (prawdopodobieństwa z kursów zamknięcia po usunięciu marży). Rynek to bardzo silny punkt
   odniesienia – jeśli model ma wyższy log-loss, to rynek prognozuje lepiej.
2. **Kalibracja** – dla przedziałów 0–10%, 10–20%, … porównanie przewidywanej i faktycznej
   częstości; ECE to średni błąd w punktach procentowych.
3. **Wynik finansowy** (po 12% podatku, z prognozą = mieszanka model + rynek z kursów przedmeczowych)
   – pojedyncze typy „value” (także z podziałem na rynki, ligi i wielkość przewagi) oraz symulowane
   kupony z tego samego optymalizatora co generator (1 na tydzień).
   Przy kuponach porównywana jest szansa trafienia wg modelu, wg rynku i faktyczna.
4. **Model a rynek** – log-loss mieszanki „w·model + (1−w)·rynek”. Jeśli najlepsze jest 0% modelu,
   model nie wnosi nic ponad kursy, a jego „value” to głównie błędy.

Kursy podwójnej szansy w danych historycznych są wyliczane z kursów 1X2, a dla BTTS nie ma
historycznych kursów – te rynki mają ocenę trafności i kalibracji, BTTS bez wyniku finansowego.

`python -m typerbot strojenie` sprawdza siatkę 36 ustawień (liczba meczów × półokres ×
regularyzacja) na Twojej historii i wybiera najlepsze według log-loss (nie według zysku –
zysk w backteście jest zbyt zaszumiony i łatwo go „przeuczyć”). Podaje też najlepszy udział
modelu w mieszance z rynkiem; `--zapisz` zapisuje wszystko w ustawieniach.

## Wiersz poleceń

Wszystko, co robi interfejs, jest też dostępne jako polecenia (`python -m typerbot --help`):
`sync`, `status`, `mecze`, `prognozy`, `typy`, `kupon`, `backtest`, `strojenie`, `budzet`, `klucz`,
`csv`, `druzyny`, `demo`, `gui`.

## Rozwiązywanie problemów

| Objaw | Co zrobić |
|---|---|
| Szara kropka źródła, „Brak klucza API” | wpisz klucz w Ustawieniach i kliknij Odśwież dane |
| Czerwona kropka, „Nieprawidłowy klucz” | sprawdź klucz (bez spacji); dla The Odds API – czy nie wyczerpał się miesięczny limit |
| „Niedostępne w planie darmowym” przy API-Football | normalne dla bieżącego sezonu – aplikacja korzysta wtedy z innych źródeł; zakres dostępnych sezonów zapamiętuje sama |
| Pomarańczowa kropka, „Brak połączenia” | aplikacja pokazuje dane z cache; sprawdź internet i odśwież później |
| Brak meczów lub kursów w generatorze | kliknij Odśwież dane; kursy są pobierane tylko dla lig z meczami w wybranym zakresie dat |
| Ta sama drużyna pod dwiema nazwami | *Ustawienia → Dopasowanie nazw drużyn → Połącz z inną drużyną…* |
| `No module named 'numpy'` (lub inny moduł) przy starcie | biblioteki nie są zainstalowane w użytym Pythonie – uruchom `instaluj.bat`, a potem `uruchom.bat` (albo aktywuj `.venv` przed `python -m typerbot`) |
| `py -3.12`: „No suitable Python runtime found” | masz inną wersję Pythona – to nie przeszkadza; użyj `instaluj.bat` lub `python -m venv .venv` |
| Coś działa nie tak | log błędów: `%LOCALAPPDATA%\TyperBot\typerbot.log` |

Źródło OddsPapi (kursy Superbet) jest zaimplementowane według dokumentacji API; identyfikatory rynków
i nazwę bukmachera aplikacja ustala automatycznie. Gdyby kursy Superbet się nie pojawiały, ocena typów
korzysta ze średniej rynkowej (kolumna „Źródło” pokaże „średnia”).

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
├── model/
│   ├── dixon_coles.py          dopasowanie modelu (gradient analityczny, L-BFGS), macierz wyników
│   ├── data.py                 okno ostatnich meczów, wagi czasowe, xG, „mało danych”, beniaminki
│   ├── predictor.py            prognozy meczów, siła lig (Liga Mistrzów)
│   ├── markets.py              1X2, podwójna szansa, powyżej/poniżej, obie strzelą
│   ├── backtest.py             walk-forward, metryki, kalibracja, symulacja finansowa
│   └── tuning.py               strojenie parametrów
├── betting/
│   ├── odds.py                 marża, prawdopodobieństwo implikowane, podatek, EV
│   ├── settlement.py           rozstrzyganie typów
│   ├── evaluation.py           ocena typów: prognoza, kurs referencyjny, implikowane, EV, value
│   ├── rationale.py            uzasadnienia: forma, statystyki, bilans, liczby modelu
│   ├── coupon.py, optimizer.py kandydaci i optymalizator kuponu (DP, alternatywy)
├── services/sync.py            synchronizacja z izolacją błędów źródeł
├── services/predict.py         prognozy nadchodzących meczów (zapis w bazie)
├── services/coupons.py         generator kuponów, wymiana zdarzeń, kurs ręczny
├── services/register.py        rejestr postawionych kuponów i automatyczne rozliczanie
├── services/stats.py           bilans, ROI, trafność – ogółem, miesiące, rynki, ligi
├── services/budget.py          miesięczny limit stawek, wydatki i bilans miesiąca
├── ui/                         interfejs PySide6: okno, 5 zakładek, wykresy, motyw, zadania w tle
├── demo/                       syntetyczny świat meczów i transport udający API
└── cli.py                      polecenia wiersza poleceń
packaging/                      konfiguracja PyInstaller (TyperBot.exe)
tests/                          testy jednostkowe, integracyjne i interfejsu (pytest)
*.bat                           instaluj / uruchom / zbuduj_exe – instalacja, start i budowa .exe
```
