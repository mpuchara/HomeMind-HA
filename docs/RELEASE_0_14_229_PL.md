# HomeMind 0.14.229: skalowanie niezależnych agentów G0

## Zgłoszenie i granice diagnozy

Na świeżej instalacji .228 użytkownik mierzy event -> decision p95: około 64 ms dla jednego agenta Shadow, 250 ms dla trzech i 400-500 ms dla czterech. Wszyscy są G0 na osobnych urządzeniach. Sensor Tournament zgłasza SQLITE_BUSY („database is locked”); uruchomienie treningu piątego agenta nie udaje się. Nie dostaliśmy nowego eksportu etapów, więc nie rozstrzygamy jednej przyczyny całego czasu 500 ms. To inne zgłoszenie niż poprzednie SQLITE_CORRUPT. Wzrost p95 nie dowodzi sam w sobie wykładniczej złożoności: liniowy strumień pracy może nieliniowo wydłużać oczekiwanie po nasyceniu wspólnego CPU/writera.

## Poprawki

- Ochrona tych samych obserwacji sensora przed retencją jest liczona wspólnie dla okien różnych agentów. Zamiast przetwarzać wspólny sensor ponownie dla każdego zestawu sensorów agenta, stosujemy dokładne maksimum okresu ochrony dla sensora. Okna agentów, owner links, granice włączne, retry i rollback pozostają.
- Kolejka Tournament rozlicza wspólne, niezmienne rekordy historii/stanów tylko raz dla prac oczekujących i wykonywanych. Małe grupy rekordów są internowane słabymi referencjami; nie utrzymują danych po ostatnim właścicielu. Prywatny model, runtime, kontenery i metadata budżetu pozostają naliczane każdemu agentowi. Rozmiary cache, headroom host/cgroup, limit kolejki, FIFO target edges i jawne luki nadal obowiązują.
- Wywołanie policy na rzeczywistym workerze Root nie synchronizuje opcjonalnego membership Tournament przed decyzją. Robi to observer na dokładnie przechwyconym modelu i kontekście Root. Nieudana synchronizacja nie zapisuje fingerprintu sukcesu. Jawne wywołania i granice promocji pozostają synchroniczne.
- Natywne SQLITE_BUSY observera ma ograniczony cooldown. W tym czasie nowe obserwacje nie uruchamiają kolejnych kosztownych snapshotów; przerwana ciągłość jest jawną luką. Inne błędy nadal są raportowane. To nie ukrywa awarii jako poprawnie zebranych dowodów.
- Każdy kolejny fragment zapisu ContextRowBuffer ponownie sprawdza priorytet admission treningu przed przejęciem Store.lock. Pozostałe pełne snapshoty są zachowane do retry. Nie zmieniamy walidacji modelu, kwalifikacji Control ani trwałości komend.

## Pomiary hosta

Benchmark obejmuje rzeczywisty Engine scheduler, pięć osobnych urządzeń Shadow oraz stos Sensor Tournament. Używa syntetycznych obserwacji i deterministycznej confidence Root, bez HA network. Porównania są wykonywane w osobnych procesach dla 1/3/4/5 agentów, z trzema powtórzeniami i zmienną kolejnością przed/po. SHA decyzji są zgodne. Rozrzut p95 jest istotny i nie potwierdza jednolitego przyspieszenia całej ścieżki; nie deklarujemy czasu reakcji na Pi.

Drugi pomiar używa rzeczywistego SQLite i 1600 obserwacji na sensor. Przy ośmiu wspólnych sensorach i osobnym target dla każdego agenta liczba aktualizacji ochrony zmienia się następująco:

| Agenci | .228 | .229 |
|---:|---:|---:|
| 1 | 14400 | 14400 |
| 3 | 43200 | 17600 |
| 4 | 57600 | 19200 |
| 5 | 72000 | 20800 |

Pełny SHA końcowych okresów ochrony i owner links jest identyczny. To ogranicza mnożenie wspólnych zapisów. Nie jest to dowód, że cały runtime skaluje się podliniowo albo że ukończono trening piątego agenta na urządzeniu.

## Walidacja i instalacja

Końcowa walidacja w toku. 66 testów celowanych OK. Nowe regresje sprawdzają pięć urządzeń w budżecie współdzielonych danych, aktywne referencje podczas odrzucenia oczekującej pracy, prywatność zmienionych atrybutów, zerowy budżet, Root policy przy zajętym writerze, rzeczywiste SQLITE_BUSY, retry synchronizacji oraz oddanie zapisu admission treningu między fragmentami. Sprawdzamy także dokładne dane ochrony na nowej i starszej ścieżce SQL oraz rzeczywisty scheduler pięciu agentów bez wywołań HA service.

Podmienić źródła tego samego dodatku, zachowując konfigurację i dane; przebudować obraz, uruchomić, Ctrl+F5. Rebuild modeli nie jest wymagany. Na Pi trzeba ponownie porównać 1/3/4/5 aktywnych G0, te same sensory i p95 ostatnich 60 s po rozgrzaniu modeli; następnie uruchomić trening kolejnego agenta i pobrać świeży eksport. Weryfikacja Pi, treningu piątego agenta i fizycznego sterowania pozostaje otwarta. Nie zwiększamy limitu dysku i nie wyłączamy ochrony WAL.
