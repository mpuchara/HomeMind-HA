# 0.14.194: mniej kopiowania i kodowania przy zapisie Shadow

Kontynuacja optymalizacji HomeMind-HA po .193. Do tej iteracji nie dostarczono nowego eksportu runtime; do diagnozy użyto ostatniego dostępnego eksportu .192 z 14 października 12:34.

W tym eksporcie przygotowanie snapshotu Shadow kosztowało średnio 53 ms na operację (224 snapshoty), a kodowanie JSON 192 ms (35 operacji). Snapshoty kopiowały cały niezmienny model, a writer odtwarzał go i ponownie przechodził przez całe drzewo. Weryfikacja modeli była już aktywna w RAM. Checkpointy WAL nadal dochodziły do 59 s; nowa poprawka nie zmienia fizycznego I/O checkpointu.

## Zmiany

Gdy źródło modelu jest zweryfikowanym prywatnym obiektem tylko do odczytu, zapis Shadow zamraża tylko bieżące liczniki, schemat oceny i pozostałe metadane. Snapshot zachowuje referencję do dokładnie tego niezmiennego źródła. Późniejsza zmiana metadanych lub opublikowanie nowego modelu nie zmienia wcześniej przyjętego snapshotu.

Gotowy fragment JSON modelu jest przygotowywany raz przy przyjmowaniu źródła do RAM. Zawiera pełny model, jego checksum i ignorowane przez checksum pola bookkeeping. Writer dołącza go do świeżo zamrożonych metadanych, zachowując identyczny JSON. Kopie learnera i format na dysku pozostają zwykłymi dict/list/JSON. Wewnętrzne bytes są tworzone wyłącznie przez aplikację i nie są ładowane z plików zewnętrznych.

Dotychczasowy łączny budżet 64 MiB obejmuje teraz kanoniczne bajty oraz gotowe fragmenty JSON; nadal obowiązuje najwyżej 64 źródła i limit 2 MiB na każdy kanoniczny bufor lub fragment JSON. Nie jest to limit całego RSS procesu. Jeśli dodatkowy fragment nie mieści się w budżecie, źródło może zachować szybkie sprawdzanie checksum, a zapis korzysta z wcześniejszego pełnego snapshotu. Nie wspiera to nowym cache obiektów własnych lub źródeł bez poprawnej sumy.

Snapshot utrzymuje źródło i jego budżet do czasu utrwalenia lub zastąpienia nowszym. Porzucone źródła zwalniają budżet. Nie powstają dodatkowe pliki cache. Harmonogram i liczba zapisów, bramki promocji, uczenie, etykiety Correct oraz kontrole WAL, bazy i wolnego miejsca pozostają bez zmiany.

Diagnostyka pokazuje `shared_source_snapshots` w `engine.context_persistence`, a `sealed_policy_snapshots` rozdziela `canonical_bytes`, `persistence_json_bytes` i `persistence_fallbacks`.

## Weryfikacja

2309/2309 testów przeszło w Linuksie/WSL bez pominięć (324.531 s); compileall przeszedł. Osiem nowych regresji sprawdza zamrożone metadane, własność niezmiennego źródła, brak przejścia po dużym źródle przy snapshotach, zgodność JSON i restarta, zachowanie nowszego snapshotu po błędzie SQLite, błąd zamrażania, budżet i jego zwalnianie oraz predykcje rzeczywistego learnera. Porównanie .193/.194 używa rzeczywistego ContextTournament/SQLite i modelu z gęstymi próbkami klasyfikatora. Zmienia metadane przy każdej obserwacji, a źródło modelu co osiem obserwacji. Sprawdza dokładną zgodność każdego JSON, liczbę zapisanych modeli i odczyt po restarcie. Mierzy osobno snapshot, kodowanie, pełne opróżnienie oraz nowe przyjęcie źródła. To pomiary komponentów na komputerze testowym, bez obciążenia microSD, nie wynik całej decyzji na Raspberry Pi.

W trzech porównaniach mediana przyspieszenia snapshotu wynosi 152.96×, kodowania 9.85×, a pełnego opróżnienia do SQLite 1.91×. Każdy JSON jest identyczny bajtowo; liczba utrwaleń i odczyt po restarcie są zgodne. Benchmark observera zachowuje identyczne cechy, predykcje, wytrenowane wagi i liczby nowych etykiet (mediana stosunku czasu 0.99×). Pomiar observera obejmuje głównie predykcje i nie obiecuje proporcjonalnego zysku całej instalacji. Nowe przyjęcia źródła są mierzone osobno; korzyść dotyczy przede wszystkim ponownych snapshotów niezmiennego modelu. [Pełny raport](benchmarks/SHADOW_PERSISTENCE_0_14_194_VALIDATION.json).

## Instalacja

Zainstaluj .194 i zrestartuj dodatek. Nie jest potrzebny nowy trening ani Full Rebuild. W kolejnym eksporcie porównaj snapshot, JSON, liczbę współdzielonych źródeł i zużycie RAM. Długie checkpointy oraz ilość danych zapisywanych do SQLite pozostają osobnymi kosztami.
