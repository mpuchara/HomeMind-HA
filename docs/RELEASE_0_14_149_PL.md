# HomeMind Adaptive AI 0.14.149

Log 0.14.148 wykazał poprawę: średnio 320 ms dla puli sensorów, 2,57 s dla obserwacji kontekstu i 1,05 s zdarzenie → decyzja. Ostatnie p95 zdarzenie → decyzja pozostawało 2,88 s. Samo predict w śladach zajmowało średnio 1,8 ms. Nie było aktywnego treningu. Log nie rozdzielał kosztów poszczególnych rozszerzeń kontekstu.

## Zmiany

- Zwykły przebieg bez aktywnego probation nie tworzy pełnego snapshotu modelu. Dawniej snapshot powstawał przy każdej obserwacji, niezależnie od tego, czy wystąpiła promocja.
- Snapshot powstaje po spełnieniu warunków promocji i wyborze schematu, tuż przed jego migracją. Zawiera aktualne współczynniki i dane, jest odłączony od późniejszych mutacji, a błąd jego przygotowania zatrzymuje promocję przed zmianą modelu.
- Snapshoty należą do konkretnej obserwacji i wątku. Dopasowanie do starego schematu w historii promocji chroni checkpoint, gdy po udanej promocji wystąpi kolejna nieudana próba. Istniejące scoring, progi rollbacku i Shadow po promocji pozostają zachowane.
- Weryfikacja kandydatów nie liczy tej samej checksumy dwukrotnie podczas jednego odczytu. Każdy odczyt nadal sprawdza integralność, również dla kandydatów w cache. Zmieniony lub uszkodzony payload nadal jest odrzucany i odbudowywany z championa.
- Metryka i ślad `context_probation_snapshot` pozwalają zobaczyć koszt wymaganych snapshotów promocji i aktywnego probation. W zwykłych obserwacjach bez tych zdarzeń nie powinno być nowych próbek tej metryki.

## Weryfikacja i ograniczenia

Osiem nowych regresji obejmuje brak zbędnych snapshotów dla ciepłego i zimnego cache, najnowszy odłączony checkpoint, retry po błędzie, dopasowanie przy kolejnej nieudanej próbie, kolejność przygotowania przed migracją, zatrzymanie migracji przy błędzie, jednokrotne sprawdzenie checksumy oraz wykrywanie zmiany payloadu w cache. Pełny zestaw: 1739 uruchomionych testów (repozytorium ponownie odkrywa także klasy importowane przez starszy test kontraktu wydania). Dotychczasowa regresja odtwarzania poprzedniego modelu po 30 niekorzystnych wynikach pozostaje wymagana.

Pomiary deweloperskie obejmują rzeczywisty kontrakt cech, 96 obserwowanych sensorów po 96 przykładów, 256 przykładów klasyfikatora i 20 obserwacji po rozgrzaniu. Średnio 152,80 ms → 80,14 ms (około 48% mniej), p95 163,56 ms → 87,35 ms. Nie są pomiarem opóźnienia całej aplikacji na urządzeniu Home Assistant. Poprawka nie przypisuje całych pozostałych 2–3 sekund jednemu miejscu i nie dowodzi przewagi jakości agenta nad automatyzacją.

## Instalacja

Aktualizacja i restart dodatku wystarczą; model, dane i rewizja treningu `shared-home-intents-v26` pozostają zgodne. Rebuild nie jest wymagany. Ręczna instalacja: `HomeMind-Adaptive-AI-0.14.149-addon-root.zip`; sumy SHA256 są publikowane obok ZIP.
