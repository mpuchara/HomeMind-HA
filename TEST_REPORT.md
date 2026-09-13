# Testy wydania 0.10.1

153 testy unittest przeszły, w tym 25 testów eksperymentów. [Pełny wynik](docs/TEST_RESULTS_0_10_1.txt).

[Scenariusze i ograniczenia](docs/RELEASE_0_10_1.md), [symulator](docs/SIMULATOR_0_10_1.json), [mikrobenchmark](docs/BENCHMARK_LOCAL_0_10_1.json).

Sprawdzono odrębność trzech kontekstów, realne aktualizacje wag po wyniku, zmianę kolejnych wyborów, kontrolę porównawczą, budżet po restarcie, brak nagrody za ACK i za utratę obserwacji, powolne urządzenia, granice fizycznych nastaw i pełną ścieżkę wykonania z ręczną korektą. Zachowano wszystkie 127 testów pobranego main.

Menu sprawdzono w przeglądarce na lokalnym fixture: wybór kierunku, zapis, ponowne otwarcie i układ. Pakiet sprawdza także wszystkie pliki JS, kompilację Python, jeden punkt wywołania HA i sumy plików ZIP. Nie wykonano sterowania na fizycznym HA ani pomiarów Raspberry Pi 4.

Nowy test wykonuje rzeczywisty app.js, sprawdza pierwsze zapytanie API, timer i podpięcie przycisków. Na oryginalnym kodzie 0.10.0 odtworzono ReferenceError: toggleExplore is not defined. Na poprawce test przechodzi.
