# HomeMind 0.14.203: mniej konkurencji z obsługą zdarzeń sensorów

## Diagnoza nowych eksportów

W 0.14.202 znaczną część opóźnienia stanowi oczekiwanie przed rozpoczęciem targetu. W ostatnim eksporcie średnia `event_to_target_start` wynosi około 293 ms, a `event_to_decision` około 382 ms. To czas od odebrania zdarzenia w aplikacji, a nie od fizycznego pomiaru czujnika. Ocena Tournament po decyzji nadal zajmuje pracownika tego targetu. Eksporty pokazują Shadow i odrzucenia, bez próbek rzeczywistych wywołań HA.

W krótkim śladzie z 14:32 wątki obsługujące HTTP wykonały 11 pełnych odczytów wyników epizodów, łącznie przez około 10,9 s czasu ściennego. Najdłuższy pojedynczy odczyt trwał około 2,42 s. Te odczyty i następujące po nich dekodowanie JSON konkurują z inference o CPU, GIL i dostęp do SQLite. Nie jest to pomiar dodatkowego opóźnienia każdego pojedynczego zdarzenia.

## Poprawka

Porównanie polityk pobiera w jednym zapytaniu wspólne epizody oraz pola rzeczywiście używane do agregacji. Pełne JSON audytu i obserwowalności nie są kopiowane do Pythona. Ich poprawność nadal sprawdza SQLite; uszkodzony audyt wspólnego epizodu blokuje porównanie. SQLite bez rozszerzenia JSON korzysta ze starego pełnego dekodera. Pełny odczyt audytowy `_policy_rows` pozostaje dostępny.

Wyniki porównań są przechowywane jako niemutowalny JSON w RAM. Domyślny limit `runtime_episode_comparison_cache_mb` wynosi 64 MiB, zakres 0–256 MiB, maksymalnie 32 wyniki i 16 MiB na jeden wynik. Wartość 0 wyłącza cache. Limit nie rezerwuje pamięci z góry. Każdy odczyt sprawdza świeżą rewizję SQLite. Triggery zmieniają rewizję agenta przy INSERT, UPDATE i DELETE w tabelach epizodów i wyników, w tej samej transakcji co dane. Obejmuje to bezpośrednie SQL, retencję, przeniesienie epizodu i zmianę klucza polityki. Rollback cofa także rewizję. Jawna otwarta transakcja nie korzysta z cache i go nie wypełnia. Równoczesny zapis podczas obliczenia uniemożliwia zapisanie wyniku pod mieszaną rewizją. Każdy odbiorca dostaje własny obiekt wyniku.

Runtime Debug pokazuje czas `episode_comparison` oraz `engine.deferred_persistence.episode_comparisons`: rozmiar, trafienia, pudła, wyparcia i pominięcia budżetu.

## Weryfikacja i instalacja

**2422 testy przeszły**, w tym 16 nowych regresji. Sprawdzono również start docelowego entrypointu do `ready`, kompilację Pythona, składnię JavaScript i zgodność źródeł z testowanym checkoutem.

W trzech parach testów na Linuksie: powtórne porównanie niezmienionych epizodów przyspieszyło około **201×**, pierwsze obliczenie około **6×**, a p95 zdarzenie → Executor Shadow pod obciążeniem panelu spadł z około **40 ms do 26 ms**. Wszystkie zdarzenia dotarły do executora; wyniki porównań epizodów miały identyczne SHA256.

Wyniki testów i porównania z 0.14.202 są w `docs/benchmarks/EPISODE_REACTION_0_14_203_VALIDATION.json`. Benchmark wstrzykuje zdarzenia sensora do rzeczywistego Engine, uruchamia scheduler, politykę i Executor w Shadow równolegle z porównaniami epizodów z częstotliwością do 20 odświeżeń na sekundę. Sprawdza dostarczenie wszystkich zdarzeń, identyczne wyniki porównań i brak usług HA. Nie zawiera pełnego pipeline Tournament, sieci HA ani potwierdzenia urządzenia; wynik z komputera testowego nie jest prognozą opóźnienia na Raspberry Pi.

Paczka zawiera poprzednie poprawki do 0.14.202. Nie wymaga Full Rebuild. Migracja dodaje tabelę rewizji i triggery, zachowując dotychczasowe dane i modele. Tryb agentów, kolejność uczenia Tournament i zabezpieczenia Control pozostają zachowane.
