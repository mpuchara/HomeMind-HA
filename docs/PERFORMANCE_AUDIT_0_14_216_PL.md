# Audyt performance i plan poprawek: 0.14.216

Data: 18.10.2026. Audytowany commit lokalny: `cd8a0f817d360cf39351248db7a119f26ea4fca7`, drzewo `abd44d645e0f5f9fd96ba982d00de587e2a5f769`.

## Wniosek

Największy problem to nadal współdzielenie czasu pracowników celu, interpretera i zasobów zapisu przez reakcję oraz prace dodatkowe. Kolejka w tle odsuwa wykonanie części pracy, ale jej przygotowanie nadal zajmuje pracownika celu. Osobny proces w 0.14.215 izoluje wskazane operacje JSON/fit, a nie cały przebieg obserwatorów. Więcej cache RAM pomaga wybranym operacjom, lecz nie zapewniło poprawy całej reakcji w istniejących benchmarkach.

Kolejność prac: **wiarygodny pomiar pełnej kompozycji → usunięcie powtarzanych odczytów pomocniczych → zwolnienie pracownika celu przed pracą opcjonalną → ograniczenie kosztu snapshotów i blokad → izolacja CPU oraz kontrola I/O**. Zmiany startu oceniać niezależnie od opóźnienia decyzji.

## Zakres i jakość dowodów

- Przejrzano przekrojowo wejście HA, scheduler, kompozycję `process_agent`, Root/Hybrid/Teach, Home Context, Candidate Shadow, Tournament, IPC, połączenia SQLite, utrwalanie, start, UI i narzędzia pomiarowe. To audyt ścieżek i zależności, nie deklaracja przeczytania każdej linii repozytorium.
- Kod: lokalna 0.14.216 na `codex/startup-and-runtime-scaling-20261018`. Odczyt `adaptive_ai/config.yaml` z domyślnej gałęzi GitHub podczas tego audytu pokazał **0.14.184**. Domyślna gałąź, paczka dodatku i audytowany commit nie są tym samym artefaktem; do każdego pomiaru trzeba dołączać wersję i hash źródeł.
- Dane z urządzenia: trzy eksporty `adaptive-ai-runtime-debug-20261018-111115/111206/111228.json`, wszystkie **0.14.215**. Brak eksportu wdrożonej 0.14.216 i brak faz startu z czasu zgłoszonych prawie 10 minut.
- Wykorzystano zapisane wyniki walidacji 0.14.216: 2602 testy OK, porównania niezależnych źródeł, zgodność ramek i modeli. W tym audycie nie uruchamiano ponownie pełnej regresji i nie zmieniano kodu wykonawczego.
- Dowody uzupełniające: `benchmarks/PERFORMANCE_AUDIT_0_14_216_EVIDENCE.json`. Zawierają hashe wejść, analizę wyłącznie zapytań z pracowników `device-control` i rozdzielenie CPU/wall time.
- P95 poszczególnych etapów nie sumują się. Zachowane fragmenty trace są ograniczone i częściowo mogą obejmować współbieżne operacje. Off-CPU obejmuje m.in. I/O, GIL, blokady i planistę; nie dowodzi samodzielnie przyczyny oczekiwania.

## Punkt odniesienia

| Pomiar | Wynik | Interpretacja |
| --- | --- | --- |
| Urządzenie .215, event → decision p95, ostatnie 60 s | 567–690 ms | Problem potwierdzony dla .215, nie jest wynikiem .216. |
| Urządzenie .215, event → target start p95 | 338–434 ms | Opóźnienie występuje również przed rozpoczęciem pracy celu. |
| Urządzenie .215, inference p95 | 210–234 ms | Sam model numeryczny nie jest jedynym kosztem wnioskowania. |
| Urządzenie .215, context_shadow_capture p95 | 187–237 ms | Przygotowanie pracy opcjonalnej ma istotny koszt. |
| Zachowane 197 spanów inference_agent | średnio 252 ms wall, 74 ms CPU, 178 ms off-CPU | Potrzebny pomiar oczekiwania, nie tylko profiler CPU. |
| Host .215 → .216, Store + HistoryManager, 200 tys. rekordów | 78,70 → 8,73 ms | Potwierdzona poprawa komponentu startowego, bez pomiaru całego startu Pi. |
| Host .215 → .216, ciepły capture 778 encji, mediana | 3,12 → 2,68 ms | Lokalna poprawa około 16%, bez dowodu poprawy całej reakcji. |
| Host .215 → .216, Control z obciążeniem CPU, p95 | 15,42 → 16,98 ms | Nie potwierdza przyspieszenia reakcji. |
| Host .215 → .216, Root Shadow z obciążeniem CPU, p95 | 6,41 → 7,66 ms | Wynik gorszy; krótkie scenariusze nie ustalają istotności ani przyczyny. |

## Ustalenia

### F01, P0: benchmark reakcji nie obejmuje pełnego runtime

**Potwierdzone w kodzie.** `tools/benchmark_control_priority.py:26` buduje Engine i instaluje Tournament. Około linii 71–78 wyłącza housekeeping, resync, timer i warmup; akcja/pewność Root są deterministyczne. Produkcyjna kompozycja w `runtime_composition.py:242–325` instaluje dodatkowe usługi, m.in. Candidate, provenance, observation, drift, confidence i Tiny MLP.

Benchmark jest przydatnym testem komponentowym, ale nie odtwarza całego kosztu instalacji. Test capture 778 encji również jest osobnym testem, nie pełną reakcją 20 agentów na zdarzenia 778 encji. Dlatego krótkie wyniki kilkunastu milisekund nie wyjaśniają pomiarów urządzenia.

**Poprawka:** nowy benchmark uruchamiający `trial_queue_main.py` i rzeczywistą końcową kompozycję, z kontrolowanym transportem HA, zapisanymi agentami/modelami/generacjami oraz kontrolowanym strumieniem zdarzeń. Zachować obecne benchmarki i poprawnie je nazywać. Przyspieszeń nie zatwierdzać wyłącznie na fixture z podmienionym Root.

### F02, P0: początek mierzonej latencji pomija część wejścia

**Potwierdzone w kodzie.** `HAEventStream.run` najpierw odbiera i parsuje komunikat, obsługuje stan socketu i wywołuje pipeline. `Engine.on_state_changed` zapisuje `received_perf` dopiero wewnątrz `engine.lock`, około `engine.py:626`. Pomiar może pomijać wcześniejsze oczekiwanie na blokadę oraz pracę nakładek przed wejściem do bazowej metody. Nie mierzy czasu powstania stanu w samym HA ani wcześniejszego buforowania w bibliotece socketu.

Istnieje bogatszy `engine.inference_hot_path_metrics`, udostępniany przez status w `queue_main.py:88`, ale `runtime_debug_log.export_payload` nie dołącza tego snapshotu. Utrudnia to podział `post_predict` na Hybrid, Composer i inne koszty w dostarczonym pliku.

**Poprawka:** dodatkowy monotoniczny znacznik zaraz po powrocie `ws.recv`, korelacja event/decision/generation, osobno parse/ingress-lock/context/scheduler/worker/decision/service/ACK. Zachować dotychczasowy licznik i jego nazwę jako legacy, a nowemu nadać odrębną wersjonowaną semantykę. Eksportować istniejący zbiór etapów. Mierzyć narzut debugowania włączonego i wyłączonego.

### F03, P1: decyzja gotowa nie oznacza zwolnienia pracownika celu

**Potwierdzone w kodzie i zgodne z symptomem kolejki .215.** `Engine.process_target` (`engine.py:1585`) wykonuje cały zarejestrowany `process_agent` w jednym zadaniu. `Engine.process` (`1461`) czeka z ponownym przetwarzaniem tego celu do zakończenia jego Future. Po bazowej decyzji nakładka Tournament nadal wykonuje `submit/_capture` (`context_tournament.py:805`, `tournament_observation_queue.py:142`). Zewnętrzna nakładka Tiny MLP może po `original()` wykonać jeszcze `service.observe` (`tiny_mlp_shadow.py:806`). Hybrid potrzebny do wyboru akcji jest osobnym przypadkiem i musi pozostać przed decyzją.

**Poprawka:** jawne rozdzielenie fazy autorytatywnej od obserwacji. Pracownik celu publikuje kompletną decyzję i lekki, odłączony pakiet obserwacji; dodatkowe ewaluacje wykonuje planista obserwatorów. Nie wystarczy przenieść kosztownego `_capture` do innego wątku, jeśli nadal przygotowuje go pracownik celu lub drugi wątek walczy o ten sam GIL.

**Warunek spójności:** snapshot musi odpowiadać decyzji i czasowi zdarzenia. Bez odczytu „najnowszych” danych już po przekazaniu zadania; zachować kolejność zmian targetu, cutoff i jawne przerwy przy przeciążeniu. Synchroniczne bramki promocji/probation i Control pozostają jawne, a ich czas jest raportowany.

### F04, P1: capture nadal skaluje się z całym domem

**Potwierdzone w kodzie; dominująca część kosztu w .216 wymaga nowego trace.** `TemporalObservationSnapshots.capture` przechodzi wszystkie encje. `DetachedMappingSnapshots.capture` (`detached_mapping_snapshots.py:14`) pod wspólną blokadą cache przechodzi całą mapę i porównuje pełne rekordy. `_capture` kopiuje rejestr oraz serializuje niemal całe `vars(policy)` pod `policy.lock`. Cache ograniczył powtórne kopiowanie, ale nie usunął pełnych przebiegów ani kosztu polityki.

**Poprawka:** wersjonowane, niemutowalne ramki stanu aktualizowane przy ingest; przy przekazaniu referencja do ramki i różnic, zamiast kolejnego obchodzenia całego domu dla każdego agenta. Oddzielna wersja topologii rejestru. Współdzielone snapshoty modelu z poprawną epoką mutacji i ograniczonym czasem życia. Nie ograniczać dowolnie sensorów puli, bo zmieniłoby to uczenie.

**Istotne:** `model_revision` i `tournament_revision` nie wystarczą jako jedyne klucze cache modelu. `fast_light_objective.py:548–554` celowo aktualizuje wagi bez zmiany tych identyfikatorów. Potrzebna osobna epoka aktualizacji wag, walidacji i decay, z testami wszystkich mutacji.

### F05, P1: pomocnicze odczyty SQLite nadal występują na pracownikach celu

**Potwierdzone w kodzie i zachowanych śladach .215.** Po odfiltrowaniu UI i pracowników tła pozostają m.in.:

| Wywołanie na device-control | Liczba spanów | Średni wall | Najdłuższy span |
| --- | ---: | ---: | ---: |
| `agent_candidate_user_promotion._active_observation_row` | 130 | 17,9 ms | 624,0 ms |
| `agent_explore._table_exists` | 197 | 11,6 ms | 496,8 ms |
| `agent_candidate_blocked_shadow_evidence._active_comparison_edge` | 64 | 22,1 ms | 137,7 ms |
| `agent_explore._active_free_for_live` | 197 | 4,7 ms | 63,0 ms |

Wszystkie te zarejestrowane wywołania korzystały z pożyczonego połączenia. Nie można ich tłumaczyć samym otwieraniem SQLite. Wall time nie jest czasem wykonania zapytania na CPU i zawiera oczekiwanie. `_active_observation_row` jest wywoływane przez bramki before/after; `agent_explore.py:756` pyta o sesję przed delegacją do bazowego `_finish`, który sam sprawdza, czy istnieje trial.

**Poprawka:** usuwać zbędne wywołania tam, gdzie dokładny stan RAM dowodzi nieaktywności; istnienie tabel ustalać przy kompozycji. Dla aktywnego przypadku jeden niemutowalny kontekst lifecycle na daną obserwację, współdzielony przez before/after. Unieważnianie przy każdej właściwej mutacji i po poprawnym commit; uwzględnić rollback, raw SQL i izolowane procesy. Bez TTL jako dowodu aktualności i bez cachowania zgody na fizyczne sterowanie.

### F06, P1: Candidate Shadow współdzieli wykonawcę z lifecycle i maintenance

**Potwierdzone w kodzie, skala opóźnienia jest hipotezą do pomiaru.** `agent_candidates.py:1221–1259` najpierw przechodzi lifecycle/build/finish, a potem wywołuje `_maintenance`. Drain Shadow jest wewnątrz owiniętej maintenance (`agent_candidate_shadow_runtime.py:1459`). Wczesne drain wewnątrz maintenance nie omija wcześniejszego `_finish_build_if_ready`. Wspólna sesja połączenia obejmuje cały pass.

**Poprawka:** osobne kolejki pilnych obserwacji i lifecycle, deadline oraz limit czasu pracy na przebieg; długie walidacje/finalizacja poza wykonawcą świeżych obserwacji. Fair scheduling po root, bez blokowania wszystkich przez jedną generację. Mierzyć wiek ostatniej ukończonej obserwacji na kafelku i maksymalny wiek backlogu, nie samą liczbę wybudzeń.

### F07, P1/P2: Home Context trzyma wspólną blokadę podczas obliczeń

**Potwierdzone w kodzie, udział w p95 wymaga profilowania.** `ContextEngine.forecast` (`context_engine.py:466`) pod `context.lock` przygotowuje reliability, wykonuje `home.forecast` i augmentację obecności. Ingest `Engine.on_state_changed` wywołuje `context.observe` pod `engine.lock`, co łączy opóźnienie wejścia z dostępnością kontekstu. `SemanticReliability.evaluate` przy syntetycznym disagreement może dla jednego źródła przejść inne źródła (`semantic_reliability.py:474`). Cache adaptacyjny jest czyszczony do jednego wpisu przy nowym wyniku (`context_engine.py:450`), więc zmiana obszaru/czasu ogranicza ponowne użycie.

**Poprawka:** najpierw oddzielne pomiary oczekiwania i pracy pod obiema blokadami. Potem odłączony wejściowy snapshot reliability/obszaru i publikacja po zgodności rewizji, indeksy źródeł oraz cache per obszar z pełną semantyką czasu. Nie zaokrąglać dowolnie czasu forecast ani nie korzystać z danych przyszłych, aby zwiększyć trafienia cache.

### F08, P2: jeden proces obliczeniowy nie daje priorytetu świeżym zadaniom

**Potwierdzone w kodzie.** `BackgroundCompute.call` (`runtime_background_compute.py:72`) szereguje wywołania wspólnym RLock i blokującym request/response. Limit 30 s dotyczy własnej operacji po przejęciu blokady, nie całego oczekiwania klienta na ten lock. Duży JSON może opóźnić fit/obserwatora. `dumps` wykonuje pickle przy sprawdzaniu progu, a `write_frame` koduje żądanie ponownie; `roundtrip` zawsze używa IPC w scope.

**Poprawka:** pomiar osobno queue-wait/encode/pipe/compute/decode i rozmiaru; usunięcie podwójnego kodowania oraz mierzone progi opłacalności. Dopiero potem dispatcher z priorytetami lub osobne ograniczone wykonawce JSON/fit. Pełna izolacja CPU tworzenia cech i ewaluacji obserwatorów jest kolejnym etapem, z typowanymi wejściami i walidacją wyników w procesie głównym. Nie zwiększać liczby procesów bez łącznego budżetu RAM/CPU.

### F09, P2: wiele writerów konkuruje o jeden SQLite i I/O

**Potwierdzone w kodzie i logach .215; przyczyna konkretnego p95 nieustalona.** Są osobne writery provenance, feature journal, Tournament, fast-light, housekeeping, GC i checkpoint. `ContextRowBuffer.flush` (`context_row_buffer.py:143`) wykonuje cały batch pod Store.lock; rozmiar batcha jest ograniczony liczbą/bajtami, a nie czasem commit na danym nośniku. `StorageRetry` ogranicza część SQLITE_BUSY, ale nie czas oczekiwania na Python RLock ani fizyczne I/O. Sam fakt użycia WAL nie usuwa pojedynczego writer lock SQLite.

**Poprawka:** zapisy opcjonalne w mikro-batchach, osobno przygotowanie danych i sekcja zapisu, monitoring Store.lock/commit i wieku utrzymywanych kolejek. Priorytetyzacja trwałych komend/feedback nad diagnostyką. Porównanie UI/GC/checkpoint osobno i łącznie. Centralny arbiter zapisu dopiero po inwentaryzacji barier trwałości, bez łączenia transakcji o różnych kontraktach. Zachować page/WAL/free-space/payload admission oraz retry/rollback.

### F10, P2/P3: start jest lepszy, ale nie ma jeszcze dowodu czasu całego urządzenia

**Potwierdzone w kodzie.** .216 usunęło agregacje archiwum z konstrukcji HistoryManager i poprawiło granice SQL. Nadal przed ready są importy, migracje, odczyt checkpointów, kompozycja usług, pobranie HA states i reconcile. Candidate startuje w `fast_queue_main.py:184`, checkpoint w `main.py:136`, retencja w `main.py:163`, zanim osiągnięto ready. Nowy overview po 20 s może skanować archiwum przy każdym restarcie, bez harmonogramu uwzględniającego aktywność realtime.

**Poprawka:** profil całego startu, rozdzielenie niezbędnej kompozycji zabezpieczeń od opcjonalnych prac, bramka uruchamiania maintenance po gotowości i polityka wieku zapisanego overview. Migracji nie odraczać, jeśli bieżące sterowanie wymaga nowego schematu. Mierzyć cold/warm boot na kopii rzeczywistej bazy, nie tylko pusty smoke test.

### F11, P3: RAM i cache SQLite wymagają priorytetów oraz pomiaru, nie celu procentowego

**Potwierdzone w kodzie, niedobór slotów jest hipotezą.** Pool ma maksymalnie cztery połączenia (`runtime_sqlite_cache.py:127`); te same lease mogą otrzymać Root, Candidate i Tournament. Odczyty HTTP poza lease otwierają zwykłe połączenia. Logi nie dowodzą, że brak slotu jest głównym ograniczeniem. Limity 256 MiB SQLite, 64 MiB kolejki, 64 MiB historii i 16/8 MiB nowych cache to różne pule, nie pomiar faktycznego RSS ani jeden wspólny budżet.

**Poprawka:** wspólny raport wykorzystania, ewikcji, dup-charge i headroom oraz rezerwacja dla realtime dopiero po potwierdzeniu konkurencji. Ocenić nowe cache w A/B z identycznym workloadem; usuwać lub warunkować te bez zysku end-to-end. Zwiększenie page cache nie usuwa Store.lock, GIL ani kosztu SELECT/JSON.

## Plan wdrażania

Estymacja S/M/L oznacza względną wielkość zmiany, nie zobowiązanie czasowe. Każdy etap kończy osobny PR i porównanie tego samego strumienia wejściowego. Etapy można odrzucić, jeśli zwiększają koszt lub naruszają zgodność.

| Etap | Priorytet / rozmiar | Konkretna zmiana | Warunek odbioru |
| --- | --- | --- | --- |
| E0 | P0 / M | Pełny production benchmark, znaczniki wejścia, eksport istniejących etapów, provenance wersji/commitu. | Uruchomiona dokładna kompozycja produkcyjna; brak ukrytych podmian Root w teście end-to-end; raport narzutu pomiaru i osobne metryki timer/event. |
| E1 | P1 / S–M | Usunięcie zbędnych table/session reads i jeden lifecycle read-context before/after. | Brak SQL dla udowodnionej nieaktywności; poprawne unieważnianie promote/discard/feedback/rollback/raw SQL; zgodność decyzji i bramek Control. |
| E2 | P1 / L | Jawny koniec pracy krytycznej celu, asynchroniczne dodatkowe obserwacje i przyrostowe snapshoty. | Zablokowany observer nie przetrzymuje następnego zdarzenia celu; brak pełnego skanu domu i serializacji całego modelu w stałym koszcie każdej decyzji; dokładne stare ramki/cutoff/target edges. |
| E3 | P1 / M | Osobne deadline/budżety Shadow i lifecycle Candidate. | Wolna finalizacja jednej generacji nie opóźnia wszystkich Shadow; kontrolowany wiek backlogu; zgodność przyszłych etykiet i brak HA z Candidate. |
| E4 | P1/P2 / M–L | Krótsze blokady Home Context i obliczenia reliability/forecast na wersjonowanych wejściach. | Udokumentowany spadek lock-wait i ingress delay; ta sama prognoza przy tym samym czasie i stanie; brak nowych danych przyszłych. |
| E5 | P2 / M–L | Mniej IPC, jawna kolejka/priorytety i dalsza izolacja CPU obserwatorów. | Zmierzony zysk end-to-end oraz przepustowość uczenia; łączny limit RAM/CPU, restart/timeout i parity; Root nie czeka na opcjonalny compute lock. |
| E6 | P2 / M–L | Mikro-batche i priorytety writerów; tuning checkpoint/GC na pomiarach. | Malejące ogony lock/commit, stabilne kolejki i WAL; wszystkie durability barriers, retry i limity dysku zachowane. |
| E7 | P2/P3 / M | Harmonogram startup/background oraz polityka wieku overview. | Cold/warm start na dużej bazie; szybkie ready po pełnej kompozycji zabezpieczeń; brak burstu maintenance pogarszającego pierwsze minuty pracy. |

**Pierwszy pakiet do wykonania:** E0 + E1. Następny: E2 + E3. E4 wybierać na podstawie nowego pomiaru blokad. Dalszych cache/procesów nie dodawać przed wynikiem tego pomiaru. Zmiana liczby workerów, interwałów i próbkowania nie zastępuje usunięcia zbędnej pracy.

## Protokół pomiarowy i kryteria

1. Zachować baseline .215 i .216 oraz kopię reprezentatywnej konfiguracji/modeli. Do testu przygotować zanonimizowany, uporządkowany strumień z czasami event i receive, z burstami LD2410, zmianami celu i korektami metadata. Transport HA w automatycznych testach jest stubem; nie wysyłać poleceń do instalacji w ramach audytu.
2. Macierz: 3 aktywnych agentów oraz większa konfiguracja do 20 rzeczywiście kwalifikujących się agentów; osobno 20 skonfigurowanych z 3 wykonującymi inferencję. 778 encji, cold/warm history/model cache, duża baza. Osobno Control, Root Shadow i Candidate Shadow, bez utożsamiania tych trybów.
3. Scenariusze: spoczynek; częsty strumień sensorów; burst; UI otwarty/zamknięty; obserwatorzy; lifecycle build/finalize; trening; checkpoint; GC; kombinacje obserwowane na urządzeniu. Przełączenia diagnostyczne w benchmarku nie są zmianą domyślnego produktu.
4. Co najmniej pięć naprzemiennych par uruchomień, ustalona rozgrzewka, minimum kilka minut stabilnego pomiaru na scenariusz; raport wszystkich serii i zmienności. Bootstrap confidence intervals dla p95, jeśli liczba prób na to pozwala. Nie wybierać najkorzystniejszego runu.
5. Raport: p50/p95/p99/max, event/decision ID, CPU/wall, lock-wait, SQL prepare/body/commit/close, worker-release, event → decision, decyzja → publikacja UI, wiek Candidate, luki/coalescing i opóźnienie zapisu. Hardware Control dodatkowo service request/return i ACK skorelowany z tą samą komendą; nie mieszać go ze Shadow.
6. Proponowana początkowa bramka PR: co najmniej 20% poprawy p95 w zdefiniowanym obciążonym scenariuszu, bez udowodnionej regresji ponad 5% w pozostałych obowiązkowych scenariuszach. Przy niepewności wynik jest nierozstrzygnięty, a nie PASS. To propozycja kryterium walidacji, nie prognoza osiągniętego zysku.
7. Proponowany cel produktu do sprawdzenia na docelowym sprzęcie: event → decision p95 poniżej 100 ms dla kwalifikujących się Root Control/Shadow w uzgodnionym strumieniu. Osobne SLA wieku ukończonej obserwacji Candidate ustalić po pomiarze E0; nie deklarować go na podstawie 33 historycznych próbek. Cel nie jest aktualnym wynikiem ani gwarancją.
8. Niezależne warunki: żadnych dodatkowych odrzuceń prawidłowych intencji, pogorszenia jakości modeli ani ukrytego spadku uczenia; raport obsłużonych zdarzeń i gapów. Porównania obejmują wektory/metadata, predykcje, future-label pairing, kalibrację i zgodność lifecycle, nie tylko Desired.
9. RAM ma być ograniczony i użyteczny. Liczyć sumę procesów, aktywnych ramek, page cache, pamięci cache oraz realny headroom hosta/cgroup; RSS procesu głównego nie jest całym kosztem. Nie celować w określony procent zajętej pamięci.

## Granice planu

W planie nie ma usuwania kontroli świeżości, OFF confirmation, kalibracji, trwałych komend/feedback, handoff, bramek awansu ani limitów dysku/WAL. Część tych etapów pozostanie synchroniczna z powodu semantyki. Ich czas ma być jawny.

Na podstawie tego audytu nie można obiecać, że .216 reaguje szybciej na urządzeniu, ani że poprawki cache skróciły prawie dziesięciominutowy start do konkretnej liczby sekund. Potrzebny jest pomiar pełnej kompozycji i faz startu. Audyt i plan są gotowe; implementacja E0–E7 nie została wykonana w tym zadaniu.
