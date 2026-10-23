# Zabezpieczenie i diagnostyka bazy po SQLITE_CORRUPT

Logi potwierdzają „database disk image is malformed” podczas zapisu audytu provenance. Bez pliku bazy nie wiadomo jeszcze, czy uszkodzony jest tylko indeks, tabela, czy inne strony. Same eksporty JSON nie zawierają pliku bazy do naprawy. Nie należy zakładać, że winna jest karta albo zanik zasilania: te logi nie rozstrzygają przyczyny.

## 1. Zabezpiecz aktualne dane

Zatrzymaj dodatek Adaptive AI w Home Assistant. Dopiero po zatrzymaniu wykonaj ręczną kopię z danymi tego dodatku: Ustawienia > System > Kopie zapasowe > utworzenie ręcznej kopii, zaznacz Adaptive AI. Pobierz kopię na komputer. Nie usuwaj ani nie przywracaj obecnej instalacji. Jeśli kopia jest szyfrowana, zachowaj zestaw awaryjny/klucz. [Oficjalna instrukcja kopii i pobierania](https://www.home-assistant.io/common-tasks/general/#creating-a-manual-backup).

Ten dodatek ma backup: hot. Dlatego zatrzymanie przed wykonaniem kopii jest tu celowe: źródłowe pliki nie powinny zmieniać się podczas zabezpieczania. Jeśli watchdog sam ponownie go uruchamia, dopilnuj, żeby pozostał zatrzymany do zakończenia kopii. Przy ręcznym dostępie do plików zachowaj cały prywatny katalog danych, w tym adaptive_ai.db, towarzyszące adaptive_ai.db-wal i adaptive_ai.db-shm, jeżeli istnieją, oraz konfigurację. Nie kasuj WAL/SHM ani nie kopiuj samego .db z pracującej aplikacji.

Pobrana kopia ma zabezpieczyć obecny stan, również jego uszkodzenie. Nie jest jeszcze kopią potwierdzoną jako zdrowa. Zachowaj ją bez zmian; do analizy rozpakuj drugą kopię folderu danych na komputerze. Nie trzeba przesyłać wielogigabajtowej bazy, aby uzyskać pierwszy raport.

## 2. Sprawdź rozpakowaną kopię tylko do odczytu

W repozytorium .227 uruchom lokalnym Pythonem 3.11+ (ścieżkę kopii zastąp własną):

```powershell
python tools/check_database_health.py --database-copy 'C:/kopie/AdaptiveAI/data/adaptive_ai.db'
python tools/check_database_health.py --database-copy 'C:/kopie/AdaptiveAI/data/adaptive_ai.db' --full
```

Uruchamiaj na roboczej kopii rozpakowanych danych; obecne pliki WAL/SHM pozostaw obok .db z tymi samymi nazwami. Pierwsze polecenie robi quick_check, drugie pełny integrity_check, obejmujący także relacje indeksów. Oba mogą potrwać przy bazie około 14 GB. Kod wyjścia 0 i ok=true oznacza brak wykrytych błędów danej kontroli; kod 2 oznacza błąd lub raport niepoprawności. Wynik JSON można wkleić do dalszej analizy. [Zakres kontroli SQLite](https://www.sqlite.org/pragma.html#pragma_integrity_check).

Nie uruchamiaj Rebuild model, VACUUM, REINDEX, .recover ani kasowania tabel na oryginale. Odzyskiwanie może zmienić lub utracić dane i wymaga walidacji modeli, generacji oraz komend przed podmianą. [Ograniczenia odzyskiwania SQLite](https://www.sqlite.org/recovery.html#limitations).

## 3. Wersja .227

Instalacja .227 zachowuje dane i ogranicza wpływ uszkodzonego audytu na odbiór stanów HA. Nie naprawia bazy i nie zastępuje powyższej kopii. Przyjęte zdarzenia w RAM nie przetrwają zatrzymania procesu. Dalszy stan historii i innych tabel wymaga oceny raportu kontroli.
