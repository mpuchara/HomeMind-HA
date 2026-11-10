# HomeMind 0.14.235 — test Raspberry Pi 5 / Home Assistant (TEST ONLY)

Wydanie z gałęzi `test/pi-0.14.235` na bazie 0.14.234. **Nie instaluj na jedynym produkcyjnym HA bez pełnego backupu.** Gate D pozostaje BLOCKER. Przygotuj klon systemu z kopiami dodatku, konfiguracji i baz; początkowo testuj bez Control.

1. Pobierz artefakt `HomeMind-HA-0.14.235-Pi-TEST-<SHA>` z udanego **push** workflow `Validate HomeMind`. ZIP zawiera `HomeMind-HA-0.14.235-addon.zip` i `HomeMind-HA-0.14.235-repository.zip`, sumy SHA-256 i pliki `SOURCE_COMMIT.txt` / `MANIFEST.sha256`. Sprawdź sumy i zgodność SHA z commitem gałęzi przed instalacją.
2. Zrób spójną migawkę całego HA i danych HomeMind, w tym `rl_models`, SQLite, historii, agentów, Candidate i Correct/Undo. Przetestuj przywracanie na kopii.
3. Zainstaluj wyłącznie na kopii i uruchom dodatek. Zapisz godzinę startu, responsywność UI, stan Current/Desired, liczby agentów i generacje Candidate.
4. Obserwuj discovery, `resource_guard.recorder_last_failure` i `discovery_recorder_coverage_v1` (meta bazy HomeMind). Przy HTTP 500 zbierz zanonimizowany traceback HA Core z tego samego czasu; nie wystarczy sam status HTTP.
5. Przerwij pojedynczy skan testowy po 1–2 zatwierdzonych fragmentach. Sprawdź, że `initial_discovery_complete` nie powstał po częściowym skanie. Uruchom dodatek ponownie i sprawdź, że przyjęte fragmenty nie są pobierane ponownie, a wszystkie luki są odzyskiwane.
6. Po pełnym sukcesie sprawdź `discovery_deep_history_complete`, `initial_discovery_complete`, `manual_discovery_refresh`, liczbę agentów, tryby, aktualność Current i czy nie powstała fałszywa klasyfikacja. Brak agentów jest wynikiem poprawnym tylko przy kompletnej historii i rzeczywistej nieaktywności.
7. Zmierz HA event→intent p50/p95/p99, CPU/RSS/temperaturę/throttling Pi, zajętość SQLite/WAL, opóźnienia HTTP i stabilność WebSocket. Bez fizycznej próby nie ogłaszaj gotowości produkcyjnej.

**Rollback:** zatrzymaj dodatek, przywróć pełne pliki oraz dane ze spójnej kopii. Nie przeprowadzaj downgrade samych źródeł bez danych ani resetu modeli; nigdy nie czyść Recorder. W razie błędu zapisz logi, wersję, commit, `request_shape`, status i traceback po anonimizacji.

Ograniczenia/stan diagnozy: [Root cause](PI_RECORDER_HTTP500_ROOT_CAUSE.md) · [Recovery](PI_DISCOVERY_RECOVERY.md). Przetestowanie na prawdziwym Raspberry Pi i wyjaśnienie HTTP 500 pozostają **OPEN**.
