# HomeMind Adaptive AI 0.14.151

Log `adaptive-ai-runtime-debug-20261009-041557.json` z 0.14.150 nie potwierdził poprawy całego przebiegu. W 36 ocenach średnie zdarzenie → decyzja wyniosło 991 ms, obserwacja kontekstu 2,37 s, jej najwolniejsza próba 7,62 s. Ostatnie CPU wynosiło 94%. Nie było aktywnego treningu ani błędów w śladach. Snapshot RAM zajmował średnio 4,8 ms, weryfikacja jednego kandydata 44,6 ms, a konwersja partii JSON 229 ms. Poprzedni log miał 231 ocen i inny okres obserwacji; zestawienie średnich nie jest kontrolowanym porównaniem wersji.

## Przyczyna i poprawka

Metryki Context Tournament identyfikują championa przez `tournament_revision`, która pozostaje stała podczas zwykłego starzenia wag i nauki online. Walidacja dokładnego kandydata porównywała tę wartość z `model_revision`, zmieniającą się po tych operacjach. Po pierwszym takim zdarzeniu istniejący kandydat był uznawany za niezgodny i tworzony ponownie przy każdym odczycie. Licznik jego nauki wracał do zera, a kolejny niezależny wynik mógł być odrzucony przez tę samą niespójną kontrolę wersji.

- Walidacja, metadane nowego kandydata, parowana nauka i reset przy zmianie proponowanego schematu używają tej samej stałej epoki co Metrics. Dla starszej polityki bez `tournament_revision` pozostaje rygorystyczny fallback do `model_revision`.
- Cache instancji jest związany z rewizją i checksumą sprawdzonego payloadu źródłowego. Zwykłe starzenie instancji nie wymusza odtwarzania starych wag. Zmieniony poprawny payload wymusza odtworzenie również wtedy, gdy ma ten sam identyfikator rewizji. Payload bez checksumy nie korzysta z ciepłego cache.
- Każdy odczyt nadal sprawdza checksumę. Uszkodzony payload jest odbudowywany, a zmiana championa, schematu lub kontraktu danych nie pozwala wykorzystać wcześniejszego dowodu. Pozostają dotychczasowe bramki promocji i rollbacku.
- Nowe `context_candidate_load` mierzy cały odczyt kandydata, obejmując także ewentualną odbudowę. Ślad `context_candidate_cache` rozróżnia `hit`, `restore` i `rebuild`.

## Weryfikacja

Dziewięć nowych regresji używa rzeczywistych polityk i starzenia wag. Obejmuje zachowanie instancji i dowodu po starzeniu championa, aktualizacji online i starzeniu kandydata, odtworzenie zmienionego poprawnego źródła, odrzucenie dowodu innego championa, naukę z przyszłego wyniku po starzeniu obu polityk, odrzucenie wyniku starej epoki, poprawną epokę po zmianie proponowanego schematu oraz ochronę starszych polityk. Dotychczasowa regresja uszkodzonego ciepłego cache pozostaje wymagana. Pełny zestaw: 1757 uruchomionych testów, w tym klasy ponownie odkrywane przez starszy test kontraktu wydania.

Benchmark z rzeczywistym kontraktem cech i TemporalHistory używa 96 sensorów po 96 historycznych próbek, 256 przykładów klasyfikatora i 20 obserwacji po rozgrzaniu. Następnie symuluje upływ minuty przez cofnięcie znacznika ostatniego starzenia o 61 s i wykonuje rzeczywiste `policy.decay()`. 0.14.150 tworzy 80 kandydatów; 0.14.151 zachowuje istniejące instancje i tworzy zero nowych. Średni czas obserwacji: 413,75 ms → 55,24 ms; p95: 442,02 ms → 60,55 ms. CPU, z końcowym zapisem oczekującej partii: 7781 ms → 1016 ms. To około 87% mniej w tym odtworzonym scenariuszu na komputerze deweloperskim, a nie deklaracja redukcji opóźnienia całej aplikacji na urządzeniu HA.

Benchmark 0.14.150 obejmował krótką serię bez przejścia przez starzenie wag; nie odtwarzał tej niespójności epok. Nowy pomiar kandydata i ślad cache pozwalają zweryfikować jej usunięcie w kolejnym logu. Pozostałe koszty puli sensorów i housekeeping nadal wymagają oceny. Ta poprawka nie dowodzi przewagi jakości agenta nad automatyzacją.

## Instalacja

Aktualizacja i restart dodatku wystarczą. Historyczny model, format JSON i rewizja treningu `shared-home-intents-v26` pozostają zgodne; Rebuild nie jest wymagany. Pakiet ręcznej instalacji: `HomeMind-Adaptive-AI-0.14.151-addon-root.zip`. Sumy SHA256 są publikowane obok ZIP.
