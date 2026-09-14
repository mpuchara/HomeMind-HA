# Testy wydania 0.10.9

186 testów unittest przeszło. [Pełny wynik](docs/TEST_RESULTS_0_10_9.txt) · [Opis scenariuszy i ograniczeń](docs/RELEASE_0_10_9.md).

Sprawdzono backend obu rodzajów uczenia, utrwalenie modelu i kontekstu, brak usług HA przy nauce Desired, odróżnianie Current od Desired, wyścig z treningiem i uzgadnianie kart. Lokalna przeglądarka potwierdziła widoczność przycisków, ich działanie na symulowanym urządzeniu i filtrowanie.

Pełne sprawdzenia pakietu obejmują Python, wszystkie JS, symulator, mikrobenchmark, zgodność wersji i integralność ZIP. Docker oraz fizyczny HA nie były testowane lokalnie.
