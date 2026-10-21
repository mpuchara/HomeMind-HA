# HomeMind 0.14.224

## Zgłoszenie i odtworzenie

Po przerwaniu dwóch treningów przy aktualizacji do .223 użytkownik widzi Sending request po Resume/Rebuild. Brak świeżego eksportu .223 z tej próby, więc dokładny stos blokady na urządzeniu pozostaje niepotwierdzony.

Odtworzenie na kodzie .223, przez prawdziwy serwer HTTP i SQLite, pokazuje podatną ścieżkę: obie operacje czekają na Store.lock podczas zapisywania stanu waiting. Klient przekracza timeout, ale oba żądania trafiają do kolejki po zwolnieniu blokady. Dotychczasowe odseparowanie replay od HTTP nie obejmowało tej fazy. Dodatkowo rekord unknown w mapie klienta blokował oba przyciski dla agenta do odświeżenia strony.

## Zmiana kontraktu

Panel wysyła żądanie z identyfikatorem do POST /api/agents/{id}/training-requests. Serwer szybko potwierdza jedynie odbiór do ograniczonej pamięci RAM. Nie nazywa tego trwałym przyjęciem do kolejki. Oddzielny worker wykonuje dotychczasową trasę Resume lub Rebuild, wraz ze wszystkimi strażnikami Candidate, zwolnieniem Control i trwałym zapisem stanu waiting. Dopiero jej sukces 202 oznacza accepted. Żaden zapis modelu ani reset nie trafia do wątku HTTP odbierającego nowy format żądania. Starsze bezpośrednie trasy zachowują dotychczasowy synchroniczny kontrakt.

GET /api/training-requests/{request_id} odczytuje RAM i pokazuje etap, w tym oczekiwanie na dostęp do bazy. Ledger ma limit 128 identyfikatorów, łącznie z aliasami równoległych żądań tej samej operacji; zakończone wyniki są zachowywane przez co najmniej 15 minut. Pełny ledger odmawia nowego żądania zamiast usuwać świeży identyfikator i ryzykować drugie wykonanie. Ta sama para identyfikator/action/agent zwraca zapisany stan; konflikt nie zmienia istniejącego żądania. Ledger nie jest trwałym dziennikiem treningu i znika po restarcie. Trwałe modele i kursory pozostają w istniejącej bazie.

Oczekujące jawne przyjęcie ma pierwszeństwo przed ponawialnymi zapisami diagnostycznymi. Te ostatnie zgłaszają SQLite busy i zachowują dane do kolejnej próby. Nie pomijamy limitów dysku/WAL/rezerwy ani nie przerywamy jawnych zapisów i izolowanych workerów treningowych.

## Zachowanie panelu

Odbiór żądania, oczekiwanie na zapis i faktyczne przyjęcie do kolejki są rozróżnione. Close/Escape zamyka okno, nie zatrzymuje treningu. Po utracie odpowiedzi panel sprawdza identyfikator przez GET; nigdy automatycznie nie ponawia POST ani DELETE. Check request odczytuje stan bez nowego treningu. Jeżeli identyfikator zniknął po restarcie, panel pozwala jawnie ponownie potwierdzić tę samą operację, zachowując identyfikator, aby opóźniony pierwszy POST nie utworzył drugiego zadania. Inna operacja dla tego samego agenta pozostaje zablokowana do wyjaśnienia wcześniejszego żądania.

## Walidacja

Końcowa walidacja: **2762 testy OK / 367.330 s lokalnie**, 19 testów celowanych, entrypoint, testy UI, compileall i 22 skrypty JS. Kod wykonawczy d4183644 przeszedł CI Python 3.11/3.13, dodatkowe kontrole i obraz Docker. Zgodność manifestu 641 plików źródeł/testów/narzędzi potwierdzona. Test realnego HTTP importuje trial_queue_main, otwiera ponownie bazę dwóch przerwanych agentów i rejestruje oryginalne trasy wraz z Candidate guard. Przy zablokowanym Store żądania otrzymują szybkie potwierdzenie odbioru i dostępny status, ale nie udają trwałego queue admission. Po zwolnieniu blokady oba workery HistoryManager startują. Po kolejnym restarcie stare identyfikatory nie uruchamiają niczego automatycznie. Sprawdzamy również zachowanie modeli/kursorów/benchmarków, blokadę Candidate Rebuild, odroczenie zapisu bez utraty wierszy i odzyskanie potwierdzenia przy błędzie po dodaniu zadania.

To test kontrolowanej blokady na hoście. Nie zastępuje eksportu z urządzenia użytkownika ani pomiaru pełnego treningu na Pi. Test UI korzysta z rzeczywistych funkcji aplikacji w modelu DOM. Zasoby RAM i wykres z .223 pozostają bez zmian.

## Instalacja

Podmienić źródła tego samego dodatku i przebudować jego obraz, zachowując identyfikator, konfigurację i prywatne dane. Następnie uruchomić dodatek i mocno odświeżyć panel (Ctrl+F5), aby załadować nowy kod żądań, oraz sprawdzić wersję .224. Dla obu przerwanych agentów wybrać Resume training. Rebuild model nie jest wymagany do tej aktualizacji i nadal oznacza uczenie od początku.

PR: https://github.com/mpuchara/HomeMind-HA/pull/340. CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/39643700315. Raporty w docs/benchmarks/PERFORMANCE_PLAN_0_14_224_*.json. Końcowy commit obejmuje tylko raporty i metadane.
