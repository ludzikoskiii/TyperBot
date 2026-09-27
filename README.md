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
| 4 | Interfejs (3 zakładki: Kupony, Historia, Ustawienia), historia kuponów, statystyki | **gotowy** |
| 5 | Dopracowanie, plik .exe | **gotowy** |
| 6 | Kalibracja modelu na historii football-data.co.uk, nowy optymalizator kuponów | **gotowy** |
| 7 | Tylko źródła bez klucza i rejestracji, 38+ lig z całego świata, ranking Elo, kursy szacunkowe | **gotowy** |

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
   python -m typerbot gui --demo   # interfejs w trybie demo – bez internetu (dane syntetyczne)
   ```

4. Kliknij **Odśwież dane** – wszystkie źródła są darmowe, bez klucza i rejestracji.
   **Pierwsza synchronizacja trwa kilka minut** – pobiera 8 sezonów historii 38 lig z plików
   football-data.co.uk (najpierw bieżące sezony, potem starsze). Postęp widać w pasku stanu na dole;
   kolejne odświeżenia trwają kilka sekund.
5. (opcjonalnie) W *Ustawienia → Model i backtest* kliknij **Strojenie parametrów**, a potem
   **Zapisz najlepsze ustawienia** – model i udział modelu w prognozie zostaną dobrane do Twoich danych.
   Wartości domyślne są już skalibrowane (sekcja „Kalibracja modelu”).

   Tryb demo w wierszu poleceń (raport tekstowy):

   ```powershell
   python -m typerbot demo
   python -m typerbot demo --awaria openfootball   # symulacja awarii jednego źródła
   python -m typerbot demo --backtest              # + prognozy i backtest modelu na danych demo
   ```

Testy: `python -m pytest` (ok. 6 minut; obejmują też interfejs i test „każdy dzień tygodnia”).

## Plik .exe (bez instalowania Pythona)

Na komputerze z Pythonem uruchom **`zbuduj_exe.bat`** – zbuduje folder `dist\TyperBot` z plikiem
`TyperBot.exe` (ok. 300 MB, bo zawiera Pythona, Qt i biblioteki obliczeniowe) i od razu uruchomi
test `TyperBot.exe --self-test`. Folder można skopiować na inny komputer z Windows 10/11.
`TyperBot.exe --demo` otwiera tryb demo. Dane i ustawienia są w `%LOCALAPPDATA%\TyperBot\`
– wspólne dla `.exe` i uruchamiania z Pythona.

## Interfejs

Trzy zakładki: **Kupony**, **Historia**, **Ustawienia**.

**Kupony** – ekran główny:

1. **Kurs docelowy** (duże pole, np. 5,00).
2. Zakres: **Dziś** / **Jutro** / **Najbliższe 3 dni** / **Własny** (od–do).
3. Duży przycisk **Generuj kupony** – obok pojawiają się do 3 kuponów, posortowane od najwyższej
   szansy trafienia.

Każdy kupon wygląda jak kupon u bukmachera: mecz (liga, godzina), typ, kurs (≈ i kolor ostrzegawczy =
kurs szacunkowy), pasek prawdopodobieństwa i **jedno zdanie uzasadnienia** (np. „Arsenal wygrał 7 z 9 ostatnich
meczów u siebie; model 64% i rynek 61%.”). Pod typami: **kurs łączny**, **kurs po podatku (−12%)**,
**szansa trafienia** (i osobno model / rynek) oraz przycisk **Kopiuj kupon** (tekst do schowka;
kupon zostaje oznaczony w Historii jako skopiowany). Menu **⋯** przy typie: wymień zdarzenie, wpisz
kurs z oferty, usuń zdarzenie, szczegóły meczu.

Wszystko inne jest w zwiniętej sekcji **Zaawansowane** (rozsądne wartości domyślne): tolerancja
kursu, liczba zdarzeń (2–4), min. prawdopodobieństwo typu (40%), tryb (najwyższa szansa / tylko
value), maks. różnica model–rynek (8 pkt), **kursy szacunkowe** (domyślnie „gdy brak prawdziwych”),
**kraje i ligi** (wyszukiwarka i zaznaczanie całych krajów), rynki, drużyny „mało danych”
i **Zapisz jako domyślne**. Przycisk **Diagnostyka** pokazuje, skąd przyszły
mecze i kursy i ile odpada na każdym filtrze. Przełącznik **Wszystkie mecze** po prawej pokazuje
dawną zakładkę „Mecze”: prognozy 1 / X / 2 / >2,5 / BTTS i po kliknięciu wszystkie typy meczu
(prognoza, model, rynek, kurs, EV, EV po podatku) z opisem formy, średnich i bilansu.

**Historia** – wszystkie ułożone kupony (ten sam zestaw typów tylko raz) z wynikiem **trafiony /
nietrafiony / w trakcie**, rozliczane automatycznie; szczegóły z wynikami meczów; filtr
„Tylko skopiowane”. Podzakładka **Statystyki**: trafność kuponów i pojedynczych typów osobno dla
rynków i lig, wynik w jednostkach (1 kupon = 1 jednostka), krzywa wyniku, miesiące.

**Ustawienia** – trzy podzakładki: **Ogólne** (ligi używane w aplikacji – drzewo „kraj → ligi”
z wyszukiwarką, rynki, kursy, marża kursu szacunkowego, podatek), **Źródła danych** (stan każdego źródła
i data ostatnich danych, włączanie źródeł, pobieranie, dopasowanie nazw drużyn), **Model i backtest**
(parametry modelu i rankingu Elo, backtest ze skutecznością, kalibracją i wynikiem w jednostkach,
mieszanka model + rynek, strojenie).

Pasek stanu pokazuje każde źródło danych (zielona kropka = OK, pomarańczowa = brak połączenia – aplikacja
działa na danych z bazy) i **datę ostatnich danych** („dane z: 27.09 18:02”).
Dane odświeżają się w tle przy starcie i co 3 godziny (przycisk **Odśwież dane** – od razu); przy okazji
rozliczają się zakończone kupony. Wszystkie obliczenia i pobieranie działają w tle – okno się nie zawiesza.
W „Historii” jest **Eksportuj do CSV…** (plik otwiera się w polskim Excelu).

## Bez kwot – wynik w jednostkach

Aplikacja nie zna i nie zapisuje żadnych kwot (bez stawek, wypłat i limitów budżetu). Historia to lista
kuponów, które ułożył generator – każdy trafia tam sam po wygenerowaniu, a ręczna zmiana kuponu
(wymiana typu, kurs z oferty) aktualizuje wpis. Filtr **„Tylko skopiowane”** pokazuje kupony, które
skopiowałeś (zwykle te, które zagrałeś) – statystyki liczą się wtedy tylko dla nich.

Wynik liczony jest w **jednostkach**: każdy kupon to 1 jednostka stawki, trafiony zwraca
**kurs × 0,88** (12% podatku od stawki; opcja „Bukmacher pokrywa podatek” w Ustawieniach),
nietrafiony – 0. Zwrot (ROI) = wynik / liczba rozliczonych kuponów. Podatku od wygranej powyżej
2280 zł aplikacja nie uwzględnia – zależy od kwoty.

## Źródła danych – wszystkie bez klucza, rejestracji i limitu miesięcznego

| Źródło | Co daje | Zasięg | Aktualizacja | Warunki |
|---|---|---|---|---|
| **football-data.co.uk** (pliki CSV) | **główne**: nadchodzące mecze **z kursami** (1X2, powyżej/poniżej 2,5), wyniki, historia z kursami, w ligach głównych statystyki (strzały) | **38 lig w 27 krajach**: Anglia (5 poziomów), Szkocja (4), Niemcy, Włochy, Hiszpania, Francja (po 2), Holandia, Belgia, Portugalia, Turcja, Grecja + Polska, Argentyna, Austria, Brazylia, Chiny, Dania, Finlandia, Irlandia, Japonia, Meksyk, Norwegia, Rumunia, Rosja, Szwecja, Szwajcaria, USA | kursy na weekend w piątek po południu, na środek tygodnia we wtorek po południu | pliki do pobrania bez klucza; aplikacja pobiera kilka plików co kilka godzin |
| **openfootball** (football.json na GitHubie) | **terminarz całego sezonu z wyprzedzeniem** i wyniki (bez kursów) | czołowe ligi Europy (Anglia 1–2, Niemcy, Hiszpania, Francja, Włochy, Holandia, Portugalia) i Brazylia; pliki innych lig, gdy się pojawią | ok. 1–2 razy w tygodniu | domena publiczna (CC0) |
| **OpenLigaDB** | terminarz i wyniki na bieżąco (bez kursów) | Bundesliga 1–3, Puchar Niemiec | na bieżąco | bez klucza; limit serwisu 1000 zapytań/godz. – aplikacja wysyła kilka |
| **international_results** (GitHub) | wyniki reprezentacji od 1872 r. – ranking Elo reprezentacji | wszystkie reprezentacje | ok. raz w miesiącu | domena publiczna (CC0) |
| **openfootball/clubs** | warianty nazw klubów („Man United” = „Manchester United FC”) | 27 krajów | rzadko (co 30 dni) | domena publiczna (CC0) |

Nie używamy źródeł, które wymagają klucza, rejestracji albo mają limit miesięczny (The Odds API,
OddsPapi, football-data.org, API-Football – usunięte; zapisane wcześniej klucze aplikacja sama kasuje
z Menedżera poświadczeń), ani stron, których regulamin zabrania automatycznego pobierania
(np. Flashscore, SofaScore, FotMob, FBref) lub które są za logowaniem (Club Elo od 23.09.2026).

**Lista lig budowana jest z danych:** pokazujemy ligi, dla których są mecze w bazie; liga, która pojawi się
w plikach football-data.co.uk, a nie ma jej w katalogu, dopisuje się sama. Źródła opcjonalne (openfootball,
OpenLigaDB, reprezentacje) można wyłączyć w *Ustawienia → Źródła danych*.

**Bez internetu:** wszystko, co pobrane, zostaje w bazie SQLite, a odpowiedzi są w cache. Przy braku
internetu albo awarii źródła aplikacja działa na ostatnich danych i pokazuje ich datę (pasek stanu,
Diagnostyka, *Ustawienia → Źródła danych*). Każde źródło to osobny moduł (`typerbot/data/sources/`) –
awaria jednego nie blokuje pozostałych.

**Czego darmowe źródła bez klucza nie dają:** terminarza meczów reprezentacji, większości lig Afryki
i Azji oraz niższych lig w większości krajów. W przerwach reprezentacyjnych (np. **21.09–06.10.2026**)
czołowe ligi nie grają – grają głównie niższe ligi angielskie i ligi spoza Europy; diagnostyka
w takiej sytuacji mówi to wprost i podaje najbliższy termin meczów w wybranych ligach.

## Praca z prawdziwymi danymi

```powershell
python -m typerbot sync            # pobranie danych (kolejne uruchomienia korzystają z cache)
python -m typerbot sync --force    # pominięcie cache
python -m typerbot status          # stan źródeł, data ostatnich danych, problemy
python -m typerbot mecze --dni 3   # nadchodzące mecze z kursami (najwyższy, w nawiasie średnia rynkowa)
python -m typerbot druzyny --liga EKS   # jak nazwy drużyn z różnych źródeł zostały połączone
python -m typerbot prognozy --dni 3     # prognozy modelu dla nadchodzących meczów
python -m typerbot backtest             # test modelu na 3 ostatnich zakończonych sezonach
python -m typerbot backtest --ligi PL,EKS --sezony 2023,2024,2025 --tryb value --kurs 3
python -m typerbot strojenie --zapisz   # dobór parametrów modelu na Twojej historii
python -m typerbot typy --dni 3 --value # ocena typów: prognoza, kurs, implikowane, EV (★ = value)
python -m typerbot kupon                # 3 kupony wg ustawień (domyślnie kurs 5,00 ±10%, 3 dni)
python -m typerbot kupon --jutro --kurs 3 --ligi PL,EKS --rynki 1X2,DC
python -m typerbot kupon --wymien A2    # zamienniki dla 2. zdarzenia kuponu A
python -m typerbot kupon --wymien A2 --na 3        # wymiana i przeliczenie kuponu
python -m typerbot kupon --kurs-reczny A2=1,95     # kurs z oferty bukmachera
python -m typerbot diagnoza             # dlaczego nie ma kuponu (źródła, filtry, powód)
```

Baza i logi: `%LOCALAPPDATA%\TyperBot\` (`typerbot.db`, `typerbot.log`).

## Model prognoz

**Dixon-Coles** – liczba goli każdej drużyny ma rozkład Poissona zależny od:
siły ataku i obrony obu drużyn, przewagi własnego boiska i ogólnego poziomu bramek w lidze.
Korekta ρ poprawia prawdopodobieństwa wyników 0:0, 1:0, 0:1 i 1:1.

- **Okno danych:** N ostatnich meczów każdej drużyny (domyślnie 80 – w praktyce ok. 2 sezony).
- **Wygaszanie:** mecz sprzed „półokresu” (domyślnie 365 dni) waży o połowę mniej.
- **Regularyzacja:** siła drużyn jest łagodnie ściągana do średniej ligi (domyślnie 5).
  Bez tego model „wierzy” w przypadkowe serie i jest zbyt pewny siebie – backtest to pokazał
  (przy słabej regularyzacji typy „70–80%” trafiały w ok. 54%).
- **„Mało danych”:** mniej niż 6 meczów w ostatnim roku i mniej niż 10 meczów w historii rankingu Elo,
  albo liga z mniej niż 60 meczami w bazie; beniaminek startuje z siłą nieco poniżej średniej ligi.
  Takie drużyny domyślnie nie trafiają na kupon.
- **Puchary:** wspólny model wszystkich lig z parametrem siły każdej ligi. Mecz drużyn z różnych lig
  jest oznaczony jako „niższa pewność”.

**Ranking Elo (z samych wyników)** – dla wszystkich drużyn z bazy, także lig bez szczegółowych
statystyk i reprezentacji. Po każdym meczu ocena rośnie albo maleje o `K · G · (wynik − oczekiwany wynik)`
(K = 20, G rośnie z różnicą bramek, przewaga boiska 60 pkt, dla reprezentacji 100 pkt i szybsze zmiany;
teren neutralny bez przewagi). Drużyna po awansie lub spadku zachowuje ocenę, nowa drużyna startuje
z oceną nieco poniżej średniej ligi. Różnica ocen zamieniana jest na oczekiwane gole modelem Poissona
(`λ = średnie gole w lidze · e^(±β·różnica/400)`, β dopasowane na historii) i dalej – jak w Dixonie-Colesie –
na macierz wyników i wszystkie rynki.

**Model w aplikacji = Dixon-Coles + Elo:** gdy obie drużyny mają pełne dane, prognoza to mieszanka
(domyślnie 50% Elo – dobrane backtestem); gdy drużyna ma mało meczów w oknie Dixona-Colesa, ale dłuższą
historię wyników (np. po awansie z niższej ligi) – sam Elo; reprezentacje – Elo. Tam, gdzie są kursy,
prognoza i tak opiera się na rynku (udział modelu 0%); model decyduje tam, gdzie kursów brak.

Z macierzy wyników (0–10 bramek) liczone są wszystkie rynki: 1X2, podwójna szansa,
powyżej/poniżej 2,5 i obie strzelą.

## Ocena typów i generator kuponu

Dla każdego typu (1X2, podwójna szansa, powyżej/poniżej 2,5, obie strzelą) aplikacja pokazuje:

| Kolumna | Znaczenie |
|---|---|
| Model | prawdopodobieństwo z modelu (Dixon-Coles + ranking Elo) |
| Rynek | prawdopodobieństwo ze średnich kursów wielu bukmacherów po usunięciu marży |
| **Prognoza** | `udział modelu × model + reszta × rynek` – tego używa ocena i generator |
| Kurs | średnia rynkowa (albo najwyższy, Pinnacle, Bet365 – ustawienie); **„kurs szacunkowy – sprawdź u bukmachera”** (≈), gdy źródła nie podają kursu: z innych kursów meczu (podwójna szansa z 1X2, BTTS i powyżej/poniżej z oczekiwanych goli) albo – gdy mecz nie ma żadnych kursów – z prognozy modelu pomniejszonej o typową marżę (domyślnie 7%, ustawienie): `kurs = 1 / (p · 1,07)` |
| Implikowane | prawdopodobieństwo z kursu referencyjnego po usunięciu marży |
| EV | `prognoza × kurs − 1` (przed podatkiem); **value** (★), gdy EV > 0 |
| EV po podatku | to samo z 12% podatkiem – dla gry pojedynczej |

Dlaczego mieszanka z rynkiem: backtest pokazał, że sam model przy kuponach systematycznie zawyża
szansę trafienia (optymalizator wybiera typy, w których model najbardziej „nie zgadza się” z rynkiem –
często są to jego błędy). Udział modelu dobiera `strojenie --zapisz` na Twoich danych
(domyślnie **0%** – na 10 sezonach żaden udział modelu nie poprawił prognozy rynku, patrz
„Kalibracja modelu”). Model dalej służy do filtra zgodności, uzasadnień i jako prognoza tam,
gdzie nie ma kursów. Podatek od stawki płaci się raz za kupon, dlatego „value” pojedynczego typu jest
liczone przed podatkiem, a **EV kuponu – po podatku** (12% od stawki).

Generator:
- bierze mecze z zakresu dat (dziś / jutro / najbliższe X dni / własny zakres), z wybranych lig i rynków;
- pomija typy poniżej minimalnego prawdopodobieństwa i drużyny „mało danych” (chyba że je dopuścisz);
- w trybie „najwyższa szansa” bierze tylko typy, w których **model i rynek są zgodni** (różnica
  ≤ 8 pkt proc.) – duża rozbieżność bez wyraźnego powodu to częściej błąd modelu niż okazja;
  w trybie „value” – tylko typy z dodatnim EV;
- kursy szacunkowe (≈) trafiają na kupon **tylko wtedy, gdy z prawdziwymi kursami kuponu nie da się
  ułożyć** (ustawienie „Kursy szacunkowe”: gdy brak prawdziwych / zawsze / nigdy; w backteście takie
  typy trafiały rzadziej, niż zapowiadały). Taki kupon ma **ostrzeżenie na górze**, każdy typ dopisek
  „kurs szacunkowy – sprawdź u bukmachera”, a skopiowany tekst – „UWAGA: kupon z kursami szacunkowymi”;
  przy równej szansie prawdziwy kurs ma pierwszeństwo;
- **jeden typ z meczu** i żadnych typów zależnych: dwa mecze tej samej drużyny w zakresie dat
  (np. liga i puchar) nie trafiają na jeden kupon;
- szuka kombinacji o kursie w zakresie dokładnie (programowanie dynamiczne), maksymalizując
  `Σ log(p · kurs) − 0,02 · liczba zdarzeń` – przy tym samym kursie łącznym wygrywa **wyższa szansa
  trafienia**, a przy remisie **mniej zdarzeń** (każde zdarzenie to kolejna marża bukmachera);
- układa **3 alternatywne kupony** (każdy ma co najmniej połowę innych meczów niż poprzednie),
  posortowane od najwyższej szansy trafienia;
- gdy kuponu nie da się ułożyć, pokazuje konkretny powód i podpowiedź, np. „W wybranych ligach nie ma
  meczów na 27.09–30.09. W tym terminie grają: League One (Anglia): 12, … Wybrane ligi mają przerwę
  w rozgrywkach: ostatni mecz nd 20.09, następny pt 09.10” albo „Żaden mecz nie ma jeszcze kursów
  bukmacherów – pliki z kursami pojawiają się w piątek i we wtorek”.

**Diagnostyka** pokazuje: z kiedy są dane, ile meczów w zakresie dat przyszło z każdego źródła i z których
dni, ile z nich ma kursy, rozkład na dni (wszystkie ligi / wybrane / z kursem), ile zostaje po każdym
filtrze generatora i listę problemów ze źródeł.

Szansa trafienia kuponu zakłada niezależność meczów (jeden typ z meczu i brak wspólnych drużyn
ogranicza zależności).

## Kalibracja modelu

Dane: sezony 2014/15–2025/26 z plików football-data.co.uk dla Premier League, La Liga,
Bundesligi, Serie A, Ligue 1 i Ekstraklasy (ok. 25 tys. meczów; dwa pierwsze sezony służą tylko
jako historia do nauki) – wyniki i kursy zamknięcia Bet365 (w tym środowisku
domena football-data.co.uk była zablokowana, więc użyto publicznej kopii tych samych plików
z GitHuba, `xgabora/club-football-match-data-2000-2025`; aplikacja pobiera pliki bezpośrednio
z football-data.co.uk). Procedura bez „podglądania”:

1. **Strojenie** na sezonach 2021/22–2022/23 (siatka 64 ustawień, walk-forward co tydzień):
   log-loss 1X2 modelu 1,0129 (stare 20 meczów / 180 dni / regularyzacja 10) → **0,9990**
   (80 / 365 / 5). Rynek (kursy po usunięciu marży) ma 0,9814 – nadal lepiej.
2. **Usuwanie marży:** metoda Shina minimalnie lepsza od proporcjonalnej (0,98137 wobec 0,98171)
   – lepiej ujmuje przewagę faworytów.
3. **Mieszanka model + rynek:** na każdym zbiorze najlepszy był udział modelu **0%** (każde
   +10% modelu podnosi log-loss). Domyślny udział modelu to więc 0.
4. **Sprawdzenie** na 8 innych sezonach (2016/17–2020/21 i 2023/24–2025/26, 16 111 meczów):

| Miara (sezony testowe) | Przed | Po |
|---|---|---|
| Log-loss 1X2 modelu | 1,0057 | 0,9905 |
| Log-loss 1X2 prognozy (tego używa generator) | 0,9754 | **0,9705** (= rynek) |
| Błąd kalibracji prognozy (ECE) | 0,90 pkt proc. | **0,25 pkt proc.** |

Symulowane kupony na tych samych sezonach (3 kupony na tydzień, kursy zamknięcia, zwrot po 12%
podatku, 1 kupon = 1 jednostka):

| Kurs docelowy | Przed: trafione (zapowiadane) | Przed: zwrot | Po: trafione (zapowiadane) | Po: zwrot | Zdarzeń na kuponie przed → po |
|---|---|---|---|---|---|
| 2 | 51,3% (52,4%) | −17,7% | 48,4% (48,1%) | **−16,0%** | 2,00 → 2,00 |
| 3 | 31,0% (36,3%) | −25,3% | 30,9% (32,6%) | **−21,2%** | 2,14 → 2,00 |
| 5 | 17,1% (22,0%) | −31,6% | 18,9% (19,8%) | **−21,9%** | 3,17 → 2,02 |
| 10 | 8,0% (11,2%) | −36,2% | 9,7% (9,7%) | **−21,0%** | 4,50 → 3,02 |

Co to znaczy:
- zapowiadana szansa trafienia jest teraz **uczciwa** (różnica ≤ 2 pkt proc., wcześniej model
  obiecywał do 5 pkt więcej, niż trafiało);
- kupony mają mniej zdarzeń i średnio wyższy kurs w zakresie (np. 4,73 zamiast 4,54 przy celu 5),
  bo każde zdarzenie to kolejna marża;
- **wynik nadal jest ujemny** – marża bukmachera i 12% podatku są większe niż przewaga, którą da się
  wyciągnąć z publicznych danych. Aplikacja układa możliwie najlepsze kupony, ale nie odwraca
  matematyki zakładów.

Po aktualizacji aplikacji zapisane ustawienia, które były starymi wartościami domyślnymi, zmieniają
się jednorazowo na skalibrowane; ustawienia zmienione ręcznie zostają bez zmian.

## Ranking Elo – porównanie modelu przed i po

Walk-forward tydzień po tygodniu (model uczy się tylko na wcześniejszych meczach) na **38 ligach**
z plików football-data.co.uk (ta sama kopia z GitHuba co wyżej). Udział Elo i K dobrane na sezonach
2021/22–2022/23 (najlepsze: 50% Elo, K = 20 – to wartości domyślne), wynik na **innych sezonach:
2023/24–2025/26, 28 531 meczów**:

| Miara (niżej = lepiej, poza trafnością) | Przed: Dixon-Coles | Po: Dixon-Coles + Elo |
|---|---|---|
| Log-loss 1X2 | 1,0235 | **1,0216** |
| Brier 1X2 | 0,6138 | **0,6125** |
| Log-loss powyżej/poniżej 2,5 | 0,6861 | **0,6823** |
| Log-loss obie strzelą | 0,6894 | **0,6873** |
| Trafność 1X2 | 48,7% | **48,8%** |
| Błąd kalibracji (ECE) | **0,67 pkt proc.** | 0,78 pkt proc. |

Drużyny z małą liczbą danych (1 641 meczów – np. po awansie, początek sezonu):

| Miara | Przed | Po |
|---|---|---|
| Log-loss 1X2 | 1,0398 | **1,0376** |
| Log-loss powyżej/poniżej 2,5 | 0,6963 | **0,6885** |
| Błąd kalibracji (ECE) | 1,52 pkt proc. | **1,33 pkt proc.** |

Co to znaczy: Elo poprawia prognozy na wszystkich rynkach, najbardziej przy drużynach z krótką historią
(dziedziczą ocenę z niższej ligi) i przy powyżej/poniżej. Poprawa jest umiarkowana, a błąd kalibracji
na całym zbiorze wzrósł nieznacznie (o ok. 0,1 pkt proc.). **Rynek (kursy bez marży) nadal jest dokładniejszy**
(log-loss 1X2 1,0024 wobec 1,0216), dlatego tam, gdzie są kursy, prognoza opiera się
na rynku, a model przydaje się głównie dla meczów bez kursów (kurs szacunkowy) i do filtra zgodności.

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

`python -m typerbot strojenie` sprawdza siatkę 27 ustawień (liczba meczów × półokres ×
regularyzacja) na Twojej historii i wybiera najlepsze według log-loss (nie według zysku –
zysk w backteście jest zbyt zaszumiony i łatwo go „przeuczyć”). Podaje też najlepszy udział
modelu w mieszance z rynkiem; `--zapisz` zapisuje wszystko w ustawieniach.

## Wiersz poleceń

Wszystko, co robi interfejs, jest też dostępne jako polecenia (`python -m typerbot --help`):
`sync`, `status`, `mecze`, `prognozy`, `typy`, `kupon`, `diagnoza`, `backtest`, `strojenie`,
`druzyny`, `demo`, `gui`.

`python -m typerbot diagnoza` (te same parametry co `kupon`) pokazuje, ile meczów i kursów przyszło z każdego
źródła, ile zostaje po każdym filtrze generatora i dlaczego kuponu nie da się ułożyć. `status` wypisuje też
listę problemów ze źródeł z ostatniej synchronizacji.

## Rozwiązywanie problemów

| Objaw | Co zrobić |
|---|---|
| Pomarańczowa kropka, „Brak połączenia” | aplikacja działa na danych z bazy (data przy „dane z:”); sprawdź internet i odśwież później |
| „Brak meczów” / „W wybranych ligach nie ma meczów” | często przerwa reprezentacyjna – diagnostyka podaje ligi grające w tym terminie i najbliższy termin wybranych lig; zaznacz więcej krajów w „Zaawansowanych” |
| Kupon z kursami „≈” | mecze bez kursów bukmacherów (np. środa przed publikacją pliku we wtorek) – sprawdź kursy u bukmachera albo poczekaj na plik z kursami |
| Generator nie ułożył kuponu | pod komunikatem jest konkretny powód i podpowiedź; przycisk **Diagnostyka** pokazuje, ile meczów i kursów przyszło z każdego źródła i ile zostaje po każdym filtrze |
| „⚠ problemy ze źródeł: N” na pasku stanu | kliknij napis – lista problemów z ostatniej synchronizacji (źródło, ligi, stan, co zrobić) |
| Ta sama drużyna pod dwiema nazwami | *Ustawienia → Dopasowanie nazw drużyn → Połącz z inną drużyną…* |
| `No module named 'numpy'` (lub inny moduł) przy starcie | biblioteki nie są zainstalowane w użytym Pythonie – uruchom `instaluj.bat`, a potem `uruchom.bat` (albo aktywuj `.venv` przed `python -m typerbot`) |
| `py -3.12`: „No suitable Python runtime found” | masz inną wersję Pythona – to nie przeszkadza; użyj `instaluj.bat` lub `python -m venv .venv` |
| Coś działa nie tak | log błędów: `%LOCALAPPDATA%\TyperBot\typerbot.log` |

## Struktura projektu

```
typerbot/
├── config/        ustawienia (SQLite), katalog lig, usuwanie starych kluczy (keyring)
├── data/
│   ├── db.py, schema.py        SQLite (WAL) i migracje
│   ├── http.py, ratelimit.py   cache HTTP, ograniczanie zapytań
│   ├── quota.py                liczba zapytań i status źródeł (data ostatnich danych)
│   ├── teams.py, team_seeds.py dopasowanie nazw drużyn między źródłami (+ warianty z openfootball/clubs)
│   ├── repository.py           zapis meczów i kursów, łączenie meczów z kilku źródeł
│   └── sources/                football_data_csv (główne), openfootball, openligadb, international, club_names
├── model/
│   ├── dixon_coles.py          dopasowanie modelu (gradient analityczny, L-BFGS), macierz wyników
│   ├── data.py                 okno ostatnich meczów, wagi czasowe, „mało danych”, beniaminki
│   ├── elo.py                  ranking Elo z wyników i zamiana na gole (Poisson)
│   ├── predictor.py            prognozy meczów: Dixon-Coles + Elo, siła lig
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
├── services/register.py        historia wygenerowanych kuponów i automatyczne rozliczanie (w jednostkach)
├── services/stats.py           trafność kuponów i typów, wynik w jednostkach – miesiące, rynki, ligi
├── services/diagnostics.py     diagnostyka generatora: źródła, filtry, powód braku kuponu
├── ui/                         interfejs PySide6: okno, 3 zakładki, wykresy, motyw, zadania w tle
├── demo/                       syntetyczny świat (7 lig z 6 krajów, reprezentacje) i transport udający źródła
└── cli.py                      polecenia wiersza poleceń
packaging/                      konfiguracja PyInstaller (TyperBot.exe)
tests/                          testy jednostkowe, integracyjne i interfejsu (pytest)
*.bat                           instaluj / uruchom / zbuduj_exe – instalacja, start i budowa .exe
```
