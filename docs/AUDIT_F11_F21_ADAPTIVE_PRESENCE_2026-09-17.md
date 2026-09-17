# Stage 10 — adaptacyjna interpretacja sygnału obecności (F11, F21)

## Cel MVP

MVP realizuje **wirtualny próg obecności** wyłącznie wewnątrz HomeMind. Nie zmienia progu, czułości ani konfiguracji fizycznego sensora w Home Assistant.

Przewidywane przyjście do pokoju (`arrival_probability` z anonimowego `RoomBeliefModel`) jest traktowane jako prior. Prior może wcześniej podnieść znaczenie słabego, lokalnego surowego sygnału radaru/aktywności, ale sam prior nie staje się dowodem obecności. Do utworzenia `virtual_presence_active` potrzebny jest niezależny lokalny raw score.

## Runtime

Ścieżka procesu pozostaje:

`run.sh -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py`

`MultiHorizonPolicy` nadal pobiera siedem istniejących `home:*` slotów. Stage 10 celowo **nie zmienia wersji ani pozycji tych slotów**:

- `occupancy_now` pozostaje bazowym/fizycznym przekonaniem z RoomBelief;
- po aktywacji wirtualnej obecności można podnieść tylko `occupancy_in_1s`, `occupancy_in_3s`, `occupancy_in_5s`;
- dzięki temu zapisane wcześniej wagi nie są reinterpretowane jako nowa cecha.

Pełny posterior i jego pochodzenie są addytywnie dostępne w `forecast['adaptive_presence']`.

## Mały model probabilistyczny

`AdaptivePresenceModel` używa interpretowalnej fuzji bayesowskiej:

1. prior = prognoza przyjścia do pokoju w horyzoncie 3 s;
2. prior jest osłabiany przez niskie `trajectory_confidence` oraz jakość kalibracji RoomBelief;
3. wybierany jest **jeden** najlepszy lokalny raw channel (`radar_activity` / `auxiliary`);
4. raw percentage/score nie jest traktowany jako P(occupied). Przechodzi przez konserwatywną krzywą likelihood; model ma też jawny kontrakt kalibracji na niezależnych etykietach;
5. wpływ likelihood jest skalowany przez `communication_reliability * freshness`;
6. prior odds × likelihood ratio daje posterior;
7. posterior przechodzi przez histerezę wejścia/wyjścia.

Wybranie jednego raw kanału jest świadome: dwa siblingi `still energy` i `move energy` z tego samego radaru nie są mnożone jak niezależne dowody. Pozostałe kanały są raportowane diagnostycznie.

## Stała granica jako benchmark

Każda ewaluacja równolegle liczy prosty benchmark `raw >= 0.60`.

Mierzymy nie tylko poprawność ON/OFF, ale:

- `mean_lead_seconds` — ile wcześniej virtual presence weszło przed przekroczeniem stałego progu;
- `confirmed_early` — liczba wcześniejszych wzbudzeń później potwierdzonych przez benchmark;
- `false_on_count`;
- `false_on_cost = false_on / adaptive_on_edges`;
- liczbę wzbudzeń zablokowanych przez false-ON budget.

## Histereza i limit fałszywych wzbudzeń

MVP używa osobnego progu wejścia i wyjścia. Wirtualne ON bez późniejszego potwierdzenia stałym progiem jest rozliczane jako false ON. Po przekroczeniu ograniczonego budżetu powtarzających się false ON nowe wirtualne wejścia są czasowo blokowane.

Stan wirtualnego ON i pending confirmation jest runtime-only. Restart nie odtwarza starego virtual ON.

## Brak raw signal

Sam `binary_sensor` nie mówi, czy sygnał jest „blisko progu”. Capability jawnie zwraca wtedy:

`mode = anticipation_only`

HomeMind nadal może korzystać z wcześniejszej prognozy ruchu/arrival z innych pokoi, lecz **nie deklaruje adaptacyjnego progu lokalnego** i nie tworzy `virtual_presence_active` bez raw evidence.

Capability podaje faktycznie dostępne:

- local raw sources,
- binary occupancy sources,
- możliwość virtual threshold,
- możliwość arrival anticipation,
- ograniczenie binary-only,
- status przyszłego hardware threshold adaptera.

## Niezależność dowodów

Raw role używane przez Stage 10 (`radar_activity`, `auxiliary`) nie tworzą hipotez ruchu w RoomBelief. Dzięki temu ten sam raw sensor nie jest jednocześnie źródłem arrival prior i nowym lokalnym likelihood.

Dodatkowo:

- MVP mnoży tylko jeden local raw likelihood;
- `record_independent_label()` odrzuca model-output jako ground truth;
- source nie może etykietować sam siebie;
- output sensora oznaczonego jako zmodyfikowany przez przyszły adapter progu nie może być niezależną etykietą skuteczności.

## Replay

`HistoricalHomeView` używa tego samego `AdaptivePresenceModel` i tego samego `ContextEngine.augment_home_forecast()` co live. Histereza jest odbudowywana causal as-of z replayowanych zdarzeń; pakiet po czasie zapytania nie bierze udziału w wyniku.

Stage 10 nie dodaje nowego policy-vector slotu, więc nie ma migracji wektorów ani wymuszonego retrainingu istniejących polityk.

## Przyszły hardware threshold adapter — kontrakt, nie implementacja I/O

`HardwareThresholdAdapterContract` jest domyślnie `enabled=False` i nie wysyła żadnej komendy do Home Assistant ani sensora.

Kontrakt wymaga:

- jawnej whitelisty sensorów;
- zakresu min/max;
- kroku kwantyzacji;
- maksymalnej liczby zmian w oknie czasu;
- wyłącznego lease sensora;
- TTL lease;
- snapshotu konfiguracji przed zmianą;
- planu restore wartości i snapshotu;
- jawnego oznaczenia, że sam kontrakt nie wykonuje physical I/O;
- reguły, że output po własnej zmianie progu nie jest niezależnym dowodem sukcesu.

Dopiero osobny przyszły adapter sprzętowy może wykonać zatwierdzony plan; Stage 10 nie dodaje takiej ścieżki wykonawczej.

## Przykładowy event flow

1. Hall PIR/radar tworzy anonimową hipotezę ruchu w stronę kuchni.
2. `arrival_probability_by_horizon['3s']` rośnie.
3. Kuchenny raw radar pokazuje 40% — poniżej benchmarku 60%.
4. Przy dobrej jakości źródła prior + raw likelihood przekraczają próg virtual presence.
5. `occupancy_now` pozostaje bez zmian, ale `occupancy_in_1s/3s/5s` może zostać podniesione.
6. Polityka światła może przygotować światło przed stałym lokalnym progiem.
7. Jeśli raw później przekroczy 60%, zapisujemy zysk czasu. Jeśli nie — false ON cost i ewentualnie blokada kolejnych wirtualnych wzbudzeń.

## Testy deterministyczne

`tests/test_adaptive_presence.py` obejmuje:

- prior bez lokalnego dowodu;
- słaby raw signal + prawidłową trajektorię i realny lead-time względem stałej granicy;
- fałszywą trajektorię;
- szum / niską jakość;
- sprzeczny lokalny sygnał;
- niedublowanie sibling raw channels;
- false-ON budget;
- brak raw danych / binary-only capability;
- bezruch z radar occupancy ON i spadającym raw activity;
- brak reinterpretacji `occupancy_now`;
- zakaz użycia threshold-modified output jako niezależnej etykiety;
- whitelist/range/step/rate/lease/TTL/restore/snapshot przyszłego adaptera.

## Ograniczenia MVP

- Nie zmieniamy fizycznego progu sensora.
- Nie śledzimy tożsamości osoby.
- Raw-score calibration ma konserwatywny start; osobny kanał niezależnych etykiet może ją później doprecyzować.
- Nie próbujemy mnożyć wielu skorelowanych kanałów z jednego urządzenia.
- Nie deklarujemy causal influence; mierzymy predictive timing gain i false-ON cost.
- Stage 10 nie wysyła żadnych usług Home Assistant i nie zmienia granicy `ActionIntent -> Executor`.
