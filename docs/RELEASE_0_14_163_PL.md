# HomeMind Adaptive AI 0.14.163

## Nowy log i zakres poprawki

Log adaptive-ai-runtime-debug-20261009-220924.json pochodzi z 0.14.162. Recent CPU wynosi 83,403%, od startu 42,433%, RSS 102,066 MiB. Recent event → decision p95 to 197,474 ms, z 17 próbek; Shadow observation p95 450,998 ms. Nie ma błędów bazy, kolejek ani workera, a pending zapisów Context wynosi zero. Cache kodowania ma 1360 trafień, 6 misses i 6 wpisów zajmujących około 1,55 MiB, więc nie widać thrashingu.

Są 940 ładowania i weryfikacje kandydatów przy 235 obserwacjach, czyli cztery weryfikacje na obserwację. Średnia jednej obowiązkowej weryfikacji wynosi 23,634 ms. Retained trace obejmuje około 71 sekund; nie zawiera prune ani krytycznych spanów, więc nie potwierdza bezpośrednio czasu działania poprawki czyszczenia z 162. Brakuje też próbek treningu/fit. Różne okna i tempo zdarzeń nie pozwalają przypisać wzrostu CPU samemu wydaniu.

## Pełna kontrola zawartości bez dodatkowej pętli po wagach

Poprzednio kod Pythona sprawdzał typ każdego liścia modelu, a następnie natywny pickle ponownie przechodził całą strukturę, aby zamrozić zawartość. Teraz prywatny Pickler używa natywnego przejścia i odrzuca wszystkie redukcje niestandardowe, zanim mogą uruchomić kod obiektu. Jego memo jest sprawdzane pod kątem dokładnych typów; binarne builtins i zbiory są odrzucane. PickleBuffer ma osobny callback odrzucający. Klucze słownika nadal muszą być scalar-only: niepuste tuple są wykrywane w memo, a puste tuple jako klucze są sprawdzane osobno. Obiekty nieobsługiwane trafiają do poprzedniego encodera.

Każde wywołanie tworzy świeże kompletne bajty zawartości. Trafienie cache wymaga SHA obrazu i dokładnego porównania bajtów; nie opiera się na tożsamości obiektu lub deklarowanym checksum. Każda weryfikacja ponownie oblicza SHA256 wszystkich kanonicznych bajtów modelu. Nie zapamiętujemy wyniku weryfikacji. Zachowane są limit cache 16 MiB i odłączony snapshot dla encodera. Nie dekodujemy pickle z bazy, eksportu ani pliku; trwały format modeli nadal jest JSON. Zapisane modele, ich checksum, wagi i kontrakty v26 pozostają zgodne.

Dodane metryki context_candidate_features i context_candidate_predict mierzą osobno budowę wektora i predykcję każdego kandydata, także nieudane fazy. Nie zmieniają kolejności predict → paired score → learn.

## Walidacja

20 nowych testów obejmuje bajty obrazu, 100 losowych zagnieżdżonych drzew, scalar keys, tuple keys, aliasy, cykle, unicode, signed zero, wielkie int, NaN/Inf, klasy/metaklasy, subclassy, funkcje, bufory, numpy i typy binarne. Sprawdzają brak wykonania niestandardowych redukcji, brak dekodowania obiektów nieobsługiwanych, SHA przy warm cache, wykrycie mutacji bez zmiany revision/checksum oraz odłączony obraz przy zmianie źródła. Osobne regresje obejmują realną predykcję kandydata i błędy cech/predykcji. Pełny zestaw: 1941 testów.

tools/benchmark_native_witness.py porównuje zamrożone 0.14.162 i obecną implementację w tym samym procesie, z niezależnymi równymi cache dla pełnego SHA i zapisu JSON Shadow. Cztery modele, dwa rozmiary, trzy naprzemienne powtórzenia. Wszystkie SHA i dane JSON pozostają zgodne. Mediany z trzech powtórzeń Windows: pełna kontrola SHA 6,539 → 2,977 ms dla mniejszych modeli i 27,584 → 12,633 ms dla większych (około 2,2×). Dekodowanie snapshotu i JSON Shadow 9,404 → 5,193 ms oraz 77,827 → 46,718 ms (około 1,7–1,8×). Poszczególne powtórzenia są zmienne. To ciepły benchmark komponentów; nie prognoza CPU ani całej latencji HA. Benchmark jest wykonywany także w CI.

Implementację dispatch/reducer_override i memo sprawdzono w kodzie CPython oraz dokumentacji: https://docs.python.org/3/library/pickle.html#custom-reduction-for-types-functions-and-other-objects i https://github.com/python/cpython/blob/3.11/Modules/_pickle.c . Testy działają na wspieranych Python 3.11 i 3.13.

## Instalacja

HomeMind-Adaptive-AI-0.14.163-addon-root.zip lub repository.zip, z SHA256. Aktualizacja i restart, bez Rebuild.
