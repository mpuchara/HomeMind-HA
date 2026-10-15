# HomeMind 0.14.206: mniej długich blokad między decyzjami

Sześć eksportów 0.14.205 z 20:52–20:54 nadal pokazuje około 157 ms średniego
oczekiwania na rozpoczęcie targetu i około 242 ms do decyzji. W poszczególnych
oknach p95 przekracza sekundę. Samo usunięcie debounce i interwał Shadow nie
wystarczyły. W deduplikowanym śladzie porównanie epizodów trwa do 3,20 s,
obserwacja Shadow do 5,38 s, a checkpoint WAL do 3,69 s. Są to różne operacje,
wykonywane także równolegle; ich maksima nie są sumą opóźnienia jednej decyzji.

Zmiana usuwa duże odczyty audytu z zimnej ścieżki porównań epizodów. SQLite
utrzymuje dwa indeksy obejmujące etykiety, metryki, klucze i wynik walidacji JSON.
Zapytanie porównania może odczytać je bez pobierania wielkich pól audytu ani ich
ponownego parsowania. Wynik porównania, obsługa błędnego audytu, transakcje,
rollback oraz unieważnianie cache po zewnętrznym SQL pozostają zachowane.
Indeksy są aktualizowane przez SQLite przy zapisach, więc nie ma osobnej kolejki
ani opóźnionej kopii danych.

Pierwszy start tworzy indeksy dla istniejących epizodów. To jednorazowy odczyt
audytu i dodatkowe miejsce na małe dane porównania. Działają dotychczasowe
limity dysku i WAL. Przy StoragePressure, zajętej bazie lub przerwanym tworzeniu
opcjonalnego indeksu działa dotychczasowe świeże zapytanie. Usunięcie indeksu
przez narzędzie zewnętrzne również uruchamia tę ścieżkę. Nie jest wymagany
Full Rebuild. Zmiana nie usuwa samych audytów.

Kosztowna obserwacja kandydatów Tournament po decyzji dodatkowo ustępuje,
jeśli Engine ma już oczekujące zdarzenia dla agentów. Zwykły interwał pozostaje
1000 ms. Przy ciągłym ruchu maksymalny odstęp od początku ostatniej udanej
obserwacji wynosi domyślnie 5000 ms (`runtime_shadow_max_deferral_ms`). W wolnej
chwili obserwacja wraca już po zwykłym interwale. Wartość 0 wyłącza dodatkowe
ustępowanie. Nie przerywamy obserwacji, która już się rozpoczęła.

Zdarzenia targetu, zmiany wartości/dostępności, nowa polityka/schemat i ponowienie
po błędzie nadal omijają odroczenie. Krótkie ON/OFF i filtrowanie własnego ACK
mają testy także przy oczekujących innych targetach. Odczyt obciążenia nie usuwa
żadnych wyzwalaczy. Polityka sterująca i zabezpieczenia Executor pozostają
aktywne dla każdego dopuszczonego zdarzenia. Dalsze ograniczenie próbkowania
kontekstu kandydatów zmienia ich późniejszą historię i uczenie; nie oznacza
identycznych przyszłych wag.

Nowe liczniki są dostępne w
`engine.context_persistence.observation_scheduler.deferred_for_realtime`,
`max_deferral_ms` i `retry_agents`. Harmonogram oraz lista ponowień są ograniczone
do 256 małych wpisów każdy. Nie dodano wątków, nie osłabiono retencji ani
limitów przestrzeni/WAL.

Benchmark `tools/benchmark_tail_latency.py` porównuje niezależne źródła 0.14.205
i 0.14.206 w osobnych procesach. Dane: 96 par epizodów, 128 KiB poprawnego audytu
na wiersz, osiem zimnych zapytań na serię i trzy pary serii. Pełne wyniki mają
identyczny SHA. Na Windows Python 3.11 mediana zapytania wyniosła 22,5–25,4 ms
przed zmianą i 3,3–4,0 ms po zmianie, mediana przyspieszenia 6,78 raza.
Jednorazowe utworzenie indeksów zajęło 34–48 ms na tym małym zbiorze.
Nie prognozuje to czasu migracji całej bazy na microSD.

Test EXPLAIN dodatkowo sprawdza brak odczytów kolumn tabel źródłowych i brak
wykonywania funkcji json_valid podczas tego zapytania. Osobny benchmark
`tools/benchmark_control_priority.py --continuous` wstrzykuje kolejne prawdziwe
zdarzenie Engine po poprzedniej decyzji, bez sztucznego zegara ani opóźnień.
Sprawdza zachowanie decyzji, dostarczenie wszystkich zdarzeń i chronione
wywołanie Control do atrapy HA. Akcja/confidence Root są deterministyczne.

Wyniki pomiarów hosta nie obejmują sieci HA, fizycznego urządzenia ani wszystkich
równoległych prac na Raspberry Pi. Eksporty użytkownika zawierają intencje
Shadow/odrzucenia, bez próbek wywołań usług HA. Nie można z nich potwierdzić czasu
fizycznego sterowania. Już rozpoczęta ciężka obserwacja i konkurencyjne I/O
nadal mogą powodować długie ogony. Po instalacji porównuj `event_to_target_start`,
`event_to_decision`, `wrapped_inference`, liczniki odroczeń oraz, w trybie Control,
`event_to_service_dispatch` i `event_to_state_ack`.

Szczegóły diagnozy, pomiarów i końcowej walidacji znajdują się w
`docs/benchmarks/TAIL_LATENCY_0_14_206_*.json`.

Walidacja końcowa: 2474 testy Linux Python 3.12.3 / NumPy 2.3.3, bez pominięć,
w tym 19 nowych regresji; kompilacja modułów, składnia wszystkich dostarczanych
JS i produkcyjny entrypoint do ready zakończone powodzeniem.

Po zakończeniu pełnych testów powtórzono porównania na Linux Python 3.12.3.
Trzy pary niezależnych źródeł zachowały pełne wyniki porównań (SHA), a zimne
zapytania przyspieszyły 4,03 raza według mediany trzech par. Dodatkowe indeksy
zajęły 94 208 bajtów w tym zbiorze. Nie dodają kopii wielkich audytów.

W trzech parach po 768 kolejnych zdarzeń rzeczywistego Engine wszystkie zdarzenia
zakończyły się decyzją, wyniki Executor miały identyczny SHA i każda seria
wykonała dokładnie jedno chronione wywołanie light.turn_on do atrapy HA.
Mediana przyspieszenia typowej reakcji wyniosła tylko 1,03 raza. Ten mały,
syntetyczny zestaw nie odtwarza wielosekundowych ogonów z urządzenia.
Raport CONTROL_PRESSURE_LINUX podaje oddzielnie liczniki odroczeń, czasy p95
oraz czas wysłania końcowej usługi. Nie przedstawiamy tego jako potwierdzenia
usunięcia wszystkich opóźnień Raspberry Pi.
