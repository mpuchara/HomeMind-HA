# 0.14.218: odłączenie zapisu Shadow i ograniczenie blokad SQLite

Pakiet na bazie 0.14.217 / PR #333 (`f84211d676cf07c8ef80ea72aa6868d8747f38f7`). Status: **draft, odbiór performance pozostaje otwarty**. Pełna lokalna regresja: **2717 testów OK w 328.914 s**; entrypoint, oba testy UI, compileall i składnia 21 plików JavaScript przechodzą. Manifest obejmuje 624 pliki źródeł, testów i narzędzi.

## Ustalenia i poprawki

Profilowanie pełnej kompozycji wskazało czekanie na Store i częste przekazywanie GIL pomiędzy Pythonem a SQLite. Po usunięciu jednego ograniczenia ujawniało się kolejne. Zachowane próby obejmują także regresje; krótkie korzystne pomiary nie są odbiorem wydania.

- **Oddzielny writer dowodów Candidate.** Predykcja i publikacja kafelka nie czekają na utrwalenie historii ani licznika false-early. Writer ma własne połączenie między turami, bez utrzymywania transakcji podczas oczekiwania. Nie wykonuje predykcji, replay, Executor ani wywołań HA.
- **Ograniczony RAM.** Decyzje i potencjalne powroty false-early współdzielą jedną kolejkę 1024 pozycji oraz zakodowanych payloadów 16 MiB. Opcja `runtime_candidate_evidence_mb` dopuszcza 4–64 MiB. Payload jest niezmienny i naliczany do commitu. Limit nie oznacza limitu całego RSS; odkodowana porcja, obiekty i SQLite mają własny koszt. Pełna kolejka zgłasza jawną lukę, bez usuwania przyjętych danych.
- **Krótsze transakcje i dokładny retry.** Drain może pobrać do 1024 pozycji, ale transakcja decyzji obejmuje najwyżej 32 obserwacje. Potwierdzenie każdego zatwierdzonego podzbioru zwalnia jego dokładny payload. Błąd zachowuje niezapisany sufiks; nie uruchamia ponownie modelu.
- **Liczniki jakości poza predykcją.** Potencjalne false-early trafia do kolejki pod tą samą blokadą co publikacja kafelka. Writer grupuje obserwacje danego Root w FIFO, zachowuje każdą rzeczywistą zmianę i sprawdza epokę generacji ponownie po rezerwacji zapisu. Kursor sesji/sekwencji zapisuje atomowo z licznikami, więc retry po commicie nie liczy wyniku ponownie. Taka aktualizacja nie przyznaje gotowości do awansu, nie wskrzesza odrzuconej generacji i zachowuje blokadę offline.
- **Promote pozostaje świeży.** Oczekujące obserwacje jakości blokują Promote pod blokadą publikacji. Pełny status oraz promocja wyliczają aktualne bramki. Lista UI wykorzystuje jedynie kompletny wynik porównania policzony już dla tej samej karty w tym samym żądaniu. Nie ma TTL zgody na sterowanie.
- **Najnowsza ramka przy przyjęciu Root.** Kolejka Shadow rezerwuje identyfikatory w FIFO, a payload odłącza dopiero przy rozpoczęciu pracy danego Root. Zachowuje dokładny kontekst Parent, generację i czas obserwacji.
- **Usunięta praca o udowodnionym braku skutku.** Pusty standardowy cache etykiet Teaching pomija odczyt konfiguracji. Explore wykonuje jeden świeży SELECT bez poprzedzającego sprawdzania tabeli. Nieaktywny standardowy proposer Trial Knowledge nie buduje rejestru ani sesji. Niestandardowe implementacje zachowują pełną ścieżkę.
- **Lifecycle nie skanuje bezczynnych kart.** Maintenance pobiera tylko queued/building/discarding, zachowując historyczną semantykę pustego stanu i niestandardowych dostawców wierszy. Świeży odczyt braku wygasłych backupów omija pusty DELETE i Store. Nie jest to jeszcze pełne oddzielenie build/finalize od Shadow.
- **Ograniczone grupy VALUES.** Obserwacyjne provenance, Context Row Buffer, archiwum sensorów i Teaching history ograniczają liczbę parametrów i rozmiar instrukcji. Zachowują oryginalne UPSERT, wersje mutacji, received_ts, rollback i durability barriers. Rezerwacja poleceń, dispatch i ACK zachowują dotychczasowe granice synchroniczne.
- **Ochrona dziennika bez iteracji każdego wyniku w Pythonie.** Dokładne maksimum przedziałów zachowuje domknięte końce binary64, przerwy i cofnięcia expiry. Odczyt do 256 próbek zwraca jedną natywną krotkę REAL, bez konwersji floatów do tekstu. Starszy SQLite i przedziały niefinitywne zachowują poprzednią ścieżkę SQL.
- **Bez zbędnych callbacków w czystym odczycie.** SQLite `query_only` wymusza zakaz zapisu, a połączenie czasowo wyłącza wyłącznie własny callback budżetu, który nie wykonuje pracy podczas SELECT. Odtwarza go przed powrotem do trybu zapisu. Zewnętrzne callbacki i anulowanie pozostają. Testy obejmują próby zapisu przez przygotowaną instrukcję i funkcję SQL oraz późniejsze przerwanie zapisu przez budżet VM.
- **Ograniczone usuwanie starych okien.** SQLite zwraca natywnie JSON identyfikatorów tekstowych, najwyżej 64 właścicieli i 256 starych ID na właściciela w jednej porcji. Usuwa linki i okna w tej samej transakcji, zachowuje kolejność oraz cap. Brak funkcji JSON uruchamia poprzednią ścieżkę; błędy blokad nadal powodują rollback. JSON nie przenosi timestampów ani parametrów modeli.

Limity stron, WAL, miejsca wolnego, payloadów, VM i RAM pozostają. Admission jest świeże na wejściu, wyjściu i przy commit oraz w istniejących granicach 50 ms / 1 MiB w porcji. Zwiększenie kolejki nie zastępuje stabilnego tempa zapisu.

## Pomiar i zachowane próby

Metryki Candidate kończą się przy publikacji w RAM, przed trwałym zapisem. Event i receipt pochodzą z tej samej ramki Root. Sam Parent nie tworzy próbki ukończonego Shadow, timer nie pożycza starego receipt. Historia zgłasza pending; Correct nie zastępuje nowszej niezapisanej obserwacji starszym Desired z bazy.

Benchmark uruchamia rzeczywisty entrypoint, polityki, końcowe warstwy pipeline i HTTP. Kontroluje wyłącznie konfigurację i lokalny transport HA. Domyślne modele uczą się na cechach produkcyjnego extractora; predykcje nie są podmieniane. Pierwotna rzadka fixture `legacy` pozostaje wyłącznie krótkim funkcjonalnym smoke Control, z niezmienionym wymaganiem liczby poleceń i bramkami.

Próby 180 s wykazały narastanie kolejek lub luki i zostały odrzucone. Raporty `LONG_CURRENT_20_REJECTED*.json` zachowują rozkłady, serie kolejek oraz SHA i lokalną ścieżkę pełnych surowych danych. Indeks `PERFORMANCE_PLAN_0_14_218_EXPERIMENTS.json` zachowuje również późniejsze krótkie próby i profile.

Po natywnym pruning i grupowaniu zapisów próba 60 s obsłużyła 9896 obserwacji Candidate przy 9903 decyzjach Root, bez luk i błędów UI. Na końcu czekał jeden Root, ale pozostawały 522 operacje dowodów. Root event → decision p95 wyniósł 170,06 ms, Candidate 185,14 ms. To wynik pośredni, bez zatwierdzenia stabilności. Około 159,39 ms p95 przypadało na oczekiwanie przed startem celu, przy samej inferencji 3,89 ms p95. Percentyli etapów nie sumuje się.

Eksperyment diagnostyczny z czterema pracownikami pogorszył p95 Root do 269,37 ms i przyniósł dwa błędy UI. Został odrzucony. Produkt zachowuje pulę 1–2 pracowników i dotychczasową kwantyzację interpretera (domyślnie już 1 ms). Powtórzenie z ustawieniem 1 ms nie jest nową optymalizacją.

Pierwsza długa próba finalnych zmian nie zwróciła JSON: sprzątanie katalogu tymczasowego ścigało się z ostatnim otwarciem bazy przez sygnalizowany, lecz niezakończony wątek. Benchmark oczekuje teraz na Thread Engine/History/EventStream po zamknięciu produkcyjnego main i przed usunięciem katalogu; przekroczenie terminu pozostaje błędem. Nie ukrywa błędów cleanup. Powtórzenie jest wymagane.

## Odbiór i pozostała praca

Najświeższa pełna lokalna walidacja obejmuje 2717 testów. Poprzedni opublikowany commit `59db81f` ma zielone CI: Python 3.11, 2682 testy / 263,277 s; Python 3.13, 2682 testy / 292,892 s; obraz Docker. Nowe źródła wymagają własnego CI. Receipty i wcześniejsze błędy znajdują się w QA.

Pakiet rozszerza E0/E1 oraz części E3/E6. Pozostają: pełna izolacja lifecycle, odłączone przyrostowe ramki i epoka wszystkich mutacji modelu, pozostała praca po decyzji, blokady Home Context, IPC/CPU na urządzeniu, scenariusze build/training/checkpoint i cold/warm start na dużej bazie. Macierz krótkich prób nie zastępuje protokołu odbioru E0–E7.

Brak pomiaru Raspberry Pi, fizycznego urządzenia i startu na kopii bazy użytkownika. Nie ma podstaw do stwierdzenia, że prawie dziesięciominutowy start został usunięty. Schematy modeli i wyuczone parametry pozostają zgodne; Full Rebuild nie jest wymagany. Draft nie jest wydaniem wdrożonym.

## Końcowe grupowanie liczników i dekoracja Confidence

Usunięto kolejne pełne liczenie porównań z listy Confidence. Kompletny wynik tej samej karty jest wykorzystywany ponownie tylko w tym żądaniu. Status pojedynczego agenta nadal liczy świeżo. Późniejsza dekoracja nie może nadpisać `comparison_evidence_pending` i ponownie pokazać zgody na Promote.

Writer przygotowuje liczniki najwyżej czterech Root poza Store, a następnie zapisuje je we wspólnej, zarezerwowanej transakcji. Generacja i bieżąca bramka offline są sprawdzane ponownie na tym samym połączeniu. Nie ma wewnętrznego `conn()` mogącego przypadkiem zatwierdzić połowę grupy. Błąd czwartego Root cofa cztery, błąd kolejnej grupy zachowuje pierwszą już zatwierdzoną. Postcommit retry korzysta z trwałych kursorów. Limity 1024 pozycji / 16 MiB i 32 obserwacji w transakcji decyzji pozostają.

Próba 180 s poprzedzająca końcowe sprawdzenie świeżości lifecycle: Root 29770 próbek event → decision i Candidate 29742 próbek w tym samym oknie (29762 zadania Candidate z rozgrzewką); zero luk, odrzuceń i błędów UI/serwera. P95 Root 153,86 ms, Candidate 172,56 ms. Na końcu czekało 485 operacji dowodów; trend drugiej połowy wyniósł -0,015 operacji/s. To pomiar hosta i tej konkretnej próby, bez parowanego dowodu przyspieszenia.

WAL miał około 979 MB przy automatycznym limicie hosta około 25,5 GB i progu reclaim zależnym od tego limitu. Tego rozmiaru nie utożsamia się z zaległością checkpointu. Końcowe narzędzie zbiera osobno liczbę stron logu, strony pozostałe do checkpointu, odzyskiwanie miejsca i rzeczywistą alokację. Odbiór stabilności WAL i pełny odbiór performance pozostają otwarte.

Próba dodatkowej przerwy 5 ms między turami dziennika została odrzucona i wycofana: pogorszyła p95 do 262 ms oraz zachowała luki. Także próby pogarszające wynik i pełne skróty SHA surowych danych pozostają w indeksie. Poprzedni CI `42fff4ff` zakończył się sukcesem: 2708 testów na 3.11 (290,331 s) i 3.13 (274,492 s), dodatkowe benchmarki oraz obraz. Końcowe źródła wymagają nowego CI.

## Pomiar zamrożonych końcowych źródeł, 360 s

Pełny manifest 624 plików jest zgodny z walidacją 2717 testów i pozostał niezmieniony podczas próby. 20 kwalifikujących się Root, 20 Candidate, 778 encji, UI kafelków co 100 ms i pełne listy co 4 s. Root event → decision p95: 150,32 ms (61064 próbek), Candidate: 168,39 ms (61018 próbek w tym samym oknie). Zero luk, odrzuceń, błędów Candidate, UI i serwera. Odrębne liczniki kolejki obejmują również rozgrzewkę; nie służą do zawyżania pokrycia w oknie pomiarowym. Coalescing zastąpił 42 oczekujące ramki nowszymi, cztery czekały na końcu.

Pozostało 419 operacji dowodów. Trend ostatnich 180 s wyniósł +0,225 operacji/s, więc sam test nie dowodzi zerowego długoterminowego trendu. Kolejka nie przepełniła się. Pełne serie i maksimum są w raporcie. Bez dodatkowej macierzy nie ogłaszamy procentowego przyspieszenia ani odbioru całego planu.

WAL: 72 przebiegi PASSIVE bez błędu, ostatnio 288343/288343 skopiowane strony, 0 zaległych. Zaalokowany plik około 1,19 GB pozostał poniżej progu reclaim 6,38 GB wynikającego z limitu WAL 25,54 GB w automatycznej alokacji hosta. Reclaim nie został uruchomiony. To rozróżnienie rozmiaru pliku i backlogu checkpointu nie potwierdza zachowania rzeczywistej karty SD ani presji dyskowej urządzenia.
