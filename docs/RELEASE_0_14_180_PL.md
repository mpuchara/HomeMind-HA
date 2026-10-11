# Adaptive AI 0.14.180

Import Recorder działa szybciej z zachowaniem limitów bazy, WAL i wolnego miejsca. W logu 0.14.179 przygotowanie historii trwało około 51 minut: archiwum miało już ponad 5 milionów rekordów, a trening czekał na zakończenie importu. Ochrona dysku sprawdzała system plików przy każdym wierszu i ponownie zatrzymywała zapis przy około 64 MiB WAL.

## Zmiany

- W pętli zapisu ponownie wykorzystujemy wynik pomiaru przez najwyżej 50 ms lub do 1 MiB danych wejściowych. Każdy początek i koniec polecenia SQL oraz zatwierdzenie transakcji nadal wykonują świeżą kontrolę. Limit pojedynczego wiersza pozostaje 16 MiB, a SQLite nadal ma twardy limit stron bazy. 1 MiB danych wejściowych nie jest limitem fizycznego zapisu ani gwarancją jego czasu.
- Worker próbuje odzyskać WAL wcześniej: przy mniejszej z wartości 64 MiB i jednej czwartej skonfigurowanego budżetu WAL. Ponowna próba po pięciu sekundach nie czeka na dawną minutową przerwę. TRUNCATE nadal wymaga ukończonego PASSIVE; aktywne odczyty pozostają chronione, a busy timeout wynosi zero.
- Każda całkowicie zaimportowana porcja Recorder zapisuje własne pokrycie przed następnym żądaniem. Po przerwaniu i restarcie pobieramy brakujący starszy zakres oraz krótki overlap. Nieukończona odpowiedź i pominięta porcja nie oznaczają pełnego pokrycia; istniejące rekordy pozostają w archiwum.
- Maksymalny konfigurowalny rozmiar bazy wzrasta z 8 do 64 GiB, WAL do 4 GiB, a rezerwy wolnego miejsca do 64 GiB. Dotychczasowe domyślne wartości 2048/128/2048 MiB pozostają. Panel pokazuje także limit WAL, rezerwę i miejsce zmiany konfiguracji.

## Ustawienia dysku

Home Assistant: **Ustawienia → Dodatki → Adaptive AI → Konfiguracja**. Wartości są podawane w MiB i obowiązują po zapisaniu oraz restarcie dodatku. Przy około 21 GiB wolnego miejsca widocznego w ostatnim eksporcie można zacząć od:

```yaml
storage_database_max_mb: 10240
storage_wal_max_mb: 512
storage_min_free_mb: 4096
```

Jeżeli system plików dodatku rzeczywiście pokazuje 44 GB wolnego miejsca, można ustawić bazę na 20480 MiB. Limit określa maksimum; aplikacja nie rezerwuje ani nie wypełnia od razu całego miejsca. Wybierz wartość względem `free_bytes` w Storage, ponieważ rozmiar całej karty i wolne miejsce w innym widoku mogą oznaczać inny zakres.

Przy 90% wykorzystania budżetu bazy lub połowie budżetu WAL zapis masowy i trening mogą być wstrzymane. Rezerwa chroni wolne miejsce niezależnie od limitu bazy. Retencja historii nadal wynosi do 10 dni i nie usuwa modeli ani ręcznych preferencji. Wyższy limit bazy nie gwarantuje przechowywania każdej liczby rekordów przez pełne 10 dni.

Po aktualizacji wznów przerwany trening. Nie kasuj bazy i nie wykonuj Full Rebuild wyłącznie z powodu tej poprawki. Zakończone zakresy zachowane przez wcześniejsze wydania są nadal używane; nie da się odtworzyć watermarków porcji, których wcześniejsza wersja nie zapisała.

## Weryfikacja i wynik pomiaru

Dwanaście nowych regresji sprawdza redukcję odczytów dysku bez zmiany danych, reakcję na presję w trakcie batcha i przed commit, limit wiersza, większą konfigurację, rzeczywiste odzyskanie WAL przed pauzą oraz trwałe pokrycie po przerwaniu i przy brakujących zakresach. Istniejące testy twardej pojemności bazy, rezerwy, odczytów przypiętych do WAL, retencji i historii również pozostają.

[Benchmark](benchmarks/STORAGE_IMPORT_0_14_180.json) porównuje zamrożoną ochronę 0.14.179 z bieżącym zapisem do rzeczywistego SQLite. Dla 8000 rekordów pełnego kontekstu, wraz z powtórnym upsert, mediana przyspieszenia komponentu wyniosła **6,86×**, a liczba pomiarów dysku spadła z 18111 do 630. Dla minimalnej historii wynik wyniósł **5,66×**. Wszystkie wiersze i pełne SHA-256 danych były identyczne. Porcje nadal mają najwyżej 128 rekordów.

To lokalny pomiar komponentu importu na Windows/Python 3.11, z wyłączonymi kooperacyjnymi pauzami. Nie oznacza takiego samego przyspieszenia całego treningu na karcie SD. Czas odpowiedzi Recorder, replay i współdzielone I/O trzeba zmierzyć w nowym logu instalacji.

Kontrola wydawnicza obejmuje 2157 testów na Pythonie 3.11 i 3.13, benchmarki, JavaScript, obraz dodatku i oba pakiety instalacyjne. Publikacja następuje po pomyślnej kontroli CI.
