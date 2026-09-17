# Jak działa HomeMind Adaptive AI — aktualny model uczenia

Ten dokument opisuje bieżący produkt. W szczególności rozdziela mechanizmy, które wcześniej bywały zbiorczo nazywane „RL”, choć mają inne znaczenie dowodowe.

## 1. Home Assistant dostarcza obserwacje, nie prawdę o preferencji

HomeMind odbiera bieżące `state_changed` przez WebSocket oraz korzysta z historii Recorder. Dane są zapisywane lokalnie, aby replay, Teach, benchmarki i odbudowa modeli nie musiały za każdym razem pobierać całej historii z HA.

Stan sensora lub targetu mówi, **co zostało zaobserwowane**. Sam fakt, że światło było ON, nie oznacza jeszcze „światło było potrzebne”, a brak ręcznej korekty nie jest automatyczną pozytywną etykietą komfortu.

## 2. Percepcja buduje osobny model obecności/ruchu

`ContextEngine` i `RoomBeliefModel` łączą źródła o jawnych rolach: PIR, binary occupancy, radar, raw activity, tracker, door i pomocnicze źródła.

Wyniki percepcji obejmują osobno:
- occupancy now,
- arrival/departure probability,
- prognozę dla horyzontów,
- uncertainty,
- observability,
- evidence sources i ich jakość.

Prawdziwe pola probabilistyczne są kalibrowane na niezależnych przyszłych epizodach. Nie są tym samym co `Decision strength` polityki.

## 3. Urządzenie ma logiczną tożsamość ponad encjami HA

`DeviceAgent/DeviceCapabilities` wiąże agentów z fizycznym zasobem przez jawny mapping lub HA `device_id`. Friendly name nie jest używany do zgadywania tożsamości.

Dzięki temu np. power i brightness jednej lampy współdzielą manual override, lease i min dwell. Dla HVAC/rolet obowiązuje kontrakt procesu o dłuższej dynamice; polityka szybkiego światła nie jest przedstawiana jako pełny model komfortu HVAC.

## 4. Historia targetu może być demonstracją

Przy cold start system może wykorzystać istniejącą automatyzację i historyczne zachowanie targetu jako **demonstrację**:

```text
obserwowany kontekst -> obserwowana akcja/stan targetu
```

To użyteczne do bootstrapu, ale nie jest fizycznym eksperymentem ani kontrfaktycznym dowodem, że każda inna akcja byłaby gorsza. Replay automatyzacji jest proxy zachowania, nie etykietą „potrzeby światła”.

## 5. Domyślna polityka jest lekkim contextual bandit

Domyślny backend pozostaje oparty o DiagonalLinUCB / `MultiHorizonPolicy`. Działa na kompaktowym, jawnym schemacie cech wybranych z szerokiego candidate pool.

Ważna zasada contextual bandit:

```text
reward znamy tylko dla logowanej / wykonanej akcji
```

Niewybrana akcja ma nieznany reward. System nie przypisuje jej automatycznie porażki ani sukcesu. Full-ridge LinUCB istnieje jako challenger benchmarkowy/Shadow; nie jest automatycznie instalowany jako nowy backend.

## 6. Correct i preference są jawną informacją użytkownika

`Correct` oraz `Change decision` nie są tylko słabym rewardem do wspólnego worka danych. Są wiązane z konkretnym kontekstem, generacją i czasem.

- Correct wskazuje oczekiwaną decyzję dla obserwowanego punktu/historii.
- Change decision zapisuje bieżącą jawnie podaną preferencję.
- Jednorazowy wyjątek nie musi stać się trwałą regułą.
- Trwała instrukcja nie wygasa jak statystyczna obserwacja historyczna.
- Undo wycofuje właściwą etykietę bez kasowania niezwiązanego uczenia.

## 7. Explore używa kontrolowanych eksperymentów

Eksperyment ma wersjonowany `TrialRecord` zawierający m.in.:
- hipotezę,
- zbiór legalnych akcji,
- przypisaną akcję i propensity,
- moment dispatchu i ACK,
- źródła outcome,
- wynik/termination reason,
- reward, jeśli outcome jest znany,
- marker dokładnie-jednokrotnej aplikacji do konkretnej generacji.

ACK oznacza, że transport/urządzenie wykonało komendę. **ACK sam nie jest rewardem komfortu.** Brak wiarygodnego outcome pozostaje nieznany.

## 8. Candidate uczy się w izolacji

Nowy Candidate zaczyna od dokładnego snapshotu bezpośredniego rodzica. Live nie jest trenowany „w miejscu” przez eksperyment lub dryf.

Przepływ wygląda w uproszczeniu tak:

```text
Live G0
  -> Correct / Explore / Autonomous / Change decision
  -> Candidate G1 (Shadow)
  -> dalsza nauka
  -> Candidate G2 (Shadow, porównany z G1)
```

Rodzic pozostaje nienaruszony, a rollback może przywrócić dokładny wcześniejszy model.

## 9. Shadow jest proxy, nie fizycznym wynikiem

W Shadow polityka wykonuje inference i może być oceniana na tych samych przyszłych epizodach co rodzic, ale nie wysyła usług HA.

Dlatego wynik Shadow oznacza:

> „co model przewidziałby w obserwowanym kontekście”

Nie oznacza:

> „wiemy, jaki byłby fizyczny skutek tej niewykonanej akcji”.

To rozróżnienie jest zachowane w provenance, EpisodeEvaluator i benchmarkach.

## 10. Fizyczny outcome zaczyna się dopiero po Executorze

Jedyna fizyczna ścieżka sterowania to:

```text
policy -> ActionIntent -> safety/resource guards -> Executor -> HA service -> ACK/outcome
```

Executor sprawdza m.in. manual priority, kwalifikację, aktualność modelu i konfiguracji, legal action mask, wspólne lease, cooldown/min dwell, support/novelty, ownership i ACK.

Dopiero outcome po rzeczywiście wykonanej komendzie można traktować jako skutek fizycznej akcji — i nadal trzeba poprawnie przypisać go do konkretnej próby oraz okna obserwacji.

## 11. Confidence nie jest jedną liczbą o jednym znaczeniu

Runtime rozdziela:
- presence probability,
- forecast uncertainty,
- expected action utility,
- data coverage,
- empirical policy quality,
- preference alignment,
- decision strength.

Legacy `confidence` jest zachowane dla kompatybilności, ale UI nie powinno opisywać go jako prawdopodobieństwa komfortu.

## 12. Kwalifikacja Control pozostaje konserwatywna

Dla binarnego targetu sam wysoki średni wynik nie wystarcza. Każdy kierunek ON/OFF musi mieć osobne held-out evidence. Bieżący kontrakt wymaga co najmniej 20 próbek na akcję oraz 95% dolnej granicy Wilsona powyżej 78%.

Cold start bez dowodów daje Shadow/fallback i informację o brakującym evidence — nie niższy próg.

## 13. Promocja używa oddzielnego przyszłego testu

Candidate ma oddzielone:
1. dane użyte do budowy/wyboru challengera,
2. przyszłe dane finalnej oceny.

Po rozpoczęciu final evaluation okno jest zamrażane. Regularne podglądanie statusu nie może rozszerzać testu, aż wynik stanie się korzystny. Named validation gates zachowują każde veto, a promocja jest atomowa.

## 14. Dryf tworzy Candidate zamiast resetować Live

Monitor może rozróżnić:
- awarię sensora,
- zmianę topologii/przeniesienie sensora,
- nowy zwyczaj,
- nową preferencję.

Trwałe pogorszenie może rozpocząć izolowaną adaptację. Przejściowa awaria nie powinna powodować natychmiastowego retrainingu. Po promocji monitorowana jest liczba epizodów do odzyskania jakości oraz możliwość rollbacku.

## 15. Historia i trening mają ograniczony koszt operacyjny

Normalny status korzysta z cursorów i sufficient statistics. Teach-RL wykonuje bounded batch as-of zamiast N+1 zapytań. Heavy jobs mają ograniczoną kolejkę i backpressure. Surowe dowody potrzebne do audytu, Undo, replay i rollbacku pozostają zachowane.

## 16. Benchmark produktu F24

`tools/benchmark_product_runtime.py` symuluje ukrytą prawdziwą obecność oraz ukrytą potrzebę światła **poza** mapą obserwacji. Sensory mają opóźnienia, noise, missingness oraz różne formaty. Światło wpływa na późniejszy odczyt lux, więc model może zostać ukarany za skróty oparte na skutku własnej akcji.

Scenariusze obejmują:
- jednego i dwóch domowników,
- rozwidlenie ruchu,
- bezruch,
- brak przyjścia,
- szybki powrót,
- dzień/noc,
- jawną zmianę preferencji,
- fałszywy sensor,
- przeniesiony sensor,
- zmianę zwyczaju.

Dane są dzielone na train, validation i untouched future test. Porównywane są fixed automation, bieżąca polityka produkcyjna, challenger full-ridge w Shadow i conservative fallback. Raportuje się needed light, false ON, premature OFF, opóźnienie, chatter, korekty/100 epizodów i koszt obliczeń z wieloma seedami i przedziałami niepewności.

Benchmark nie obniża progu kwalifikacji i nie wstawia gotowego pozytywnego `benchmark_score`. Jeżeli bieżąca polityka lub challenger nie spełnia kryteriów, raport ma to pokazać. **Brak poprawy jest prawidłowym wynikiem.** Benchmark sam nigdy nie wdraża nowego backendu.
