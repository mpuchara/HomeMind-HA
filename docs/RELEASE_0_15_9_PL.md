# HomeMind 0.15.9 TEST — paginacja przygotowania historii

Baza: `bc60087a2085165ecd6d273a31c1ee25f907d8b6`, 0.15.8 / PR #377.

## Poprawka P0

`Preparation.copy()` obejmuje cały przekazany predykat nawiasami przed dodaniem
ograniczeń snapshotu i kursora. Dotyczy wszystkich tabel i kluczy. Wybór starszego
seeda również respektuje utrwalony rowid snapshotu. Niepusta partia musi zwiększyć
kursor (porównanie leksykograficzne dla par), zanim nastąpi INSERT lub checkpoint.
`TrainingPreparationNoProgress` zawiera tabelę, strategię, query key, oba kursory,
liczbę wierszy i nazwę pliku generacji identyfikującą zadanie. Legalna deduplikacja
z postępem kursora pozostaje dozwolona. Ten sam invariant obejmuje witness recovery
i checkpointed archive iterator. Audyt `training_snapshot`, `history_recovery`,
`archive_reads` nie wykazał drugiego takiego składania OR z granicą keysetu;
predykaty OR wewnątrz podzapytań Correct pozostają semantycznie odseparowane.

## Aktualizacja i bezpieczeństwo

Kontrakt scratch zmienia się z v5 na v6, łącznie z hashem nazwy pliku. Wadliwy v5
nigdy nie jest wznawiany jako v6. Stary deferred descriptor po zmianie wersji jest
wycofywany; kolejka buduje aktualny opis zadania z istniejącego archiwum.
Sprzątanie v5 wymaga zapisanego PID nieżyjącego workera oraz zgodnego source,
agent_id i kontraktu w pliku. Nieznany lub aktywny właściciel: plik pozostaje,
ale nowa wersja go nie używa. Nie usuwamy archiwum, cache Recorder, modeli,
agentów, kandydatów ani Correct. Nie zmieniamy semantyki selekcji cech.

Krótkie snapshoty źródłowe i rowid fences z 0.15.8 pozostają. Jedynie docelowa,
izolowana generacja grupuje do ośmiu partii / budżetu bajtów / jednej sekundy
pracy pomiędzy zapisami. Dane i ich kursor zatwierdzane są w jednej transakcji,
przy zachowanym domyślnym SQLite durability. Przerwanie może ponowić wyłącznie
niezatwierdzone partie. Źródłowy WAL nie jest przytrzymywany podczas commitów.

## Diagnostyka i koszt

Oddzielne liczniki payload_rows_fetched, unique_rows_copied, duplicate_rows,
committed_batches, payload_bytes_fetched i inserted_payload_bytes (z flagą
exact; przy mieszanej deduplikacji licznik stanowi dolną granicę) oraz kursory,
czasy ostatniego postępu, commit_seconds i status_write_seconds.
bytes_copied zachowuje historyczne znaczenie bajtów payloadu; nie jest pomiarem
fizycznego I/O. Porównanie na Linuksie mierzy osobno `/proc/self/io.write_bytes`.
Status treningu i bootstrap zapisują plik maksymalnie raz na sekundę przy niezmienionym etapie,
z natychmiastowym raportowaniem zmiany fazy i zakończenia. Supervisor kończy
przygotowanie po 600 s bez zmiany kursora/query/strategii zamiast ufać heartbeat.
Długie pojedyncze operacje indeksowania również podlegają temu konserwatywnemu
limitowi; nie jest to adaptacyjna estymacja czasu SQL.

Przygotowanie ma aktywną flagę również w worker_bootstrap; nie pokazuje starego
ETA replay. Widok pokazuje tabelę, odczyty, unikalne wiersze i wiek postępu.
Formatter p0 normalizuje minuty po zaokrągleniu (brak `23 h 60 min`).

## Testy i dowody

`tests/test_training_pagination_0159.py`: rzeczywisty SQLite, obie tabele,
5 i 385 checkpointów, seed i granice snapshotu, fail-safe wszystkich trzech
rodzajów kursora, legalna deduplikacja, atomowy rollback, odbudowa v5 bez zmian
archiwum/modeli/Correct/cache oraz konserwatywne sprzątanie.

`tools/validate_training_0159.py`: wykonuje oryginalne Preparation z przypiętego
0.15.8 na tej samej bazie. Stara wersja powtarza pięć rekordów i jest zatrzymana
po 100 stronach, kandydat odczytuje dokładnie pięć i kończy. Osobny zbiór 8192
wierszy bez OR pozwala mierzyć normalny koszt commitów, bez mylenia go z pętlą.

Ten sam skrypt uruchamia rzeczywiste subprocessy produkcyjnego workera na
fixture: ponad 25 agentów, 243 dodatkowe encje / 116640 stanów kontekstu,
oba typy checkpointów po pięć rekordów, obecność, dwóch trenowanych agentów,
96 przejść targetu, dwa kolejne chunki drugiego agenta i równoległe zdarzenia SQLite runtime.
Sprawdza zapis modelu, benchmark, Shadow i progress 100%. To test hosta;
nie reprezentuje 14 dni / 40 mln stanów na fizycznym Pi.
`benchmark_generation_preparation.py` porównuje pełny worker 0.15.8 i 0.15.9
na identycznej milionowej bazie bez checkpointów wyzwalających pętlę.

### Zmierzony wynik hosta

Windows: 26 skonfigurowanych agentów, 245 encji widocznych dla workera,
116640 stanów kontekstu, obie tabele checkpointów dokładnie 5/5 odczytów/zapisów.
Drugi agent: 2 chunki, 147.85 s procesu, 8721 równoległych zapisów runtime;
model zapisany, benchmark, Shadow/100%. Pierwszy agent również wytrenowany
w osobnym dziecku i jego model pozostał niezmieniony. Użycie cache cech
między chunkami oraz continuation_cached_rows potwierdzone.

WSL /tmp ext4, identyczne 8192 rekordów po ~900 bajtów: 65 → 9 commitów,
1.222 → 0.297 s, `/proc/self/io.write_bytes` 12648448 → 9142272 bajtów.
Kontrola błędnego OR: 100 commitów / 500 odczytów / 5 unikalnych zapisów
przed przerwaniem testu; hotfix: 1 commit / 5 odczytów / 5 zapisów / done.
To lokalny nośnik hosta, nie pomiar SD karty ani instalacji Raspberry Pi.

## Recorder P2 i otwarte pomiary

Dodane thread-local liczniki `training_job_timings.recorder_costs` rozdzielają
oczekiwanie na nagłówki HTTP (wraz z HA), odczyt body, bajty odpowiedzi,
JSON, cały etap normalizacji/zapisu, SQLite admission, wspólne INSERT/ON CONFLICT
dedup, commit/release, coverage persistence i przerwy między chunkami.
Nie przechowują URL, tokenów ani treści odpowiedzi. Czas obliczeń HA nie jest
oddzielany od transportu bez instrumentacji po stronie HA. INSERT i dedup
stanowią jedną operację SQLite; nie prezentujemy ich jako niezależne pomiary.

Nie mamy nowej próbki tej fizycznej instalacji: dominujący składnik 11066 s
pozostaje nieustalony. Nie zwiększamy współbieżności ani nie ograniczamy kontekstu
na podstawie samego całkowitego czasu. Istniejący cache coverage pozostaje wspólny
między agentami i trwały; nie rozszerzamy reguł kompatybilności rozdzielczości
bez osobnego dowodu kompletności, atrybutów i received_time. Eksperyment selekcji
kontekstu pozostaje osobnym zadaniem. Cel ~60 minut na Pi 5 jest NIEZWERYFIKOWANY.

## Wydanie

CI: Python 3.11/3.13, istniejący pełny zestaw benchmarków, Docker i aarch64/QEMU.
Wyniki bieżącego uruchomienia i pakiet instalacyjny są publikowane jako artefakty
Actions. Instalacja zachowuje katalog danych i opcje; wymaga przebudowania dodatku.
TEST ONLY. Brak automatycznego merge do main i brak walidacji fizycznego Pi.
