# HomeMind-HA / Adaptive AI 0.10.0

Lokalne sterowanie Home Assistantem: wspólny model aktywności domu, małe polityki urządzeń i deterministyczny Executor. Dla szybkich urządzeń polityka wybiera stan docelowy; profil urządzenia określa ACK, stabilizację, odstępy i czas ręcznego przejęcia.

Control wyłącza rozpoznane automatyzacje sterujące danym celem i sprawdza ich wyłączenie. Własne potwierdzenia poleceń nie uruchamiają blokady ręcznej. Prawdziwa zmiana użytkownika pozostaje ważnym sygnałem uczenia; szybkie światła zachowują profil realtime bez długiej blokady sterowania. Shadow nie wywołuje usług HA.

**0.10.0: eksperymenty kontekstowe każdego agenta.** Przycisk **Eksperymenty** pozwala wybrać obecność, otoczenie lub pracę innych urządzeń. Osobny model online porównuje małe próby ze zwykłą decyzją, uczy się z wyników i ręcznych korekt oraz zapisuje wiedzę i limity w bazie. Włączenie nie przebudowuje dotychczasowego modelu. Domyślnie: wyłączone, maksymalnie 6 prób na 24 h, co najmniej 15 minut przerwy, obserwacja od 30 sekund (dłużej dla wolnych urządzeń).

Zachowane są zmiany aktualnego `main`: kolejka treningów FIFO, szybki profil świateł, kwalifikacja i przegląd Control oraz transakcyjne przejmowanie automatyzacji z przywracaniem po opuszczeniu Control. Uczenie historyczne uruchamia się ręcznie; w całej aplikacji działa najwyżej jedno ciężkie zadanie. Restart nie odtwarza automatycznie archiwum.

**Migracja z 0.8:** agenci, ustawienia, archiwum i feedback zostają zachowane. Niezgodne modele są kopiowane do tabeli kopii i oznaczane NEEDS_RETRAIN, z wstrzymanym sterowaniem. Należy wykonać Train, ocenić Shadow i dopiero włączyć Control.

- [Instalacja ZIP i zachowanie danych](docs/INSTALLATION_PL.md)
- [Obsługa](docs/QUICK_START_PL.md)
- [Architektura](docs/ARCHITECTURE_0_9.md)
- [Eksperymenty i uczenie online](docs/EXPERIMENTS_PL.md)
- [Raport wydania i ograniczenia](docs/RELEASE_0_10_0.md)
- [Testy](TEST_REPORT.md)
- [Benchmark na Raspberry Pi 4](docs/BENCHMARK_PI4.md)

Obsługiwane adaptery obejmują światła, przełączniki, wentylatory, rolety, klimatyzację i wartości numeryczne. Jest to wydanie eksperymentalne. Wyniki symulatora nie dowodzą niezawodności konkretnej instalacji HA.
