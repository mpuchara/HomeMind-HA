# 0.14.220: RAM przy przygotowaniu treningu

Wersja rozwija 0.14.219. Końcowa walidacja w toku. Sprzęt potwierdzony przez użytkownika: Raspberry Pi 4, 8 GB RAM, microSD 64 GB.

Trzy eksporty 0.14.219 pokazują trening przed uruchomieniem workera modelu: import Recorder, worker idle i 0 MB RSS workera. Ten sam agent wielokrotnie wraca do kolejki po `database_budget`, przy około 12,58 GB wykorzystania z limitu 13,98 GB. Progi są liczone od przydziału bazy, a nie od całej karty. Faktyczna konfiguracja przydziału to 70%. Kompaktowe dane i hashe eksportów są w `benchmarks/PERFORMANCE_PLAN_0_14_220_INPUTS.json`.

## Zmiany

- Przy dostępnym RAM jawny trening może buforować do 1024 przygotowanych wierszy / 1 MiB wejścia na transakcję, zamiast 128 wierszy. JSON atrybutów jest kodowany raz, przed wejściem do Store. Samotny duży wiersz nadal podlega zwykłemu limitowi payloadu i rollback. Mała lub nieznana dostępna pamięć zachowuje profil 128 wierszy. Import discovery zachowuje dotychczasowe ustawienia.
- Limit RSS procesu przygotowującego historię skaluje się do headroom i limitu cgroup. Domyślny sufit to 1536 MiB, do 20% znanego RAM i części dostępnej pamięci po rezerwie 512 MiB. Brak informacji o pamięci zachowuje dawny sufit 500 MiB. To limit, nie rezerwacja ani oczekiwane zużycie.
- Przy odmowie `database_budget` pobrana odpowiedź i bieżąca porcja mogą pozostać w RAM przez maksymalnie 300 s. Oczekiwanie nie trzyma Store.lock ani transakcji. Automatyczny cleanup może zwolnić miejsce. Ponowienie wymaga zejścia poniżej 85% limitu bazy oraz 25% limitu WAL. Coverage jest publikowane dopiero po zakończonym, trwale zapisanym oknie Recorder.
- Po wyczerpaniu czasu zadanie wraca do kolejki. Ponowny start po blokadzie bazy ma 5 s cooldown i próg wznowienia poniżej 85%, zamiast restartować import przy 89,99%. Anulowanie pozostaje dostępne. Nie zmienia to progów zapisu, limitów dysku, retencji historii, modeli ani zabezpieczeń Control.

Nowe opcje dodatku: `training_preparation_memory_limit_mb` (256–2048, domyślnie 1536), `training_recorder_batch_rows` (128–1024, domyślnie 1024), `training_recorder_capacity_wait_seconds` (0–900, domyślnie 300; 0 wyłącza czekanie na pojemność). Są to opcje wydajności i nie unieważniają modeli. Efektywny profil jest w eksporcie `history.training_preparation_profile`, a oczekiwanie kolejki w `training_queue.storage_retry`.

## Pomiary i ograniczenia

Pięć naprzemiennych par na zdrowej lokalnej bazie, po 8000 wierszy: 63 → 8 transakcji, identyczny checksum całej historii. Mediany 171,34 → 153,83 ms, około 10,2% krócej. Najdłuższy zapis porcji w tych próbach: 15,79 ms dla profilu 128 oraz 20,93 ms dla 1024. Większy bufor może wydłużyć pojedyncze posiadanie writera; testy hostowe nie ustalają jego czasu na microSD. Pomiar wyłącza pauzy QoS i nie dowodzi przyspieszenia całego treningu ani sterowania na Pi. Narzędzie: `tools/benchmark_training_preparation_ram.py`; surowe pary są w raporcie benchmarku.

RAM nie zastępuje trwałego archiwum. Jeżeli cleanup nie może usunąć wystarczająco dużo zbędnej diagnostyki, trening nadal bezpiecznie czeka. Nie usuwamy chronionej historii ani nie zwiększamy automatycznie budżetu dysku, by wykazać postęp.

Aktualizacja nie wymaga Full Rebuild modeli. Instalacja z ZIP wymaga podmiany źródeł tego samego lokalnego dodatku i przebudowy obrazu. Przed instalacją wykonaj backup dodatku z danymi.
