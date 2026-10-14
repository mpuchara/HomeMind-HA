# 0.14.195: mniej wymuszonego I/O w buforze Shadow

Eksport z 14 października 15:01 pochodzi z .193. Nie mierzy działania .194. Średni czas zdarzenie → decyzja wynosi 259 ms, pełnej obsługi agenta 352 ms, a kontekstowego Shadow 237 ms. We wcześniejszym eksporcie .192 było odpowiednio 650, 480 i 318 ms. Okna pracy i obciążenia eksportów są różne; nie jest to kontrolowany pomiar wersji. Diagnostyka potwierdza pominięcie 429 727 zbędnych pomiarów stron w .193.

Mimo lepszych średnich, ślad agenta łazienki zawiera 11,7 s pełnej obsługi i 9 s w Shadow, a następne zdarzenie czekało już około 11 s. Bufor statystyk pokazuje 288 oczekujących wierszy, około 10 MB naliczonego RAM i 21 wymuszonych opróżnień. Maksymalny checkpoint wynosi w tym eksporcie około 6 s. Same liczniki nie przypisują wszystkich 21 opróżnień do konkretnej pauzy; poprawka usuwa odtworzony lokalnie mechanizm blokowania.

## Zmiana

Gdy writer zapisuje pełny snapshot, nowe metadane mogą odwoływać się do dokładnie tego samego niezmiennego obiektu JSON. Dotychczas całe próbki i historia były naliczane ponownie do limitu RAM. Około 10 MiB wspólnych danych w zapisie i oczekującej wersji przekraczało w rozliczeniu 16 MiB, wymuszając synchroniczne opróżnienie i oczekiwanie na writer.

Bufor observed_pool rozlicza duże obiekty str przez tożsamość, tylko dla tego samego wiersza i kolumny. Wspólny JSON pozostaje naliczony raz, a obie wersje metadanych mają własny narzut. Ledger ma konserwatywny narzut 512 bajtów na obiekt i obejmuje także dane zapisywane. Domyślny limit 16 MiB i limit wierszy pozostają bez zmiany. Równe teksty w różnych obiektach i inne wiersze są liczone osobno; zapobiega to zbyt małemu rozliczaniu różnych sensorów i ich SQL bindings.

Rozliczenie dotyczy kolumn history_json, samples_json i screening_json, jeśli niezmienny obiekt ma co najmniej 1024 bajty. Nie dodaje cache wartości ani nie pomija próbek. Zastąpienie wiersza, commit i błąd aktualizują referencje atomowo. Nowszy pełny wiersz pozostaje po błędzie zapisu. Ostatnia referencja zwalnia naliczony budżet. Rzeczywiste przekroczenie limitu nadal uruchamia dotychczasowy zapis przez chronioną ścieżkę SQLite.

Model, etykiety, cechy, bramki promocji i ochrona bazy/WAL/wolnego miejsca pozostają bez zmiany. To budżet bufora, nie całkowitego RSS procesu. Wydanie zawiera optymalizacje snapshotów i JSON z .194.

Diagnostyka bufora pokazuje shared_payload_columns, shared_payloads, shared_payload_references, shared_payload_bytes i duplicate_payload_charge_avoided_bytes. Ostatnia wartość oznacza uniknięte ponowne naliczenie obiektu już współdzielonego, nie zmniejszenie jego fizycznego rozmiaru.

## Weryfikacja

2317/2317 testów przeszło w Linuksie/WSL bez pominięć (283.189 s); compileall przeszedł. Osiem nowych regresji oraz dotychczasowe testy uczenia i ochrony storage przeszły. Testy obejmują jednoczesny zapis i przyjęcie nowszych metadanych dla 10 MiB danych, prawdziwą presję dla różnych obiektów, osobne wiersze, rollback zachowujący najnowsze liczniki, odmowę zbyt dużego wejścia i rejestrację tylko bufora observed_pool. Porównanie .194/.195 używa 288 wierszy i około 10 MiB syntetycznego JSON w limicie 16 MiB. Writer jest kontrolowanie wstrzymany przez 300 ms. Wynik końcowy wszystkich wierszy ma identyczny SHA. Nie jest to pomiar czasu reakcji Raspberry Pi ani dowód, że ten mechanizm powodował całą zaobserwowaną pauzę.

W trzech próbach stara ścieżka przyjmowała metadane po około 300.7 ms, nowa po 0.90 ms, jeszcze podczas kontrolowanego zatrzymania writera. Opróżnienia wymuszone spadły z jednego do zera, a liczba commitów z trzech do dwóch, przy identycznych wszystkich wierszach i limicie 16 MiB. Stan bufora podczas zapisu wynosi około 10,5 MiB. Oddzielne porównanie observera zachowuje identyczne cechy, predykcje, wagi po uczeniu i liczby nowych etykiet (mediana stosunku czasu 1.02×). [Pełny raport](benchmarks/CONTEXT_BUFFER_0_14_195_VALIDATION.json).

## Instalacja

Zainstaluj .195 i zrestartuj dodatek; można przejść bezpośrednio z .193. Nie jest wymagany nowy trening ani Full Rebuild. W kolejnym eksporcie sprawdź overflow_flushes, współdzielone dane, event_to_decision i długie operacje Shadow. Fizyczne I/O checkpointów i koszt uczenia przy nowych etykietach pozostają osobnymi kosztami.
