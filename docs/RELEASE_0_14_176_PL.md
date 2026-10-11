# HomeMind-HA 0.14.176: ochrona miejsca na dysku

Poprzednia poprawka ograniczała zatrzymany plik WAL, ale nie obejmowała całego cyklu przechowywania danych. W kodzie pełny trening miał domyślnie 7 dni, rzadka pamięć historyczna 35 dni, a archiwum 365 dni. Funkcja usuwania starego archiwum nie była wywoływana przez proces działający w tle. To nie zapewniało kontroli zajętości dysku.

## Nowe zasady

- Pełny trening, dodatkowa pamięć historyczna i surowe archiwum mają domyślnie 10 dni. Przy wczytywaniu konfiguracji stare wartości domyślne są migrowane, a okna ograniczane do maksymalnie 10 dni. Schemat opcji Supervisora przyjmuje wcześniejsze wartości podczas aktualizacji; wartości powyżej 10 są ograniczane przez aplikację.
- Sprzątanie działa automatycznie co minutę, w małych transakcjach. Obejmuje historię sensorów, decyzje, zdarzenia, pochodzenie danych, okna cech, obserwacje epizodów, próbki kandydatów i kalibracji, stare checkpointy oraz kopie modeli.
- Zachowujemy aktualne modele, konfigurację agentów, ręczne korekty, własność sterowania i trwałe metadane oceny. Nie usuwamy tego, czego model już się nauczył. Dla odtworzenia stanu na początku okna zostaje pojedynczy wcześniejszy stan sensora oraz jeden checkpoint domu.
- Usunięcie dowodów kalibracji i oceny kandydatów zwiększa ich rewizję, aby stary raport z pamięci podręcznej nie kwalifikował sterowania na podstawie usuniętych danych.

## Limity domyślne

| Zasób | Limit lub próg |
|---|---|
| Główna baza SQLite | 2048 MiB, natywny limit stron |
| WAL | próg wstrzymania zapisów 128 MiB; odzyskiwanie od mniejszej z wartości: 64 MiB lub połowa limitu WAL |
| Wolny dysk | 2048 MiB rezerwy dla Home Assistanta |
| Krytycznie mało miejsca | poniżej 256 MiB zatrzymujemy wszystkie nowe zapisy |
| Pliki transportowe treningu | 128 MiB na katalog |
| Pojedynczy plik JSON / parametry zapisu | 16 MiB |
| Log pojedynczego procesu treningu | 1 MiB; dalsze wyjście jest odbierane i odrzucane |

Limity bazy, WAL i rezerwy są edytowalne w konfiguracji dodatku. Przy 90% wykorzystania bazy, połowie limitu WAL albo naruszeniu rezerwy dysku wstrzymujemy dopisywanie masowych obserwacji i uruchamianie treningu. Małe zapisy ustawień i dziennika własności sterowania pozostają możliwe do progu krytycznego. Usuwanie starych danych może działać przy łagodnym wstrzymaniu. Nieznana ilość wolnego miejsca blokuje nowe zapisy. Odczyty pozostają dostępne. UI pokazuje stan ochrony, rozmiar bazy, WAL i wolny dysk.

Limit wieku nie jest limitem rozmiaru: intensywny radar może osiągnąć budżet wcześniej niż w 10 dni. W takim przypadku nagrywanie zostanie wstrzymane. WAL sprawdzamy przed i po zapisie oraz podczas długich instrukcji; pojedyncza transakcja może chwilowo przekroczyć próg. To ochrona wzrostu danych aplikacji, a nie globalny limit plików innych dodatków lub kopii zapasowych.

Nowe bazy mają incremental auto-vacuum, dzięki czemu usunięte strony można stopniowo oddawać systemowi plików. W istniejącej bazie wolne strony są ponownie używane. Nie uruchamiamy pełnego VACUUM wymagającego dodatkowej kopii bazy na potencjalnie pełnym dysku. SQLite nie pozwala ustawić limitu stron mniejszego niż istniejąca baza; dla takiej bazy obowiązuje wstrzymanie wzrostu, a natywny limit docelowy dotyczy nowych baz. Pliku WAL nie wolno ręcznie kasować: może zawierać jedyną kopię zatwierdzonych danych.

Jeżeli usunięcie dodatku skasowało jego dane, nowa instalacja wymaga ponownego nauczenia agentów z historii dostępnej w Home Assistant. Retencja tego dodatku nie zmienia retencji Recorder w HA.

## Walidacja

27 nowych testów obejmuje limit stron, brak wolnego miejsca, blokadę WAL przez czytelnika, odzyskanie zapisów, transakcje i kursory, zmianę presji w trakcie partii, retencję 10 dni, strefy czasowe, klucze obce, zachowanie ręcznych korekt, unieważnianie kalibracji, ograniczenie powtarzalnych błędów w logu Supervisora i limity plików/logów. Cały zestaw zawiera 2113 testów. Wydanie jest publikowane po walidacji CI i kontroli zgodności paczek z zatwierdzonym kodem.

Źródła zachowania SQLite: [max_page_count](https://www.sqlite.org/pragma.html#pragma_max_page_count), [auto_vacuum](https://www.sqlite.org/pragma.html#pragma_auto_vacuum).

Dodatkowa poprawka 0.14.176: próg odzyskiwania WAL jest dopasowany do ustawionego limitu, również przy starcie aplikacji. Regresja z blokującym czytelnikiem sprawdza odzyskanie przez rzeczywisty worker, a nie ręczne TRUNCATE w teście.
