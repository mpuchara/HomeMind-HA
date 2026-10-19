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

Pierwsza długa próba finalnych zmian nie zwróciła JSON: sprzątanie katalogu tymczasowego ścigało się z ostatnim otwarciem bazy przez sygnalizowany, lecz niezakończony wątek. Benchmark oczekuje teraz na Thread Engine/History/EventStream po zamknięciu produkcyjnego main i przed usunięciem katalogu; przekroczenie terminu pozostaje błędem. Nie ukrywa błędów cleanup. Późniejsze powtórzenie 360 s zakończyło się poprawnie; wynik jest poniżej.

## Odbiór i pozostała praca

Najświeższa pełna lokalna walidacja obejmuje 2717 testów. CI końcowych źródeł `6b883526` przeszło na Pythonie 3.11 i 3.13, wraz z dodatkowymi benchmarkami i obrazem Docker. Receipty wcześniejszych wersji oraz błędy prób znajdują się w QA.

Pakiet rozszerza E0/E1 oraz części E3/E6. Pozostają: pełna izolacja lifecycle, odłączone przyrostowe ramki i epoka wszystkich mutacji modelu, pozostała praca po decyzji, blokady Home Context, IPC/CPU na urządzeniu, scenariusze build/training/checkpoint i cold/warm start na dużej bazie. Macierz krótkich prób nie zastępuje protokołu odbioru E0–E7.

Brak pomiaru Raspberry Pi, fizycznego urządzenia i startu na kopii bazy użytkownika. Nie ma podstaw do stwierdzenia, że prawie dziesięciominutowy start został usunięty. Schematy modeli i wyuczone parametry pozostają zgodne; Full Rebuild nie jest wymagany. Draft nie jest wydaniem wdrożonym.

## Końcowe grupowanie liczników i dekoracja Confidence

Usunięto kolejne pełne liczenie porównań z listy Confidence. Kompletny wynik tej samej karty jest wykorzystywany ponownie tylko w tym żądaniu. Status pojedynczego agenta nadal liczy świeżo. Późniejsza dekoracja nie może nadpisać `comparison_evidence_pending` i ponownie pokazać zgody na Promote.

Writer przygotowuje liczniki najwyżej czterech Root poza Store, a następnie zapisuje je we wspólnej, zarezerwowanej transakcji. Generacja i bieżąca bramka offline są sprawdzane ponownie na tym samym połączeniu. Nie ma wewnętrznego `conn()` mogącego przypadkiem zatwierdzić połowę grupy. Błąd czwartego Root cofa cztery, błąd kolejnej grupy zachowuje pierwszą już zatwierdzoną. Postcommit retry korzysta z trwałych kursorów. Limity 1024 pozycji / 16 MiB i 32 obserwacji w transakcji decyzji pozostają.

Próba 180 s poprzedzająca końcowe sprawdzenie świeżości lifecycle: Root 29770 próbek event → decision i Candidate 29742 próbek w tym samym oknie (29762 zadania Candidate z rozgrzewką); zero luk, odrzuceń i błędów UI/serwera. P95 Root 153,86 ms, Candidate 172,56 ms. Na końcu czekało 485 operacji dowodów; trend drugiej połowy wyniósł -0,015 operacji/s. To pomiar hosta i tej konkretnej próby, bez parowanego dowodu przyspieszenia.

WAL miał około 979 MB przy automatycznym limicie hosta około 25,5 GB i progu reclaim zależnym od tego limitu. Tego rozmiaru nie utożsamia się z zaległością checkpointu. Końcowe narzędzie zbiera osobno liczbę stron logu, strony pozostałe do checkpointu, odzyskiwanie miejsca i rzeczywistą alokację. Odbiór stabilności WAL i pełny odbiór performance pozostają otwarte.

Próba dodatkowej przerwy 5 ms między turami dziennika została odrzucona i wycofana: pogorszyła p95 do 262 ms oraz zachowała luki. Także próby pogarszające wynik i pełne skróty SHA surowych danych pozostają w indeksie. Poprzedni CI `42fff4ff` zakończył się sukcesem: 2708 testów na 3.11 (290,331 s) i 3.13 (274,492 s), dodatkowe benchmarki oraz obraz. Nowszy CI `6b883526`, opisany poniżej, potwierdził końcowe źródła.

## Pomiar zamrożonych końcowych źródeł, 360 s

Pełny manifest 624 plików jest zgodny z walidacją 2717 testów i pozostał niezmieniony podczas próby. 20 kwalifikujących się Root, 20 Candidate, 778 encji, UI kafelków co 100 ms i pełne listy co 4 s. Root event → decision p95: 150,32 ms (61064 próbek), Candidate: 168,39 ms (61018 próbek w tym samym oknie). Zero luk, odrzuceń, błędów Candidate, UI i serwera. Odrębne liczniki kolejki obejmują również rozgrzewkę; nie służą do zawyżania pokrycia w oknie pomiarowym. Coalescing zastąpił 42 oczekujące ramki nowszymi, cztery czekały na końcu.

Pozostało 419 operacji dowodów. Trend ostatnich 180 s wyniósł +0,225 operacji/s, więc sam test nie dowodzi zerowego długoterminowego trendu. Kolejka nie przepełniła się. Pełne serie i maksimum są w raporcie. Dodatkowa macierz poniżej ocenia przyspieszenie oddzielnie; ta pojedyncza próba nie jest odbiorem całego planu.

WAL: 72 przebiegi PASSIVE bez błędu, ostatnio 288343/288343 skopiowane strony, 0 zaległych. Zaalokowany plik około 1,19 GB pozostał poniżej progu reclaim 6,38 GB wynikającego z limitu WAL 25,54 GB w automatycznej alokacji hosta. Reclaim nie został uruchomiony. To rozróżnienie rozmiaru pliku i backlogu checkpointu nie potwierdza zachowania rzeczywistej karty SD ani presji dyskowej urządzenia.

## Pięć naprzemiennych par, 20 Root + 20 Candidate

Ta sama nowa fixture produkcyjnych cech i narzędzie dla obu źródeł; 36 s na przebieg, pięć par w odwracanej kolejności, 20 kwalifikujących się agentów i UI. P95 Root event → decision: 124,60 ms w 0.14.217 i 132,89 ms w obecnym kodzie (+6,65%). Bootstrap całych par: od -13,04% do +29,55%. Wynik **nierozstrzygnięty** co do poprawy Root; nie przechodzi odbioru end-to-end. Samo wnioskowanie: 6,61 → 3,28 ms, lecz koszt całego przebiegu i oczekiwanie nie spadły w sposób potwierdzony tym testem.

Liczbę obserwacji Candidate rozliczono jako różnicę licznika na końcu okna i przy jego pierwszej próbce, bez zakończonych zadań rozgrzewki. Pełne liczby, coalescing, luki i błędy każdego przebiegu pozostają w raporcie. Większej liczby wykonanych obserwacji nie utożsamiamy z przyspieszeniem Root.

Przed tą macierzą narzędzie dostało pasywny znacznik fixture emit → ws receive i licznik oczekujących wiadomości. Znacznik jest usuwany przed serializacją wiadomości HA, nie trafia do cech ani do produkcyjnego odbiorcy. Runtime ma ten sam hash co próba 360 s. Zmieniono tylko narzędzie i jego test; wszystkie 22 testy modułu przeszły w 19,710 s. Poprzedni pełny przebieg 2717 testów pozostaje oddzielnym receipt; CI `6b883526` sprawdził także te dodatkowe asercje.

## Pięć par, 3 aktywnych / 20 skonfigurowanych, Shadow

P95 Root event → decision: 15,17 → 9,98 ms (-34,21%). Bootstrap całych par: od -38,44% do -30,11%. Próbki: 10746 / 10756, więc wynik nie pochodzi z ukrytego istotnego zmniejszenia liczby decyzji. Jest to potwierdzona poprawa tego krótkiego scenariusza na hoście. Nie rozstrzyga wariantu 20 aktywnych i nie jest pomiarem fizycznego HA ani dużej bazy instalacji. Pełne błędy i luki poszczególnych przebiegów pozostają w raporcie.

## Pięć par, 3 aktywnych / 20 skonfigurowanych, Control

P95 Root event → decision: 125,20 → 80,88 ms (-35,40%). Bootstrap całych par: od -38,65% do -29,98%; próbki Root 6220 / 8339. Ten wynik dotyczy decyzji przy włączonych bramkach Control i lokalnym transporcie, a nie czasu fizycznej akcji. W oknach pomiarowych jest 12 / 15 próbek service oraz 12 / 15 dopasowanych ACK. Liczba wszystkich poleceń, obejmująca rozgrzewkę i końcowe opróżnienie, wynosi po 15. Pokrycie jest zbyt małe do odbioru p95 usługi/urządzenia. Błędy i luki każdego przebiegu są jawne w raporcie.

CI aktualnego narzędzia i runtime, commit `6b883526`: Python 3.11, 2717 testów OK / 275,440 s; Python 3.13, 2717 testów OK / 278,140 s; dodatkowe benchmarki i obraz Docker przeszły. Package jest celowo pomijane dla PR. Dalsze zmiany raportów nie zmieniają źródeł wykonawczych.

## Debug trace i końcowy status pakietu

Pięć naprzemiennych par po 18 s, te same zamrożone źródła, 3 aktywnych / 20 skonfigurowanych: trace OFF p95 8,48 ms, ON 9,86 ms (+16,26%). Po 5355 próbek. Czas CPU procesu głównego wzrósł z 26,62 do 29,67 s (+11,46%); nie obejmuje procesów obliczeniowych. Nie było luk, błędów ani zaległych komunikatów transportu przy zatrzymaniu wejścia. To krótka próba opisowa. Trace nadal domyślnie jest wyłączony; nie zmieniono jego limitów ani semantyki pomiarów.

Odbiór całego planu pozostaje OTWARTY. Potwierdzona poprawa decyzji dotyczy krótkich scenariuszy hosta z 3 aktywnymi agentami; wynik 20 Root jest nierozstrzygający. Sześć minut bez przepełnienia nie dowodzi nieograniczonej stabilności kolejki. Nie potwierdzono przyspieszenia urządzeń HA ani startu bazy produkcyjnej. Zestaw końcowy: `benchmarks/PERFORMANCE_PLAN_0_14_218_ACCEPTANCE.json`; indeks prób i sumy surowych lokalnych danych pozostają dostępne.

Użytkownik potwierdził sprzęt docelowy: Raspberry Pi 4, microSD 64 GB. Pojemności RAM nie podano. Macierz była wykonywana na hoście, nie na tym Pi. Kolejny etap powinien mierzyć czas zajmowania SQLite, presję zapisów i start dużej bazy na microSD; nie przenosimy budżetów WAL z hosta na kartę. Zachowujemy obecny limit workerów, ograniczone bufory RAM, ochronę wolnego miejsca oraz bramki Control i Promote.
