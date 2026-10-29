# Etap 1: diagnoza regresji runtime i ograniczona optymalizacja

Baza: `d460e0e10f18bcfebcd4565dcfdf54bf8782157b`, 0.14.230,
`codex/performance-capture-20261027`, PR #346. Nie main.
Gałąź: `codex/runtime-regression-stage1-20261029`.
0.14.231 jest już zajęte przez niezależny PR #347; stąd wersja testowa 0.14.232.
Nie przeniesiono zmian snapshotów z #347 do tej gałęzi.

## Diagnoza i granice dowodu

Pierwotna macierz .230 miała tylko 20 zdarzeń na serię, obejmowała rozruch,
nie uruchamiała `start_wal_keeper/start_runtime_cache/start_wal_checkpoint`
wywoływanych przez rzeczywisty `main.initialize_runtime` i nie rejestrowała
generacji Root. Ostatni brak prowadził do powtarzania zapytań `_is_root_live`
zamiast jednorazowego zainstalowania wrappera dokładnego kontekstu.
Nie stosowała też ustawień BLAS i timeslice głównego entrypointu. Czas CPU/wall
obejmował shutdown. Są to błędy reprezentatywności fixture, a nie zmiany algorytmu.
Nie dowodzą same w sobie przyczyny różnicy między dwiema wersjami.

Wstępny profil .230, 4 agentów / 200 zdarzeń po 24 warmup, pokazał scheduler
około 0,17-0,20 ms wall na wywołanie, cechy około 0,48-0,61 ms i predykcję
około 0,17-0,20 ms. Duży koszt dotyczył SQLite, otwierania/konfiguracji połączeń
oraz pracy i oczekiwania writerów; czas oczekiwania Store w tych profilach
dotyczył głównie background evidence. Nie wolno przypisać go w całości Root.
Pełne próbki i finalne porównania uzupełniają poniższe receipts.

## Jedna zmiana produkcyjna

`runtime_sqlite_cache.session_sql_taints` pamięta wyłącznie boolean będący
wynikiem dotychczasowych regexów klasyfikujących tekst instrukcji. Maksymalnie
512 kluczy po 2048 znaków; duże SQL, obiekty niestandardowe i podklasy str
przechodzą ścieżką bez cache. Wyniki są niezmienne. Nie ma cache parametrów,
rekordów, rewizji, praw do zapisu ani decyzji o budżecie.

Klasyfikator jest wywoływany dla każdej instrukcji na połączeniu z cache.
Profil 20-agentowy wykazał 10437 wywołań, około 0,02 ms thread CPU na wywołanie
z narzutem sondy. Osobny mikrobenchmark przed implementacją oraz po niej
porównuje identyczną funkcję referencyjną i SHA wszystkich booleanów.
W pięciu parach / 100 tys. wywołań CPU komponentu spadł z 270-338 ms do 9-11 ms.
To wynik komponentowy; nie jest deklaracją poprawy całego runtime.

Nie zmieniono modeli, RL, kontekstu, snapshotów, kolejności zdarzeń,
przechwytywania Tournament, FIFO, luk, probation, promocji ani Control.
Każda instrukcja SQL nadal się wykonuje. Autoryzacja, świeże odczyty,
admission, checkpointy, commit/rollback i limity WAL/dysku pozostają aktywne.

## Nowe pomiary

`benchmark_runtime_scaling.py` i `compare_runtime_scaling.py`:

- wspólny harness dla dwóch niezależnych drzew źródła; naprzemienne procesy;
- identyczny SHA początkowych stanów i wszystkich payloadów zdarzeń;
- stały syntetyczny zegar event/received wall time, rzeczywiste perf/CPU/monotonic;
- pełne próbki okna (maksymalnie 100 tys. na metrykę), osobny warmup;
- CPU i wall kończą się przed shutdown; CPU wątków przez `/proc/self/task`;
- opcjonalna sonda `--probe`: ingress, context, Engine/Store lock wait i hold,
  kolejka executora, scheduler, cechy, model, capture/execute Tournament i SQL;
- `--probe-overhead`, `--diagnostics`, `--same-source` oraz ablacje fixture;
- `--candidates --training --correct`: aktywne porównywane generacje,
  rzeczywiste update polityki i trwałe zapisy modelu izolowanego agenta,
  rzeczywiste history/point/label/undo Correct oraz kontrola trwałości undo.

Sonda nie jest importowana przez runtime, pozostaje wyłączona domyślnie.
Każda operacja zatrzymuje do 4096 próbek i podaje liczbę odrzuconych.
Spany są inclusive; procesowego CPU ani nakładających się czasów nie sumujemy.
`sqlite_execute` obejmuje VM, I/O, busy wait i przełączanie GIL, nie jest czystym
pomiarem oczekiwania na SQLite. Szczegółowy podział SQLite wymaga dalszego dowodu.
Ingress zaczyna się w Engine; nie mierzy sieci HA. Trening fixture nie zastępuje
14-dniowego replay HistoryManager ani importu Recorder. Correct może zachować
jawną lukę, jeśli nie istnieje obserwowana decyzja; nie wymuszamy jej obejścia.

## Walidacja i status

W toku: końcowa macierz hosta, pełne testy i CI. Do zakończenia Etapu 1 wymagane
są wyniki całej macierzy i wyjaśnienie istotnych regresji. Bez takiego dowodu
status etapu pozostaje otwarty. Brak akceptacji Raspberry Pi i fizycznego HA.

## Instalacja i rollback

Wersja eksperymentalna. Przed instalacją zachować poprzedni pakiet i pełny spójny
backup dodatku/danych SQLite (backup HA albo kopia po zatrzymaniu dodatku, wraz
z WAL/SHM). Podmienić źródła tego samego dodatku, przebudować obraz i uruchomić.
Nie wymaga Full Rebuild modeli ani migracji danych. W razie regresji zatrzymać,
przywrócić poprzednie źródła i przebudować obraz z tym samym katalogiem danych.
Jeśli odtwarzany jest backup bazy, uwzględnić utratę korekt dodanych po backupie.
Nie uruchamiać dwóch wersji na jednej bazie. Bez automatycznego merge/stable.
