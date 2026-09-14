# HomeMind-HA / Adaptive AI 0.11.0

Karta agenta ma cztery akcje: **Shadow/Control**, **Wrong decision**, **Settings** i **Teach**. Wrong decision uczy poprawnego Desired. Teach otwiera powiększalny wykres historii z wyborem dokładnej chwili i zapisem poprawnego Desired. Cofanie ostatniej korekty jest dostępne w Teach oraz Settings; pozostałe operacje i diagnostyka są w Settings.

Ręczne etykiety są odwracalne i nie giną podczas treningu bazowego RL. W Shadow zmieniają predykcję; w Control decyzję wykonuje Executor po sprawdzeniu kontekstu i urządzenia. Current ma niezależne od diagnostyki odświeżanie około 0,5 s.

[Instalacja](INSTALACJA_PL.md) · [Obsługa i opis wydania](docs/RELEASE_0_11_0.md) · [Audyt ręcznej nauki RL](docs/AUDIT_MANUAL_LEARNING_0_11_0.md).

Lokalne sterowanie Home Assistantem: wspólny model aktywności domu, małe polityki urządzeń i deterministyczny Executor. Dla szybkich urządzeń polityka wybiera stan docelowy; profil urządzenia określa ACK, stabilizację, odstępy i czas ręcznego przejęcia.

Control wyłącza rozpoznane automatyzacje sterujące danym celem i sprawdza ich wyłączenie. Własne potwierdzenia poleceń nie uruchamiają blokady ręcznej. Prawdziwa zmiana użytkownika pozostaje ważnym sygnałem uczenia; szybkie światła zachowują profil realtime bez długiej blokady sterowania. Shadow nie wywołuje usług HA.

**0.10.0: eksperymenty kontekstowe każdego agenta.** Przycisk **Eksperymenty** pozwala wybrać obecność, otoczenie lub pracę innych urządzeń. Osobny model online porównuje małe próby ze zwykłą decyzją, uczy się z wyników i ręcznych korekt oraz zapisuje wiedzę i limity w bazie. Włączenie nie przebudowuje dotychczasowego modelu. Domyślnie: wyłączone, maksymalnie 6 prób na 24 h, co najmniej 15 minut przerwy, obserwacja od 30 sekund (dłużej dla wolnych urządzeń).

Zachowane są zmiany aktualnego `main`: kolejka treningów FIFO, szybki profil świateł, kwalifikacja i przegląd Control oraz transakcyjne przejmowanie automatyzacji z przywracaniem po opuszczeniu Control. Uczenie historyczne uruchamia się ręcznie; w całej aplikacji działa najwyżej jedno ciężkie zadanie. Restart nie odtwarza automatycznie archiwum.

**Migracja z 0.8:** agenci, ustawienia, archiwum i feedback zostają zachowane. Niezgodne modele są kopiowane do tabeli kopii i oznaczane NEEDS_RETRAIN, z wstrzymanym sterowaniem. Należy wykonać Train, ocenić Shadow i dopiero włączyć Control.

- [Instalacja ZIP i zachowanie danych](docs/INSTALLATION_PL.md)
- [Obsługa](docs/QUICK_START_PL.md)
- [Architektura](docs/ARCHITECTURE_0_9.md)
- [Eksperymenty i uczenie online](docs/EXPERIMENTS_PL.md)
- [Raport bazowego wydania 0.10.6](docs/RELEASE_0_10_6.md)
- [Testy](TEST_REPORT.md)
- [Benchmark na Raspberry Pi 4](docs/BENCHMARK_PI4.md)

Obsługiwane adaptery obejmują światła, przełączniki, wentylatory, rolety, klimatyzację i wartości numeryczne. Jest to wydanie eksperymentalne. Wyniki symulatora nie dowodzą niezawodności konkretnej instalacji HA.
