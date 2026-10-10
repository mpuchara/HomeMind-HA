# HomeMind Adaptive AI 0.14.171

Cały przebieg decyzji jednego agenta korzysta teraz z jednego połączenia SQLite. Obejmuje to istniejące warstwy Live, Candidate, Explore i Sensor Tournament. Poprzednio osobne odczyty generacji i stanu Explore otwierały kolejne połączenia, mimo że blok Shadow miał już własną sesję. Połączenie jest zamykane po każdym agencie, także po błędzie.

## Co pokazały logi 0.14.170

Dwa eksporty z tego samego uruchomienia, oddalone o 46 sekund, nie zawierają błędów blokady bazy ani porzuconych zapisów. Optymalizacja snapshotów działa: 8801 pominiętych kopii i 139 kopii wymuszonych nowym dowodem lub oknem, czyli około 98% pominięć. Przez ostatnie 46 sekund dochodzi pięć pełnych snapshotów przy 52 kolejnych obserwacjach.

Mimo tego `wrapped_inference` zajmuje średnio około 372 ms, a Shadow około 257 ms. W trace są osobne otwarcia SQLite dla odczytów generacji, sprawdzenia tabeli Explore i odczytu aktywnej sesji, zwykle po kilkanaście ms. Wartości CPU z dwóch krótkich okien są bardzo różne; nie stanowią porównania przed i po aktualizacji. Koszty checkpointów WAL pochodzą z osobnego wątku i nie należy dodawać ich do czasu pojedynczej decyzji.

## Zachowane właściwości

Każde wywołanie `Store.conn()` nadal wykonuje aktualne zapytania i niezależnie zatwierdza lub wycofuje swoją transakcję. Zmiana konfiguracji przez inny proces jest widoczna przy kolejnym odczycie w tej samej sesji. Nie ma cache wyników SQL ani długiej transakcji odczytu blokującej checkpoint WAL. Zagnieżdżony odczyt w trakcie aktywnego zapisu nadal otwiera osobne połączenie i nie widzi niezatwierdzonych danych właściciela.

Zagnieżdżona sesja Shadow ustawia limit oczekiwania na blokadę SQLite 250 ms raz na cały blok i przywraca poprzedni limit po jego zakończeniu, także po wyjątku. Limit foreground pozostaje 30 s, a procesu treningowego 60 s. Równoległe cele mają oddzielne połączenia i ustawienia w swoich wątkach. Kolejność zarejestrowanych warstw, walidacja pełnej sumy SHA modeli, kwalifikacja Executor, dane treningowe i zasady sterowania pozostają zachowane.

## Walidacja i zakres wyniku

Dodano 12 regresji z rzeczywistym SQLite i dispatchera Engine: kolejność warstw, świeża konfiguracja, widoczność zapisów przed przekazaniem sterowania, rollback po błędzie, ograniczenie blokady do 250 ms, ponowienie zapisu, niezależność wątków, zamykanie połączeń, brak alokacji po zatrzymaniu i zagnieżdżone limity. Pełny zestaw obejmuje 2043 testy.

Paired benchmark wykorzystuje rzeczywisty dispatcher i odczyty Candidate/Explore oraz 32 przykładowe zapytania Shadow. W każdym z trzech powtórzeń wykonuje 128 decyzji z identycznymi zapytaniami i wynikami. Otwarcia połączeń spadają z pięciu do jednego na decyzję. Mierzony fragment SQL, wraz z otwarciem i zamknięciem, jest 2,08–2,55 razy szybszy na Linuxie. [Pełny raport](benchmarks/INFERENCE_SQL_SESSION_0_14_171.json).

Benchmark nie obejmuje walidacji modeli, obliczania cech, wywołań HA ani równoległego I/O checkpointów. Nie oznacza, że cała decyzja będzie tyle samo razy szybsza. Rzeczywisty efekt należy sprawdzić następnym logiem w porównywalnych warunkach.

## Instalacja

Zaktualizuj dodatek do 0.14.171 i uruchom go ponownie. Ta zmiana nie wymaga Rebuild ani ponownego treningu. Po kilkunastu minutach sprawdź `wrapped_inference`, `event_to_decision`, `context_shadow_observation`, CPU i wpisy `sqlite_session_open`. Oczekiwany efekt to jedno otwarcie sesji na przebieg agenta zamiast dodatkowych otwarć dla każdej warstwy. Pełna walidacja modeli, cechy kontekstu i I/O WAL pozostają kolejnymi kosztami do obserwacji.
