# HomeMind Adaptive AI 0.14.174

Log `adaptive-ai-runtime-debug-20261011-110753.json` z wersji 0.14.173 pokazuje plik WAL o rozmiarze 18 993 064 072 B oraz błąd `OperationalError: database or disk is full`. Checkpoint osiągnął maksymalnie 15 852,94 ms. Decyzja trwająca 7 143,01 ms pokrywa się czasowo z checkpointem trwającym 8 500,75 ms. To wskazuje na istotny problem z I/O; sam log nie pozwala przypisać całego zużycia CPU do tej jednej przyczyny.

Wspólny kontekst kandydatów działa: w jednej obserwacji są trzy trafienia i jedno obliczenie prognozy trajektorii. Średni czas podstawowej inferencji wynosi 50,02 ms, ale całej ścieżki 424,23 ms. Niedawny event do decyzji p95 wynosi 3 041,94 ms, CPU 135,53%. Bieżący checkpoint miał wszystkie 89 615 ramek skopiowane, mimo pozostawionego pliku około 19 GB. Rozmiar przydzielonego pliku nie jest rozmiarem nieskopiowanych danych.

## Zmiany

- Wszystkie połączenia piszące Store, także proces treningu, ustawiają `journal_size_limit=67108864`. SQLite ogranicza zatrzymany plik przy bezpiecznym resecie WAL. To nie jest twardy limit aktywnych ramek ani wielkości transakcji.
- Pracownik checkpointów zachowuje zwykły PASSIVE. Po pełnym skopiowaniu danych może spróbować TRUNCATE, gdy fizyczny WAL ma co najmniej 256 MiB. Próby następują najwyżej raz na 60 sekund. Używają `busy_timeout=0`, bez Store.lock. SQLite rozstrzyga wyścigi z nowym czytelnikiem lub pisarzem; zajęta baza jest normalnym wynikiem, nie powodem do oczekiwania lub usuwania pliku poza SQLite.
- Błąd odzyskiwania miejsca nie wyłącza sprawnego pracownika PASSIVE i nie przenosi dodatkowych checkpointów na wszystkie foreground commity. Błędy samego PASSIVE zachowują istniejący mechanizm awaryjny.
- Diagnostyka w RAM pokazuje rozmiar DB, WAL, wolne miejsce, liczbę prób i powodzeń odzyskiwania, zajętą bazę, błędy i odzyskane bajty. Eksport logu nie wykonuje operacji dyskowych.
- Osobny span `sqlite_wal_reclaim` odróżnia odzyskiwanie pliku od kopiowania danych przez checkpoint.

Nie zmieniono zasad treningu, etykiet, wag, trajektorii, warunków jasności ani kwalifikacji sterowania. Aktualizacja nie wymaga Rebuild.

## Walidacja i ograniczenia

13 nowych regresji wykorzystuje prawdziwe pliki SQLite: odzyskanie istniejącego WAL, duża transakcja przekraczająca limit, bezpieczny reset, aktywny snapshot czytelnika, częściowy checkpoint, pisarz rozpoczynający transakcję po wstępnej kontroli, rollback, odstęp między próbami, mały WAL, działanie pracownika, niezależność od Store.lock i diagnostyka. Pełny zestaw liczy 2086 testów.

`busy_timeout=0` ogranicza oczekiwanie na blokady, ale nie czas fizycznego I/O. Pierwsze odzyskanie bardzo dużego pliku może kosztować czas dysku. Limit nie rozwiązuje niedoboru miejsca zajętego przez samą bazę ani długich aktywnych snapshotów. Po instalacji kolejny log powinien potwierdzić spadek WAL, wolne miejsce i p95. Nie deklarujemy zmierzonego przyspieszenia całego HA na podstawie testów funkcjonalnych.

Semantyka została sprawdzona w dokumentacji SQLite: [journal_size_limit](https://www.sqlite.org/pragma.html#pragma_journal_size_limit), [checkpointy](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint), [duże pliki WAL](https://www.sqlite.org/wal.html#avoiding_excessively_large_wal_files).
