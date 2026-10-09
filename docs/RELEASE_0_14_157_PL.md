# HomeMind Adaptive AI 0.14.157

Log `adaptive-ai-runtime-debug-20261009-150746.json` z 0.14.156 potwierdza działanie cache: 340 hits, 6 misses, 6 wpisów i 1 622 483 bajty payloadów. Średnia kontrola kandydata trwa 22,951 ms, cała obserwacja Shadow 327,197 ms. Poprzedni log miał odpowiednio 49,609 i 519,327 ms, ale okna i obciążenia nie są identyczne: to nie jest kontrolowany pomiar procentowej poprawy. CPU recent 36,35%, RSS 101,91 MiB. Ostatnie p95 event → decision 156,4 ms, wrapped inference 619,7 ms. Debug jest wyłączony podczas eksportu; zwykły pierścień zachował część zdarzeń, bez critical spans, więc nie opisuje całej sesji.

W eksportowanych danych brak błędów SQLite, kolejek i workera. 64 checkpointy bez BUSY/error; ostatni skopiował 2982/2982 ramek, remaining=0. Fizyczny WAL około 42 MiB jest alokacją do ponownego wykorzystania, a nie zaległością. Bieżące niewielkie partie oczekują na zwykły flush; brak dowodu na narastającą kolejkę. Maksymalny commit w zachowanym oknie to około 14,5 ms, mimo checkpointów do około 2,6 s w osobnym workerze.

## Co zmieniamy

Kodowanie partii modeli Shadow do JSON nadal zajmuje średnio 212,268 ms (ostatnie p95 252,439 ms). Writer korzysta teraz z tego samego kodowania niezmienionego ciała candidate_policy, które przygotowano do kontroli SHA. Nie tworzymy drugiego cache polityk. Oba zastosowania dzielą istniejący limit 16 MiB bajtów świadków i JSON oraz 16 wpisów. Narzut struktur i chwilowych obiektów jest dodatkowy, więc limit nie opisuje całego RSS. Różnice w aliasach lub kolejności słowników mogą powodować bezpieczne dodatkowe miss i wpisy w ramach limitu.

Każde użycie ponownie tworzy prywatny snapshot całej czystej treści i wymaga dokładnego porównania jego bajtów. Wagi, próbki, schemat i metadane nie są identyfikowane wyłącznie przez revision/checksum. Bieżący oczekiwany checksum, top-level bookkeeping i wszystkie pola zewnętrznego modelu (liczniki, próbki, watermark itd.) są kodowane na nowo z niezmiennego snapshotu partii. Nie cache'ujemy wyniku weryfikacji: każde sprawdzenie integralności nadal oblicza pełne SHA256 canonical JSON.

Persisted JSON zachowuje całą treść i zgodność restartu; kolejność pól wewnątrz candidate_policy może być inna, co nie zmienia danych ani canonical SHA. Legacy stringi przechodzą bez zmian. NaN/Inf, custom obiekty i cykle zachowują dotychczasowy wynik lub błąd serializacji; encoder zapisu nie dziedziczy checksumowego default=str. Nie zmieniamy uczenia, kwalifikacji sterowania ani modelu v26. Krótkie prawidłowe wizyty nadal pozostają prawidłowymi przykładami.

Pozostają prywatne niezmienne snapshoty RAM, serializowany drain, latest-wins przy ponowieniu nieudanej partii, commit i bariery shutdown. Pickle służy tylko istniejącym snapshotom utworzonym wewnętrznie; pliki, SQLite i restarty nadal używają JSON. RAM `shadow_json_encoding_cache` pokazuje calls/hits/encodes/fallbacks bez SQL. Hits oznaczają ponowne użycie ciała modelu, nie udany commit; wspólne `model_encoding_cache` liczy teraz zarówno kontrole, jak i serializację zapisu.

## Weryfikacja i ograniczenia

19 regresji sprawdza ponowne użycie istniejącego wpisu, świeże liczniki/bookkeeping/checksum, mutacje wag i odrzucenie niewłaściwego SHA, losowe macierze, nested pola, puste ciała, Unicode/surrogate/signed zero, legacy i custom/nonfinite/cycle fallback, brak zatrucia checksumowego cache, wspólny limit bajtów/wpisów, niezmienny snapshot zapisu, odczyt po restarcie, błąd SQL i ponowienie partii oraz eksport diagnostyki RAM. Pełny zestaw: 1867 testów, z dotychczasowymi kontrolami integralności/paired epochs. CI dodatkowo sprawdza obsługiwane wersje Python, benchmarki i obraz dodatku.

`python tools/benchmark_shadow_json.py` mierzy trzy serie po osiem partii czterech modeli, po 73 728 wartości liczbowych. Obejmuje odczyt własnego snapshotu i kodowanie, zmieniając liczniki/watermark; sprawdza pełną zgodność danych i SHA ze starą ścieżką. Syntetyczny pomiar Windows: mediana median około 121 → 51 ms na partię (około 2,4 razy szybciej). W tym normalizowanym scenariuszu wszystkie 96 zapisów wykorzystały 4 już przygotowane wpisy, bez dodatkowych bajtów payloadów cache. Dokładne serie są w BUILD_INFO. To pomiar komponentu, nie prognoza całości opóźnień na HA.

Zimne, zmieniane, usunięte z LRU, zbyt duże lub inaczej uporządkowane/aliasowane modele nadal trzeba kodować. Pozostają obserwacja puli (średnio około 95 ms), obliczanie cech/scoring/uczenie Shadow, inferencja i I/O. Optymalizacja zapisu może ograniczyć konkurencję o CPU, ale samo skrócenie decyzji wymaga nowego pomiaru na urządzeniu. Ten log nie dowodzi jakości decyzji względem automatyzacji.

## Instalacja

`HomeMind-Adaptive-AI-0.14.157-addon-root.zip` i SHA256 są w wydaniu GitHub. Aktualizacja i restart, bez Rebuild. Polityka checkpointu i trwałości NORMAL pozostaje jak w 0.14.155.
