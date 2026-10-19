# 0.14.219: start i przygotowanie kontekstu na Raspberry Pi

Pakiet rozwija 0.14.218 / PR #334. Status: **wersja testowa, pełny odbiór performance pozostaje otwarty**. Końcowa lokalna regresja: **2736 testów OK / 393.571 s**; entrypoint, oba UI, compileall i składnia 21 plików JS przeszły. CI końcowego kodu `867e8a9d` przeszło na Pythonie 3.11/3.13, wraz z dodatkowymi benchmarkami i obrazem Docker. Sprzęt użytkownika: Raspberry Pi 4, microSD 64 GB; pojemności RAM nie podano.

## Co wynika z czterech eksportów 0.14.218

- Instalowanie rozszerzeń trwało 334,1384 s. Wczytanie modeli SQLite zajęło 0,3027 s. Eksport nie rozbija czasu instalowania na konkretne usługi, więc nie przypisujemy całych 334 s jednej funkcji.
- P95 event → decision w ostatnich 60 s wynosi 268–385 ms, a opcjonalne przygotowanie ramki Tournament 226–331 ms. Percentyli tych etapów nie sumuje się. Samo przygotowanie cech Ridge również pozostaje kosztowne.
- Writer dowodów Candidate ma pustą kolejkę, zero błędów i zero odrzuceń we wszystkich czterech snapshotach. Nie jest obecnie głównym wskazanym ograniczeniem.
- Rejestr encji ma ponad 3,3 mln trafień cache, ale nadal był obsługiwany wpis po wpisie podczas każdego odłączenia ramki. Checkpoint i reclaim microSD także zajmują sekundy; pozostają oddzielnym źródłem zakłóceń.
- W tych eksportach nie ma próbek fizycznego wywołania usługi HA. Pomiary dotyczą decyzji i obserwacji, nie opóźnienia urządzeń.

Kompaktowe dane i SHA-256 wejściowych eksportów: `benchmarks/PERFORMANCE_PLAN_0_14_219_INPUTS.json`. Surowych stanów i nazw urządzeń nie kopiujemy do raportu.

## Zmiany

1. Indeks porządku receipt w provenance usuwa pełne sortowanie historii przy odtwarzaniu cache 8192 zdarzeń. Zapytanie i jego limit pozostają te same. Kolejność remisów oraz processed-event deduplication są sprawdzane względem poprzedniego zapytania. Pierwsze zbudowanie indeksu nadal wymaga odczytu tabeli; kolejne starty wykorzystują istniejący indeks. Błąd budżetu/miejsca/blokady nie obchodzi admission i pozostawia oryginalną ścieżkę. Eksport podaje osobno czas indeksu, odczytu i liczbę wczytanych wierszy.
2. Start podaje nazwane czasy instalatorów oraz aktualnie wykonywane komponenty. Pomiar jest ograniczony do 128 nazw, obejmuje czas ścienny i CPU wątku, działa bez debug trace i zapisuje niepowodzenie przed ponownym zgłoszeniem wyjątku. Zagnieżdżone czasy zawierają swoje dzieci; nie należy ich sumować. Kolejność instalacji i bramki gotowości nie są przesuwane.
3. Odłączony rejestr encji może ponownie wykorzystać pełny snapshot po natywnym sprawdzeniu identyfikatorów źródeł, kolejności kluczy i całej zagnieżdżonej zawartości. Zmiana w miejscu nadal jest wykrywana, zastąpienie obiektu zachowuje dotychczasową semantykę. Zwracany jest nowy kontener. Dodatkowe struktury są naliczane do istniejącego limitu cache; nie utrzymują danych wcześniej usuniętych przez eviction. Optymalizacja dotyczy rejestru, bez założenia niezmienności aktywnych stanów HA.

Nie zmieniamy modelu, schematu cech, parametrów uczenia, liczby workerów, limitów dysku ani fizycznych bramek Control/Promote. Full Rebuild modeli nie jest wymagany przez ten pakiet.

## Granice odbioru

Walidacja funkcjonalna i pomiary komponentów są zapisywane osobno. Hostowy benchmark samego zapytania/skanu/cache nie dowodzi przyspieszenia startu Pi, całej reakcji agenta ani urządzenia. Brak dużej bazy użytkownika uniemożliwia lokalne odtworzenie czasu 334 s; nowa diagnostyka ma wskazać rzeczywisty koszt kolejnego startu. Pełna izolacja lifecycle, odłączone ramki ingest, długie przeciążenie writerów oraz odbiór E0–E7 pozostają otwarte.

## Pomiar komponentów końcowego kodu

Host, pięć naprzemiennych par każdej operacji, identyczny wynik: odczyt 8192 zdarzeń z 50 tys. wierszy (payload około 2 KiB) 415.48 → 14.20 ms mediany. Pierwsze utworzenie indeksu w tej małej fixture: 115.32 ms. Niezmieniony rejestr 779 encji: 0.292 → 0.162 ms. To mediany operacji komponentów, nie p95 całej inferencji ani pomiar Raspberry Pi. Plany SQL, pary i hashe źródeł są w `benchmarks/PERFORMANCE_PLAN_0_14_219_COMPONENTS.json`.

## Oddzielenie zapisów cache raportów Confidence

Pełna lista Candidate mogła czekać na globalną blokadę Store wyłącznie, aby zapisać wyliczony cache raportu. Teraz trzy jawnie dopuszczone tabele cache korzystają z próby przejęcia Store bez oczekiwania i SQLite busy timeout 0. Zajęta blokada lub odmowa budżetu pomija tylko zapis cache; klient otrzymuje właśnie wyliczony raport. Pozostałe błędy SQL są zgłaszane. Zapis jest pomijany także wewnątrz transakcji właściciela, aby pomocniczy conn() nie zatwierdził jego danych. Timeout oraz flagi retry są odtwarzane, a reclaim WAL pozostaje respektowany. W eksporcie pojawia się `sqlite.confidence_cache_writes`.

Tworzenie epoki selekcji, zamrożenie końca finalnej oceny, etykiety, surowe obserwacje i bramki Promote zachowują obowiązkowe transakcje. To unikanie oczekiwania na blokady przy zapisie przyspieszającego cache, nie gwarancja terminu fizycznego I/O.

Przed tą zmianą zachowano dwie odrzucone próby 60 s z 20 Root: odpowiednio 1 i 2 błędy UI, bez luk Candidate. Pięć par po 18 s dało p95 225,82 → 166,25 ms, ale bootstrap od -70,79% do +19,92%; wynik jest nierozstrzygający i nie stanowi odbioru końcowych źródeł.

Po zmianie, na zamrożonych 628 plikach: 60 s Shadow20, p95 183,21 ms, 0 błędów UI/Candidate i 0 luk; zapisano 19 cache, pięć razy pominięto zajętą blokadę Store. Control3 przez 30 s: p95 decyzji 78,21 ms, 0 błędów/luk, 3 zapisy cache i 7 pominięć blokady. Obie próby mają 30 s rozgrzewki i lokalny stub HA. Nie są parowanym dowodem przyspieszenia, pomiarem Pi ani odbiorem opóźnienia urządzeń. Wcześniejsze błędy i pełne lokalne surowe dane pozostają w QA.

## Końcowa walidacja i instalacja testowa

Manifest 628 plików kodu, testów i narzędzi jest identyczny z obiema próbami po poprawce cache, lokalnym pełnym testem i opublikowanym kodem. Późniejszy commit raportów zmienia wyłącznie dokumentację oraz metadane. Pakiet pozostaje testowy: nie ma dowodu ogólnej poprawy Root ani przyspieszenia startu produkcyjnej bazy na Pi. Kolejny eksport zawiera szczegółowe `startup.components` oraz `sqlite.confidence_cache_writes`. Podmiana źródeł wymaga przebudowania dodatku; ponowne trenowanie modeli nie jest wymagane przez ten pakiet.
