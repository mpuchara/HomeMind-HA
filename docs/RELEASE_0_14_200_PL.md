# HomeMind-HA 0.14.200

Wersja wykorzystuje dodatkowy RAM do zachowania cache SQLite pomiędzy decyzjami.
Instalacja wymaga aktualizacji dodatku i restartu. Modele i historia zachowują
dotychczasowy format; Full Rebuild nie jest potrzebny.

## Co pokazały eksporty

Cztery eksporty z 15 października, 03:05:17–03:06:28, pochodzą z 0.14.199.
RSS procesu wynosił około 222–233 MiB. Sama informacja o 3,1% użycia pamięci
nie określa wolnej pamięci w kontenerze ani potrzeb innych dodatków.

W ostatnim eksporcie storage ma stan `normal`, zero zablokowanych zapisów,
WAL około 16 MiB. Candidate Shadow i finalizowane epizody Tournament nie mają
błędów ani zaległych zapisów. Pozostają ponawiane kolizje SQLite w innych
kolejkach, zatem nie oznacza to całkowitego braku contention.

Średni `wrapped_inference` to około 339 ms, a `context_shadow_observation`
około 213 ms. W zachowanym trace 33 otwarcia sesji inference zajęły łącznie
około 866 ms, średnio 26 ms. Sesja 0.14.199 obejmowała jedną decyzję: kolejna
decyzja ponownie tworzyła połączenie, cache stron i przygotowane zapytania.
Istniejący cache sealed policies zajmował tylko około 8 MiB z limitu 64 MiB;
podnoszenie jego limitu nie usuwało tego kosztu.

## Cache RAM

`runtime_sqlite_cache_mb` ma domyślnie 256 MiB, zakres 0–512. Zero wyłącza
zachowywanie połączeń między decyzjami. Zmiana opcji wymaga restartu dodatku.
Połączenia powstają na żądanie, bez wstępnego przydzielania całego budżetu.

Efektywny budżet to minimum z ustawienia, 1/16 całkowitej pamięci, 1/4
dostępnej pamięci oraz dostępnej pamięci pomniejszonej o rezerwę 512 MiB.
Uwzględniane są Linux MemTotal/MemAvailable oraz limity i bieżące zużycie
cgroup v1/v2, również limity grup nadrzędnych. Nieznana albo zbyt mała
rezerwa powoduje powrót do zwykłych krótkich sesji.

Pula utrzymuje maksymalnie cztery połączenia. Przy domyślnym pełnym budżecie
każde otrzymuje cel cache 64 MiB zamiast dotychczasowych 16 MiB. Przy większej
konfiguracji górny limit na połączenie wynosi 128 MiB. Oddzielne mmap tych
połączeń jest wyłączone. `cache_size` SQLite jest celem dla cache stron,
nie twardym limitem RSS procesu: Python, modele, bufory, przygotowane zapytania
i aktywne operacje SQLite wymagają dodatkowej pamięci.

Headroom jest próbkowany najwyżej raz na pięć sekund. Istniejący worker WAL
przycina bezczynne cache także bez nowych decyzji; kontrola następuje również
przy pobieraniu i oddawaniu połączeń. Aktywny zapis kończy swoją transakcję,
a jego połączenie zostaje wycofane po powrocie, jeżeli nowy budżet go nie mieści.
Zajęta pula nie tworzy kolejki oczekiwania: dodatkowa sesja używa dotychczasowej
ścieżki. Powolne tworzenie nowego połączenia nie blokuje dostępu do puli.

## Świeżość i trwałość

Cache zawiera strony i przygotowane zapytania SQLite, bez cache wyników SELECT.
Każdy odczyt nadal wykonuje SQL. Zewnętrzna zmiana konfiguracji jest widoczna
przez SQLite, bez czasu życia cache ani opóźnionej invalidacji ustawień Control.
Jedno połączenie ma wyłącznego właściciela na czas sesji.

Każdy `Store.conn()` zachowuje oddzielny commit/rollback. Wszystkie pozostałe
kursory są zamykane przed oddaniem połączenia do puli, tak aby bezczynny cache
nie zatrzymywał snapshotu czytelnika WAL. Błąd sesji lub zmiana jej lokalnych
ustawień/rozszerzeń powoduje zamknięcie połączenia. Zamknięcie dodatku zwalnia
cache po zatrzymaniu producentów i końcowych zapisach.

Procesy historycznego treningu, sesja WebSocket i background writers zachowują
dotychczasową obsługę połączeń. Ochrona wolnego miejsca, WAL, stron bazy,
rozmiaru payloadu, native busy timeout i rollback na presji pozostaje aktywna.
Eksport diagnostyczny pokazuje `sqlite.runtime_cache`: efektywny budżet,
cele przydzielonych cache, idle/leased, trafienia, otwarcia, bypassy i odrzucenia.
Sam eksport nie wykonuje SQL ani próbkowania systemu plików.

## Walidacja i ograniczenia

Pełna regresja Linux/Python 3.12.3: 2380 testów OK (17 nowych). Compileall,
składnia JavaScript diagnostyki i uruchomienie docelowego entrypointu: OK.
Sparowany benchmark Linux: mediana przyspieszenia komponentu SQL 1,52×
(3 pary po 256 decyzji), zgodność wszystkich wyników.

Regresje obejmują świeżość po zewnętrznym UPDATE, niezakończony kursor i
rzeczywiste TRUNCATE WAL, rollback, niezależne commity, zagnieżdżony timeout,
izolację równoległych sesji, presję storage i RAM, cgroup, shutdown i trening.
Benchmark wykorzystuje prawdziwy dispatcher Engine oraz odczyty Candidate,
Explore, konfiguracji i 96 wierszy kontekstu. Oba warianty wykonują wszystkie
te same odczyty. Wyniki i surowe czasy są w raporcie JSON obok tej notatki.
To pomiar komponentu na hoście, bez CPU modeli, HA i równoległego I/O urządzenia.
Nie jest prognozą przyspieszenia całej inferencji ani pomiarem RSS na Raspberry Pi.

Poprawiono też test okna WAL z 0.14.199: CI ujawniło zależność od zaplanowania
wątku zapisującego w 100 ms. Fixture synchronizuje zapis na granicy okna,
a osobny test sprawdza poprawny wynik BUSY, gdy writer nadal jest aktywny.
Nie wydłużono produkcyjnego okna ani SQLite busy timeout.

Po instalacji warto porównać nowy eksport po rozgrzaniu: `runtime_cache.hits`,
`cache_target_mb`, RSS, `wrapped_inference`, opóźnienie od zdarzenia do decyzji
i stan kolejek. Pula może wykorzystać mniej pamięci, jeśli roboczy zbiór stron
jest mały; zapełnianie RAM bez potrzeb nie przyspiesza aplikacji.
