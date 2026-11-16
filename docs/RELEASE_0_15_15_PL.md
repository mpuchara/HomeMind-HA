# Adaptive AI 0.15.15 — telemetria postępu generacji

W 0.15.14 panel pozostawał na 13% mimo 4 107 231 fetched/unique, 8133 batches committed i last advance 1 s ago. Kod emit() ogranicza progress przygotowania do maks. 9% w obrębie workera i bazuje na liczbie zakończonych zapytań/encji, nie postępie wierszy aktualnej encji; p0.js traktuje to jako overall %, dlatego 13% nie oznacza braku przetwarzania. 4.1 mln odczytów w siedem dni jest możliwe dla szybko emitujących radarów.

Zmiana: przez 0 dodatkowych SELECT/COUNT i 0 nowych transakcji, raportujemy aktualną encję, ukończone encje, cursor timestamp, udział przebytego zakresu czasowego bieżącej encji (nie ilość pozostałych wierszy!), wykładniczo wygładzoną prędkość kopiowania. UI rozróżnia overall % i cursor %, pokazuje wiek kursora i ostrzega >120 s. Wartości są telemetryczne, nie obiecujemy oszacowania ETA na podstawie nierównomiernych w czasie zdarzeń. Nie zmieniamy przebiegu treningu i nie należy restartować działającego zadania tylko dla nowego UI.

Uwaga: Gate CI 0.15.14 po 3300 unit testów nie przeszedł w scenariuszu kill_and_restart z konkurencyjnym checkpointem WAL (9 retry SQLITE_BUSY w finalizacji). Nie uznajemy 0.15.15 za produkcyjny dopóki ten gate nie przejdzie i fizyczne Pi nie zakończy pełnej ścieżki.
