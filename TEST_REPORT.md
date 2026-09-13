# Testy wydania 0.9.2

107 testów unittest przeszło. [Pełny wynik](docs/TEST_RESULTS_0_9_2.txt).

[Opis regresji i ograniczeń](docs/RELEASE_0_9_2.md), [symulator](docs/SIMULATOR_0_9_2.json), [mikrobenchmark](docs/BENCHMARK_LOCAL_0_9_2.json).

Odtworzono błąd HTTP zmiany trybu przy siedmiu niedostępnych konfiguracjach automatyzacji. Wersja poprawiona włącza Control, zachowując weryfikację wyłączenia znanych automatyzacji celu. Sprawdzono składnię Python/JavaScript i integralność paczki. Nie deklarujemy pomiarów fizycznego HA ani Raspberry Pi 4.
