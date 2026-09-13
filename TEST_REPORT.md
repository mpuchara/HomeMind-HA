# Testy wydania 0.10.6

174 testy unittest przeszły. [Pełny wynik](docs/TEST_RESULTS_0_10_6.txt), [opis wydania](docs/RELEASE_0_10_6.md).

Na czystej bazie odtworzono awarię opublikowanego 0.10.5 przy wykonaniu fast_queue_main.py: AttributeError: NoneType object has no attribute lock, przed uruchomieniem HTTP. Dotychczasowe 170 testów przechodziło mimo tej awarii.

Cztery nowe testy używają osobnych procesów Pythona i rzeczywistego punktu wejścia: import bez bazy, kolejność HTTP/baza/rozszerzenia/pracownicy, widoczność błędów inicjalizacji oraz prawdziwe zapytania HTTP podczas celowo wstrzymanego startu. Weryfikują też kolejkę FIFO, obserwatory korekt i działanie adaptera odkurzacza po imporcie Executor.

Pakiet sprawdza wszystkie siedem skryptów JS, kompilację Pythona, zgodność wersji, [symulator](docs/SIMULATOR_0_10_6.json), [mikrobenchmark](docs/BENCHMARK_LOCAL_0_10_6.json) i integralność ZIP. Lokalny daemon Docker jest niedostępny; test uruchomienia kontenera dodano do CI, bez deklarowania jego wyniku przed wykonaniem. Nie wdrażano na fizycznym HA użytkownika ani nie mierzono Raspberry Pi 4.
