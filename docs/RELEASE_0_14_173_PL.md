# HomeMind Adaptive AI 0.14.173

Kandydaci kontekstu korzystają teraz ze wspólnego, krótkotrwałego odczytu historii sensorów i prognozy domu w ramach jednej obserwacji agenta. Każda następna obserwacja tworzy nowy widok, także przy identycznym timestampie. Dane nie są przechowywane między zdarzeniami ani współdzielone między urządzeniami.

## Wynik logu 0.14.172

Eksport z 11.10, 02:47 pokazuje poprawne oznaczenia kolejki celu i timestampów REST/WS. Ostatnie p95 event → decision wynosi 686 ms, a wrapped inference 633 ms. Poprzedni eksport 0.14.171 pokazywał odpowiednio 107 s i 1167 ms; duża część wcześniejszej metryki event → decision pochodziła ze starych timestampów i ponowień, więc nie jest to miara rzeczywistego przyspieszenia włączania światła.

Nie ma błędów zapisów ani checkpointów, ale CPU wynosi około 94%, a maksymalny checkpoint nadal przekracza 6,9 s. Samo wnioskowanie ma średnio 47 ms, podczas gdy cała obserwacja Shadow średnio 308 ms. Każdy przebieg sprawdza cztery modele kandydatów. Wydanie 0.14.173 ogranicza powtarzaną pracę przy ich cechach; koszt pełnej walidacji modeli i I/O nadal należy mierzyć.

## Zmiana

Wykorzystujemy istniejący `SharedInferenceTemporal` dla kandydatów Sensor Tournament. Prognoza trajektorii domu dla tego samego celu i czasu jest obliczana raz, a identyczne odczyty czasowe sensorów są współdzielone. Dodatkowo wynik skanowania ostatniej poprawnej zmiany wartości jest zapamiętywany tylko w tym widoku. Zachowany algorytm uwzględnia brak danych, wartości niepoprawne, próbki przyszłe, nieuporządkowaną historię oraz timestamp zapasowy. Zapytanie dla innego czasu pomija tę pamięć.

Kandydaci zachowują osobne schematy, wektory, predykcje i stan uczenia. Pełny SHA całego aktualnego modelu pozostaje sprawdzany przy każdej ocenie, także przy ciepłej pamięci modeli. Kolejność predykcja → wspólna przyszła etykieta → ocena → uczenie, bramy promocji oraz zasady Executor pozostają zachowane. Brak modelu domu nadal oznacza brak prognozy w metadanych. Błąd obserwacji usuwa jej krótkotrwały widok.

## Walidacja

13 nowych regresji, 2073 testy łącznie. Test porównawczy wykorzystuje produkcyjny observer kandydatów, pełne sprawdzenie modeli, rzeczywiste cechy i predykcje, ContextEngine z 45 źródłami obecności w dziewięciu obszarach oraz SQLite. Porównuje każdy wektor, etykiety i metadane z zachowaniem 0.14.172. Liczba prognoz spada z czterech do jednej na obserwację; liczba pełnych sprawdzeń modeli pozostaje cztery.

[Raport pomiarów](benchmarks/SHADOW_CONTEXT_0_14_173.json). Wyniki są syntetyczne i nie stanowią prognozy przyspieszenia całej instalacji. Pełny czas obserwacji obejmuje także inne koszty i I/O, które mogą dominować nad oszczędnością cech.

## Instalacja

Aktualizacja do 0.14.173 i restart dodatku, bez Rebuild ani ponownego treningu. Kolejny log po 10–15 minutach pracy tych samych agentów pozwoli porównać CPU, context_candidate_features, context_shadow_observation, event → decision i checkpointy SQLite.
