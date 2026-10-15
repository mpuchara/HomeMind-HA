# HomeMind 0.14.202: krótsza ścieżka od sensora do polecenia

Paczka zawiera wcześniejsze poprawki 0.14.198–0.14.201. Aktualizacja nie wymaga Full Rebuild ani zmiany trybu agentów.

## Co zmienia ta wersja

- Instrukcje Teach i model preferencji korzystają z cech już obliczonych dla bieżącej decyzji. Wcześniej oba dopasowania mogły osobno budować pełny wektor. Teraz używają dokładnie tego samego stanu, czasu obserwacji i cech co propozycja polityki. Dopasowanie nie wykonuje dodatkowego przeliczenia. Zakres żyje tylko podczas składania tej decyzji, w jednym wątku; inny czas, obiekt stanu, model, schema lub rewizja uruchamia zwykłe obliczenie.
- Czterej kandydaci Tournament współdzielą wynik odczytu czasu ostatniej komunikacji sensora w jednej obserwacji. Kolejna obserwacja ponownie czyta historię. Zachowane są granice czasu zdarzenia i otrzymania danych.
- Zdarzenia, które czekały na poprzedni przebieg tego samego targetu, pomijają dodatkowy debounce po zwolnieniu pracownika. Nowe serie zdarzeń nadal podlegają skonfigurowanemu grupowaniu. Nie zmniejszono liczby ocen Shadow ani danych uczących.

## Co pokazują nowe pomiary

Diagnostyka zapisuje osobno: `event_to_target_start`, `event_to_decision`, `event_to_service_dispatch`, `ha_service_request`, `event_to_service_return`, `service_to_state_ack` oraz `event_to_state_ack`. Licznik poleceń HA obejmuje rzeczywiste wywołania Control. Shadow nie generuje próbek usługi ani ACK. Odpowiedź usługi nie oznacza potwierdzenia fizycznego urządzenia. ACK oznacza dopasowany stan targetu przetworzony przez agenta i obejmuje opóźnienie jego zaplanowania.

Panel pokazuje także p95 od zdarzenia do wywołania HA z ostatnich 60 sekund. Brak próbki daje `waiting`. Karta agenta pokazuje pomiary dotyczące tego samego polecenia; nowa próba kasuje poprzedni ACK, a API nie sumuje czasów z różnych akcji.

## Diagnoza eksportów 11:32

Wszystkie trzy eksporty pochodzą z 0.14.201. Średni czas zdarzenie → decyzja wynosi około 187 ms, a ostatni p95 około 624 ms. Sama decyzja → zakończenie executora w Shadow trwa średnio poniżej 1 ms. Pełny pipeline zajmuje średnio około 311 ms; znaczna część czasu po decyzji przypada na Tournament i może opóźniać kolejne zdarzenie tego samego targetu. Sesja obejmuje Shadow i odrzucone intencje; nie potwierdza czasu wykonania fizycznych akcji.

Ta wersja skraca pracę na ścieżce decyzji i część pracy po niej. Tournament nadal zachowuje kolejność predict → score → learn oraz serializację targetu. Samo zwiększenie limitu RAM nie usuwa tych kosztów CPU i oczekiwania.

## Walidacja

Porównanie na Linuksie z 0.14.201: mediana przyspieszenia fragmentu z dwoma dopasowaniami wyniosła **1,88×**, a pełnej obserwacji czterech kandydatów **1,14×**. W każdej z trzech par zachowano identyczne SHA wszystkich cech, metadanych, predykcji i sygnatur.

**2406 testów przeszło** na Linuksie z Pythonem 3.12.3, w tym 10 nowych regresji. Sprawdzono start docelowego entrypointu do stanu ready, kompilację Pythona, składnię JavaScript i zgodność źródeł z testowanym checkoutem.

Wyniki pełnych testów i porównań rzeczywistych polityk z 0.14.201 zapisano w `docs/benchmarks/RUNTIME_REACTION_0_14_202_VALIDATION.json`. Benchmark dopasowania instrukcji i preferencji porównuje wszystkie cechy, metadane, predykcje i sygnatury przy identycznym czasie obserwacji. Nie jest pomiarem sprzętowej reakcji HA. Gdy agent nie ma faktów wymagających dopasowania, optymalizacja sygnatur nie przyspiesza tego etapu.

Po aktualizacji można zebrać nowy Runtime Debug podczas faktycznej pracy Control. Pozwoli on porównać osobno oczekiwanie na pracownika, gotowość decyzji, wywołanie HA oraz przetworzenie potwierdzonego stanu. Włączenie Control pozostaje jawną decyzją użytkownika i wymaga dotychczasowej kwalifikacji.
