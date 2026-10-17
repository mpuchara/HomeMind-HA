# 0.14.215: izolacja obliczeń od reakcji agentów

Zgłoszone po restarcie 0.14.214: event → intent p95 557,2 ms w ostatnich 60 s, 357,6 ms w zachowanej telemetrii oraz event → decision p95 496 ms przy 159 próbkach. Nie otrzymaliśmy nowego eksportu ze śladami etapów 0.14.214. Wcześniejsze pliki 0.14.213 nie potwierdzają przyczyny tych nowych wartości.

## Przejrzana ścieżka

1. WebSocket HA i zarejestrowany pipeline zdarzeń przekazują stan do `Engine.on_state_changed`. Aktualizacja stanu, rewizji i bieżącego Home Context pozostaje autorytatywna i zsynchronizowana. Dalsze kroki dodają historię czasową i budzą scheduler.
2. `Engine.run/process/process_target` wybierają zależnych agentów, scalają oczekujące zmiany i uruchamiają pracowników celu. Kolejność dla jednego celu pozostaje zachowana.
3. `Engine.process_agent` rozwiązuje feedback, buduje wersjonowane cechy, wykonuje Ridge/wybrany backend, stosuje Teach, preferencje, warunki światła i dotychczasowe potwierdzenie OFF. Powstaje ukończona decyzja i ActionIntent.
4. `Executor` ponownie sprawdza aktualne ustawienia, model, stan i zależności. Control zachowuje trwałe bramki kwalifikacji, współdzielone zasoby, handoff, cooldown, zapis komendy i wywołanie HA. Shadow nie wysyła usług HA. Powrót usługi i zgodny ACK urządzenia pozostają odrębnymi pomiarami.
5. Candidate Shadow i Sensor Tournament wykonują dodatkowe obserwacje i uczenie. Przeniesienie do wątków w 0.14.214 nie oddzielało kosztownych operacji JSON od interpretera Pythona obsługującego agentów. Ponadto przygotowanie kolejki kopiowało historię całego domu, a serializacja polityki trzymała blokadę predykcji.

W obecnym kodzie `event_to_intent` i `event_to_decision` otrzymują tę samą próbkę na granicy ukończonej decyzji. Dwie różne wartości wyświetlone w osobno odświeżanych widokach nie pozwalają rozdzielić kosztów etapów. Nie zmieniamy tych metryk, próbkowania ani definicji percentyli, aby poprawiać prezentowane liczby.

## Zmiany

- Jeden trwały, prywatny proces wykonuje duże operacje JSON/canonical encoding i dopasowanie klasyfikatorów niezależnych Candidate/Tournament. Nie importuje aplikacji, Store ani klienta HA; nie wykonuje sterowania ani zapisów do bazy. Główny proces zachowuje decyzje, walidację i zapis danych.
- Proces obliczeniowy ma niższy priorytet CPU na Linuksie, jeden wątek BLAS, ramki IPC do 16 MiB i limit czasu operacji 30 s. `runtime_compute_ram_mb` domyślnie 256 MiB, zakres 256–1024 MiB, ogranicza jego przestrzeń wirtualną na Linuksie. To nie pomiar ani rezerwacja RSS. Start wymaga wolnej pamięci ponad ten budżet i dodatkowe 128 MiB. Błąd kończy opcjonalną obserwację z jawną przerwą; nie uruchamia ponownie ciężkiego zadania w procesie sterowania.
- Prywatny cache do 64 MiB i 2048 encji przechowuje odłączone historie sensorów. Zmiana jednej encji unieważnia jej kopię; poprawka metadanych czasu odbioru również unieważnia kopię. Starsze oczekujące obserwacje zachowują własny widok i cutoff czasu. Kolejka nadal nalicza koszt utrzymywanych historii, także podczas aktywnego przetwarzania.
- Przygotowanie historii odbywa się poza globalną blokadą Engine. Polityka odłącza stan liczbowy przed kodowaniem JSON/checksum, więc obliczenia i IPC nie trzymają blokady predykcji. Home Context koduje odłączone snapshoty poza blokadami bieżącego przekonania o obecności.
- Przy przyjęciu SealedPolicy ponownie wykorzystujemy pełne, zweryfikowane kodowanie z cache. Każde użycie nadal sprawdza świeży SHA. Nie zapamiętujemy wyniku weryfikacji checksum.
- Cache 32768 krótkich, jawnie strefowych timestampów HA usuwa powtarzane parsowanie w cechach, lagach i warunkach światła. Timestampy bez strefy zachowują zależność od bieżącej lokalnej strefy czasu.

Pozostają limity bazy/WAL/wolnego dysku/payloadów, kolejność etykiet, utrzymane modele oraz wszystkie bramki Control. Awans/probation i atomowy feedback do żywego modelu zachowują oryginalne zabezpieczenia. Samo wnioskowanie, obsługa Home Context, wspólne SQLite i sieć HA nadal mają koszt; to nie jest gwarancja twardego czasu rzeczywistego.

## Weryfikacja i aktualizacja

Regresje sprawdzają zgodność współczynników, predykcji, JSON i checksum między procesami, zachowanie NaN i Unicode, brak IPC na zwykłej ścieżce, restart procesu, odblokowanie predykcji podczas kodowania, limity ramek oraz historię, cutoffy i poprawki metadanych.

Raporty `benchmarks/COMPUTE_ISOLATION_0_14_215_*.json` zawierają pełne testy i porównania niezależnych źródeł. Obciążenie testowe wykonuje rzeczywiste dopasowanie i serializację czterech zapełnionych polityk, bez dodawania sztucznego oczekiwania. Obie wersje mają po jednym natywnym wątku obliczeń. Akcja i pewność Root są deterministyczne, HA zastępuje stub. Nie są to czasy wdrożonego Raspberry Pi ani fizycznego ACK.

Wyniki trzech naprzemiennych par niezależnych źródeł 0.14.214 i 0.14.215, po 48 zdarzeń w każdej serii:

| Pomiar na hoście | 0.14.214 | 0.14.215 |
| --- | ---: | ---: |
| Control pod obciążeniem CPU, mediana p95 | 100,70 ms | 14,43 ms |
| Root Shadow pod obciążeniem CPU, mediana p95 | 36,79 ms | 6,93 ms |
| Control bez obciążenia CPU, mediana p95 | 8,67 ms | 8,32 ms |
| Powtarzane przygotowanie historii, mediana czasu | 2,47 ms | 0,87 ms |

Wszystkie zdarzenia ukończone; hashe decyzji zgodne. Control wykonał jedno wywołanie stubu HA w każdej serii, Shadow zero. Nie stwierdzono ogólnego przyspieszenia przy braku obciążenia: mediana Control wyniosła 6,09 i 6,55 ms. Pierwsze przygotowanie historii wypełnia cache i może być droższe. Wynik pod obciążeniem dotyczy ochrony reakcji przed opcjonalnymi obliczeniami; nie dowodzi wzrostu przepustowości uczenia ani jednakowej latencji wszystkich trybów Candidate.

Pełna walidacja: **2589 testów, 305,124 s, OK, bez pominięć**, Linux Python 3.12.3 / NumPy 2.3.3. Dodatkowo: rzeczywisty bundle Candidate i pasywny pracownik Shadow, zgodność całych snapshotów i wyuczonych głowic, Control podczas GC, produkcyjny JavaScript w VM, gotowość uruchomienia 0.14.215, kompilacja Pythona i kontrola składni 21 skryptów JS. Manifest 597 znormalizowanych plików źródłowych wiąże pomiary z kodem. Osobny test potwierdza odmowę startu procesu przy niewystarczającym RAM.

Po instalacji 0.14.215 zrestartować dodatek. Full Rebuild nie jest potrzebny. Nowy limit procesu ma wartość domyślną; nie trzeba zmieniać budżetu dysku.

Nowy eksport pokazuje `engine.context_persistence.deferred_observations.temporal_snapshots` oraz `isolated_compute`: trafienia i pamięć cache, PID, aktywność, żądania, błędy, timeouty i limit procesu. Oceniać razem z `event_to_target_start`, `inference`, `event_to_decision`, `context_shadow_capture` i odrębnymi service/ACK. Pomiar z instalacji po tej aktualizacji jest konieczny do potwierdzenia rzeczywistego p95.
