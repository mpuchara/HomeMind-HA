# HomeMind Adaptive AI 0.14.161

## Mniej powtarzanej pracy obserwatora

Observed pool przechowuje historię sensora oraz przykłady powiązane z niezależnymi zmianami targetu. W 0.14.160 zmiana sensora ponownie kodowała do JSON także niezmienione przykłady i screening. Teraz wykorzystujemy istniejące niezmienne ciągi JSON tych dwóch pól. Historia pozostaje świeża. Nowa niezależna zmiana targetu zawsze generuje nowe przykłady i screening. Po restarcie pierwszy snapshot zawiera całość danych odczytanych z bazy. Nie dodajemy drugiego cache modeli ani nie pomijamy kontroli SHA256.

Krótkie wizyty, w tym mycie rąk, nadal dostarczają niezależnych etykiet ON/OFF. Każda obserwacja nadal zwiększa liczniki dostępności, zachowuje zmiany historii i own-action leakage. Bufor zapisuje kompletne, niezmienne wiersze; coalescing, retry i końcowe bariery trwałości pozostają zgodne. Cel treningu, wagi, predict → paired score → learn i bramki promocji pozostają zgodne.

## Kolejka Correct

W oknie logu występuje 139 odczytów pustej kolejki workflow w około 40 sekund. Worker odpytywał SQLite co 250 ms. Zgłoszenie Correct posiada sygnał wybudzający, ustawiany po trwałym commit. Teraz worker korzysta z tego sygnału, a 30 sekund jest okresem kontrolnego odczytu. Opróżnia zaległe zadania bez czekania, również przy starcie i odzyskiwaniu. Clear następuje przed claim, a stop sprawdzany jest także przed wait. Zgłoszenie z aplikacji budzi worker od razu; zapis z innego procesu bez sygnału zostanie wykryty przy odczycie kontrolnym, do 30 sekund. HTTP 202, idempotencja request_id i zasady uruchamiania treningu Candidate pozostają zgodne.

## Pomiary i walidacja

Log adaptive-ai-runtime-debug-20261009-195244.json pochodzi z 0.14.160. Recent CPU 57,659%, RSS 124,047 MiB, recent event → decision p95 253,383 ms i wrapped inference p95 778,286 ms. Obserwacja Shadow trwa średnio 329,051 ms. Błędy SQLite, kolejek i workera: 0; brak aktywnych i krytycznych spanów. Background checkpoint miał maksymalnie około 4,30 s, lecz nie był foreground checkpointem ani błędem. Fizyczny rozmiar WAL nie określa zaległości zapisu.

Benchmark tools/benchmark_observed_pool_json.py wykonuje poprzedni i obecny encoder w tym samym procesie na identycznych danych: 96 zatrzymanych przykładów, 32 punkty historii, 96 zmian sensora, w tym 6 nowych etykiet. Windows: cała sekwencja 51,5–55,6 ms → 8,9–10,3 ms, około 5,1–6,2×. Mediana obserwacji 0,45–0,46 ms → 0,019–0,020 ms. Każdy snapshot ma identyczne bajty. To syntetyczny pomiar komponentu; nie prognozuje czasu decyzji ani CPU na urządzeniu HA. Wpływ na cały runtime wymaga kolejnego porównywalnego logu.

10 nowych testów sprawdza cache-miss po restarcie, niezmienione przykłady, odświeżanie screeningu, krótkie etykiety i niezmienność starszego snapshotu. Test integracyjny porównuje wszystkie pola trwałych wierszy starego i nowego obserwatora, synchronicznie i po coalescingu batch, uwzględniając własną komendę, zmiany dostępności i krótkie wizyty. Testy workera obejmują idle, zgłoszenie między pustym claim a wait, zgłoszenie podczas wait, recovery i stop. Cały zestaw: 1911 testów. Benchmark kontroli bajtów jest także w CI.

## Instalacja

Paczki HomeMind-Adaptive-AI-0.14.161-addon-root.zip i repository.zip oraz SHA256 są w wydaniu GitHub. Aktualizacja i restart; Rebuild nie jest wymagany. Modele i schemat treningu shared-home-intents-v26 pozostają zgodne.
