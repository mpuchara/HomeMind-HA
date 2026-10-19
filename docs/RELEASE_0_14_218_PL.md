# 0.14.218: niezależny zapis Candidate i krótsze porcje audytu

Drugi pakiet planu performance, na bazie 0.14.217 / PR #333 (`f84211d676cf07c8ef80ea72aa6868d8747f38f7`). Status: draft; pełna regresja kończy się 2681 testami OK w 310.685 s. Entry point, oba testy VM, compileall i składnia 21 plików JS przechodzą. Macierz porównań i długi test są w toku. Nie jest to zakończenie całego E0–E7.

## Co blokowało runtime

Próbkowanie stosów pełnej kompozycji pokazało dwa kolejne ograniczenia. Wątek Candidate czekał na Store podczas utrwalania własnych decyzji, mimo że budowa cech i predykcja trwały ułamki milisekundy. Po odłączeniu zapisu pozostałe writery nadal czekały na porcję provenance, która wykonywała osobne instrukcje SQLite i świeże pomiary dysku dla kolejnych wierszy audytu.

Wykryto też koszt nieaktywnego Trial Knowledge: w trybie Shadow lub przy wyłączonych Experiments odtwarzał cały rejestr encji i czytał sesję, choć standardowy proposer od razu kończył pracę.

## Wprowadzone zmiany

- Jeden osobny writer utrwala wyłącznie niezmienne wyniki Candidate. Predykcje nadal wykonuje dotychczasowy pracownik Candidate; nie dodano mu prawa do Executor ani HA. Writer używa własnego połączenia między porcjami, bez otwartej transakcji podczas oczekiwania.
- Kolejka ma 1024 pozycje i limit zakodowanych payloadów 16 MiB. Opcja dodatku `runtime_candidate_evidence_mb` dopuszcza 4–64 MiB. Przyjęte payloady, również w trakcie transakcji, pozostają naliczane do commitu. Pełny bufor przerywa ciągłość dowodów i zgłasza lukę; nie usuwa poprzednich danych. Limit nie jest limitem całego RSS: pozostają koszty jednej odkodowanej porcji, obiektów i cache SQLite.
- Writer zapisuje maksymalnie 32 obserwacje w atomowej transakcji, ograniczonymi grupami VALUES. Błąd zachowuje dokładny niezapisany payload do retry, bez ponownej predykcji. Zamknięcie zatrzymuje writer przed jawną barierą pełnego zapisu.
- Kolejka Shadow rezerwuje tylko identyfikatory rootów w FIFO. Payload każdego roota jest odłączany tuż przed jego wykonaniem. Zmiana czekającego roota może więc zastąpić starą ramkę, zamiast tworzyć drugi przebieg tej samej generacji po zakończeniu przestarzałego zadania.
- Standardowy matcher Teaching z pustym własnym cache etykiet pomija niepotrzebny odczyt konfiguracji agenta. Po unieważnieniu cache lub przy niestandardowym matcherze pozostaje pełna dotychczasowa ścieżka. Odczyt sesji Explore wykonuje jeden świeży SELECT, z obsługą wyłącznie braku opcjonalnej tabeli; inne błędy są nadal błędami.
- Standardowy nieaktywny proposer Experiments nadal wykonuje własną bramkę i czyści przygotowaną propozycję pod tą samą blokadą. Trial Knowledge nie buduje wtedy rejestru ani sesji. Aktywne Control zachowuje pełną ścieżkę i świeże odczyty. Stary komunikat o braku bliskiej akcji nie może ożywić sondy informacyjnej w Shadow.
- Zapis obserwacyjnych decyzji provenance, zdarzeń i linków używa ograniczonych grup VALUES oraz istniejącego `storage_batch`. Zachowano atomowość całej przyjętej porcji i retry. Ścieżki rezerwacji komend, dispatch oraz ACK pozostały synchroniczne w swoich dotychczasowych granicach.
- Context Row Buffer używa grup VALUES tam, gdzie zachowują semantykę instrukcji. Porcje ogranicza liczba wierszy oraz pełny rozmiar wierszy, bez rabatu za współdzielenie stringów w RAM. Pojedynczy duży zaakceptowany wiersz nadal podlega istniejącemu admission. Między commitami zwalniany jest Store. Niestandardowe wyrażenie VALUES zachowuje executemany.
- Ochrona zdarzeń dziennika jest wyliczana jako rozłączne przedziały dokładnego punktowego maksimum. Dla skończonych timestampów REAL domknięty koniec jest reprezentowany przez wyłączną następną wartość binary64. Zegarów obserwacji nie zmieniono. Zdarzenie pasuje do co najwyżej jednego przedziału. SQLite starsze niż 3.33 i przedziały z nieskończonością lub NaN używają poprzedniego SQL.
- Większa kolejka okien może zapisać do 512 pozycji na turę. Liczba powiązań encji ogranicza turę do 4096, z zachowaniem pojedynczego niepodzielnego wiersza. Szeroki schemat nie mnoży więc pracy przez sam limit pozycji. Jawny flush nadal opróżnia wszystkie przyjęte dane.

Limity stron, WAL, miejsca wolnego, payloadów i VM pozostają. Pomiary admission są świeże na wejściu, wyjściu i przy commit oraz w dotychczasowych granicach 50 ms / 1 MiB wewnątrz porcji. Nie dodano cache zgody na sterowanie.

## Historia i pomiar

Kafelki Candidate są aktualizowane w RAM przed utrwaleniem dowodów. Historia zgłasza oczekujące obserwacje osobno. Correct zwraca lukę, jeżeli nowsza przyjęta obserwacja nie jest jeszcze trwała, zamiast zastępować ją poprzednim Desired z SQLite. Nie odtwarza modelu dla starego punktu.

Nowe `candidate_enqueue_to_decision` kończy się przy publikacji w RAM, przed zapisem. `candidate_deferred_event_to_decision` i `ws_receive_to_candidate_decision` zachowują zegary skopiowane z tej samej ramki Root. Późniejsze sensory nie podmieniają początku pomiaru. Timer bez receipt nie pożycza poprzedniego eventu. Dotychczasowe metryki Root i pasywnego Candidate zachowują znaczenie.

Benchmark nadal uruchamia produkcyjny entrypoint, polityki, finalne warstwy pipeline i HTTP. Nowa fixture uczy modele na cechach wyliczonych przez produkcyjny extractor z naprzemiennych stanów obecności. Nie podmienia predykcji. Odczyt Candidate-live co 100 ms obciąża obie wersje. Co sekundę zapisuje stan kolejek w RAM, aby koniec testu nie maskował narastania backlogu.

Control obejmuje lokalny transport HA i dopasowany ACK. Należy podać liczbę komend w ścisłym oknie pomiarowym; suma `service_calls` zawiera również rozgrzewkę. Mała liczba komend nie wystarcza do wiarygodnej oceny p95 tej ścieżki. Stały hash wejść dotyczy zewnętrznego strumienia sensorów; odpowiedzi ACK wynikają z faktycznych komend danego przebiegu.

## Odbiór i granice

Wyniki końcowe zostaną zapisane w `docs/benchmarks/PERFORMANCE_PLAN_0_14_218_*.json`. Próby `probe218-*` są eksperymentami pośrednimi, a nie odbiorem finalnego kodu. Dotychczasowe wyniki 217 i nowa fixture mają różne kontrakty treningu; końcowe porównanie uruchamia tę samą nową fixture dla obu źródeł.

Pakiet rozszerza E0/E1 oraz części E3/E6. Pełne oddzielenie lifecycle od obliczeń Shadow, przyrostowe odłączone ramki, epoka mutacji modeli, pozostała praca po decyzji, IPC/CPU na docelowym sprzęcie i start dużej rzeczywistej bazy pozostają otwarte. Samo zwiększenie RAM nie jest odbiorem stabilności.

Nie ma pomiaru Raspberry Pi, fizycznego urządzenia ani startu na kopii bazy użytkownika. Nie ma podstaw do stwierdzenia, że prawie dziesięciominutowy start instalacji został usunięty. Schematy modeli i wyuczone parametry instalacji nie zmieniają się; Full Rebuild nie jest wymagany.

Smoke Control zachowuje pierwotne rzadkie cechy treningowe (`--training-fixture legacy`) i niezmienione wymaganie co najmniej trzech komend w krótkim teście. Pomiary performance domyślnie używają produkcyjnego extractora. Pierwszy CI 3.11 wykazał różnicę fixture (dwie komendy); 3.13 i obraz przeszły. Ponowny CI sprawdza końcowe źródła. Licznik opóźnienia Candidate wymaga faktycznego wyniku Shadow; sam wynik Parent nie tworzy próbki.
