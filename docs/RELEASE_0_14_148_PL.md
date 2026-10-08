# HomeMind Adaptive AI 0.14.148

Log wersji 0.14.147 wskazał, że wątek nadal długo zajmuje obserwacja kontekstu po przygotowaniu decyzji. Jej średni czas wynosił 4,77 s, ostatnie p95 6,21 s; sam observed pool średnio 0,93 s. Ostatnie p95 zdarzenie → decyzja wynosiło 3,75 s, a rdzeń inference 83 ms. Trening nie był aktywny. Koszt obserwacji może opóźniać obsługę następnych zdarzeń; czas tego etapu nie jest bezpośrednio czasem predykcji.

## Zmiany

- Jeden przebieg obserwacji używa połączenia SQLite przypisanego do wątku. Każdy blok zapisu nadal osobno zatwierdza lub wycofuje dane. Tryb Shadow i kwalifikacja są trwałe przed ewentualnym przekazaniem sterowania. Zagnieżdżony odczyt podczas aktywnego zapisu korzysta z niezależnego połączenia.
- Głosy jakości aktywnych i konkurencyjnych sensorów zapisują się w jednej transakcji przed oceną promocji. Wszystkie głosy pozostają zachowane.
- Tick bez nowej historii sygnału i etykiety celu aktualizuje tylko liczniki. Nie serializuje ponownie niezmienionych przykładów ani ocen. Nowe dane nadal zapisują pełny stan; usunięty w bazie wiersz z cache jest odtwarzany atomowo.
- Wewnętrzny odczyt liczby obserwowanych sensorów wykonuje ograniczony COUNT, zamiast wielokrotnie obliczać jakość wszystkich encji. Szczegółowe podsumowania w interfejsie nadal są dostępne.

## Weryfikacja

Sześć nowych testów obejmuje widoczność trwałego trybu dla niezależnego wątku przed przekazaniem sterowania, izolację zagnieżdżonego odczytu, rollback, zachowanie historii i wszystkich głosów, odtworzenie usuniętego wiersza oraz odczyt licznika bez pełnego skanowania jakości. Pełny zestaw: 1725 testów.

Odtworzony łańcuch rozszerzeń kontekstu, Windows/Python 3.11, 96 sensorów po 96 przykładów, 20 obserwacji po rozgrzaniu: 44 → 1 fizycznych połączeń SQLite i 9 → 2 synchronicznych commitów na przebieg. Czasy zależą od cache i obciążenia dysku; jest to pomiar deweloperski, nie gwarancja opóźnienia całej aplikacji na urządzeniu Home Assistant. Po aktualizacji należy porównać `context_shadow_observation`, `wrapped_inference` oraz `event_to_decision` na tym samym scenariuszu.

Aktualizacja zachowuje model i nie wymaga Train/Rebuild. Wagi uczenia i reguły sterowania pozostają zgodne z 0.14.147. Ogólna przewaga jakości agenta nad automatyzacją nadal wymaga walidacji.

## Instalacja

Zaktualizuj dodatek do 0.14.148 i uruchom ponownie. Archiwum ręcznej instalacji: `HomeMind-Adaptive-AI-0.14.148-addon-root.zip`; sumy SHA256 są publikowane obok ZIP.
