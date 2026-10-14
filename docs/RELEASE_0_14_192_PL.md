# 0.14.192: szybszy Shadow i większe indeksy treningowe w RAM

W eksporcie .191 z 14 października średnia obsługa decyzji wynosiła 425 ms, a kontekstowy Shadow 306 ms. Weryfikacja źródła modelu kandydata powtarzała się 297 220 razy i kosztowała średnio 23 ms. Nie było aktywnego treningu; zapisane zakończone zadania trwały około 2 h 43 min, przy szczytowym RAM workera około 342 MiB i limicie 768 MiB. Są to pomiary istniejącej wersji, nie wynik nowej poprawki.

## Kontekstowy Shadow

Po poprawnej weryfikacji źródło modelu jest kopiowane do prywatnych kontenerów tylko do odczytu. Zwykła modyfikacja zagnieżdżonych wag, próbek i schematu jest odrzucana. Przy każdym użyciu nadal liczony jest świeży SHA-256, ale z gotowych kanonicznych bajtów: nie trzeba ponownie przechodzić całego drzewa i kodować JSON. Nie jest przechowywana poprzednia odpowiedź na pytanie, czy checksum jest poprawny.

Bufory kanonicznych bajtów mają łączny limit 64 MiB, najwyżej 64 źródła i 2 MiB na źródło. Limity dotyczą tych buforów, nie całkowitego RSS procesu. Porzucone źródła zwalniają budżet. Po wyczerpaniu budżetu, przy własnym typie obiektu lub brakującej sumie działa dotychczasowa weryfikacja. Uszkodzona suma odrzuca źródło. Nowsze źródło opublikowane podczas przyjmowania do RAM jest zachowane; bieżąca obserwacja może wtedy użyć modelu bazowego.

Kopia robocza i wewnętrzny snapshot odzyskują zwykłe dict/list. JSON na dysku zachowuje format. Dotyczy to kandydatów sensorów kontekstowych, a dotychczasowe uczenie Live, generacje Correct i bramki promocji pozostają zgodne. Diagnostyka eksportuje liczniki `sealed_policy_snapshots`, zajętość i limity. Nie powstają dodatkowe pliki cache na karcie.

## Trening

Na hoście z nowego logu domyślny równoległy worker nadal ma limit 768 MiB. W tym budżecie indeks historii rośnie ze 128 do 192 MiB. Domyślny limit źródłowych wierszy indeksu przejść rośnie z 65 536 do 262 144 przy budżecie workera co najmniej 768 MiB. Gęsty sensor nie musi więc tak szybko przechodzić na kosztowne wyszukiwanie w bazie. Własne inne limity indeksu przejść są zachowane, a małe profile pamięci korzystają z poprzednich mniejszych indeksów.

Dostępny RAM jest dzielony między równoległe workery z rezerwą dla procesu nadrzędnego. Wewnętrzna kontrola RSS replay korzysta teraz z przyznanego limitu workera, zamiast stałych 500 MiB. Nadzorca nadal egzekwuje ten limit. Limity bazy, WAL i wolnego miejsca, anulowanie oraz warunki publikacji modelu pozostają aktywne.

W logu większość czasu treningu zajmował replay; sama poprawa indeksów nie dowodzi usunięcia całych 2 h 43 min. Kolejny eksport pozwoli sprawdzić rzeczywisty zysk na Raspberry Pi.

## Weryfikacja

Pełny zestaw 2294/2294 testów przeszedł w Linuksie/WSL bez pominięć w 319.038 s; compileall przeszedł. Piętnaście nowych regresji sprawdza niezmienność, świeży SHA, zgodność kopii i zapisu, zmianę źródła, limity i ich zwalnianie, profil RAM oraz zgodność gęstego indeksu z SQLite. Starsze testy diagnostyki uwzględniają nową fazę przyjmowania do RAM i wspólną ścieżkę kontroli przy każdej obserwacji. Benchmark uruchamia rzeczywisty observer czterech kandydatów w osobnych procesach .191/.192, z syntetycznym wejściem i próbkami klasyfikatora. Porównuje dokładne SHA cech, predykcji i wytrenowanych wag po rzeczywistej przyszłej zmianie celu. To pomiar komponentu na komputerze testowym, bez ruchu HA i równoczesnego I/O microSD.

W trzech porównaniach mediana przyspieszenia wyniosła 1.32×, zakres 1.15–1.72×. Cechy, predykcje, wytrenowane głowice i liczby nowych etykiet mają identyczne wyniki w każdej parze. Nie jest to pomiar Raspberry Pi ani obietnica skrócenia całego treningu w tym samym stosunku. [Pełny raport](benchmarks/SEALED_SHADOW_RAM_0_14_192_VALIDATION.json).

## Instalacja

Zainstaluj .192 i zrestartuj dodatek. Nie jest wymagany Full Rebuild ani ponowne trenowanie istniejących modeli. Zmiany runtime działają po restarcie, a większe indeksy będą użyte w następnym zleconym treningu. Nie uruchamia to automatycznie nowego treningu.
