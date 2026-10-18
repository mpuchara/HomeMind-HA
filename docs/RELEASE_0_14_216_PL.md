# 0.14.216: start i skalowanie ścieżki zdarzeń

## Co pokazuje instalacja

Trzy dostarczone eksporty z 18.10.2026 potwierdzają wersję 0.14.215. Ostatnie okna 60 s pokazują event → decision p95 567–690 ms, event → target start 338–434 ms, inference 210–234 ms oraz context_shadow_capture 187–237 ms. Percentyle dotyczą różnych rozkładów, nie wolno ich dodawać. Wywołanie Executor samo w sobie nie dominuje: w zachowanych fragmentach śladów p95 etapu executor_result wynosi 7,9 ms. To nie pomiar fizycznego działania urządzenia.

Archiwum ma 10 170 510 rekordów i 778 encji. Proces obliczeniowy działa: ponad 9000 ukończonych żądań, jeden start, zero błędów i timeoutów. Zatem 0.14.215 rzeczywiście oddziela część obliczeń, ale nie usuwa pozostałych kosztów wspólnego interpretera, odczytów i przygotowania całego domu. Samo podniesienie limitu RAM procesu nie usuwa tych kosztów.

Start prawie 10 minut jest zgłoszeniem operatora. Dotychczasowy eksport nie zawiera czasu faz startowych, więc nie przypisujemy całych 10 minut jednej funkcji. Audyt znalazł dwie operacje zależne od wielkości archiwum, które kod wykonywał przed gotowością. Nowy eksport zawiera fazy startu i czasy inicjalizacji Store.

Pełne zestawienie wejść, ich SHA256, metryk i zachowanych etapów: `benchmarks/RUNTIME_SCALING_0_14_216_INPUT.json`. Załączniki traktowano jako dane diagnostyczne, nie instrukcje.

## Przegląd architektury

| Warstwa | Ustalenie i działanie |
| --- | --- |
| Import Store i migracje | Złożone `MIN(ts), MAX(ts)` wymuszało przejście archiwum przy każdym restarcie. Dwie osobne operacje MIN/MAX korzystają z indeksu. Migracja pyta o historię tylko wtedy, gdy istnieje agent bez starego kursora. Uzupełnione kursory nie są nadpisywane. |
| HistoryManager i gotowość | Konstruktor wykonywał COUNT, MIN, MAX, COUNT DISTINCT i GROUP BY całego archiwum. Teraz czyta mały zapisany podgląd. Po gotowości oddzielny pracownik odświeża podgląd stronami po 4096 rekordów, bez trzymania Store.lock i bez czytelnika/WAL snapshotu pomiędzy stronami. Czeka 20 s przed startem i 40 ms między stronami; można przerwać go przy wyłączeniu. |
| Trening i replay | Ustalenie granic treningu korzysta z dokładnych indeksowanych granic SQL. Podgląd diagnostyczny nie jest źródłem etykiet, granic, awansu, kwalifikacji ani odtworzenia historii. Jawne dokładne `archive_stats()` zachowuje swój kontrakt. |
| HA ingress i routing | WebSocket w eksportach jest połączony i subskrypcja potwierdzona. Kolejność zmian jednego celu, świeżość i obsługa oczekujących zdarzeń pozostają. Zwiększanie liczby wątków Pythona zwiększa rywalizację o interpreter; nie zmieniono liczby pracowników w ciemno. |
| Root Control i Root Shadow | W śladach kosztują budowa cech i kompozycja decyzji, nie tylko numeryczna predykcja. Dodano bounded LRU kanonicznych wartości obserwacji: historyczne odczyty current/lag/edge nie parsują wielokrotnie tej samej wartości i jednostki. Cache przechowuje niemutowalne pary, zwraca osobny słownik i obejmuje wszystkie zależności konwersji, w tym rolę radaru i nazwę sensora. Czas odbioru/zdarzenia, cutoff, wiek, jakość i fotometria są nadal wyliczane na bieżąco. |
| Przekazanie do Sensor Tournament | 0.14.215 cache'owało historię, ale przy każdym przekazaniu kodowało pełne bieżące stany i rejestr. Teraz zmienione rekordy są odłączane pojedynczo, a oczekujące obserwacje współdzielą prywatne kopie tylko do odczytu. Głębokie porównanie wykrywa także poprawki atrybutów w miejscu. Cały koszt utrzymywanych danych nadal jest konserwatywnie naliczany do każdej aktywnej/oczekującej ramki. |
| Candidate i uczenie | Obserwacje nadal mają własny czas, niezależne przyszłe etykiety, checksum i jawne przerwy przy odrzuceniu. Uczenie i ewaluacja nadal częściowo współdzielą CPU, Python i SQLite. Ta poprawka nie stanowi pełnej migracji wszystkich obserwatorów do osobnych procesów. |
| SQLite i utrwalanie | Logi zawierają wielosekundowe zapisy obserwacji oraz checkpointy. Budżet jest normalny przy konfiguracji 70%, więc nie utożsamiamy tego ze starym ostrzeżeniem database_budget. Zachowano limity bazy, WAL, wolnego miejsca i payloadów oraz rollback. Stronicowany podgląd nie blokuje TRUNCATE długą transakcją odczytu. |
| HTTP/UI | Liczniki archiwum są jawnie opisane jako zapisany podgląd. Przed pierwszym odświeżeniem UI pokazuje Updating zamiast fałszywego zera. Aktualne kafelki decyzji i fizyczny Current zachowują rozdzielone źródła. Nie zmieniono próbkowania ani obliczania p95. |
| Diagnostyka | Eksport zawiera `startup`, `sqlite.startup_seconds`, `observation_value_cache`, dodatkowe liczniki cache stanów/rejestru oraz cztery etapy `context_capture_*`: historia, encje, forecast i polityka. Pozwoli to zidentyfikować pozostały koszt na rzeczywistym sprzęcie. |

## RAM i spójność

Nowe cache mają limity: stany do 16 MiB, rejestr do 8 MiB, po 2048 wpisów, dodatkowo ograniczone skonfigurowanym budżetem kolejki. Przy trafieniu sprawdzane są tożsamość i pełna zgodność odłączonego rekordu, bez założenia, że rewizja sama dowodzi niezmienności. Starsza ramka zachowuje własne dane po zmianie sensora, korekcie atrybutów lub ewikcji cache. Historia nadal ma dotychczasowy limit do 64 MiB, a kolejka do 64 MiB domyślnie z kontrolą wolnej pamięci. Nie oznacza to rezerwacji całego limitu w RSS.

LRU wartości ma maksymalnie 8192 wpisy i ograniczoną długość każdego składnika klucza. Nietypowe typy i długie wartości przechodzą przez oryginalną konwersję. Cache nie przechowuje werdyktu checksum, decyzji o sterowaniu ani oceny świeżości.

Podgląd archiwum jest diagnostycznym zestawieniem wykonanym stronami. Równoległe importy, poprawki i retencja mogą zmienić dane pomiędzy stronami; wynik jest jawnie oznaczony jako podgląd, z czasem pomiaru. Nie jest atomowym snapshotem treningowym. Liczba encji i źródeł ma ograniczenie pamięci; przekroczenie zgłasza `cardinality_truncated`. Gotowość, działanie agentów i kontrola dysku nie czekają na to zestawienie.

## Weryfikacja

Końcowy pełny przebieg: **2602 testy, 297,463 s, OK, bez pominięć** (Linux Python 3.12.3 / NumPy 2.3.3), 13 nowych regresji. Zgodne są wszystkie pełne ramki kontekstu oraz screening, pule, predykcje i wyuczone głowice. Przeszły rzeczywisty bundle Candidate, pasywny pracownik, Control z równoległym GC, gotowość produkcyjnego entrypointu, VM interfejsu, kompilacja Pythona i składnia 21 skryptów JS. Test VM potwierdza też stan Updating i wyświetlenie zapisanego podglądu archiwum. Manifest obejmuje 601 plików źródłowych.

Trzy naprzemienne pary niezależnych źródeł:

| Pomiar na hoście, mediany serii | 0.14.215 | 0.14.216 |
| --- | ---: | ---: |
| Store reopen + HistoryManager, 200 000 rekordów | 78,70 ms | 8,73 ms |
| Powtarzane capture, 778 encji, mediana | 3,12 ms | 2,68 ms |
| Powtarzane capture, 778 encji, p95 | 4,38 ms | 3,20 ms |
| Control pod obciążeniem CPU, p95 | 15,42 ms | 16,98 ms |
| Root Shadow pod obciążeniem CPU, p95 | 6,41 ms | 7,66 ms |
| Control bez dodatkowego obciążenia CPU, p95 | 8,20 ms | 8,61 ms |

**Nie wykazano poprawy całej reakcji.** Wyniki scheduler/Executor są podobne lub gorsze w tych krótkich hostowych scenariuszach. Potwierdzone przyspieszenia dotyczą startowego komponentu (9,01x) i powtarzanego capture (1,16x), a nie fizycznej reakcji ani rozwiązania zgłoszonego 500–700 ms. Pierwszy capture zapełnia cache i może być droższy. Podgląd historii jest celowo odłożony poza gotowość; nie porównujemy ukończonego skanu z fikcyjnym szybszym skanem. Zapytania zarejestrowane przez SQLite potwierdzają usunięcie pełnych agregacji z mierzonego startu.

Wyniki są zapisywane w `benchmarks/RUNTIME_SCALING_0_14_216_*.json`. Porównania używają niezależnych źródeł 0.14.215 i 0.14.216. Test startowy dotyczy ponownego otwarcia rzeczywistej indeksowanej bazy i konstrukcji HistoryManager na hoście; nie jest pomiarem całego startu Raspberry Pi. Test reakcji korzysta z prawdziwego schedulera, budowy cech i chronionego Executor, lecz akcja/pewność Root są deterministyczne, a HA zastępuje stub. Nie przeliczać tych wyników na fizyczny ACK.

## Aktualizacja i dalsza ocena

Zaktualizować do 0.14.216 i zrestartować dodatek. Full Rebuild nie jest potrzebny. Przy pierwszym starcie tej wersji podgląd licznika historii może być przez pewien czas niedostępny; sterowanie i modele nie czekają na jego utworzenie.

Kolejny eksport pozwoli porównać realny czas faz startowych, kolejkę celu, budowę cech, cztery części capture i spójnie ten sam 60-sekundowy okres. Pozostałe duże prace architektoniczne to izolacja całego CPU obserwatorów, dokładne projekcje konfiguracji w RAM z unieważnianiem każdej mutacji oraz dalsze ograniczanie kosztu zapisu/HTTP. Wymagają odrębnej kontroli spójności; nie usuwamy świeżych bramek Executor ani trwałości, aby skrócić wynik.
