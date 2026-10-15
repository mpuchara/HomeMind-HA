# HomeMind 0.14.204: wspólne obliczenia cech w obserwacji Shadow

Sześć eksportów 0.14.203 z 15 października, 15:35–15:39 czasu polskiego,
pokazuje średnio około 242 ms na obserwację Shadow i 74 ms na wnioskowanie
polityki. Cały opakowany przebieg zajmuje około 344 ms i nadal zajmuje
pracownika targetu po przygotowaniu decyzji. Średni czas od odebrania zdarzenia
HA do decyzji wynosi 197–200 ms; nie jest to opóźnienie fizycznego czujnika.
Trening jest nieaktywny, poziom storage to `normal`, liczba blokad budżetu
zapisu wynosi zero. Checkpointy nadal potrafią trwać sekundy. Pomiar off-CPU
obejmuje oczekiwanie na planistę, GIL i I/O; sam nie rozstrzyga ich udziału.

W czterech modelach kandydackich powtarza się przeliczanie tych samych sensorów.
Wersja 0.14.204 oblicza kompletny opis sensora raz w jednej obserwacji Shadow.
Każda polityka układa własny wektor i etykiety w kolejności swojego schematu,
otrzymuje niezależne metadane i przechodzi pełną kontrolę SHA. Wyników modeli,
oceny prequential, uczenia po wyniku i warunków promocji nie zmieniono.

Bufor działa wyłącznie w jednym synchronicznym wywołaniu na danym wątku.
Wymaga tej samej instancji agenta, mapy stanów i historii oraz tego samego czasu.
Klucz uwzględnia sensor, typ schematu, wersję semantyki, target i właściwość,
profil reakcji, dokładne opóźnienia serii oraz wykluczenie sensora. Limit to
256 opisów. Po wyjściu, także po wyjątku, bufor jest odrzucany. Przekroczenie
limitu oznacza zwykłe obliczenie cechy. Live i replay poza tym zakresem dalej
obliczają bieżące dane. Nie zmieniono limitów RAM/dysku, retencji ani zapisów.

Porównanie dwóch niezależnych źródeł, 0.14.203 i 0.14.204, w Linuksie
Python 3.12.3 / NumPy 2.3.3, trzy pary po 120 przebiegów:

- Liczba wywołań ekstraktora pojedynczej obserwacji cechy: 128 → 44.
- Mediana przyspieszenia obliczeń cech: 1,35×; całej obserwacji: 1,20×,
  czyli około 16% mniej czasu całego komponentu przy braku konkurującego wątku CPU.
- Przy konkurującym wątku CPU obliczenia cech są szybsze w każdej parze,
  ale czas całej obserwacji jest zmienny: 0,88–1,18×, mediana 1,04×.
  Ten pomiar nie dowodzi stabilnego spadku całkowitego opóźnienia pod obciążeniem.
- Identyczne kompletne wektory, etykiety, metadane, predykcje, liczby kontroli
  integralności oraz wagi i próbki po niezależnej zmianie wyniku targetu.

Benchmark używa rzeczywistego obserwatora, czterech polityk, produkcyjnej
kompozycji obserwatora i uczenia po wyniku. Nie obejmuje sieci HA, karty microSD,
konkurencyjnych checkpointów ani fizycznego potwierdzenia urządzenia. Wyników
nie należy przeliczać na gwarantowane opóźnienie Raspberry Pi. Przy kolejnym
pomiarze urządzenia należy porównać `context_candidate_features`,
`context_shadow_observation`, `wrapped_inference` i `event_to_target_start`,
osobno z wyłączonym i włączonym szczegółowym debugowaniem.

Nie jest wymagany Full Rebuild. Zmiana jest przygotowana na bazie gałęzi
0.14.203 i obejmuje wcześniejsze poprawki; PR jest kolejną zmianą w tym stosie.
Raporty diagnozy i porównań znajdują się w `docs/benchmarks/SHADOW_FEATURES_0_14_204_*.json`.

Walidacja końcowa: 2437 testów Linux Python 3.12.3 / NumPy 2.3.3, bez pominięć,
kompilacja modułów, składnia wszystkich dostarczanych plików JavaScript i uruchomienie
produkcyjnego entrypointu do `ready` z wersją 0.14.204 zakończone powodzeniem.
Dodano 15 testów regresji. Raport zawiera hashe sprawdzonych plików źródłowych.
