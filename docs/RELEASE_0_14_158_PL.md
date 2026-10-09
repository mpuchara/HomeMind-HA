# HomeMind Adaptive AI 0.14.158

Log `adaptive-ai-runtime-debug-20261009-161114.json` z 0.14.157 pokazuje działający cache JSON Shadow: 260 calls, 260 hits, 0 encodes/fallbacks. Wspólny cache ma 952 hits/17 misses/16 wpisów/4 706 503 bajty payloadów. Średni zapis JSON wynosi 154,924 ms, ostatnie p95 168,832 ms (poprzedni log: średnia 212,268 ms/p95 252,439 ms). Różne obciążenia nie pozwalają przypisać całej różnicy samej poprawce. CPU recent 23,92%, RSS 122,79 MiB. Brak błędów SQLite, kolejek lub workera; w chwili eksportu kolejki są opróżnione. Checkpoint: 93 runs, 0 BUSY/error, 18672/18672 ramek, remaining=0. Fizyczny WAL około 73,37 MiB jest alokacją, nie zaległością; maksymalny commit zachowanego okna 13,665 ms.

Ogólne opóźnienia nadal są istotne. Ostatnie event → decision p95 to 558,463 ms, wrapped inference p95 1260,965 ms, więcej niż w poprzednim oknie. Critical spans zachowały obserwacje Shadow trwające 4,328 i 4,050 s na device-control_0, a otaczające przebiegi agenta 4,538 i 4,135 s. Produkcyjna decyzja powstaje przed tą pracą Shadow, lecz cały wątek czeka na jej zakończenie przed następnym zdarzeniem. To możliwe źródło opóźnień kolejnych decyzji. Nie jest to dowód błędu uczonej polityki ani efekt wolnego commit. Log nie trwa przez pełną sesję, a stare pomiary nie rozdzielają dokładnie fit, aktualizacji, serializacji i wyboru sensorów.

## Zmiana obliczeń

BinaryStateClassifier.fit używał macierzy 4 × dims, mimo że już wcześniej maska variance > 1e-10 zerowała stałe kolumny cech i hinge. Teraz iloczyny macierzy w 160 krokach optymalizatora obejmują wyłącznie kolumny pozostawione przez tę samą maskę. Nie usuwamy żadnej zmiennej cechy. Po dopasowaniu współczynniki są rozwijane do pierwotnych identyfikatorów i pełnych wymiarów, z zerami w pominiętych kolumnach.

Normalizacja wszystkich cech, progi wariancji/scale, balans klas, wagi pojedynczych przykładów, reservoir i recent tail, deduplikacja ID, funkcja celu, clipping/hinge, 160 iteracji, learning rates i ridge pozostają. Fit nadal uruchamia się przy tej samej liczbie zaakceptowanych przykładów i przy pełnej wadze korekty. Nie zmniejszamy treningu ani nie odrzucamy krótkich prawidłowych wizyt. Usuwamy obliczenia na zerach. Algebra jest ta sama; kolejność redukcji float może dać minimalnie inne współczynniki, więc nie obiecujemy identycznych bajtów ani zachowania dokładnych remisów na granicy decyzji. Nowy zapis zawsze liczy własne pełne SHA256.

Format classifier contract 1, pełne długości center/scale/weights/hinge oraz wersje policy/schema/v26 pozostają zgodne. Stare modele są czytane bez migracji. Predict → paired score → learn, stable champion epochs, wykluczenie own-command i bramki kwalifikacji/promotion pozostają.

## Pomiary i walidacja

Nowe RAM metrics: binary_classifier_fit, context_candidate_training, context_candidate_serialization, context_candidate_score i context_pool_selection. Fit trace podaje dims, rows, varying_features i design_columns. Timingi bez debug to tylko liczniki RAM; szczegółowe span i critical tail wymagają włączenia debug. Nie dodajemy SQL ani polling. Status error/exception jest raportowany i propagowany do istniejącej obsługi.

20 nowych regresji porównuje współczynniki i decyzje z zamrożoną pełną ścieżką 0.14.157: macierze rzadkie/gęste/losowe, nieciągłe ID, wszystkie stałe, próg wariancji, scale floor, nierówne klasy i wagi, reservoir/recent ID, pełny zapis/odczyt, schema remap, 64-próbkowy interwał i natychmiastową korektę, minimalną liczbę przykładów, wymiary trace i błędy. Rzeczywisty paired future test potwierdza kolejność i nowe fazy. Dotychczasowe testy nieruchomej obecności, checksum i epoch są zachowane. Pełny zestaw: 1887 testów; CI obejmuje Python 3.11/3.13, obraz i benchmark zgodności starego optymalizatora.

`python tools/benchmark_classifier_fit.py` odtwarza syntetyczne dopasowanie 256 przykładów i 512 wymiarów, z jednym wątkiem BLAS jak w Dockerfile. Osiem przebiegów na komputerze Windows: 12 zmiennych cech 34,10 → 4,30 ms; 48 cech 35,60 → 7,12 ms (około 5 razy szybciej); 128 cech 35,50 → 11,66 ms; prawie gęste 511 cech 43,90 → 40,34 ms. Maksymalna różnica score poniżej 5e-15; współczynniki mieszczą się w tolerancji 1e-10 i wszystkie badane decyzje są zgodne. Raport jest w BUILD_INFO. To pomiar komponentu, nie prognoza opóźnień HA.

Nie przypisujemy całych 4 sekund tylko fit: stary trace nie ma takiej rozdzielczości. Nowy log pokaże jego udział oraz koszt serializacji i wyboru puli. Pozostają obliczenia cech, scoring, serializacja/JSON, synchroniczna obserwacja Shadow, I/O i inne źródła opóźnień. Gęste dane mogą skorzystać niewiele; zmiana układu macierzy może mieć różny koszt na urządzeniach.

## Instalacja

`HomeMind-Adaptive-AI-0.14.158-addon-root.zip` i SHA256 są w wydaniu GitHub. Aktualizacja i restart, bez Rebuild. Optymalizacja dotyczy następnych dopasowań; istniejące gotowe współczynniki nie są przeliczane podczas instalacji. Polityka checkpointu/trwałości NORMAL i wspólny limit cache 16 MiB pozostają.
