# Raport wydania 0.9.0 — 2026-09-13

## 1. Zmienione moduły

Rozdzielono main.py na settings/storage, HA transport, context/context_engine, home_state/home_bootstrap, policy_backend/policy, intent/executor, engine, history/replay, rewards i telemetry. Rozszerzono UI, konfigurację, Dockerfile oraz CI. Zachowano profile urządzeń w control.py. Szczegóły: [architektura](ARCHITECTURE_0_9.md), [audyt 0.8](AUDIT_0_8.md).

## 2. Finalna architektura

~~~mermaid
flowchart LR
 E[HA Event] --> C[Context]
 C --> H[Shared Home State]
 H --> P[Micro Policy]
 C --> P
 P --> I[ActionIntent]
 I --> X[Executor]
 X --> S[HA Service]
 S --> R[Observation / Reward v2]
 R --> P
~~~

## 3. Migracja DB

Migracja addytywna zachowuje agenty, ustawienia, historię i feedback. Niekompatybilne modele 0.8 są kopiowane do model_backups; agenty przechodzą do NEEDS_RETRAIN i paused. Nie startuje automatyczny replay ani Control. Schema cech wynosi 11, wersja polityki 10. Metadane wspólnego domu i checkpointy są przechowywane w SQLite. Watermark usuwa wyłącznie niezatwierdzone derived experiences po przerwanym uczeniu, pozwalając powtórzyć fragment bez utraty surowej historii.

## 4. Testy i pomiary

84 testy unittest przeszły. Obejmują model domu, mapowanie i sensory, decay i serializację, granice Executor/Shadow, własne ACK vs ręczne korekty, konkurencję zdarzeń, retry, migrację, anulowanie bootstrapu, strumieniowy replay, wznowienie po błędzie i wyuczony symulator antycypacji.

Pełny wynik: [TEST_RESULTS_0_9.txt](TEST_RESULTS_0_9.txt). Dodatkowo sprawdzono kompilację modułów Python, składnię JavaScript oraz lokalny panel w przeglądarce. [Benchmark lokalny](BENCHMARK_LOCAL.json) wyraźnie oddziela mikropomiar z mockiem od rzeczywistego HA.

Nie wykonano testu na fizycznym Home Assistant ani Raspberry Pi 4. Docker CLI był dostępny, ale daemon nie działał, więc obrazu nie zbudowano lokalnie. Workflow CI zawiera testy Python 3.11/3.13 i budowę obrazu; jego wyniku nie deklarujemy bez uruchomienia.

## 5. Service calls

Jedynym bezpośrednim wywołaniem HA.service jest Executor._service w executor.py. HA.service w ha.py stanowi transport. Wywołania obejmują usługi adapterów urządzeń oraz automation.turn_off przy przejęciu Control. Verify używa tego samego wykonawcy. W Shadow liczba wywołań wynosi zero, także przy istniejących automatyzacjach. Control wyłącza i sprawdza rozpoznane automatyzacje, ale respektuje rzeczywistą ręczną korektę.

## 6. Home State Model

Jeden model dla domu uczy przejścia A→B i A→B→C, rozkłady czasu i nieprzyjścia. Dostarcza aktualną i przyszłą obecność 1/3/5 s, przyjście, wyjście i pewność. Ograniczone liczniki/histogramy nie wymagają sieci neuronowej. Mapowanie pochodzi z rejestrów HA lub jawnego fallbacku, bez zgadywania po nazwach.

## 7. Decay

Waga maleje jak 2^(-dt/H). H domyślnie 30 dni dla polityki, 45 dla domu. Prior ridge zostaje, a statystyki obserwacji, momenty, kalibracja i graf stopniowo tracą wagę. Zapisywany czas decay oraz ważenie historycznych próbek przeciwdziałają odmładzaniu starej wiedzy po restarcie.

## 8. Reward v2

Ręczna korekta: -1; słaba akceptacja: +0.15; potwierdzone wyprzedzenie: +0.45 i do +0.2 za użyteczny czas; fałszywa antycypacja: -0.6; zbyt wczesna akcja: -0.35; chatter: -0.2. Suma jest ograniczana do [-1,1]. Samo przyjęcie usługi/ACK nie jest nagrodą. Brak przyjścia karze dopiero po pełnym oknie przy znanym stanie sensorów.

## 9. Główne ryzyka

- Opóźniony, niepełny lub błędny pomiar HA może zmienić prognozę i czas ACK.
- Wiele osób i nakładające się przejścia nie mają osobnej identyfikacji.
- Rzadkie zachowania wymagają danych; confidence i wynik benchmarku nie są gwarancją wykonania.
- Dynamiczne cele automatyzacji, pośrednie skrypty i zewnętrzne kontrolery mogą nie zostać rozpoznane przez przejęcie.
- Utrata sieci, błąd usługi lub nieaktualna intencja nadal wstrzymują akcję.
- RSS, CPU i opóźnienia całego systemu trzeba zmierzyć na docelowym Pi. Debounce 25 ms sam wykorzystuje część budżetu event→intent.
- Aktualizacja źródeł wymaga Rebuild i zachowania prywatnych danych. Nowa aplikacja lokalna ma inny identyfikator niż repozytoryjna.
- Wyłączone przez Control automatyzacje wymagają ręcznego przywrócenia, jeśli użytkownik rezygnuje z Control.

## 10. Celowo poza 0.9 / kierunek 1.0

Model dynamiki i planowanie HVAC, identyfikacja wielu mieszkańców, mocniejsze modele sekwencji, wdrożenie inferencji na mikrokontrolerach oraz ponowne włączenie bezpiecznej eksploracji wymagają osobnych prac i pomiarów. W 0.9 mikroeksploracja jest nieaktywna, choć zapisane ustawienie pozostaje zachowane. Nie dołączono ciężkich bibliotek ML.

## Przykład Shadow i Control

Symulator uczy wspólny graf na 200 trasach oraz rzeczywistą micro policy na osobnych próbkach i kalibracji. Czujnik Kitchen pozostaje OFF w momencie decyzji.

Terrace → Living → P(Kitchen w 3 s) około 0.89 → KitchenLightAgent przewiduje ON → ActionIntent (confidence około 0.93, support 0.72, novelty 0.28).

| Tryb | Executor | Wywołanie HA |
| --- | --- | --- |
| Shadow | SHADOW, zapis decyzji i diagnostyki | 0 |
| Control | ACCEPTED, potem obserwowany ACK | 1 × light.turn_on |

Na wirtualnej osi czasu czujnik Kitchen potwierdza przyjście 2 sekundy po utworzeniu intencji. Reward v2 wynosi około +0.5833. Osobna gałąź Bedroom daje Kitchen OFF; niepotwierdzona antycypacja przy pełnej obserwacji daje -0.6. Są to wyniki symulacji, nie pomiary opóźnienia prawdziwej instalacji. [Surowy wynik symulatora](SIMULATOR_0_9.json).
