# 0.14.187: szybszy chronological reward replay

Poprawka ogranicza powtarzane parsowanie JSON i tworzenie znaczników czasu dla tych samych próbek sensorów. Replay odtwarza nakładające się okna historii i zachowuje do 64 próbek każdego wejścia, przez co poprzednio wielokrotnie dekodował identyczne wiersze. Cache w procesie trackera ponownie wykorzystuje ich odczytany stan.

Cache ma dwa limity: 512 wpisów i 1 MiB jednostek budżetu na tracker. Jednostki uwzględniają długość danych i narzut obiektów; nie są pomiarem rzeczywistego RSS Pythona. Duże atrybuty omijają cache. Dane opuszczają cache przez LRU lub przy zamknięciu trackera. Nie powstaje plik cache ani dodatkowy zapis na kartę. Liczba sensorów, retencja, limity bazy i modelu pozostają takie jak w .186.

Klucz zawiera wszystkie pola używane przez dekoder stanu: encję, czas zdarzenia, stan, atrybuty i użytkownika. Metadane czasu odbioru FeatureJournal należą do atrybutów i również zmieniają klucz. Zapytania nadal filtrują dostępność pomiarów według czasu odbioru. Opóźnione pakiety oraz cofanie czasu replay zachowują dotychczasową semantykę. Cache przechowuje stany tylko do odczytu przez wewnętrzne procedury replay.

Analiza korelacji i dobór kontekstu z .186 nadal działają. Nie usuwamy sensorów, próbek ani przykładów treningowych w celu uzyskania przyspieszenia. Warunki jasności korzystają z tego samego pomiaru otoczenia sprzed zapalenia lampy co wcześniej.

## Pomiary

Trzy pary uruchomień rzeczywistego izolowanego workera dla każdego scenariusza, kolejność wersji naprzemienna, bez profilera. Ta sama syntetyczna historia i środowisko Linux/WSL. W tabeli mediana czasu etapu replay, nie całego pobrania historii ani treningu na Pi.

| Scenariusz | .186 | .187 | Krótszy czas replay |
| --- | ---: | ---: | ---: |
| Łazienka, energia radaru, krótki zanik i krótka wizyta | 5,876 s | 4,966 s | 15,5% |
| Światło z warunkiem jasności i światłem emitowanym przez lampę | 5,223 s | 4,602 s | 11,9% |

[Pełny zapis pomiaru](benchmarks/REPLAY_STATE_CACHE_0_14_187.json). Porównanie obejmuje dokładny hash schematu, metadanych doboru oraz zapisanych wag Ridge i klasyfikatora, a także predykcje, nagrody i dostępne wyniki walidacji. Pomija jedynie istniejący znacznik inicjalizacji `last_decay_ts`, który pochodzi z zegara wykonania, oraz pomiary czasu. Wszystkie porównania przeszły.

Mała syntetyczna próba łazienki nie odtworzyła silnej regresji .185 → .186 zgłoszonej na Raspberry Pi. Wskaźnik `0,39 rows/s` dotyczy wierszy sterowanego urządzenia; jeden taki wiersz może powodować wiele odtworzeń kontekstu i próbek sensorów. Pomiar nie wskazuje samodzielnie konkretnej przyczyny całej regresji. Do dalszego porównania rzeczywistej instalacji przydadzą się `training_phase_timings` i `temporal_replay` z eksportu podczas replay.

## Weryfikacja i zastosowanie

Pełny zestaw 2242/2242 testów przeszedł w Linuksie/WSL, bez pominięć. Weryfikacja wydania jest zapisana w [raporcie walidacji](benchmarks/REPLAY_STATE_CACHE_0_14_187_VALIDATION.json). Pięć nowych regresji sprawdza ponowne użycie odczytu, korekty tego samego wiersza, metadane odbioru, limity cache oraz zgodność kontekstu pokoju przy opóźnionym pakiecie i cofaniu czasu.

Zainstaluj .187 i zrestartuj dodatek. Model wytrenowany w .186 nie wymaga Rebuild z powodu tej poprawki. Jeśli trening został przerwany, użyj Resume, gdy jest dostępne. Kontynuuj obserwację łazienki w Shadow. W diagnostyce każdego trackera znajduje się `decoded_state_cache` z liczbą trafień, braków, wyparć i wykorzystanych jednostek budżetu.
