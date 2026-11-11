# HomeMind 0.15.7 TEST — selektywne przygotowanie treningu

Kandydat testowy. Nie wykonano wdrożenia ani potwierdzenia poprawki na fizycznym Raspberry Pi 5. Nie wykonano merge do main.

## ROOT CAUSE

Traceback z Pi wskazuje kopiowanie tabel innych niż entity_history. W 0.15.6 przygotowanie generacji kopiowało wszystkie tabele i rekordy wszystkich agentów. Następnie odczytywało całą historię do czasu końcowego, filtrując encje dopiero w Pythonie. Później fizycznie powielało historię i dziennik obserwacji. Limit 120 s obejmował również prawidłową kopię, oczekiwanie I/O i throttling. Zwiększenie timeoutu nie usuwałoby tego kosztu.

## STRATEGIA I TABELE

Wszystkie istniejące tabele zachowują schemat i wymagane indeksy w generacji. Nie zmieniamy schematu ani danych źródła. Dane są kopiowane według jawnego planu w training_generation.py:

| Kategoria | Tabele | Zakres i uzasadnienie |
|---|---|---|
| Konfiguracja | agents | Pełny mały zbiór konfiguracji kontrolerów: zachowanie kontekstu i klasyfikacji urządzeń. Bez historii i modeli innych urządzeń. Koszt widoczny w copy_tables.agents. |
| Wspólny kontekst | app_meta | Jawna lista kluczy Room Belief, Adaptive Presence, Semantic Reliability i metadanych treningu/coverage. Cache UI, eksporty i pozostałe klucze nie są kopiowane. Wartości wspólnego modelu nie są dowolnie przycinane w JSON. |
| Modele | rl_models, tiny_mlp_shadow_models | Wyłącznie wybrany agent. Rebuild nie kopiuje rl_models, które zaraz resetowałby w generacji; digest poprzedniego modelu nadal chroni publikację. Neural zachowuje dotychczasowy kontrakt inicjalizacji. |
| Doświadczenia | historical_experiences | Resume: wybrany agent z oryginalnymi id i watermarkiem. Rebuild: schemat bez starych doświadczeń, które resetowałby worker. Dane zatwierdzonego modelu pozostają w źródle. |
| Correct | rl_feedback, teaching_labels, teaching_rl_labels, manual_context_feedback, manual_feedback_journal/effects, agent_correct_operations, tiny_mlp_correct_batches/samples | Wybrana rodzina agentów tego samego urządzenia. Effects i samples poprzez właściwe feedback_id/batch_id. Ten sam zakres obowiązuje w digestach przy publikacji. Korekty innych urządzeń nie są kopiowane ani zastępowane. |
| Pochodzenie i obserwacje | provenance_events, provenance_history_links, feature_observation_events | Tylko potrzebne encje i czas <= koniec snapshotu. Keyset po event_time/rowid osobno dla encji. Oryginalne received_time pozostaje zachowane. |
| Pozostałe zależności | automatic_reward_experiences, provenance_experiences, agent_candidate_generations, teaching_rl_jobs | Rekordy związane z wybranym agentem/rodziną, jeśli wymagają ich istniejące odczyty. |
| Checkpointy kontekstu | home_checkpoints, adaptive_presence_checkpoints | Zakres potrzebny do recent/sparse replay z horyzontem kontekstu i jednym wcześniejszym seedem. Zachowane późniejsze checkpointy dla kolejnych logicznych chunków. |
| Historia | entity_history | SQL entity_id=? AND ts<=?, osobna encja, keyset ts/id. Nie odczytujemy i nie dekodujemy danych innych encji, aby odrzucać je później w Pythonie. Zachowujemy starsze dane wybranych encji potrzebne dotychczasowym seedom/sparse replay. |
| Telemetria i pozostałe | events, decision_history, cache diagnostyczne, tabele niebędące zależnościami treningu | Schemat, bez danych. Brak kopiowania wielomiesięcznych zdarzeń innych agentów. |

## RECOVERY I INTEGRALNOŚĆ

Źródło jest otwierane read-only, w transakcji SQLite obejmującej WAL. Czytelne indeksy dostarczają klucze tylko wybranych encji. Jeśli wybrany odczyt trafi na uszkodzoną stronę, odzyskiwanie pracuje na tych kluczach, partiami, z ograniczonym podziałem i liczbą prób. Nie odrzuca poprawnego prefiksu ani danych innych encji już zapisanych w generacji.

Kiedy wszystkie indeksy mapujące encje są nieczytelne, pozostaje zachowany kompatybilny fallback po ograniczonych zakresach PK z filtrem encji i czasu w SQL. Nie wykonuje powtarzanego pełnego NOT INDEXED zapytania. W tym wyjątkowym przypadku fizyczne I/O może objąć przestrzeń kluczy źródła: bez czytelnego mapowania nie obiecujemy podliniowego kosztu. Budżet prób/aktywnej pracy nadal obowiązuje. Nieznane granice strat blokują publikację zamiast tworzyć fikcyjną historię.

Nie wykonujemy napraw, DROP, REINDEX ani VACUUM źródła. SQLITE_BUSY/LOCKED oraz FULL/I/O pozostają odrębne od korupcji. Ograniczenia pamięci, dysku i rezerwy wolnego miejsca pozostają aktywne.

Na zdrowej historii recovery_rows jest widokiem. Przy lukach dane są jednokrotnie przemianowane na backing table, a entity_history staje się filtrowanym widokiem. Wybór odcinka aktualizuje jeden rekord recovery_projection. Dziennik obserwacji korzysta z tego samego rozwiązania. Brak CREATE TABLE AS SELECT powielającego całą historię i brak DELETE/INSERT całej historii przy każdej zmianie odcinka. Odczyty RAM/temporal otrzymują tę samą generację; wymuszane nazwy indeksów na widokach są pomijane, a SQLite wykorzystuje indeksy tabel bazowych.

Publikacja policy, doświadczeń/watermarku, benchmarku, kursora i neural pozostaje transakcyjna. Correct i poprzedni model są sprawdzane przed publikacją. Nie wymuszamy Shadow ani 100% przy braku poprawnego modelu i benchmarku. Nie zmieniono rewardów, etykiet ani algorytmu uczenia.

## BUDŻETY, WZNOWIENIE I DIAGNOSTYKA

Zwykła selektywna kopia nie zużywa limitu korupcji. max_recovery_attempts/depth dotyczą rzeczywistych prób odzyskiwania. Zachowany klucz max_recovery_wall_seconds ogranicza aktywny czas CPU etapu korupcji; nie obciąża go throttling ani czekanie na dysk/blokady. Osobno raportujemy elapsed_seconds, cpu_seconds, sqlite_wait_seconds (estymata czasu SQL bez CPU i throttlingu), lock_wait_seconds (zaobserwowane nieudane BUSY/LOCKED), throttle_seconds i recovery_attempts. Sukcesy SQLite nie udostępniają dokładnego rozbicia wait/lock w standardowym Python API; estymaty nie są przedstawiane jako pomiary fizycznego dysku.

Każda partia 128 wierszy zatwierdza dane i kursor kopii w jednej transakcji. Wznowienie jest dozwolone tylko dla tej samej wersji DB/WAL, zakresu encji, agenta, okna i typu zadania. Zmienione źródło unieważnia checkpoint. Zachowujemy maksymalnie jeden plik przygotowania na źródło/agenta; katalog danych ma oddzielną ograniczoną kwotę, a pliki JSON/logów zachowują limit 128 MiB. Nie zwiększono domyślnych limitów RAM/dysku.

UI otrzymuje tabelę, strategię, liczbę odczytanych i skopiowanych rekordów, logiczne bajty partii oraz czasy. Nie wykonujemy COUNT(*) źródła przy każdym raporcie. source_rows_scanned oznacza wiersze przekazane z SQLite po predykatach SQL, nie liczbę fizycznych odczytów stron; digesty Correct mają odrębny licznik. Faza przygotowania ma własny przedział postępu; replay jest raportowany osobno. Worker nie oznacza zakończenia na podstawie samej kopii.

## TESTY I PORÓWNANIE Z 0.15.6

Skrypt tools/benchmark_generation_preparation.py tworzy co najmniej 1 000 000 rekordów szumu plus 960 potrzebnych rekordów, 100 encji, dwóch agentów, 100 000 doświadczeń, 100 000 obserwacji, 50 000 zdarzeń i Correct. Obie wersje wykonują pełny trening, benchmark, zapis modelu i Shadow. Źródło referencyjne jest przypięte do 1dcaf02be52c9a17d41ca9351b5cd8c5b4da4bec.

Wstępny pomiar Windows: kopia 0.15.6 odczytała 1 251 965 rekordów payloadu; 0.15.7 — 964. Czas przygotowania: około 5.30 s vs 0.20 s; cały worker: 10.61 s vs 5.16 s. Hash uczonych parametrów jest identyczny. Są to pojedyncze pomiary hosta, nie Raspberry Pi. CPU/RSS na Windows pozostają null, a nie wymyślone. Linux CI zbiera rusage dziecka i zapisuje wynik w docs/benchmarks/generation-preparation-0157.json; końcowe wyniki wymagają odczytu po zakończeniu CI.

Testy obejmują prawdziwe uszkodzenia stron SQLite, trening drugiego agenta, zachowanie modelu pierwszego, HTTP gaps + lokalną korupcję, kill/restart, brak danych, selekcję Correct po relacjach, wznowienie kopii bez powielania rekordów i wyczerpanie rzeczywistej liczby prób. Test zegara potwierdza, że długi zdrowy copy nie zużywa budżetu korupcji. Symulacja realtime zapisuje dodatkowe zdarzenia historii i wykonuje odczyty UI podczas recovery; sprawdza liczbę zapisów i opóźnienia. Nie jest fizycznym testem HA/Pi.

## WYDANIE I OGRANICZENIA

0.15.7 TEST wymaga pełnego CI Python 3.11/3.13, Docker i ARM64/QEMU. Workflow publikuje addon/repository ZIP oraz SHA-256 po zaliczeniu tych bramek. tools/package_install_release.py pakuje bajty commita, a verify_pi_test_artifacts.py sprawdza manifesty.

Nieznana struktura zależności Correct blokuje przygotowanie zamiast pomijać korekty. Korupcja katalogu/metadanych albo brak wiarygodnych granic strat nadal mogą uniemożliwić bezpieczny trening. Jeśli źródło zmieni się po przerwaniu kopii, jego częściowego snapshotu nie można bezpiecznie użyć — przygotowanie rozpoczyna się dla nowej wersji. Duże dane rzeczywiście potrzebne jednemu agentowi nadal kosztują czas/I/O, z postępem i możliwością anulowania/wznowienia, bez automatycznego podwyższania limitów.

Fizyczny Pi 5 wymaga pomiaru czasu przygotowania na rzeczywistej bazie, RSS, opóźnień HA/Ingress i potwierdzenia pełnego treningu do Shadow. Testy hosta i ARM64/QEMU tego nie dowodzą.
