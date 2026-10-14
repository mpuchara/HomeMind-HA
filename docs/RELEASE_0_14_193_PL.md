# 0.14.193: mniej pracy SQLite podczas odczytów

Eksport .192 z 14 października 12:34 pokazuje brak treningu. Weryfikacja modeli kandydatów działa już w RAM: 6304 kontrole, bez powrotu do wolnej ścieżki, około 2,8 MiB kanonicznych danych. Średni koszt tej weryfikacji spadł do 5,4 ms z 23 ms w poprzednim eksporcie. Mimo to cała decyzja zajmuje średnio 480 ms, a kontekstowy Shadow 318 ms. Są to eksporty o różnym czasie pracy i obciążeniu, więc nie stanowią kontrolowanego benchmarku całej instalacji.

Log zawiera też kosztowne odczyty SQLite: nawet współdzielone połączenia raportują czas zakończenia odczytu w dziesiątkach milisekund. Każdy zakończony zakres odczytu uruchamiał trzy PRAGMA (`page_count`, `freelist_count`, `page_size`). Baza ma około 5,6 GB. Zapisy statystyk i modele oraz checkpointy nadal konkurują o I/O; checkpoint osiągnął 59 s. Ta poprawka ogranicza powtarzane zapytania po odczytach, nie dowodzi usunięcia całego kosztu I/O.

## Zmiany

Po samym SELECT na współdzielonym połączeniu nie są ponownie odczytywane parametry stron. Dane SELECT nadal są odczytywane na nowo, bez utrzymywania transakcji pomiędzy zakresami. Test z drugim połączeniem potwierdza widoczność jego kolejnych commitów.

Przed pierwszą operacją zapisu od ostatniego pomiaru zajętość stron jest odczytywana świeżo, również gdy bazę zmienił inny proces. Zajętość jest odświeżana także po commit albo rollback. Zmiany DDL, operacje cursor/executemany i mutujące lub nieznane PRAGMA pozostają traktowane ostrożnie. Dotychczasowe kontrole wolnego miejsca, WAL, payloadu, zakresów batch i commit pozostają aktywne. CTE są nadal ostrożnie kontrolowane jak potencjalny zapis.

Cache liczby sensorów obejmuje teraz pełną obserwację, razem z odczytami statusu promocji przed i po ocenie. Usuwany jest także po wyjątku. Schemat, dane modelu i status promocji pozostają świeże przy każdym odczycie; następna obserwacja ponownie odczytuje liczbę sensorów. Nie zmienia to cech, etykiet, uczenia, kolejności decyzji ani bramek promocji.

Diagnostyka `sqlite.storage_budget` zawiera `page_observations` oraz `readonly_page_observations_skipped`. Nie powstają dodatkowe pliki cache ani większe budżety RAM lub dysku.

## Weryfikacja

2301/2301 testów przeszło w Linuksie/WSL bez pominięć (255.618 s); compileall przeszedł. Siedem nowych regresji obejmuje widoczność zewnętrznych commitów, świeży pomiar przed zapisem po zewnętrznym wzroście bazy, rollback pod presją, DDL, PRAGMA, cursor i pełną obserwację ze statusem promocji. Benchmark porównuje rzeczywisty Store i SQLite w osobnych procesach .192/.193. Mierzy 32 odczyty na zakres i liczy wszystkie wykonane zapytania. Porównuje dokładne dane oraz późniejszy trwały zapis. To pomiar komponentu na komputerze testowym, nie całej decyzji na Raspberry Pi.

W trzech porównaniach 768 odczytów wykonuje 768 zamiast 3072 zapytań SQL, przy identycznych danych. Mediana przyspieszenia tego komponentu wynosi 2.33×. Oddzielny benchmark czterech kandydatów potwierdza dokładną zgodność cech, predykcji, wag po uczeniu oraz liczby nowych etykiet; mediana przyspieszenia całego testowego observera wynosi 1.05×. Żaden z tych wyników nie jest pomiarem Raspberry Pi. Długie checkpointy WAL i zapisy JSON pozostają osobnymi kosztami. [Pełny raport](benchmarks/READONLY_SQLITE_0_14_193_VALIDATION.json).

## Instalacja

Zainstaluj .193 i zrestartuj dodatek. Nie jest potrzebny nowy trening ani Full Rebuild. Po aktualizacji porównaj opóźnienia oraz nowe liczniki pomiarów stron. Checkpointy i zapisy JSON pozostają osobnymi kosztami do oceny w kolejnym eksporcie.
