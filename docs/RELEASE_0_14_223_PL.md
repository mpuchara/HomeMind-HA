# HomeMind 0.14.223

## Wykres

W kaflu opóźnienia decyzji i polecenia HA pojawia się dyskretna linia. Każdy punkt pokazuje rzeczywiste p95 z ostatnich 60 sekund; serwer przechowuje maksymalnie 121 punktów z 10 minut, aktualizowanych nie częściej niż co 5 sekund przy standardowym odczycie statusu. Nie ma dodatkowego pollingu ani zapisu wykresu na dysk. Brak próbek i przerwy pozostają lukami. Po starcie historia narasta od pierwszego odczytu; nie jest odtwarzana z przykładowych danych. Rozróżnienie opóźnienia decyzji, wysłania polecenia HA i potwierdzenia urządzenia pozostaje zachowane.

## Wnioski z eksportów 0.14.222

Dwa wcześniejsze treningi zakończyły się niepowodzeniem przy około 79%, z memory_exceeded i limitem RSS 768 MB mimo ponad 5 GB dostępnego RAM. Trwające treningi używają tego samego profilu 768 MB. Replay cache ma 22 tysiące wpisów przy zerowej liczbie przechowywanych wierszy: puste wyniki nie zużywały dotychczas limitu wierszy. Pełny indeks gęstego źródła przekracza 262144 rekordy i przechodzi na powtarzane zapytania SQL. Obserwacje opcjonalne i zapis ich diagnostyki wykazują kilkusekundowe opóźnienia; sama większa karta nie usuwa kosztu obliczeń i transakcji.

Aktualna alokacja dysku wynosi 70%, limit bazy około 50 GiB, użycie około 12.2 GiB, a liczba blokowanych zapisów to zero. Nie zwiększamy automatycznie alokacji, ponieważ te eksporty nie pokazują braku pojemności. Jej zmiana pozostaje dostępna przez storage_auto_use_percent: edytuj, zapisz, uruchom dodatek ponownie i zweryfikuj świeży eksport. Limity bazy/WAL i rezerwa wolnego miejsca pozostają aktywne. 128 GB karty nie oznacza 128 GB dostępnych dla dodatku.

## Zmiany wykonania

Znane stare domyślne limity workerów 1024/768 MB przechodzą na 1536 MB, a 64 MB SQLite cache na 128 MB. Mniejsze niestandardowe limity nadal obowiązują; rozmiar efektywny ograniczają dostępna pamięć, rezerwa rodzica i liczba workerów. Dla zarejestrowanych 7806 MB RAM i 5318 MB dostępnych oba workery mogą mieć po 1536 MB. To pułap RSS, nie natychmiastowa rezerwacja. Profile małych hostów pozostają ograniczone.

Puste wyniki replay mają teraz osobny limit wpisów (maksymalnie 4096), więc sesja nie może kumulować ich bez końca. Gęste źródło, które nie mieści się w indeksie całych 14 dni, dostaje indeks tylko bieżącego fragmentu. Nadal obowiązuje ten sam limit liczby rekordów i dokładny fallback do SQL przy przepełnieniu. Indeks fragmentu nie zastępuje trwałego cache całego przebiegu. Kopie stanów obserwacji współdzielą zweryfikowane pełne ramki; zmiany w miejscu nadal unieważniają cache.

## Walidacja i ograniczenia

Końcowa walidacja: **2757 testów OK / 338.731 s lokalnie**, entrypoint, oba UI, compileall i 22 skrypty JS. Dokładny kod wykonawczy d936d9a8 przeszedł CI Python 3.11/3.13, dodatkowe kontrole i obraz Docker. Zgodność manifestu 638 źródeł/testów/narzędzi potwierdzona. Testy obejmują spóźnione rekordy, brak dostępności sensora, cofanie zapytań, porównanie z wcześniejszym czytnikiem, ograniczenie pustych wpisów, profile małej pamięci i niestandardowe limity. Osobny benchmark mierzy jedynie transport zapytań gęstej historii oraz kopiowanie stanów. Nie jest pomiarem pełnego treningu ani reakcji urządzeń na Raspberry Pi. Zmiany nie naprawiają automatycznie kosztu każdej opcjonalnej analizy i wszystkich zapisów SQLite. Końcowy pomiar na Pi wymaga świeżych eksportów po instalacji.

Do kontynuowania częściowego treningu użyj Resume training. Aktualizacja nie wymaga Rebuild model ani kasowania danych dodatku.

PR: https://github.com/mpuchara/HomeMind-HA/pull/339. CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/39630939956. Raporty w docs/benchmarks/PERFORMANCE_PLAN_0_14_223_*.json. Ostatni commit obejmuje tylko raporty i metadane.
