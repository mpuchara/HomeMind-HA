# HomeMind 0.14.227

## Diagnoza z eksportów .226

Eksporty z 23.10.2026 06:20:16 i 06:21:44 czasu polskiego potwierdzają SQLITE_CORRUPT (kod 11) w provenance.flush_events_batch na wątku adaptive-ai-ha-events, ha-poll_0 i w writerze provenance. P95 ostatniego okna ha_state_to_ws_receive wynosi 109156 ms i 67058 ms, event_to_decision 929 ms i 346 ms. To różne granice pomiaru: opóźnienie znacznika HA nie jest samo w sobie dokładnym pomiarem fizycznego przełączenia, ale eksport zawiera również błędy SQL na wątku odbierającym HA.

Nieudane overflow flush powtarzają się ponad 24 tysiące razy. W kolejce pozostało 18468/19140 zdarzeń. Natywny błąd trwały trafia ponownie na ścieżkę synchronicznego flush po przekroczeniu progu 8192. To blokuje dostarczanie nowych stanów, zanim inference wykona szybką decyzję. Zwiększenie RAM nie usuwa uszkodzenia bazy. Budżet miejsca jest normalny, a wolne miejsce w eksporcie wynosi około 65 GB.

## Zmiany

Journal zatrzymuje ponawianie raw-event INSERT po natywnym SQLITE_CORRUPT/NOTADB. Przyjęta, nieudana paczka pozostaje w RAM. Nowe raw audit events nie są dokładane bez końca: zostają w ograniczonym cache do bieżącego rozpoznania origin i deduplikacji, a brak trwałego zapisu jest jawnie liczony z początkiem/końcem luki. To przerwa audytu, nie wymyślona historia urządzeń. Blokada nie znika automatycznie po timeout; potrzebna jest osobna diagnostyka offline bazy. RAM nie przetrwa restartu procesu.

Polling korzysta wyłącznie z cache provenance oraz istniejącego cache komend. Nie odpytuje historycznego audytu w ścieżce dostarczania stanów. Jawne odczyty historyczne zachowują odczyt z bazy. Obsługa SQLITE_BUSY, retry przejściowych awarii, istniejące limity dysku/WAL i synchroniczny zapis komend Control pozostają. Uszkodzenie cache recent-events przy starcie jest zgłaszane; błąd trwałej tabeli komend nadal przerywa start zamiast osłabiać rozpoznawanie own-command.

Diagnostics pokazuje uszkodzenie, zatrzymany zapis audytu, liczbę przyjętych zdarzeń w RAM i nowe luki. Status tej blokady odczytuje tylko RAM. Nowy tools/check_database_health.py wykonuje quick_check lub integrity_check na wskazanej kopii offline, przez mode=ro i query_only. Nie wykonuje REINDEX, VACUUM, odzyskiwania, usuwania ani podmiany bazy.

## Walidacja

Końcowa walidacja lokalna: **2787 testów OK / 531.081 s**, 72 testy celowane, entrypoint, UI, compileall i 22 skrypty JS. Manifest 648 plików jest zgodny z przetestowanym kodem. Exact-source GitHub CI passed on Python 3.11/3.13, required benchmarks and Docker build/start. Kontrolowany test starej .226 blokuje faktyczny handler HA przy zajętym Store.lock i uszkodzonym writerze; .227 kończy obsługę przed zwolnieniem blokady. Test sprawdza Current, kolejkę archive poll oraz zero wywołań HA service. To test hosta, nie pomiar szybkości na Pi ani odzyskanie jego bazy.

## Baza wymaga osobnego sprawdzenia

Użytkownik nie ma wcześniejszej kopii. Najpierw należy zabezpieczyć aktualny stan dodatku. Instrukcja docs/DIAGNOZA_BAZY_0_14_227_PL.md prowadzi do sprawdzenia kopii bez modyfikacji oryginału. Poprawka izoluje konkretną awarię raw audytu. Nie gwarantuje sprawności innych tabel i nie naprawia uszkodzonego pliku.

PR: https://github.com/mpuchara/HomeMind-HA/pull/343. CI źródła d21fe92f: https://github.com/mpuchara/HomeMind-HA/actions/runs/39901550327. Końcowy commit obejmuje raporty i metadane.
