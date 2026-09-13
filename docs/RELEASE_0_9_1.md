# Poprawka 0.9.1

Wersja 0.9.0 mogła przerwać bootstrap komunikatem "Live catch-up buffer exceeded" po zgromadzeniu 8192 zdarzeń. Przy wielu radarach aktualizujących energię i odległość limit był osiągalny podczas normalnego działania. Nie świadczył o braku sensorów.

## Zmiany

HomeBootstrap zamiast listy zdarzeń używa dodatkowego, ograniczonego modelu statystyk live. Na początku kopiuje bieżący stan i niedokończone przejście, ale nie starą wiedzę. W czasie importu agreguje nowe zdarzenia. Przy instalacji dodaje je do historii, wyrównując wagi decay. Liczba zapisanych surowych zdarzeń nie rośnie z czasem bootstrapu.

Model i checkpointy są zatwierdzane w jednej transakcji SQLite. Błąd zapisu pozostawia obie poprzednie wersje. Bieżąca obecność i kolejka aktualnego przejścia pozostają live.

Home Sources oddziela znaczenie pomiaru od nazwy urządzenia. Odległość, ustawienia, firmware i łączność nie są obecnością. Jeśli radar ma binarną obecność, jego surowa energia nie podtrzymuje osobno zajętości tego samego obszaru. Energia i inne poprawne wejścia nadal mogą służyć polityce agenta. Czujniki PIR/occupancy działają niezależnie od marki i integracji, także na urządzeniu z kanałami sterującymi.

Panel pokazuje listę encji, obszar i powód wykorzystania/pominięcia. "Sources without area" dotyczy wybranych encji, nie liczby fizycznych urządzeń. Brak przyjścia nie jest już liczony jako przejście między pomieszczeniami.

## Aktualizacja

Podmień pełny katalog adaptive_ai zgodnie z [instrukcją](INSTALLATION_PL.md) i wykonaj Rebuild. Nie usuwaj danych. Modele agentów zgodne z 0.9 zostają zachowane; ta poprawka nie wymusza ich ponownego Train.

Po aktualizacji uruchom raz Bootstrap Home Model, aby zastąpić statystyki zebrane z błędnie interpretowanych kanałów. Sprawdź nową listę źródeł. Faktyczny brak obszaru nadal wymaga przypisania w HA lub jawnego mapowania; aplikacja nie zgaduje pokoju z nazwy.

## Weryfikacja

97 testów przeszło. Nowe regresje obejmują:
- bootstrap z 12000 zdarzeń live podczas replay, poprawne READY i zachowaną końcową obecność;
- 30000 aktualizacji przy stałym rozmiarze statystyk;
- przejście rozpoczęte przed bootstrapem i zakończone po jego rozpoczęciu;
- łączenie statystyk o różnym wieku;
- błąd zapisu bez utraty modelu/checkpointów;
- Sonoff/PIR, własne binary sensors, radar z energią i odległością, niezależne strefy i diagnostykę źródeł.

[Wynik testów](TEST_RESULTS_0_9_1.txt), [symulator](SIMULATOR_0_9_1.json), [lokalny mikrobenchmark](BENCHMARK_LOCAL_0_9_1.json).

Konfigurację HA obejrzano tylko do odczytu. Poprawki nie zainstalowano zdalnie; nie wykonano pomiaru wydajności ani bootstrapu nowej wersji na fizycznym HA/Pi 4. Sterowanie urządzeniami nie było częścią odczytu konfiguracji.
