# HomeMind Adaptive AI 0.14.156

Log `adaptive-ai-runtime-debug-20261009-141740.json` z 0.14.155 pokazuje aktywny keeper i worker checkpointu, bez błędów SQLite/kolejek/kandydatów w eksportowanych danych. 243 checkpointy zakończyły się bez BUSY/error; ostatni skopiował 2568/2568 ramek, remaining_frames=0. Maksymalny czas checkpointu wyniósł 2,916 s. W zachowanym zwykłym oknie maksymalny commit wynosi 20,097 ms, a przeciętny commit provenance 3,628 ms. W tym oknie nie ma poprzednich wielosekundowych commit trzymających Store. Nie dowodzi to braku opóźnień poza zachowanym oknem. WAL ma około 135,4 MiB fizycznej alokacji; PASSIVE nie obcina pliku, więc rozmiar nie oznacza zaległości i może być ponownie wykorzystany.

Pozostał koszt obliczeniowy. Kontrola integralności kandydatów średnio 49,609 ms na wywołanie; 4 kontrole na obserwację. W zachowanym trace wszystkie 56 odczytów kandydatów trafiły do cache runtime, lecz nadal ponownie kodowały pełny model do JSON podczas wymaganej weryfikacji SHA256. Ostatnie p95 obserwacji Shadow to 707,6 ms, owiniętego przebiegu agenta 813,3 ms, zdarzenie → decyzja 123,8 ms. CPU recent 39,78%, od startu 51,52%. Nie trwa trening/heavy job; kolejki są opróżnione poza bieżącą partią 9 zdarzeń. Okna/obciążenia różnią się od wcześniejszych logów, więc nie wyliczamy procentowej poprawy urządzenia.

## Zmiana

Optymalizujemy samo kodowanie modeli, zachowując istniejącą definicję SHA256 canonical JSON. Wynik checksum/weryfikacji nie jest zapamiętywany. Każde wywołanie tworzy świeży prywatny binarny snapshot całej kontrolowanej treści i SHA256 jego bajtów jako klucz wyszukiwania. Dopiero dokładne porównanie wszystkich bajtów snapshotu z wcześniej zapisanym wpisem pozwala użyć zapisanych bajtów canonical JSON. Następnie ponownie obliczane jest SHA256 tych bajtów JSON i porównywane z oczekiwanym checksum, jak wcześniej. Wymuszona kolizja klucza nie pozwala na trafienie z inną treścią, bo porównanie bajtów jest obowiązkowe.

Snapshot obejmuje wagi, macierze, próbki klasyfikatora, schemat i metadane, także signed zero i rozróżnienie bool/int/float. Sam object identity, model_revision lub oczekiwany checksum nie daje trafienia. Znaczenie dotychczasowego wykluczenia top-level model_checksum i kluczy _bookkeeping pozostaje; pola z podkreśleniem wewnątrz modelu są nadal kontrolowane.

Cache działa tylko dla dokładnych typów wbudowanych JSON: dict/list/tuple i str/int/float/bool/None. Niestandardowe obiekty/podklasy korzystają z dotychczasowego encodera przy każdym wywołaniu, aby mutowalne `default=str` nie dało fałszywego trafienia. Nie czytamy pickle z plików, SQLite ani żądań. Dekodowany jest wyłącznie snapshot utworzony wewnętrznie w tej samej funkcji z modelu RAM. Przy miss JSON powstaje z tego odłączonego snapshotu, aby równoległa zmiana źródła nie zatruła cache. Persisted modele i restarty nadal korzystają z JSON, a checksum ma dokładnie dotychczasową wartość.

Cache jest LRU, z limitem 16 wpisów i 16 MiB łącznych bajtów świadków oraz kodowania. Narzut struktur i chwilowych obiektów jest dodatkowy; nie jest to limit całego RSS. Payloady o snapshotach poniżej 16 KiB nie zajmują cache. Zbyt duże wpisy nie są przechowywane. Kodowanie i snapshot nie trzymają blokady cache; tylko krótki lookup/publikacja/statystyki. `model_encoding_cache` w Runtime Debug eksportuje RAM hits/misses/bypasses, entries/bytes i limity.

## Weryfikacja i pomiar

19 regresji obejmuje pełne SHA przy warm hit, zgodność starego digest, mutacje wag/próbek/schematu bez zmiany revision/checksum, nested bookkeeping, odwrócenie mutacji, signed zero i typy, losowe macierze i JSON disk roundtrip, kolejność/referencje, NaN/Inf/cykle, custom obiekty/podklasy, odłączony snapshot i mutację podczas kodowania, wymuszoną kolizję klucza, limity bajtów/wpisów, bypass, równoległe kontrole, brak blokady przy wolnym encoderze i diagnostykę RAM. Dotychczasowe regresje integralności i stable paired epochs również przechodzą. Pełny zestaw: 1848 testów; CI kontroluje obsługiwane Pythony, benchmarki i obraz dodatku.

`python tools/benchmark_model_encoding.py` odtwarza syntetyczny test standardowej biblioteki: 4 modele po 73 728 wartości liczbowych, macierze oraz rzadkie wiersze próbek. Trzy serie po 32 kontrole obu ścieżek; SHA każdego wyniku jest porównywany ze starym encoderem. Na komputerze deweloperskim Windows mediana median: około 27,84 → 10,07 ms (około 2,8 razy szybciej dla warm hit). Zimny pierwszy przebieg około 43,25 ms, więc cache zwiększa ten koszt. 4 wpisy zajęły około 8,76 MiB bajtów payloadów. To pomiar kodowania/weryfikacji, a nie prognoza opóźnień na HA. Dane benchmarku znajdują się w BUILD_INFO.

Pozostają koszty obliczania cech/scoringu/uczenia Shadow, jego JSON persistence i I/O. Częste zmiany treści, eviction lub duże payloady ograniczają korzyść; cache zużywa dodatkowy ograniczony RAM. Nowy log pozwoli sprawdzić rzeczywiste hit/miss/bypass i czas context_candidate_validation. Format modeli, uczenie, kwalifikacja sterowania i wymagane kontrole integralności pozostają zgodne.

## Instalacja

`HomeMind-Adaptive-AI-0.14.156-addon-root.zip` oraz SHA256 są w wydaniu GitHub. Aktualizacja i restart, bez Rebuild. Checkpoint/fallback i polityka trwałości NORMAL z 0.14.155 pozostają.
