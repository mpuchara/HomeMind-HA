# Adaptive AI 0.14.147

Poprawka usuwa zbędną pracę Sensor Tournament, która zajmowała wątek agenta po przygotowaniu decyzji i mogła opóźniać obsługę następnych zdarzeń.

## Zmiany

- Pełna pula obserwowanych sensorów jest zapisywana w jednej atomowej transakcji SQLite zamiast osobnej transakcji dla każdej encji. Zachowane są wszystkie obserwacje dostępności oraz przykłady niezależnych zmian celu.
- Oceny historycznych przykładów są przeliczane tylko po dopisaniu nowego przykładu lub przy braku oceny. Zmiana sensora bez zmiany celu nie uruchamia ponownego liczenia niezmienionego zbioru.
- Nowe metryki `wrapped_inference`, `context_shadow_observation` i `context_observed_pool` pokazują koszt całej obsługi agenta, także czynności po przygotowaniu decyzji. Runtime Debug zawiera osobne ślady tych etapów.

## Weryfikacja i ograniczenia

Test regresyjny obejmuje 96 sensorów: jedna transakcja, brak ponownego przeliczania bez nowych etykiet, zachowanie wszystkich liczników i trwałych przykładów uczących. Pełny zestaw: 1719 testów.

Lokalne porównanie poprzedniej i nowej implementacji na Windows/Python 3.11, 96 sensorów po 96 przykładów, 20 obserwacji: średnio 630,87 ms → 58,65 ms (około 10,8 razy szybciej); p95 724,56 ms → 78,08 ms. To pomiar modułu na komputerze deweloperskim, nie wynik całej aplikacji na urządzeniu Home Assistant.

Log wersji 0.14.146 wykazał 10–15 s zajęcia wątku po wyniku Executor i ostatnie p95 zdarzenie → decyzja 14,65 s. Dotychczasowy log nie rozdzielał wszystkich zewnętrznych rozszerzeń; nie przypisujemy całego opóźnienia jednemu modułowi. Po aktualizacji należy powtórzyć scenariusz z Runtime Debug włączonym podczas wizyty. Niepotwierdzone krótkie włączenia nie są oznaczane jako błędy automatyzacji.

Aktualizacja zachowuje model i nie wymaga Train/Rebuild. Nie zmienia wag uczenia, progów obecności ani zasad sterowania. Ogólna przewaga jakości agenta nad automatyzacją nadal wymaga walidacji.

## Instalacja

Zaktualizuj dodatek do 0.14.147 i uruchom ponownie. Dla instalacji ręcznej użyj archiwum `HomeMind-Adaptive-AI-0.14.147-addon-root.zip`; sumy SHA256 są opublikowane obok plików ZIP.
