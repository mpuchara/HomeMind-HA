# HomeMind 0.14.205: szybsza droga od sensora do sterowania

## Diagnoza eksportów 19:46–19:47

Pięć eksportów 0.14.204 pokazuje średnio 195–197 ms od odebrania zdarzenia HA
do rozpoczęcia targetu oraz około 71 ms na wnioskowanie. Decyzja jest gotowa po
około 275 ms. Po decyzji obserwacja kandydatów Shadow nadal zajmuje tego samego
pracownika przez około 261 ms, opóźniając obsługę następnych zdarzeń. Szczegółowe
spany i metryki całego procesu obejmują różne okna czasu.

Eksporty zawierają intencje Shadow i odrzucenia. Nie ma próbek rzeczywistych
wywołań usług HA, więc nie opisują opóźnienia sieci, lampy ani fizycznego ACK.
Zestawienie metryk i deduplikowanych spanów jest zapisane w raporcie diagnozy.

## Zmiany

- Domyślne `realtime_inference_debounce_ms` wynosi 0 zamiast 25. Pierwsze
  zdarzenie nie czeka na sztuczne okno grupowania. Mechanizm zbierania zmian
  zajętego targetu i ponowień tylko dla tego targetu nadal obowiązuje. Dawne
  wartości domyślne 25/75 są migrowane do 0; pozostałe wartości są zachowane.
- Polityka sterująca i Executor nadal pracują przy każdym dopuszczonym zdarzeniu.
  Kosztowna obserwacja kandydatów Tournament po decyzji ma domyślny interwał
  1000 ms, liczony od początku poprzedniej udanej obserwacji. Zakres ustawienia
  `runtime_shadow_observation_interval_ms` to 0–5000; 0 przywraca dawną kadencję.
- Wszystkie zdarzenia targetu i zmiany jego wartości/dostępności przechodzą pełną
  obserwację także w czasie interwału. Nowa polityka, schemat lub wersja semantyki
  również wymaga obserwacji. Nieudana próba nie rozpoczyna przerwy. Krótkie ON/OFF,
  niezależne etykiety i filtrowanie własnych potwierdzeń są objęte testami.
- Rzadsze próbkowanie kontekstu zmienia historię i późniejsze uczenie kandydatów.
  To świadomy priorytet sterowania; nie oznacza identycznych wag kandydatów jak
  przy ocenie każdego zdarzenia. Aktualne wejścia polityki sterującej, kolejność
  predict/score/learn oraz warunki bezpiecznego wysłania komendy pozostają zachowane.
- Poprawiono współdzielenie opisów sensorów po odświeżeniu konfiguracji agenta.
  Kandydat w cache może nadal przechowywać starszy słownik konfiguracji. W 0.14.204
  porównanie tożsamości takiego słownika wyłączało współdzielenie mimo zgodnej
  semantyki cech. Teraz porównywane są target, właściwość i profil, przy zachowaniu
  identyczności mapy stanów, historii i czasu. Wersja semantyki, lags, wykluczenia
  i docelowy target nadal rozdzielają wpisy. Każde użycie kandydata nadal wymaga SHA.
- Liczniki wykonanych/pominiętych ocen są dostępne w
  `engine.context_persistence.observation_scheduler`. Szczegółowy debug pokazuje
  `entity_descriptors_cached` w `context_candidate_shared_context`.

Nie dodano wątków ani kolejki obserwacji. Scheduler przechowuje maksymalnie 256
małych wpisów; po wyparciu wpisu kolejna obserwacja jest wykonywana od razu.
Nie zmieniono limitów dysku, retencji, kontroli kwalifikacji ani zabezpieczeń Executor.
Full Rebuild nie jest wymagany.

## Porównanie dwóch źródeł

Benchmark uruchamia rzeczywistą pętlę `Engine.run`, odbiór zdarzeń, mechanizm
ponowień, cechy Root, czterech rzeczywistych kandydatów Tournament i Executor
w trybie Control. Przed wywołaniem usługi działają normalne zabezpieczenia
kwalifikacji, świeżości, targetu, zasobów i instrukcji. Sama odpowiedź HA jest
zastąpiona testową; akcja i confidence Root są deterministycznym fixturem.
W serii końcowa zmiana wymaga jednego zaakceptowanego `light.turn_on`.

Trzy pary po 32 zdarzenia, źródła 0.14.204 i 0.14.205, Linux Python 3.12.3 / NumPy 2.3.3:

| Pomiar | 0.14.204 | 0.14.205 |
| --- | --- | --- |
| Mediana od zdarzenia do Executor | 24,1–24,5 ms | 6,6–6,7 ms |
| Ostatnie zdarzenie do wywołania usługi | 29,2–29,8 ms | 10,8–11,5 ms |

Mediana przyspieszenia reakcji wynosi 3,67×, a wywołania usługi 2,59×.
Wszystkie 32 zdarzenia obsłużono w każdej parze, decyzje i wyniki Executor są
identyczne, a każda seria kończy się dokładnie jednym zaakceptowanym wywołaniem.
Test odświeżonego słownika konfiguracji potwierdza 44 zamiast 128 wywołań
ekstraktora w czterech kandydatach, przy czterech pełnych kontrolach SHA.

Wynik jest pomiarem komponentów na hoście. Nie obejmuje wszystkich zewnętrznych
obserwatorów dostarczanej kompozycji, sieci HA, fizycznego ACK, karty microSD ani
konkurencyjnych checkpointów. Nie jest gwarancją opóźnienia Raspberry Pi.
Na urządzeniu należy porównać nowe eksporty: `event_to_target_start`,
`event_to_decision`, `event_to_service_dispatch`, `event_to_state_ack`,
`wrapped_inference` oraz licznik pominiętych obserwacji kandydatów.

Walidacja: 2455 testów Linux Python 3.12.3 / NumPy 2.3.3 bez pominięć, w tym
18 nowych regresji. Kompilacja modułów, składnia wszystkich dostarczanych JS,
produkcyjny entrypoint do `ready` oraz test chronionego wywołania Control do
atrapy HA zakończone powodzeniem. Raport walidacji zawiera hashe źródeł.
