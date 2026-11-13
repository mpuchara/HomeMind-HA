# Adaptive AI 0.15.10 — kandydat do testów Pi

Baza: 0.15.9, a55968432c297f3db0b03454a035be73ee7457e5 (nie main). Log z 13.11.2026: SQLITE_FULL podczas zapisu __training_copy_state w trakcie kopiowania entity_history. Poprzedni limit SQLite 512 MiB dotyczył całej normalnej generacji, niezależnie od wolnego miejsca.

- Limit normalnej generacji wynika teraz z rozmiaru archiwum, wolnego miejsca systemu plików i polityki StorageBudget; pozostaje rezerwa HA i zapas na journal. Limit odzyskiwania uszkodzeń dotyczy dodatkowych danych odzyskiwania.
- Zachowujemy pełny zakres historii i dowody Correct. Nie usuwamy archiwum, modeli ani checkpointów. Zachowujemy kontrakt v6 i atomowy zapis kursora z danymi.
- Strony historii do 512 rekordów zamiast 128, z redukcją przy dużym payloadzie. Metryki rozmiaru i limitu generacji, czasu throttlingu oraz jawna diagnoza SQLITE_FULL.
- Brak pomiaru fizycznego Pi i brak gwarancji przyspieszenia całego treningu. Przed instalacją wykonaj backup danych dodatku. Instalacja: rozpakuj ZIP, podmień wyłącznie katalog źródeł adaptive_ai, przebuduj dodatek; nie usuwaj /data. Po starcie sprawdź wersję i Resume/Rebuild jednego agenta, wynik Shadow, czas fazy przygotowania oraz wolne miejsce. Universal pozostaje domyślnie wyłączony.
