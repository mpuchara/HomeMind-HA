# HomeMind 0.15.6 TEST — raport implementacji

Status: kandydat do ręcznej walidacji na Raspberry Pi 5. Brak potwierdzenia na fizycznym urządzeniu. Bez automatycznego merge do main.

## ROOT CAUSE

0.15.5 ponawia odczyt przez NOT INDEXED w spójnym snapshotcie. Pomaga to przy korupcji indeksu, ale fizycznie nieczytelna strona tabeli nadal przerywa RAMReplayIndex. Rebuild resetował model jeszcze przed rozpoczęciem workera. HTTP 500 Recorder i lokalny SQLITE_CORRUPT to odrębne awarie; ten raport nie dowodzi stanu konkretnej instalacji użytkownika.

## FILES CHANGED

`history_recovery.py`: czysta generacja, odzyskiwanie, jawne luki, publikacja transakcyjna. `training_process.py`: staging całej sesji, weryfikacja jakości i publikacja przez rodzica. `history.py`: reset wyłącznie generacji workera, budżet i cache niedostępnych fragmentów Recorder. `storage.py`: limit wielkości każdej zapisywalnej generacji. `replay.py`: diagnostyka encji i jawny stan indeksu RAM. `settings.py`, `config.yaml`, `BUILD_INFO.json`, Dockerfile: wersja i limity. Testy, skrypt walidacyjny i workflow CI.

## RECOVERY STRATEGY

Źródło jest otwierane read-only w transakcji SQLite obejmującej WAL. Metadane są kopiowane logicznie do nowego pliku; nie wykonujemy backupu fizycznie uszkodzonych stron ani napraw źródła. Czytelne rekordy historii są kopiowane porcjami po 128. Po korupcji odczytu wykonywany jest podział stabilnych zakresów id, ze skończonym budżetem prób i głębokości. Dostępny, niezależny indeks pokrywający entity_id/ts/id dostarcza dowodu położenia brakujących rekordów w czasie. Kolejność fizycznych stron nie wyznacza kolejności czasu. Bez takiego dowodu niepewność obejmuje całe okno i trening nie jest publikowany.

## SQLITE CORRUPTION HANDLING

Klasyfikacja przez natywny kod SQLite; BUSY/LOCKED, FULL i I/O nie stają się lukami. Bez DROP/DELETE/REINDEX/VACUUM źródła. Limit domyślny: 4096 prób, głębokość 32, 120 s, cache/bufor 8 MiB i plik generacji 512 MiB. Budżet pamięci nie oznacza całego RSS procesu; nadal obowiązuje istniejący supervisor RSS. Kwarantanna fizycznych kluczy jest zapamiętywana poza źródłem i unieważniana po zmianie sygnatury DB/WAL.

## DATA INTEGRITY GUARANTEES

RAMReplayIndex, temporal replay, statystyki kontekstu i pozostałe odczyty workera używają jednej czystej bazy. Oryginalne id, event_time, received_time i źródła pozostają zachowane. Replay dzieli się na odcinki bez luk; kontekst, checkpointy domu, dziennik obserwacji, otwarty dwell i continuation nie przechodzą przez granicę luki. Odcinek zaczyna się dopiero po rzeczywistych punktach synchronizacji encji. Końcowy otwarty dwell pozostaje cenzurowany. Braki nie są zerowym rewardem ani brakiem zdarzeń.

Rodzic publikuje model, doświadczenia, watermark, benchmark, kursor i neural w jednej transakcji WAL po walidacji konfiguracji/topologii/opcji. Dotychczasowy model i metadane są zachowane w `training_generation_backup`. Nowe id doświadczeń otrzymują spójny watermark. Correct, konfiguracja, kandydaci i inni agenci nie są zastępowani kopią workera. Zmiana danych Correct lub modelu Live podczas treningu blokuje publikację. Kill/restart przed publikacją pozostawia poprzedni stan modelu i kursora.

## TRAINING COMPLETION TEST

`python tools/validate_corruption_recovery.py` uruchamia rzeczywisty child produkcyjnego training_process, weryfikuje zapisany model policy v11 / schema v12, watermark i uczone parametry. Scenariusze: zdrowa baza, fizycznie uszkodzony indeks, fizycznie uszkodzona strona tabeli z dwoma agentami, uszkodzona tabela połączona z luką Recorder. Wyniki liczbowe: `docs/benchmarks/corruption-recovery-0156.json`.

## SHADOW TRANSITION TEST

Wszystkie pięć scenariuszy hosta zakończyły się mode=shadow i progress=1.0. Test dwóch agentów sprawdza również identyczność zapisanego modelu pierwszego agenta po publikacji drugiego. Zdrowy i uszkodzony indeks mają identyczny hash parametrów. Uszkodzona tabela daje inny model, ponieważ niedostępne doświadczenia nie są zastępowane fikcyjnymi.

## PERFORMANCE IMPACT

Dodatkowy etap przygotowuje dyskową generację i indeksy, zachowując izolację procesu, kolejkę, nice, RAMReplayIndex, ograniczone batchowanie i istniejący regulator CPU. Czas hosta zapisany jest w raporcie JSON; nie jest pomiarem wydajności Raspberry Pi. Na częściowej historii cache jest zerowany między odcinkami, co kosztuje czas i pamięć podręczną, ale zapobiega przeniesieniu kontekstu przez lukę.

## REGRESSION TEST RESULTS

Ukierunkowane testy prawdziwej korupcji plików, atomowości, Correct i cache: 11/11 PASS na Windows. Testy produkcyjnego workera: pięć scenariuszy treningu PASS, w tym kill/restart podczas recovery; niewystarczająca historia zachowuje poprzedni model (PASS). Pełna regresja Windows zatrzymała się w istniejącym teście identyfikacji procesu; część wcześniejszych testów zgłaszała błędy platformowe. Rozstrzygającą pełną regresję Python 3.11/3.13 oraz Docker/ARM64 wykonuje GitHub Actions. Wyniki CI wymagają uzupełnienia po zakończeniu.

## KNOWN LIMITATIONS

Brak dostępu do rzeczywistej uszkodzonej bazy użytkownika i brak fizycznego testu Pi/HA. Niedostępny katalog SQLite lub korupcja metadanych modeli/Correct blokują bezpieczne przygotowanie — wymagają osobnej migracji; nie naprawiamy ich automatycznie. Brak niezależnego dowodu czasu utraconych rekordów skutkuje UNKNOWN. Budżet przerwany podczas odzyskiwania nie publikuje niekompletnego modelu. Wiele rozproszonych luk może pozostawić za mało wspólnych, zsynchronizowanych odcinków lub próbek benchmarku. Nie stosujemy arbitralnego progu procentowego pokrycia.

Rzadkie sensory mogą ograniczać synchronizację po luce. Dla częściowej historii sparse long-memory jest wyłączone; nagrody i algorytm uczenia pozostają niezmienione. Opcjonalny neural challenger korzysta z ostatniego bezpiecznego odcinka; Ridge jest akumulowany między odcinkami. Kwarantanna Recorder jest konserwatywna; zdrowe otoczenie znanych luk jest nadal pobierane. Udany Rescan odejmuje odzyskane zakresy od kwarantanny. Supervisor usuwa własną generację po zakończeniu/ubiciu workera. Pozostałości po awarii samego rodzica podlegają retencji zadań; istniejące limity storage nadal obowiązują.

## INSTALLATION PACKAGE

ZIP i SHA-256 powstają przez `tools/package_install_release.py` z bajtów wskazanego commita. Workflow `Pi aarch64 canary / test ZIP (NOT RELEASE)` buduje ARM64 przez QEMU, wykonuje smoke kontenera i publikuje artefakt GitHub Actions. Instrukcja: rozpakować addon ZIP w katalogu lokalnego dodatku `/addons/homemind`, odświeżyć lokalne dodatki, wykonać rebuild i sprawdzić wersję 0.15.6. Przed instalacją zachować pełny backup dodatku i jego danych.
