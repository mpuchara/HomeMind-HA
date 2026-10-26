# Optymalizacja wydajności 0.14.230

## Zakres wydania

Pierwszy mały etap po audycie 0.14.229: fundament pomiarów A0 i zawężenie kopii
routingu schedulera A2a. To nie jest zakończenie całego programu A0-A7.
Bazą jest aktualny HEAD PR #345, `8e47cd6652c1364f0b048f79d507929b1cca6168`.
GitHub nadal ma otwarte drafty #345/#344; main ma 0.14.184 i nie jest właściwą bazą.
W lokalnym obszarze pracy nie znaleziono trzech osobnych dokumentów audytu
wymienionych w zadaniu; zakres ustalono z dostarczonego pełnego tekstu oraz kodu.

## Implementacja

`Engine.process()` kopiuje pod `Engine.lock` właścicieli tylko encji należących do
bieżącego zbioru zdarzeń lub oczekujących retry. Poprzednio kopiował wszystkie
zbiory całego indeksu zależności. Do filtrowania `target_changed` nie trafiają inne
encje, więc dalsza logika dostaje te same dane. Nie dodano cache, nowej kolejki ani
nowego kontraktu invalidacji. Zbiory właścicieli pozostają prywatnymi kopiami.

Snapshot stanów, pełna mapa rewizji, rewizja kontekstu i obydwa zegary odbioru są
nadal pobierane atomowo. Workery danego przebiegu współdzielą ten sam snapshot.
Retry targetu nadal zachowuje właściciela i FIFO; timer nie otrzymuje starego czasu
zdarzenia. Executor ponownie waliduje Control na dotychczasowych zasadach.

Nie zmieniono modeli, rewardów, progów, historii, Correct, trwałych komend,
generacji, promocji, rollbacku, ochrony WAL/dysku ani synchronizacji cyklu życia.
W `submit()` Tournament `False` oznacza `sync_required`; to nie jest zwykłe
przepełnienie opcjonalnej kolejki. Usunięcie `run_synchronous()` bez nowego
kontraktu promocji byłoby niepoprawne. Capture, admission i jawne luki z .229
pozostają bez zmian. A1 wymaga kolejnego niezależnego etapu.

## Pomiary i diagnostyka

`benchmark_runtime_scaling.py` uruchamia `trial_queue_main` i końcowy
`RuntimeCompositionRoot`, a następnie rzeczywisty Engine.run/on_state_changed,
worker targetu i pełny zarejestrowany pipeline. Każdy proces korzysta z osobnego
tymczasowego Store. Zwykłe MultiHorizonPolicy są dopasowane na 192 syntetycznych
demonstracjach i kalibrowane na 48 kolejnych przykładach. Model musi osiągnąć
normalny próg kwalifikacji; confidence/predict nie są nadpisywane. Każdy callback
sprawdza, że rzeczywista inferencja się odbyła. HA service jest zabronione asercją.

To fixture hosta z prostymi demonstracjami, a nie akceptacja jakości treningu
HistoryManager ani pomiar sieci HA. Wyłączono zewnętrzny poll/resync, timer warmup
i maintenance, aby izolować ingress. Zainstalowany stos zawiera Tournament;
nie utworzono aktywnego Candidate i nie uruchomiono równoległego procesu treningu.
Observer zachowuje normalny sampling i backpressure. Shutdown nie dowodzi
trwałego zapisu wszystkich opcjonalnych obserwacji. Hash porównuje decyzje Root,
nie pełną historię dowodów Tournament.

`compare_runtime_scaling.py` wykonuje serie sekwencyjnie, naprzemiennie przed/po,
w świeżych procesach. Wariant komponentowy wykonuje rzeczywiste Engine.process
z zatrzymanymi workerami, izolując koszt schedulera od inferencji/SQLite. Porównuje
SHA pełnego snapshotu przekazanego workerom. Przykład odtworzenia na Linux:

```sh
python tools/compare_runtime_scaling.py --baseline-root /path/to/0.14.229 \
  --output /path/to/results/scheduler --scheduler --events 500
python tools/compare_runtime_scaling.py --baseline-root /path/to/0.14.229 \
  --output /path/to/results/runtime --events 20
```

Baseline musi zawierać kod .229; użyty lokalnie checkout ma dokładnie ten sam tree
co `8e47cd6` (zweryfikowano pusty git diff). Zestawy 0/1/3/4/5/10/20 obejmują
sensory wspólne i niezależne. Zachowywane są surowe próbki i osobne p95 każdej serii.
Pole `event_to_decision` pochodzi z produkcyjnego zegara Engine, natomiast
`event_to_pipeline_return` mierzy od początku burstu do powrotu całego pipeline,
w tym opcjonalnego capture. Tych metryk nie wolno utożsamiać.

Agregaty Telemetry mają teraz p50/p95/p99, liczebność zachowanej próby i znacznik
`p99_sufficient`. Istniejący ring 512 próbek nie urósł; jego p99 jest zawsze
oznaczone jako niewystarczające według konserwatywnej reguły 1000 próbek. Reguła
nie stanowi dowodu statystycznej niezależności. Surowe serie benchmarku pozwalają
analizować większe próby. Agregaty liczy się przy odczycie, nie przy zdarzeniu.

Po jawnym włączeniu istniejącego logu diagnostycznego spany mierzą wall time,
thread CPU i process CPU. Process CPU obejmuje wszystkie wątki procesu i nie
wolno sumować go po nakładających się spanach. Nowy span `scheduler_snapshot`
obejmuje oczekiwanie na Engine.lock i tworzenie snapshotu, nie rozdziela tych
dwóch kosztów. Istniejące metryki Store/SQLite i capture pozostają dostępne.

Zaufany GET `/api/debug/runtime-log/timings` eksportuje wyłącznie ograniczone
spany czasowe z RAM: bez identyfikatorów agentów/encji, nazw wątków, timestampów,
stanów i dowolnych pól wejściowych. Operacje i etapy przechodzą whitelistę.
Pełny dotychczasowy `/download` nadal zawiera dane diagnostyczne instalacji;
nie jest anonimowy. Szczegółowa diagnostyka pozostaje domyślnie wyłączona.

## Wyniki i walidacja

Końcowe tabele, SHA źródła, testy i CI są zapisane w raporcie
`docs/benchmarks/PERFORMANCE_0_14_230_QA.json` oraz wynikach obok niego.
Nie należy wyprowadzać z kosztu komponentu przyspieszenia całego produktu.
Niezmierzone Feature CPU, training throughput, VM instructions, WAL checkpoint,
HA dispatch i ACK pozostają oznaczone jako brak pomiaru.

## Instalacja i rollback

Wersja eksperymentalna do ręcznego testu; bez merge do main i bez publikacji stable.

1. Wykonać pełny backup dodatku wraz z konfiguracją, modelami i bazą danych.
   Do kopiowania SQLite użyć backupu HA albo zatrzymać dodatek; zachować spójny
   zestaw SQLite/WAL/SHM. Nie kopiować samego pliku aktywnej bazy jako backupu.
2. Zachować paczkę poprzedniej wersji. Podmienić źródła tego samego dodatku:
   repository ZIP zawiera `adaptive_ai/`, addon ZIP ma `config.yaml` w katalogu
   głównym. Nie tworzyć nowego dodatku o innym slug.
3. Przebudować obraz dodatku w HA. Sam restart nie aktualizuje obrazu ze źródeł.
4. Uruchomić dodatek i odświeżyć panel. Zweryfikować wersję 0.14.230, health,
   istniejących agentów, modele, Current, Desired i stan kolejek.
5. Porównać tę samą instalację po rozgrzaniu, najpierw Shadow. Na docelowym Pi
   osobno sprawdzić Correct, trening, Candidate, discard, promocję/rollback i
   Control z dispatch oraz pasującym ACK. Nie uznawać decyzji Shadow za fizyczny ACK.
6. W razie regresji zatrzymać dodatek, przywrócić poprzednie źródła i przebudować
   obraz. Ta wersja nie zmienia schematu danych. Jeśli konieczne jest przywrócenie
   danych, odtworzyć cały spójny backup; uwzględnić utratę nowszych korekt od chwili
   backupu. Nie uruchamiać dwóch wersji na tej samej bazie równocześnie.

Pakowanie odczytuje bajty z `git archive HEAD`, a nie z roboczych plików Windows.
ZIP zawiera SOURCE_COMMIT.txt oraz MANIFEST.sha256. Narzędzie sprawdza CRC,
każdy plik, wersję config/settings/BUILD_INFO/Dockerfile i LF w run.sh.

## Pozostały zakres

A0: fazy CPU capture/features/policy, SQL lock wait/held/transakcje/VM instructions,
porównania aktywnego Candidate, równoległego treningu i Correct, wolny writer,
bursty oraz długie serie są nadal otwarte. A1: częściowe odroczenie capture z
kontraktem immutable i parity dowodów. A2a: usunięcie podwójnej kopii stanów w
run/process wymaga osobnego atomowego kontraktu, nie zostało wykonane. A2b-A6
nie są wdrożone tym PR-em. 24h soak, Pi 4, prawdziwy HA websocket i urządzenia
pozostają do wykonania. Szczegóły wznowienia są w dokumencie kontynuacji.

<!-- measured -->

## Zmierzone wyniki hosta

Pięć par serii na konfigurację, sekwencyjnie z naprzemienną kolejnością. Wariant komponentowy: 500 przebiegów na serię. Wariant runtime: 20 zdarzeń/burstów na serię. Pierwsze zimne zdarzenia są wliczone. W tej prostej macierzy modele wybierały OFF/SHADOW; to obciążenie inferencji, nie akceptacja jakości decyzji ani fizycznego ON/OFF.

| Agenci | Scheduler CPU mediana przed / po [ms] | Scheduler p95 przed / po [ms] |
|---:|---:|---:|
| 0 | 0.081 / 0.011 | 0.121 / 0.015 |
| 1 | 0.106 / 0.024 | 0.154 / 0.039 |
| 3 | 0.147 / 0.051 | 0.337 / 0.087 |
| 4 | 0.170 / 0.064 | 0.291 / 0.113 |
| 5 | 0.211 / 0.072 | 0.336 / 0.122 |
| 10 | 0.318 / 0.119 | 0.497 / 0.185 |
| 20 | 0.608 / 0.217 | 0.977 / 0.299 |

Każda para zachowała identyczny SHA pełnego snapshotu przekazanego workerom. Ta tabela izoluje scheduler: nie zawiera SQLite, inferencji ani fizycznych akcji.

| Agenci | Sensory | Event -> decision p50 przed / po [ms] | p95 przed / po [ms] | p99 przed / po [ms] | Próbki na wariant |
|---:|---|---:|---:|---:|---:|
| 0 | wspólne | brak decyzji | brak decyzji | brak decyzji * | 0 / 0 |
| 0 | niezależne | brak decyzji | brak decyzji | brak decyzji * | 0 / 0 |
| 1 | wspólne | 2.18 / 2.21 | 11.72 / 12.85 | 14.52 / 14.49 * | 100 / 100 |
| 1 | niezależne | 2.40 / 2.42 | 14.63 / 13.28 | 18.87 / 16.11 * | 100 / 100 |
| 3 | wspólne | 11.52 / 10.35 | 37.60 / 24.99 | 71.62 / 58.28 * | 300 / 300 |
| 3 | niezależne | 9.02 / 9.24 | 21.44 / 21.95 | 55.41 / 49.89 * | 300 / 300 |
| 4 | wspólne | 13.57 / 15.60 | 34.24 / 40.91 | 80.15 / 67.89 * | 400 / 400 |
| 4 | niezależne | 12.97 / 13.98 | 26.81 / 35.22 | 67.25 / 73.87 * | 400 / 400 |
| 5 | wspólne | 15.69 / 14.90 | 47.03 / 36.83 | 88.18 / 71.92 * | 500 / 500 |
| 5 | niezależne | 16.93 / 16.23 | 38.06 / 35.32 | 85.64 / 68.87 * | 500 / 500 |
| 10 | wspólne | 31.93 / 33.10 | 79.73 / 75.73 | 138.10 / 131.54 | 1000 / 1000 |
| 10 | niezależne | 29.64 / 29.38 | 67.87 / 68.54 | 125.52 / 117.05 | 1000 / 1000 |
| 20 | wspólne | 51.90 / 53.85 | 149.84 / 154.32 | 353.25 / 312.35 | 2000 / 2000 |
| 20 | niezależne | 51.50 / 52.00 | 116.65 / 113.45 | 298.64 / 256.70 | 2000 / 2000 |

`*` Za mało próbek do deklarowania wiarygodnego p99. Pooled próbki są skorelowane między agentami i zdarzeniami; nawet 1000 nie stanowi automatycznej akceptacji p99. W JSON zachowano także p95 każdej osobnej serii. Wszystkie 70 par runtime zachowało SHA decyzji Root i zero HA service.

Koszt schedulera spada powtarzalnie. Wyniki całej ścieżki są mieszane, w tym wyższe p95 dla czterech agentów w tej macierzy. Nie wykazano jednolitej poprawy ani zamknięcia warunku braku regresji całego Root. Nie przypisujemy tych różnic jednej przyczynie. Do akceptacji produktu potrzebne są dłuższe, rozgrzane serie i profile konkurencji obserwatora/writera. Wydanie jest kandydatem do testu, nie dowodem rozwiązania opóźnień Pi.

Dodatkowe rzeczywiście zmierzone metryki, pięć agentów, wspólne sensory:

| Metryka | Przed | Po |
|---|---:|---:|
| Tournament capture p95 [ms] | 11.785 | 12.387 |
| Capture history p95 [ms] | 0.031 | 0.030 |
| Capture states/registry p95 [ms] | 2.039 | 2.013 |
| Capture policy p95 [ms] | 0.550 | 0.538 |
| ContextRowBuffer Store.lock wait p95 [ms] | 0.012 | 0.013 |
| ContextRowBuffer Store.lock held p95 [ms] | 2.207 | 1.691 |
| Peak RSS pojedynczego procesu [MiB], maksimum serii | 68.06 | 68.15 |
| Feature / policy thread CPU | brak pomiaru | brak pomiaru |
| Wszystkie zapytania SQLite / VM instructions | brak pomiaru | brak pomiaru |
| Training throughput podczas inferencji | brak pomiaru | brak pomiaru |

Metryki capture i ContextRowBuffer opisują normalnie próbkowane opcjonalne zadania; nie mierzą wszystkich blokad Store ani transakcji. RSS nie obejmuje sumy procesów/cgroup.

## Narzut diagnostyki

Pięć par na pięciu agentów, po 100 burstów na serię, te same źródła .230; przed = tracing OFF, po = tracing ON.

| Sensory | Event -> decision p95 OFF [ms] | ON [ms] |
|---|---:|---:|
| wspólne | 23.70 | 28.54 |
| niezależne | 24.50 | 26.76 |

Szczegółowy tracing ma zauważalny narzut. Nie należy traktować pomiarów przy włączonym logu jako neutralnego baseline. Wszystkie pary zachowały SHA decyzji. Domyślny stan pozostaje OFF; eksport timing-only odczytuje RAM i sam nie włącza śledzenia.

## CI dokładnego źródła

Commit `7332f42abbb9896cde481f4f4439e53d7cd8809a`: [run 40419243101](https://github.com/mpuchara/HomeMind-HA/actions/runs/40419243101) zaliczył Python 3.11: 2817 testów / 251.467 s; Python 3.13: 2817 testów / 264.424 s; compileall, walidację JS, wymagane benchmarki, rzeczywisty source entrypoint, Docker build/start i pakowany vector trainer. Main-only package job został pominięty; ZIP-y są budowane i weryfikowane lokalnie osobno. Pierwsze CI wykryło pięć błędów zgodności wersji HTML; poprawiono label i klucze cache bez zmiany inferencji, następnie ponownie uruchomiono pełne CI. Plik CI.json zachowuje statusy kroków i identyfikatory jobów.

## Końcowa walidacja lokalna

Ubuntu WSL2, Python 3.12.3 / SQLite 3.45.1 / numpy 2.3.3: **2817 testów OK / 433.865 s**, exit 0; 7 nowych regresji osobno OK. Compileall całego src i tools, 22 pliki JS, debug-export UI, live-delivery UI i rzeczywisty entrypoint przeszły. SHA-256 pełnego logu: `b299e41d9e9f3a26b4ad57d22c4825cd950cec3fd64433909209d2061b8b73fb`. Manifest 658 plików kodu/testów/tools zgadza się z przetestowanym źródłem. Końcowy commit raportów/BUILD_INFO/changelog nie zmienia tych plików; nie powtarza identycznego CI. Pakiety zapisują dokładny finalny SHA w SOURCE_COMMIT.txt, a zewnętrzne sidecary zawierają SHA-256 ZIP.
