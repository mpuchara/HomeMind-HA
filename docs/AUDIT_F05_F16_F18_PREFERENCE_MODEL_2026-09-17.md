# Stage 07 — jawny model preferencji oświetlenia

Data: 2026-09-17  
Zakres: F05, F16–F18; etap 07 audytu 0.14.11.

## Cel

Preferencja użytkownika nie jest już utożsamiana z historycznym utrzymaniem stanu,
wynikiem fizycznym epizodu ani samym brakiem korekty. Dla `light.* / power` finalny
runtime może użyć małego, sprawdzalnego modelu nadzorowanego zbudowanego wyłącznie z
jawnych faktów `ManualFeedbackJournal`.

Model obecności pozostaje w `ContextEngine/SharedHomeStateModel`, bazowa polityka
`MultiHorizonPolicy` pozostaje bootstrapem z historii, a fizyczny wynik działania pozostaje
osobnym sygnałem dla istniejącej polityki/`EpisodeEvaluator`. Model preferencji nie importuje
LLM i nie wykonuje komend HA.

## Źródła danych i wagi

`LightingPreferenceModel` ma jawny kontrakt v1:

| Źródło | Znaczenie | Waga w modelu preferencji |
|---|---|---:|
| `correct_action` | jawne „powinno być ON/OFF” | `1.0` |
| `rejected_action` | jawne „ta akcja była zła” | `-0.5` dla ocenianej akcji |
| historia automatyzacji | demonstracja startowa | `0.0` — tylko osobny fallback polityki |
| wynik `EpisodeEvaluator` | pomiar środowiska | `0.0` |
| brak korekty | brak jawnej preferencji | `0.0` |

Negatywna ocena bez `correct_action` nie tworzy automatycznie pozytywnej etykiety dla
innej akcji. Dla binarnego światła to nadal ważne: „OFF było źle” nie oznacza, że każdy
przyszły podobny przypadek ma dostać twarde ON bez pozytywnej etykiety.

Masa starych próbek automatyzacji nie jest sumowana z jawnym feedbackiem. Jeżeli jawna
preferencja pasuje do kontekstu, ma pierwszeństwo nad historycznym bootstrapem; jeżeli nie
ma dopasowanej preferencji, stara polityka pozostaje fallbackiem.

## Zakres i konflikt

Model korzysta z wersjonowanej semantycznej sygnatury Stage 06 i tego samego kontraktu
`teaching.distance`. Zapis `one_time` nigdy nie jest generalizowany przez model.
`episode` może być użyty tylko przy zgodnym `episode_id`. `similar_context` i
`persistent_preference` mogą być dowodem modelu, ale konflikt oznaczony przez Stage 06
pozostaje nietrenowalny.

Bezpośrednia instrukcja ma osobny zakres. Dla nowych, journal-linked etykiet:

- `persistent_preference` pozostaje bezpośrednią instrukcją w pasującym kontekście,
- `one_time` i `similar_context` są bezpośrednią instrukcją tylko w istniejącym oknie TTL
  ActionIntent; później `one_time` znika, a `similar_context` trafia do modelu preferencji,
- `episode` działa w tym samym epizodzie; przy braku identyfikatora epizodu zachowuje tylko
  natychmiastowe okno TTL,
- stare, niepowiązane z journalem `teaching_labels` zachowują dotychczasową semantykę i nie
  są reinterpretowane.

## Kompozycja decyzji

Finalny entrypoint nadal jest:

`adaptive_ai/src/run.sh -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py`.

`preference_queue_main.py` instaluje usługę `LightingPreferenceModel` i
`PreferenceDecisionComposer` przed uruchomieniem workerów. Nie dodaje wrappera
`process_agent`.

Właścicielem kompozycji jest `Engine.process_agent`:

1. `policy.predict` tworzy historyczny bootstrap i statystyki bezpieczeństwa,
2. aktywna instrukcja o właściwym zakresie ma pierwszeństwo,
3. w przeciwnym razie model preferencji może wybrać akcję,
4. gdy model abstynuje, obowiązuje bazowa polityka; istniejący Explore może zaproponować
   bezpieczny eksperyment zgodnie z dotychczasowym kontraktem,
5. `ActionIntent` otrzymuje addytywne pole `decision_source`,
6. tylko `Executor` waliduje i ewentualnie wykonuje komendę.

Preferencja statystyczna nie dostaje wyjątku od confidence/support/novelty. Tylko jawna,
aktywna instrukcja zachowuje dotychczasowe zachowanie Executora dla `teaching_id`.
Shadow i niepromowany Candidate nadal nie wykonują fizycznych komend.

## Niezależne dowody

Jeden `feedback_id` jest jednym niezależnym dowodem. Ten sam fakt może zawierać dwie
składowe optymalizacji — dodatnią etykietę poprawnej akcji i ujemną ocenę odrzuconej — ale
`independent_evidence_count` i `calibration_evidence_count` pozostają równe 1. Wielokrotne
wywołanie inferencji nie zwiększa tych liczników.

## Deterministyczny benchmark odbioru

Test Stage 07 rozdziela dwa przyszłe konteksty:

- kontekst wymagający adaptacji, w którym historyczny bootstrap nadal mówi OFF, a jawna
  korekta mówi ON,
- nietknięty kontekst, w którym bootstrap OFF jest poprawny.

W fixture:

- bootstrap-only: 1 błąd,
- dokładne odtworzenie starej korekty bez semantycznego uogólnienia: 1 błąd,
- nowy model po jednej korekcie: 0 błędów,
- regresja w nietkniętym kontekście: 0.

To test kontraktu i adaptacji na syntetycznych wydzielonych kontekstach, nie dowód jakości
w prawdziwym domu.

## Migracja i trwałość

Etap 07 nie dodaje tabel i nie zmienia zapisanych wektorów modeli. Korzysta z addytywnego
`manual_feedback_journal`/`manual_feedback_effects` Stage 06. `ActionIntent.decision_source`
to pole runtime/telemetrii, nie migracja modelu.

Undo działa przez istniejący kontrakt Stage 06: fakt otrzymuje `undone_ts`, powiązane etykiety
są wycofywane, a potrzebny Candidate jest przebudowywany z dziennika. Model preferencji
czyta tylko aktywne fakty, więc po undo i po restarcie ten sam feedback nie ma wpływu.

## Ograniczenia

- v1 obsługuje wyłącznie `light.* / power`; jasność i inne urządzenia pozostają przy
  istniejącej polityce.
- model jest świadomie prostym nearest-context baseline, nie kalibrowanym
  prawdopodobieństwem komfortu.
- `EpisodeEvaluator` pozostaje źródłem pomiaru fizycznego, ale jego wynik nie jest jeszcze
  bezpośrednim wejściem modelu preferencji.
- aktywny `episode_id` nie jest dziś uniwersalnym polem każdego ticku runtime; dlatego
  epizodowa instrukcja bez jednoznacznego aktywnego identyfikatora po TTL abstynuje zamiast
  uogólniać się na kolejne sytuacje.
- model czyta mały zbiór jawnych faktów z SQLite przy decyzji. Optymalizacja cache/batch
  należy do etapu wydajnościowego F22, bez zmiany semantyki.

## Przykładowy przebieg

1. Historyczna polityka proponuje `OFF`.
2. Użytkownik oznacza decyzję: odrzucone `OFF`, poprawne `ON`, zakres `similar_context`.
3. `ManualFeedbackJournal` zapisuje jeden fakt z dwiema składowymi: etykietą `ON` i oceną
   `OFF`.
4. W bezpośrednim oknie TTL działa instrukcja użytkownika.
5. W późniejszym, semantycznie podobnym kontekście instrukcja nie jest już twardą nakładką;
   `LightingPreferenceModel` wybiera `ON`, źródło decyzji to `preference_model`.
6. `ActionIntent` niesie to źródło, ale normalne progi jakości i wszystkie guardraile
   Executora nadal obowiązują.
7. W Shadow wynik pozostaje tylko predykcją. W Control tylko Executor może wysłać usługę.
8. Po undo fakt znika z aktywnych dowodów; po restarcie model odtwarza ten sam stan z SQLite.
