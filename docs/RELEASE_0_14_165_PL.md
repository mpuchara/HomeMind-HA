# HomeMind 0.14.165

Wątek odbierający zdarzenia HA używa teraz jednego połączenia SQLite przez czas trwania WebSocket. Każde zdarzenie nadal wykonuje świeże zapytanie, lecz nie płaci ponownie za otwarcie, ustawienie PRAGMA i zamknięcie połączenia.

## Co pokazał log

adaptive-ai-runtime-debug-20261009-235835.json z 0.14.164: CPU recent 13,17%, od startu 36,54%, RSS 127,48 MiB, event→decision recent p95 175,14 ms z 11 próbek. Brak błędów SQLite, overflow i restartów workerów; checkpoint remaining_frames=0. Są pojedyncze wcześniejsze próbki uczenia, ale w zachowanym fragmencie dominuje obsługa zdarzeń i Shadow.

279 odczytów manual_lifecycle_lookup w 64,94 s dotyczyło sensor.espen4, camera_score, aidetection i stationary_energy. Wszystkie zwróciły zero agentów. Średnio lookup 13,835 ms: prepare 10,714 ms, body 0,798 ms, close 1,565 ms. Poprzednia poprawka usunęła dekodowanie niezwiązanych konfiguracji; dominującym kosztem tego odczytu stało się przygotowanie połączenia.

Różne długości i natężenie okien nie pozwalają przypisać całego spadku CPU poprzedniemu wydaniu.

## Mechanizm i zgodność

HAEventStream otwiera Store.connection_session po wejściu w kontekst WebSocket i zamyka ją przed obsługą błędu oraz reconnect/backoff. Nie tworzy połączenia DB, jeśli otwarcie WebSocket się nie powiedzie.

To istniejąca sesja własności połączenia, a nie wspólna transakcja. Każdy Store.conn nadal niezależnie zatwierdza lub wycofuje zmiany; każde SELECT widzi aktualne zatwierdzone dane. Nie ma cache konfiguracji ani TTL. W czasie ws.recv nie trzymamy transakcji, migawki odczytu WAL ani Store.lock. Inne wątki mają własne połączenia; istniejąca ochrona zagnieżdżonego aktywnego zapisu i przywracanie timeoutów pozostają w Store.

Jedna pamięć podręczna stron SQLite może pozostać przyłączona do rozłączenia; jest ograniczona istniejącym limitem 16 MiB. Reguły źródła korekt, Candidate, kwalifikacji Control, wagi i kontrakt treningu v26 pozostają zgodne.

## Sprawdzenie

11 regresji uruchamia rzeczywisty HAEventStream w osobnym wątku z kontrolowanym WebSocket. Sprawdza 25 zdarzeń na jednym połączeniu, świeżość konfiguracji po zewnętrznym zapisie bez RAM revision, możliwość commit/checkpointu przy otwartej sesji, brak Store lock, wcześniejszy commit przy późniejszym rollbacku, zamknięcie przy auth failure i reconnect, izolację wątków, ukrywanie Candidate, zerowe dekodowanie niezwiązanej encji i przywracanie busy_timeout. Pełny zestaw: 1965 testów.

Benchmark tools/benchmark_target_configs.py --passes 32 --session porównuje świeże zapytania z 0.14.164 i wspólne połączenie. Każdy wynik jest porównywany; dodatkowy pomiar połączeń potwierdza 32→1. Łączny czas partii zawiera otwarcie i zamknięcie sesji. Trzy naprzemienne powtórzenia, 9 i 64 agentów, encja dopasowana i niezwiązana; benchmark działa w CI.

Windows, mediana trzech partii po 32 odczyty przy 9 agentach: dopasowana encja 47,293 → 22,550 ms, niezwiązana 20,939 → 0,863 ms. Są to czasy odczytów konfiguracji, nie prognoza całej decyzji lub CPU HA.

## Instalacja

HomeMind-Adaptive-AI-0.14.165-addon-root.zip lub repository.zip z SHA256. Aktualizacja i restart, bez Rebuild.
