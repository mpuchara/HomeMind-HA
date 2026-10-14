# 0.14.197: mniej powtarzanych odczytów podczas Shadow

Eksport z 14 października 18:55 pochodzi z .196. Średni czas zdarzenie → decyzja wynosi 333 ms, a ostatnie p95 371 ms. Sam model potrzebuje średnio 64 ms, kontekstowe Shadow 266 ms. Kontekst i fast-light nie zgłaszają błędów zapisu, observed_pool nie wymusza opróżnień, a koordynacja WAL odłożyła 15 prób. Część pozostałych batchy była poprawnie ponawiana po BUSY. Limity miejsca nie odmówiły zapisów. Ostatni pomiar CPU procesu wynosi 132%, średni od uruchomienia 83%; wskaźnik procesu może przekraczać 100% przy użyciu wielu rdzeni.

W szczegółowym śladzie 30 obserwacji Shadow zajmuje łącznie około 8,5 s. Odczyt census puli sensorów wywołano 147 razy i zajmował łącznie około 2,08 s, czyli około 69 ms na obserwację. Te czasy zawierają również wpływ współbieżnej pracy; nie są kontrolowanym benchmarkiem wersji. Dlatego celem poprawki jest ograniczenie powtórzeń tego konkretnego odczytu, a nie zmiana przykładowych danych lub liczby challengerów.

## Zmiana

Dotychczas cache census działał w wewnętrznej warstwie candidate observera. Warstwy jakości, timing i promocji otaczające ten observer wykonywały kolejne odczyty tych samych identyfikatorów puli. Pełna produkcyjna obserwacja Shadow rozpoczyna teraz zakres współdzielenia, a zakresy zagnieżdżone go ponownie używają. Przy stabilnej puli produkcyjny test z czterema zewnętrznymi odczytami zmniejsza zapytania census z pięciu do jednego.

Zakres należy do bieżącego wątku i kończy się w finally. Nie przechowuje census pomiędzy obserwacjami. Cache zawiera wyłącznie ograniczoną listę trwałych identyfikatorów lub count; odczyty schematu, polityki, jakości i stanu promocji nadal wykonują swoje dotychczasowe operacje. Bieżące klucze oczekujące w RAM są uwzględniane w count także po dodaniu sensora w tej samej obserwacji. W wariancie zapisu synchronicznego nowe klucze unieważniają wcześniejszy COUNT. Zmiana limitu puli otrzymuje osobny wpis. Nie pomija odczytu w kolejnym zdarzeniu.

Główna predykcja i wynik sterowania nadal poprzedzają Shadow. Liczba sensorów, etykiety, cechy, wagi, checksum na każdym użyciu, scoring, uczenie i bramki promocji pozostają zachowane. Nie ma nowego cache na dysku, wzrostu limitów RAM ani zmiany zabezpieczeń bazy/WAL/wolnego miejsca. Bufory i retry z .196 są zawarte.

## Weryfikacja

2339/2339 testów przeszło w Linuksie/WSL bez pominięć (223.301 s); compileall przeszedł. Osiem nowych regresji sprawdza rzeczywisty wrapper procesu agenta i kolejność sterowania, zakresy zagnieżdżone, świeżą następną obserwację, zmianę schematu/promocji, obsługę wyjątku, nowe klucze w RAM, invalidację po INSERT, zmianę limitu i izolację wątków. Benchmark uruchamia produkcyjny wrapper z pulą 96 sensorów, czterema challengerami i gęstymi reservoirami klasyfikatora. Porównuje wszystkie cechy, decyzje, wartości liczników, wagi po przyszłym uczeniu i liczbę etykiet przez SHA. Wszystkie porównania zachowują identyczne wyniki. Każda z 60 mierzonych obserwacji po zmianie wykonuje jedno zapytanie census, wcześniej pięć. Mediana stosunku czasu całego observera wynosi 1,08 razy, przy rozrzucie 0,92–1,38 między trzema parami. Nie dodaje sztucznego opóźnienia SQLite. Wyniki pełnego zestawu i trzech porównań są w [raporcie](benchmarks/OBSERVATION_CENSUS_0_14_197_VALIDATION.json).

## Instalacja

Zainstaluj .197 i zrestartuj dodatek. Ponowny trening ani Full Rebuild nie są potrzebne. Po aktywności radaru kolejny eksport powinien pokazać mniej spanów state_with_pool na jedną obserwację. Czas uczenia i serializacji zmieniającego się challengera oraz fizyczne I/O checkpointów pozostają osobnymi kosztami. Wynik lokalnego benchmarku nie zastępuje pomiaru czasu reakcji Raspberry Pi.
