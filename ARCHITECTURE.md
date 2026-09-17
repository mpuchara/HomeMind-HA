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

`tools/benchmark_product_runtime.py` tworzy deterministyczny świat z ukrytą prawdziwą obecnością oraz ukrytą potrzebą światła, osobnymi od obserwacji. Sensory mają opóźnienia, noise, missingness i różne reprezentacje; akcja lampy wpływa na obserwowany lux.

Benchmark rozdziela:

```text
train: historyczne demonstracje
validation: demonstracje używane do kalibracji/wyboru challengera
future test: niewidziany wcześniej hidden truth używany wyłącznie do oceny produktu
```

Porównywane są: stała automatyzacja, bieżący produkcyjny runtime w Shadow, full-ridge challenger w Shadow i ostrożny fallback. Raport obejmuje needed-light coverage, false ON, premature OFF, opóźnienie, chatter, korekty/100 epizodów i koszt obliczeń z wieloma seedami oraz przedziałami niepewności.

Benchmark **nie** obniża kwalifikacji, nie wstawia gotowego `benchmark_score` i nie promuje backendu. Niespełnione kryteria są prawidłowym wynikiem. Wynik syntetyczny/CI nie zastępuje fizycznego M&V.

CI dodatkowo uruchamia dokładny source entrypoint oraz obraz i sprawdza, że PID 1 obrazu kończy w `trial_queue_main.py`. Dzięki temu benchmark jakości i test uruchomienia dotyczą tego samego stosu kompozycji.
