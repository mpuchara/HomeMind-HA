# Stage 08 — RoomBeliefModel: wspólny model obecności i ruchu

Data: 2026-09-17  
Zakres: F09–F11; etap 08 audytu 0.14.11.  
Baza implementacji: Stage 07 / PR #66.

## Ścieżka wykonania

Runtime pozostaje:

`run.sh -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> Engine -> ContextEngine`

Stage 08 zmienia kontrakt wewnątrz `ContextEngine`: `RoomBeliefModel v2` zastępuje pojedynczą
trajektorię `SharedHomeStateModel`. Nie zmienia `ActionIntent`, `Executor`, trybu Shadow ani
własności Control.

Ten sam `RoomBeliefModel.observe/forecast` jest używany w:

1. live `ContextEngine.observe`,
2. globalnym `HomeBootstrap`,
3. `SQLiteTemporalTracker` podczas causal as-of replay.

Dzięki temu reguły czasu, rola sensora i wygaszanie starego stanu są identyczne w live i
replay.

## Model v2

Model jest mały i interpretowalny. Nie ma sieci neuronowej ani LLM w ścieżce reakcji.
Dla każdego pokoju zwraca oddzielnie:

- `occupancy_now`,
- `arrival_probability_by_horizon` dla 1/3/5 s oraz zgodny legacy scalar
  `arrival_probability` dla 5 s,
- `departure_probability`,
- `uncertainty`,
- `observability`,
- `evidence_sources`,
- `movement_hypotheses`,
- legacy `occupancy_in_1s/3s/5s`, `trajectory_confidence`, `known`, `support`.

`FEATURE_NAMES` pozostaje bez zmian, aby nie reinterpretować zapisanych wektorów polityk.
Nowe pola są addytywną diagnostyką/kontraktem modelu pokoju.

## Role źródeł

Źródła są klasyfikowane jawnie przed wejściem do modelu:

| Rola | Znaczenie | Czy sama może potwierdzić occupancy |
|---|---|---|
| `pir` | zdarzeniowa detekcja ruchu | tak, krótkotrwale |
| `radar_occupancy` | binarne stationary presence | tak, dłużej niż PIR |
| `occupancy_binary` | ogólny binarny occupancy | tak |
| `radar_activity` | raw/still/move energy lub activity | nie |
| `door` | przejście/topologia | nie |
| `tracker` | jawny tracker HA, używany tylko zbiorczo | tak, bez wnioskowania tożsamości z binary sensorów |
| `auxiliary_probability` | jawnie probability-like score | pomocniczo |
| `auxiliary` | niekalibrowany sygnał pomocniczy | nie |

Procent jest najpierw tylko normalizowaną wartością transportową `[0,1]`. Nie staje się
automatycznie `P(occupied)`. Przykładowo `still_energy=80%` ma rolę
`activity_likelihood`, a nie prawdopodobieństwo 0.8.

Jeżeli urządzenie radarowe ma prosty binarny `presence` oraz sibling `still/move energy`,
relacja `device_id + area` klasyfikuje binarny kanał jako `radar_occupancy`, a raw kanały
pozostają wsparciem aktywności.

## Wiarygodność komunikacji vs wiek stanu

Model przechowuje osobno:

- `communication_reliability` — spada do 0 przy `unavailable`; po reconnect zaczyna od
  0.60 i rośnie po kolejnych odebranych próbkach,
- `evidence_age_seconds` / `evidence_freshness` — określa, ile bieżącej obecności dowodzi
  niezmieniony dodatni stan.

Samo to, że stan długo się nie zmienił, nie oznacza awarii komunikacji. Jednocześnie stary
`ON` nie może utrzymywać pokoju zajętego bez końca. PIR wygasa szybko; radar occupancy ma
znacznie dłuższe okno, dzięki czemu bezruch osoby nie jest mylony z natychmiastowym
wyjściem.

## Wiele osób i ruch

Model nie ma `person_id` dla binary sensorów. Zamiast jednego `pending` utrzymuje do 8
anonimowych hipotez ruchu o postaci:

`{path, area, ts, mass}`.

Kilka pokoi może mieć równocześnie wysokie `occupancy_now`. Gdy nowy sygnał wejścia może
pasować do kilku wcześniejszych ścieżek, masa prawdopodobieństwa jest dzielona pomiędzy
alternatywy i pozostaje jawna gałąź zewnętrzna/nieprzypisana. Nie wybieramy arbitralnie,
który domownik się przemieścił.

Silny `radar_occupancy=ON` w pokoju źródłowym obniża masę hipotezy, że to właśnie ta osoba
przeszła do kolejnego pokoju. Dla PIR zachowanie jest mniej restrykcyjne, bo jego `ON`
często jest timerem hold po wcześniejszym ruchu.

Topologia jest uczona z anonimowych przejść. Niepewne powiązanie daje ułamkową wagę zamiast
kilku pełnych, fałszywie niezależnych przejść. Brak następnego wejścia w `GAP=30 s` jest
jawnym wynikiem no-arrival.

## Observability i uncertainty

`observability` zależy od dostępnych ról i niezależnej wiarygodności komunikacji.
`uncertainty` bierze pod uwagę co najmniej:

- entropię bieżącego belief,
- brak obserwowalności,
- konflikt źródeł.

`unavailable` nie jest interpretowane jako pusty pokój. Przy braku obserwacji model zwraca
wysoką niepewność zamiast sztucznego potwierdzenia braku osoby.

## Causal as-of

Każde źródło ma monotoniczny timestamp. Pakiet z `ts <= last_source_ts` jest odrzucany i
nie przewija belief do tyłu.

Replay:

1. ładuje ostatni checkpoint sprzed początku okna,
2. seeduje stan `as-of t-30 s` bez uczenia przejść,
3. resetuje wyłącznie syntetyczną trajektorię ruchu,
4. odtwarza zdarzenia `t-30 < event_ts <= t` w porządku `ts,id`,
5. przekazuje te same `evidence_metadata` co live.

Dane po `t` nie mogą wejść do forecastu.

## Checkpoint i migracja

Nowy klucz metadata:

`room_belief_model_v2`

Stary:

`shared_home_model_v1`

pozostaje nietknięty jako rollback/read fallback. `ContextEngine` najpierw czyta v2, a
jeżeli go nie ma, ładuje v1 przez jawną migrację `RoomBeliefModel.LEGACY_VERSION=1`.
Eksport po migracji ma zawsze `version=2`.

`home_checkpoints` nie wymaga zmiany schematu tabeli — pole `model` zawiera teraz
wersjonowany JSON v2. Historyczny checkpoint v1 nadal jest czytelny.

Checkpoint nie zawiera:

- `sources`,
- `values`,
- aktywnych hipotez ruchu,
- `arrivals`.

Restart nie może więc odtworzyć starego `ON` jako świeżej obecności.

## Kalibracja

`record_calibration_label(prediction, observed, source)` mierzy Brier score i 5 przedziałów
kalibracji. Etykieta musi mieć niezależne źródło; `source='model:...'` jest odrzucane.
Kalibracja nie uczy modelu obecności i nie zmienia grafu ruchu.

W ten sposób model nie raportuje jakości na podstawie własnego wcześniejszego wyjścia.
Integracja z przyszłym źródłem ground-truth powinna zapisywać predykcję przed otrzymaniem
niezależnej etykiety i dopiero potem wywoływać ten kontrakt.

## Deterministyczne scenariusze odbioru

`tests/test_room_belief.py` obejmuje:

- dwie osoby / równoczesne zajęcie dwóch pokoi,
- bezruch z radarem occupancy,
- zablokowany ON PIR i długotrwały ON radaru,
- raw radar activity / szum bez automatycznego occupancy,
- `unavailable`, reconnect i odbudowę communication reliability,
- opóźniony pakiet,
- rozwidlenie trasy kitchen/bedroom,
- brak kolejnego wejścia,
- restart bez przywrócenia live ON,
- migrację checkpointu v1 -> v2,
- kalibrację tylko na niezależnych etykietach,
- jawne role PIR/radar/raw/door.

## Przykładowy przebieg zdarzeń

1. `binary_sensor.hall_pir` przechodzi ON. Pokój `hall` dostaje świeży belief obecności;
   powstaje anonimowa hipoteza `[hall]`.
2. Dwie sekundy później `binary_sensor.kitchen_presence` z radaru przechodzi ON.
3. Jeżeli hall ma tylko PIR, model może przypisać dużą część masy do `hall -> kitchen`, ale
   zachowuje gałąź nieprzypisaną.
4. Jeżeli hall ma nadal świeży `radar_occupancy=ON`, przypisanie jest znacznie słabsze —
   model dopuszcza, że jedna osoba została w hallu, a druga pojawiła się w kuchni.
5. Forecast kuchni zwraca occupancy, observability, uncertainty oraz jawne źródła dowodu.
6. Żaden z tych kroków nie tworzy `ActionIntent` i nie wykonuje komendy HA. Agent może użyć
   forecastu dopiero w swojej istniejącej ścieżce decyzyjnej.

## Ograniczenia v2

- To model zbiorczy, nie multi-target tracker osób.
- Hipotezy są ograniczone do 8 i dwóch ostatnich pokoi w ścieżce.
- Role wynikają z HA registry, device relationship i jawnych nazw/klas; błędne metadata HA
  mogą wymagać ręcznego mapowania w przyszłym etapie.
- `auxiliary_probability` oznacza źródło jawnie probability-like, ale Stage 08 nie wykonuje
  automatycznej kalibracji tego sensora względem ground truth.
- Model ruchu nie używa geometrycznej mapy mieszkania; topologia jest statystyczna z
  obserwowanych przejść.
- Kalibracja ma API i trwały checkpoint, ale nie ma jeszcze osobnego UI do zbierania
  ground-truth.
