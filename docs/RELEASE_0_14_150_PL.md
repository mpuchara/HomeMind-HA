# HomeMind Adaptive AI 0.14.150

Log `adaptive-ai-runtime-debug-20261008-230310.json` z 0.14.149 pokazuje średnio 255 ms dla puli sensorów, 2,24 s dla obserwacji kontekstu i 796 ms zdarzenie → decyzja. Ostatnie p95 zdarzenie → decyzja wynosiło 3,57 s, przy 17 próbkach. Średnie zmalały względem poprzedniego logu, ale wolne przebiegi pozostają. Nie było aktywnego treningu; samo `policy_predict` w śladach zajmowało średnio 1,8 ms. Nie ma nowych próbek snapshotu probation w zwykłej obserwacji, co potwierdza usunięcie tego kosztu w 0.14.149.

## Zmiany

- Zapis shadow nie tworzy pełnego JSON przy każdej aktualizacji kilku liczników. Tworzy szybki, odłączony snapshot całego modelu w RAM. Wątek istniejącego writera przygotowuje kanoniczny JSON dopiero dla partii przeznaczonej do SQLite.
- Snapshot z chwili obserwacji obejmuje także zagnieżdżony model kandydata i przykłady. Późniejsza mutacja cache nie zmienia zakolejkowanej wersji. Kolejne aktualizacje tego samego challengera zastępują snapshot ostatnim pełnym stanem; nie odrzucają skumulowanych głosów ani przykładów.
- Wewnętrzny snapshot używa pickle protocol 5 wyłącznie jako prywatnych bajtów w RAM, z zachowaniem skalarów zgodnych z JSON, np. `numpy.float64`. Bajty pochodzą wyłącznie z wewnętrznego modelu; nie są odczytywane z plików, SQLite ani sieci. Baza, eksport i odtwarzanie modeli nadal używają JSON.
- Partie zapisu w tle i zapisu wymuszonego są wykonywane kolejno, aby starsza partia nie nadpisała nowszego trwałego wyniku. Błąd snapshotu zachowuje poprzednią oczekującą obserwację; błąd kodowania lub SQLite odtwarza partię do ponowienia. Nowszy oczekujący snapshot ma pierwszeństwo przed starszą nieudaną partią.
- Zapis wymuszony po niezależnej zmianie stanu celu pozostaje wykonywany przed powrotem z obserwacji. Zachowany jest także zapis przy zatrzymaniu writera. Zwykłe obserwacje korzystają z istniejącego buforowania; awaryjne przerwanie procesu może utracić jeszcze niezapisaną partię, tak jak poprzednio.
- Nowe metryki i ślady Runtime Debug: `context_shadow_snapshot` (utworzenie snapshotu w obserwacji), `context_shadow_json` (konwersja całej partii do JSON) i `context_candidate_validation` (obowiązkowa weryfikacja checksumy). Konwersja JSON może wystąpić także w wątku obserwacji, gdy wynik wymusza trwały zapis.
- Weryfikacja checksumy pozostaje obowiązkowa również dla ciepłego cache i przy promocji. Reguły uczenia, selekcji, kwalifikacji i sterowania pozostają zgodne z 0.14.149.

## Weryfikacja i ograniczenia

Dziewięć nowych regresji obejmuje odroczenie JSON i odłączenie zagnieżdżonego modelu, odtwarzanie JSON po restarcie, koalescencję kompletnych głosów, skalary NumPy, retry po błędzie SQLite, zachowanie poprzedniego snapshotu przy błędzie przygotowania, pierwszeństwo nowszej obserwacji po błędzie, odzyskanie partii przy błędzie kodowania, kolejność równoległych flush oraz zapis przy zatrzymaniu. Dotychczasowe testy checksumy, prequential scoring i trwałości niezależnego wyniku pozostają wymagane. Pełny zestaw: 1748 uruchomionych testów, w tym klasy ponownie odkrywane przez starszy test kontraktu wydania.

Benchmark deweloperski używa rzeczywistego kontraktu cech, 96 sensorów po 96 historycznych próbek, 256 przykładów klasyfikatora i 20 obserwacji po rozgrzaniu. Średni czas obserwacji wyniósł 87,26 ms → 59,70 ms (około 32% mniej), p95 90,71 ms → 71,30 ms. Czas CPU całego badanego przebiegu wyniósł 1578 ms → 984 ms (około 38% mniej), łącznie z końcowym zapisem oczekującej partii. Koszt tego zapisu wyniósł 19,38 ms → 60,72 ms; konwersja JSON została przeniesiona, a jej częstotliwość zmalała dzięki koalescencji. Po zapisie w obu wariantach pozostało zero oczekujących snapshotów. Jest to pomiar ścieżki obserwacji kontekstu na komputerze deweloperskim; nie określa opóźnienia całej aplikacji na urządzeniu Home Assistant. Pozostałe opóźnienia wymagają pomiaru nowych metryk na urządzeniu. Ta poprawka nie dowodzi przewagi jakości agenta nad automatyzacją.

## Instalacja

Aktualizacja i restart dodatku wystarczą. Rewizja treningu `shared-home-intents-v26` jest zgodna; Rebuild nie jest wymagany. Pakiet ręcznej instalacji: `HomeMind-Adaptive-AI-0.14.150-addon-root.zip`. Sumy SHA256 są publikowane obok ZIP.
