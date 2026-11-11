# HomeMind 0.15.8 TEST — WAL, snapshoty i wznowienie treningu

Punkt wyjścia: PR #376, commit 01c3bd76c3d4e14f0c39aefe533bbf57844a9a6a. Bez merge do main. Fizyczny Raspberry Pi 5 / HA: walidacja NIE WYKONANA.

## Przyczyna i zakres dowodów

W 0.15.7 BEGIN źródła obejmował całą kopię, throttling, zapisy do generacji i budowę jej indeksów. Czytelnik utrzymywał stary end mark WAL. Równoległe zapisy mogły wyprzedzać checkpoint. Następnie check() traktował przygotowanie jak zapis źródła i przerywał je przy soft wal_budget. Log użytkownika potwierdza przekroczenie budżetu WAL, nie brak wolnego dysku; sam log nie dowodzi, że był to jedyny czytelnik blokujący checkpoint.

Test kontrolny na rzeczywistym SQLite WAL wykonuje identyczne 64 zapisy i checkpointy PASSIVE z długim czytelnikiem oraz przy produkcyjnej kopii partiami. W kontroli pozostają nieprzeniesione ramki; po poprawce wszystkie checkpointy kończą kopiowanie ramek, a nieblokujący worker odzyskuje alokację. Surowe wielkości WAL, busy/log/copied/remaining, czasy i maksymalny czas snapshotu zapisuje tools/validate_training_wal.py w docs/benchmarks/training-wal-0158.json. Liczb z hosta nie przenosimy na instalację użytkownika.

Mechanizm jest zgodny z dokumentacją SQLite: https://www.sqlite.org/wal.html#checkpointing oraz https://www.sqlite.org/pragma.html#pragma_wal_checkpoint.

## Spójność bez wielominutowego czytelnika

Wprowadzono małą migrację metadanych źródła: __training_source_identity, __training_prefix_versions oraz triggery monitorujące mutacje prefiksów tabel treningowych. To jawna zmiana schematu/technicznych metadanych HomeMind. Nie naprawia ani nie zmienia oryginalnych rekordów historii, Correct, modeli czy konfiguracji. Nie dotyka bazy HA Recorder. Nie wykonuje DROP, REINDEX, VACUUM ani ręcznego kasowania WAL/SHM źródła.

Krótka transakcja rejestruje tożsamość źródła, górną granicę rowid i epoch tabel. Dla AUTOINCREMENT używa trwałego sqlite_sequence, również gdy ostatnia strona danych jest uszkodzona. Zwykłe dopisanie rekordu powyżej granicy należy do następnego wejścia treningowego, także gdy jego event_time jest wcześniejszy. Istniejące ts/received_ts oraz event_time/received_time nie są zmieniane. Granica jest dodatkowym warunkiem członkostwa snapshotu, nie zmianą przyczynowości replay.

UPDATE, DELETE lub ponowne użycie id w chronionym prefiksie zwiększa epoch w tej samej transakcji co mutacja. Zmiana chronionego schematu lub triggerów, utrata źródłowej tożsamości albo różnica epoch blokuje wykorzystanie/publikację generacji. Po restarcie porównywane są trwałe granice i wersje, zamiast mtime DB/WAL. Zwykłe appendy i checkpointy nie unieważniają zapisanych kursorów.

Konfiguracje kontrolerów i potrzebny wspólny kontekst są odczytywane razem w krótkiej transakcji do ograniczonego bufora (istniejący limit pamięci przygotowania). Źródłowy czytelnik zostaje zwolniony przed throttlingiem, lokalnym zapisem i raportem postępu. SQLite VM sprawdza anulowanie/pressure i limit wstępnego odczytu. Pozostałe tabele są czytane po maksymalnie 128 rekordów z granicą rowid; źródłowy kursor jest zamykany przed zapisem i checkpointem budżetu. Dotyczy to także odzyskiwania kluczy z czytelnego indeksu oraz fallbacku po PK. Indeksy generacji i trening nie trzymają transakcji źródła.

Correct jest kopiowany według dotychczasowych relacji. Fingerprint powstaje z skopiowanego wejścia, nie z późniejszego widoku aktywnego źródła. Publikacja ponownie sprawdza Correct, model, konfigurację oraz wersję prefiksu i pozostaje atomowa. Zmiana lub utrata zaufania oznacza brak publikacji i zachowanie poprzedniego modelu.

Wersjonowanie jest konserwatywne na poziomie tabel: mutacja starego prefiksu innego agenta może unieważnić przygotowanie. Nie pomijamy takiej zmiany. Nie jest to historyczna baza MVCC przechowująca stare wartości po UPDATE/DELETE; w takim przypadku potrzebne jest nowe wejście, z jawną przyczyną.

## StorageBudget i utrzymanie WAL

Limity WAL, CPU, RAM, page count, katalogu i rezerw wolnej przestrzeni nie zostały zwiększone. check_operation rozdziela source_read, generation_write oraz checkpoint; zapis źródła zachowuje dotychczasową pełną kontrolę. Nowa rejestracja granicy jest zapisem technicznych metadanych i podlega source admission oraz page cap. Wznowienie istniejącej granicy jest tylko odczytem. Izolowana generacja nadal ma własną ograniczoną kwotę. Twardy brak rezerwy i nieznany stan dysku blokują pracę.

Soft wal_budget oznacza odroczenie, nie fikcyjny sukces ani utratę modelu. Wyjście childa zamyka źródłowe połączenia; zatwierdzone partie i kursor pozostają. Nadzorca prosi istniejący WALCheckpointWorker o utrzymanie, czeka bez procesu dziecka i bez Store/SQL lock, a następnie ponawia ten sam descriptor i to samo okno. Backoff 2/4/8 sekund, maksymalnie 5 odroczeń; pojedyncze oczekiwanie maksymalnie 90 s. Wyczerpanie daje jawny powód wal_retry_exhausted/wal_wait_timeout i nie zapętla kolejki.

Worker utrzymania działa na swoim połączeniu/threadzie. PASSIVE nie oczekuje na zamknięcie czytelników. Dotychczasowa rzadziej wykonywana próba TRUNCATE ma busy_timeout=0 i następuje dopiero po pełnym PASSIVE; nie dodano blokującego checkpointu do ścieżki HA ani ręcznego kasowania plików. Wynik busy=0 samego PASSIVE nie wystarcza: raport obejmuje także remaining_frames.

Descriptor running/waiting_wal jest zapisywany atomowo poza SQLite, z liczbą prób. Po zabiciu rodzica istniejąca kolejka odzyskuje go przez zwykłe deduplikowane admission. HistoryManager wznawia go przed ponownym pobraniem Recorder i wyznaczeniem nowego okna Rebuild. Nowy PID/token rodzica jest sprawdzany. Wersja aplikacji, konfiguracja, granice i fingerprinty muszą się zgadzać. Przerwanie już zmodyfikowanej generacji podczas samego replay nie jest przedstawiane jako bezpieczne wznowienie kopii.

## Diagnostyka i Context challenger

Postęp zawiera tabelę, strategię, rekordy i bajty, czasy SQL/CPU/throttle, WAL peak, stan jawnej transakcji i maksymalny czas źródłowego odczytu. Manifest zawiera snapshot_lifecycle i trwałe granice. Czytelnicy/połączenia dotyczą posiadanych zasobów przygotowania; standardowe API sqlite3 nie podaje globalnej liczby czytelników wszystkich procesów. Worker checkpointu raportuje ramki, busy i czas PASSIVE. Estymata oczekiwania SQL nie jest dokładnym pomiarem I/O lub lock wait.

Context challenger started jest zdarzeniem per (agent_id, entity_id, evaluation_started_ts), a nie dowodem utworzenia ciężkiego procesu treningowego. Dwa wpisy w tej samej sekundzie mogą być prawidłowe dla różnych challengerów. Sam wycinek logu nie rozstrzyga ich tożsamości. Zachowano funkcjonalność i kolejkę. Rzadkie ogłoszenie startu jest serializowane i sprawdza bieżący trwały stan, aby uniknąć duplikacji przy równoległych obserwatorach.

## Walidacja i wydanie

Testy rzeczywistego WAL obejmują starvation z kontrolą, soft pressure i bezpieczne wznowienie kopii, rzeczywiste utrzymanie przez nadzorcę, appendy poza granicą, odrzucenie zmienionego/usuniętego prefiksu oraz zabicie procesu podczas waiting_wal i odzyskanie descriptora. Regresje korupcji nadal wymagają rzeczywiście uszkodzonych stron tabel/indeksów, jawnych luk, ochrony Correct, zachowania pierwszego modelu i pełnego treningu drugiego do Shadow. Brak wystarczających danych pozostaje błędem jakości, a nie wymuszonym 100%.

CI Python 3.11/3.13 uruchamia cały zestaw, scenariusze produkcyjnego childa, porównanie milionowej bazy i nowe dowody WAL. Nowy test realtime uruchamia komponowany Engine i normalne polityki podczas rzeczywistego przygotowania generacji na tym samym WAL, porównując przypiętą 0.15.7 z kandydatem. Zbiera p50/p95/p99 wejście Engine → zakończenie procesu decyzyjnego, CPU/RSS oraz utrzymanie WAL. To syntetyczny host, bez fizycznych usług HA. Odczyty UI i opóźnienia zapisów są dodatkowo zbierane w teście dwóch agentów; nie są pomiarem fizycznego Supervisor/Ingress.

Końcowe wyniki i status pełnego CI należy odczytać z uruchomienia commita wydania. Dopiero po zaliczeniu Python, Docker i ARM64/QEMU workflow udostępnia instalacyjne addon/repository ZIP oraz ich SHA-256. Pakiet zawiera dokładne bajty commita i manifesty sprawdzane przez verify_pi_test_artifacts.py.

Na fizycznym Pi 5 pozostają: przygotowanie wielogigabajtowego lokalnego archiwum, wpływ migracji metadanych, snapshoty i WAL peak przy rzeczywistym Recorder/backfill, pozostałe aktywne czytelniki, microSD I/O, maksymalny czas UI, HA websocket/ACK, stabilność połączenia oraz pełne uczenie drugiego agenta do Shadow. Host i QEMU tego nie dowodzą.
