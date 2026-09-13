# HomeMind-HA / Adaptive AI 0.9.2

Lokalne sterowanie Home Assistantem: wspólny model aktywności domu, małe polityki urządzeń i deterministyczny Executor. Dla szybkich urządzeń polityka wybiera stan docelowy; profil urządzenia określa ACK, stabilizację, odstępy i czas ręcznego przejęcia.

Control wyłącza rozpoznane automatyzacje sterujące danym celem i sprawdza ich wyłączenie. Własne potwierdzenia poleceń nie uruchamiają blokady ręcznej. Prawdziwa zmiana użytkownika pozostaje ważnym sygnałem uczenia i czasowo przejmuje sterowanie. Shadow nie wywołuje usług HA.

Poprawka 0.9.2 usuwa globalną blokadę Control przy niepełnym skanie automatyzacji i zachowuje ich ostatnie znane powiązania. Zawiera też poprawki bootstrapu i źródeł obecności z 0.9.1.

Wersja 0.9 dodaje przewidywanie obecności za 1/3/5 sekund, wygaszanie starej wiedzy, ActionIntent, Reward v2, panel Home Intelligence i telemetrię. Losowa mikroeksploracja jest wyłączona. Uczenie historyczne uruchamia się ręcznie; w całej aplikacji działa najwyżej jedno ciężkie zadanie. Restart nie odtwarza automatycznie archiwum.

**Migracja z 0.8:** agenci, ustawienia, archiwum i feedback zostają zachowane. Niezgodne modele są kopiowane do tabeli kopii i oznaczane NEEDS_RETRAIN, z wstrzymanym sterowaniem. Należy wykonać Train, ocenić Shadow i dopiero włączyć Control.

- [Instalacja ZIP i zachowanie danych](docs/INSTALLATION_PL.md)
- [Obsługa](docs/QUICK_START_PL.md)
- [Architektura](docs/ARCHITECTURE_0_9.md)
- [Raport wydania i ograniczenia](docs/RELEASE_0_9_2.md)
- [Testy](TEST_REPORT.md)
- [Benchmark na Raspberry Pi 4](docs/BENCHMARK_PI4.md)

Obsługiwane adaptery obejmują światła, przełączniki, wentylatory, rolety, klimatyzację i wartości numeryczne. Jest to wydanie eksperymentalne. Wyniki symulatora nie dowodzą niezawodności konkretnej instalacji HA.
