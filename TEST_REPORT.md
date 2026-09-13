# Testy wydania 0.10.0

152 testy unittest przeszły, w tym 25 testów eksperymentów. [Pełny wynik](docs/TEST_RESULTS_0_10_0.txt).

[Scenariusze i ograniczenia](docs/RELEASE_0_10_0.md), [symulator](docs/SIMULATOR_0_10_0.json), [mikrobenchmark](docs/BENCHMARK_LOCAL_0_10_0.json).

Sprawdzono odrębność trzech kontekstów, realne aktualizacje wag po wyniku, zmianę kolejnych wyborów, kontrolę porównawczą, budżet po restarcie, brak nagrody za ACK i za utratę obserwacji, powolne urządzenia, granice fizycznych nastaw i pełną ścieżkę wykonania z ręczną korektą. Zachowano wszystkie 127 testów pobranego main.

Menu sprawdzono w przeglądarce na lokalnym fixture: wybór kierunku, zapis, ponowne otwarcie i układ. Pakiet sprawdza także wszystkie pliki JS, kompilację Python, jeden punkt wywołania HA i sumy plików ZIP. Nie wykonano sterowania na fizycznym HA ani pomiarów Raspberry Pi 4.
