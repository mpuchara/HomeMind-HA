# HomeMind Adaptive AI — aktualna architektura

Ten dokument opisuje bieżący stos produkcyjny po etapach F01–F24. Starsze dokumenty wersjonowane pozostają materiałem historycznym; nie są źródłem prawdy o aktualnym entrypoincie.

## Entrypoint i composition root

Obraz uruchamia:

```text
Dockerfile -> /app/run.sh -> trial_queue_main.py
                         -> RuntimeCompositionRoot
                         -> preference_queue_main.py
                         -> fast_queue_main.py
                         -> queue_main.py
                         -> main.py
```

`main.py` najpierw wystawia HTTP, a dopiero potem inicjalizuje lokalną bazę, `Engine`, realtime Home Assistant, historię i workery. `RuntimeCompositionRoot` składa wersjonowane kontrakty produktu przed startem workerów. Pozostały dług starszych installerów jest mapowany testem charakterystyki F23 i usuwany etapowo, bez jednorazowego przepisywania systemu.

## Granica wykonania

Jedyną fizyczną ścieżką sterowania pozostaje:

```text
obserwacja -> kontekst -> polityka -> kompozycja decyzji
            -> ActionIntent -> legal action/resource guards -> Executor -> Home Assistant
```

Candidate, Shadow, benchmark, kalibracja, Tournament, Explore-orchestration i diagnostyka nie mogą samodzielnie wywoływać usług HA. Executor zachowuje priorytet ręczny, lease/ownership, min dwell, legal-action mask, cooldown, ACK i rollback.

## DeviceAgent i zasoby współdzielone

Agent zachowuje stabilne `agent.id` i historię modelu. Warstwa `DeviceAgent/DeviceCapabilities` dodaje logiczne `logical_device_id` oraz `device_property` na podstawie jawnego mappingu albo HA `device_id`; nazwa encji nie jest używana do zgadywania fizycznego zasobu. Power i brightness tej samej lampy, kilka encji jednego urządzenia oraz współdzielone zasoby/strefy przechodzą przez wspólny arbiter.

Konfiguracja sensora, np. próg radaru, ma pojedynczego właściciela percepcji. Konsumenci modelu nie zmieniają jej niezależnie. HVAC/rolety mają kontrakt procesu o dłuższej dynamice; bandyta światła nie jest traktowany jako model długoterminowego komfortu.

## Percepcja i model domu

`ContextEngine` buduje jawny, wersjonowany kontekst z obserwacji HA. `RoomBeliefModel` oddziela bieżącą obecność, prognozę przyjścia/wyjścia, niepewność, obserwowalność i jakość źródeł. Binary presence, PIR, radar, raw activity, tracker i door mają jawne role. Model nie identyfikuje osób na podstawie anonimowych sensorów.

Kalibrowane prawdopodobieństwa obecności są oceniane na niezależnych przyszłych etykietach (Brier score + reliability bins). Nie są tym samym co decision strength polityki.

## Polityka i backendy

Domyślnym backendem pozostaje `DiagonalLinUCB` w `MultiHorizonPolicy`. Jest lekki, lokalny i działa na jawnym schemacie cech. Full-ridge LinUCB istnieje jako ograniczony challenger do benchmarku/Shadow; nie zastępuje domyślnego backendu automatycznie.

Zmiana backendu lub model revision tworzy nową epokę kalibracji. Wybór backendu wymaga oddzielnych train/validation/future danych; sam fakt istnienia nowego modelu nie jest powodem wdrożenia.

## Siedem różnych rodzajów dowodu

System celowo nie scala niżej wymienionych źródeł w jedno `confidence`:

1. **Historyczna demonstracja** — obserwowany stan/akcja targetu z historii. Pokazuje, co się wydarzyło, ale nie dowodzi komfortu ani kontrfaktycznej poprawności innych akcji.
2. **Reward contextual bandit** — aktualizacja dotyczy wyłącznie akcji, która rzeczywiście została logowana/wykonana. Reward nie jest przypisywany niewybranym akcjom.
3. **Model obecności** — probabilistyczna percepcja i prognoza ruchu. Ma własną kalibrację i nie jest `policy confidence`.
4. **Correct / Change decision** — jawna informacja użytkownika. Correct jest etykietą konkretnego kontekstu/generacji; trwała instrukcja nie wygasa jak statystyka historyczna.
5. **Eksperyment** — kontrolowana próba opisana `TrialRecord`: hipoteza, przypisana akcja i propensity, dispatch/ACK, źródła outcome, wynik i dokładnie-jednokrotna aplikacja do dziecka.
6. **Shadow proxy** — kontrfaktyczna predykcja Candidate/challengera na obserwowanym epizodzie. Jest użyteczna do porównań, ale nie jest fizycznym wynikiem niewykonanej akcji.
7. **Fizyczny outcome** — wynik po rzeczywistym `ActionIntent -> Executor -> HA`, powiązany z konkretną akcją i oknem obserwacji. To jedyna kategoria opisująca skutek faktycznie wykonanej komendy.

Brak feedbacku nie jest pozytywnym dowodem preferencji, ACK nie jest rewardem komfortu, a replay automatyzacji nie jest etykietą „światło było potrzebne”.

## Candidate i promocja

Każda generacja Candidate zaczyna od dokładnego snapshotu bezpośredniego rodzica i pozostaje Shadow. Correct, Autonomous, Explore i Change decision tworzą/evolving child generation bez modyfikacji rodzica. Porównanie jest direct-parent vs direct-child na tych samych przyszłych epizodach.

Promocja jest atomowa. Źródłem prawdy jest lista nazwanych wyników walidacji, a nie pojedynczy bool. Veto nie znika przez kolejność modułów. Stage 13 wymaga oddzielenia selection i fixed future evaluation, w tym niezależnego pokrycia ON/OFF. Rollback przywraca dokładny poprzedni snapshot i ownership.

## Cold start, dryf i decay

Brak historii nie obniża zabezpieczeń: agent pozostaje fallback/Shadow i raportuje brak dowodu. Dryf rozróżnia awarię sensora, zmianę topologii, zmianę zwyczaju i nową preferencję. Pogorszenie tworzy izolowanego Candidate zamiast resetować Live.

Statystyki historyczne mają jawny decay. Trwała instrukcja użytkownika nie wygasa. Starsze przykłady regresyjne mogą pozostać jako audyt/anchor bez nieograniczonej siły treningowej.

## Koszt historii i treningu

Status i diagnostyka korzystają z cursorów, sufficient statistics i bounded batches zamiast pełnego skanu historii na każde odpytywanie. Teach-RL wykonuje wsadowe zapytania as-of. Heavy-job queue jest ograniczona i ma backpressure. Surowe dowody potrzebne do audytu, undo, odbudowy modelu i rollbacku pozostają zachowane.

## Benchmark produktu F24

`tools/benchmark_product_runtime.py` definiuje deterministyczny świat z ukrytą prawdziwą obecnością oraz ukrytą potrzebą światła, osobnymi od obserwacji. Oficjalny executable to `tools/run_product_runtime_benchmark.py`: przed generowaniem train/validation instaluje ten sam finalny `RuntimeCompositionRoot`, którego używa shipped `trial_queue_main.py`, a każdy seed uruchamia w świeżym procesie. Dzięki temu Observation Contract, preference/episode/provenance, Tournament, Candidate, TrialKnowledge, confidence/drift, DeviceAgent, promotion validation i Stage17 performance composition są takie jak w produkcyjnym rootcie, bez przenoszenia globalnego stanu installerów między syntetycznymi domami.

Sensory mają opóźnienia, noise, missingness i różne reprezentacje; akcja lampy wpływa na obserwowany lux. W scenariuszu `sensor_moved` ten sam `entity_id` zmienia w future test przypisanie obszaru w Entity Registry i nowe mapowanie jest podawane do ocenianego runtime, zamiast jedynie zmieniać dane sensora. `manual_change` emituje jawne zdarzenie targetu z `context.user_id`; dla światła produkcyjny timing zachowuje domyślny manual hold, a `fast_runtime` skraca wyłącznie timing akcji/ACK/settling i nie osłabia priorytetu ręcznej zmiany. Dla `light.power` dodatkowo obowiązuje asymetryczna stabilizacja: ON pozostaje natychmiastowe, natomiast wyłącznie statystyczny `historical_policy_bootstrap` OFF z już włączonej lampy musi utrzymać się przez 6 s. Ręczny OFF, scoped instruction, explicit preference i experiment omijają ten filtr. Observation Contract v2 usuwa także own-action lux leakage i centruje nominalne metadane zdrowia transportu; stare zapisane schematy bez markera pozostają kontraktem v1 i nie są reinterpretowane. Syntetyczny efektywny target nie może zostać nadpisany przez Shadow proxy podczas aktywnego hold. Benchmark-only clock bridge zapewnia wspólny event-time dla `Engine` i `Executor`, aby syntetyczny historyczny timestamp nie wygaszał intentu względem zegara runnera. Nie zmienia to produkcyjnego TTL ani progów.

Benchmark rozdziela:

```text
train: historyczne demonstracje
validation: demonstracje używane do kalibracji/wyboru challengera
future test: niewidziany wcześniej hidden truth używany wyłącznie do oceny produktu
```

Porównywane są: stała automatyzacja, bieżący produkcyjny runtime w Shadow, full-ridge challenger w Shadow i ostrożny fallback. Raport obejmuje needed-light coverage, false ON, premature OFF, opóźnienie, chatter, korekty/100 epizodów i koszt obliczeń z wieloma seedami oraz przedziałami niepewności.

Benchmark **nie** obniża kwalifikacji, nie wstawia gotowego `benchmark_score` i nie promuje backendu. Niespełnione kryteria są prawidłowym wynikiem. Wynik syntetyczny/CI nie zastępuje fizycznego M&V. CI zapisuje pełny raport jako artefakt `product-benchmark-f24-py311/product-benchmark-f24.json`, aby lista kryteriów i przedziały niepewności były audytowalne poza logiem joba. Zwięzły raport porównawczy z bieżącego kontraktu v2 jest utrzymywany w `BENCHMARK_PRODUCT_F24.md`.

CI dodatkowo uruchamia dokładny source entrypoint oraz obraz i sprawdza, że PID 1 obrazu startuje przez `/app/run.sh`, który kończy w `trial_queue_main.py`. Dzięki temu benchmark jakości i test uruchomienia dotyczą tego samego stosu kompozycji.

### Startup inference QoS (0.14.30)

HTTP/Ingress binds before runtime initialization. The background Engine keeps inference gated while the runtime composition, realtime stream, history manager and control reconciliation are being assembled. After `startup.ready`, proactive inference receives a 3 s grace so the initial UI/static/status reads can complete first. The initial REST snapshot warms state/context but is not treated as a realtime dirty burst. Device-control inference concurrency is capped at `min(4, os.cpu_count())`; realtime HA changes observed during the gate remain queued as dirty context and are processed after the gate opens. This scheduling contract does not change policy thresholds, model data, ActionIntent semantics or Executor ownership.

### Event-driven inference scheduler (0.14.31)

The engine's 1 s loop is a lightweight timer wheel, not a global inference cadence. HA state changes trigger dependency-filtered inference immediately. Every completed inference records its next in-memory deadline: 10 s idle heartbeat for fast targets, 30 s for slower targets, or an earlier exact lifecycle deadline such as fast-light OFF confirmation, ACK/outcome observation, retry or manual-hold expiry. During manual hold the ordinary heartbeat is suppressed. The event dependency set is target + persisted/active policy inputs + explicit configured inputs + RoomBelief sources in the target area + active experiment watches; global `context.admitted` is deliberately excluded so a presence event in one room cannot wake every agent. Candidate Shadow remains attached to root inference, so it automatically inherits the same event-driven reduction. Historical/Candidate rebuilds remain serialized through TrainingQueue and the cooperative training CPU budget.

### Post-Recorder discovery QoS (0.14.32)

Automatic target discovery is maintenance, not a reason to monopolize the interpreter. After Recorder target-history chunks finish, the runtime no longer calls full-table `archive_stats()` on the critical path and no longer runs `usage_for()` independently for every target/property. Discovery computes the same first-sample-plus->1e-6-transition semantics through one bounded multi-entity `archive_iter` stream. This lets the existing Raspberry-Pi background archive throttle measure downstream Python work across the whole pass instead of resetting for many short iterators. Existing-agent reads on quiet startup/discovery are config-only. WebSocket keepalive timeouts are treated as starvation diagnostics; transport timeouts are not increased to conceal CPU/GIL pressure.

### Operational-first steady state (0.14.33)

The normal steady-state contract is saved policies + realtime HA events + ActionIntent/Executor. Recorder backfill, automatic target discovery and historical rebuilds are maintenance operations, not implicit background duties of every process start. HistoryManager starts in a local-only ready state and remains idle until an explicit discovery request. `POST /api/discovery/rescan` schedules the existing discovery pipeline on a background worker and returns immediately. Periodic UI reads use config-only agent rows plus hot in-memory runtime and do not call aggregate history readers. This recovery boundary preserves all persisted evidence and model lineage while removing heavy work from the availability path. Physical dispatch semantics are unchanged: only Executor owns HA service calls; Shadow/Candidate remain non-controlling.


### Agent hot path (0.14.34)

Realtime inference is dependency-indexed and RAM-first. A coalesced HA event pass takes one immutable state/revision snapshot and routes only to affected agents; runtime extensions may broaden eligibility but may not replace the core scheduler. Common Shadow inference avoids durable agent-config validation, while Control still reloads and validates durable configuration at the physical dispatch boundary.

Observation-only persistence is explicitly outside `event -> intent`: Shadow provenance, feature observations and evidence-window maintenance use bounded deferred queues and batch SQLite transactions. Active command provenance is hydrated once at startup and matched from memory. Candidate generation discovery, including the empty-Candidate state, is invalidation-driven. Preference facts use in-memory revision invalidation. Inactive Teach rebenchmark performs no durable config read.

The hot status path exposes recent telemetry plus deferred-journal backlog without invoking full Engine.status/history aggregates. These changes preserve model/reward/qualification semantics and are intended to stop latency from increasing simply because more agents exist or because the process has been alive longer.
