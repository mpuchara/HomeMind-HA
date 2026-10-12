# 0.14.188: koszt fragmentów treningu i replay poza RAM

Eksport `adaptive-ai-runtime-debug-20261012-221212.json` pochodzi z .187 i dotyczy treningu lampy IKEA, a nie wcześniej wskazanego agenta łazienki. Odtwarzanie nagród nadal jest wolne: 0,236 wiersza urządzenia na sekundę. W bieżącym fragmencie inicjalizacja trackerów zajęła 41,054 s, a 110 wywołań fallback do SQLite łącznie 9,907 s. Ocena korelacji sensorów w tym fragmencie zajęła 0 s.

Indeks RAM wykorzystuje około 124 MB z limitu 128 MiB. Jeden z 54 strumieni nie został przyjęty i wymaga odczytów SQLite. Cała historia zawiera około 10,08 mln wierszy. Sam brak przyjęcia tego strumienia jest poprawną ochroną limitu pamięci; problemem jest koszt zapytań fallback. W chwili eksportu limit miejsca miał poziom normalny, około 20,3 GB było wolne, baza zajmowała około 2,9 GB. Workera obsługiwał jeden proces zużywający około 88% jednego rdzenia, bez drugiego aktywnego treningu.

## Zmiany

Każdy fragment workera wcześniej budował ponownie `CausalHomeStatistics` dla całego stałego okna sesji. .188 zachowuje jeden istniejący zestaw checkpointów statystycznych między fragmentami. Przed ponownym użyciem sprawdza SHA-256 rzeczywistych wierszy źródłowych, ich kolejność i czasy odbioru, początkowe próbki oraz checkpoint sprzed okna. Zmiana danych, mapowania, wiarygodności sensorów, czasu połowicznego zaniku lub granic okna powoduje pełną budowę. Zapisy nagród i nowe obserwacje poza tym oknem nie wymagają ponownego trenowania historycznej trajektorii. Nadal obowiązuje istniejący budżet 8 MiB; dwa prywatne kursory zapytań są czyszczone na granicy fragmentu. Cache jest zwalniany razem z zasobami workera.

Zapytanie fallback z warunkiem `ts > lo OR received_ts > lo` mogło przeglądać długi prefiks historii, choć potrzebowało krótkiego przedziału. .188 rozdziela dwa rozłączne przypadki: zdarzenia w nowym przedziale i wcześniejsze zdarzenia odebrane w tym przedziale. Drugi przypadek korzysta z istniejącego indeksu `idx_entity_history_received_ts`. Zachowane są obsługa NULL, późnych i przyszłych pomiarów, kolejność oraz finalny limit K próbek na encję. Uproszczone bazy bez tego indeksu zachowują działający wariant zapytania.

Nie dodajemy indeksu na karcie ani nie zwiększamy limitów RAM, retencji, sensorów czy wymiaru modelu. Analiza korelacji jasności z .186 i cache odczytanych próbek z .187 pozostają aktywne.

## Weryfikacja

[Pomiar A/B](benchmarks/CHUNK_REPLAY_0_14_188.json) wykorzystuje identyczne syntetyczne dane w Linuksie/WSL. Dla sześciu fragmentów z 6400 wierszami źródeł pełna budowa statystyk wystąpiła raz zamiast sześciu: łączny czas przygotowania wyniósł 0,363 s zamiast 1,412 s. Dla 128 krótkich zapytań fallback nad prefiksem 60 000 wierszy czas wyniósł 0,0029 s zamiast 1,259 s, a liczba kroków VM SQLite 13 900 zamiast 138 194 500. Wyniki zapytań i statystyk były identyczne.

Rzeczywisty izolowany worker z trzema fragmentami zachował dokładnie zgodne wagi i przykłady treningowe; w drugim i trzecim fragmencie wykorzystał wcześniejsze statystyki. Zgodność wag pomija wyłącznie istniejący znacznik inicjalizacji zegara `last_decay_ts`. To lokalne pomiary komponentów i małego workera, a nie gwarancja przyspieszenia całego treningu na Raspberry Pi.

Pełny zestaw 2252/2252 testów przeszedł w Linuksie/WSL, bez pominięć. Dziesięć nowych regresji obejmuje korekty i usunięcie źródeł, zmianę czasu przez surowy SQL, początkowe próbki, opóźnione pakiety, zapis z drugiego połączenia, zmianę mapowania i wiarygodności, checkpoint, brak persistence, zwolnienie cache oraz zgodność ograniczonych i nieograniczonych zapytań replay. [Weryfikacja pełnego wydania](benchmarks/CHUNK_REPLAY_0_14_188_VALIDATION.json).

## Zastosowanie

Zainstaluj .188, zrestartuj dodatek i użyj Resume dla przerwanego treningu, jeśli jest dostępne. Ta poprawka nie wymaga Rebuild wyuczonego modelu. W następnym eksporcie sprawdź `causal_home_statistics_reused`, `causal_home_statistics_validation_seconds`, `causal_home_statistics_build_seconds`, `tracker_init_seconds` i `sqlite_fallback_seconds`. Pierwszy fragment nadal musi zbudować statystyki; późniejsze mogą je wykorzystać po sprawdzeniu danych źródłowych.
