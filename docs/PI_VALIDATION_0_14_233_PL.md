# HomeMind 0.14.233 — próba na Raspberry Pi 5 (WERSJA TESTOWA)

**Status:** Pi-test candidate, nie oficjalne wydanie. Gate D **BLOCKER** (9/13 par czasu/przyrostu CPU). Pozostałe bramki wydania pozostają otwarte: Candidate Recovery, pełna integracja HA, migracja rzeczywistej instalacji, retention/rollback, ≥4 h soak, fizyczny Raspberry Pi/HA. Ta paczka nie potwierdza gotowości produkcyjnej ani bezpieczeństwa aktualizacji danych konkretnej instalacji.

## Co zawiera paczka

Branch `test/pi-0.14.233` oparty o reporting HEAD PR #369 `4e31467b38957c3379757ef7bb45ea69f4a5f44a`. Kod runtime jest ten sam co zachowany `9c245577727c93ede3c59100f11844bf336acb1a` (tree `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`), poza aktualizacją `src/settings.py` do wersji 0.14.233. Nie przywracamy wycofanych eksperymentów. `config.yaml`, `Dockerfile`, `BUILD_INFO.json` mają wersję 0.14.233.

Github Actions `Validate HomeMind` po pełnym SUCCESS obu Pythonów, image smoke i emulowanego build/smoke ARM64 przygotowuje **artefakt do ręcznej instalacji**, nie GitHub Release:
- `HomeMind-HA-0.14.233-repository.zip`: rozpakuj do katalogu /addons (zawiera `adaptive_ai/`).
- `HomeMind-HA-0.14.233-addon.zip`: rozpakuj do katalogu istniejącego add-onu `/addons/adaptive_ai` (zawiera `config.yaml`, `src/`).
- Obydwa pliki mają `.zip.sha256`, wewnętrzny `SOURCE_COMMIT.txt` i `MANIFEST.sha256`. Wymagane jest potwierdzenie dokładnego SHA uruchomienia CI.

**Nie instaluj obu paczek na siebie.** Wybierz jedną zgodnie z aktualnym układem lokalnego add-onu. Nie rozpakowuj dodatku do katalogu danych `/data`.

## Przed uruchomieniem na Pi

1. Preferuj **oddzielną testową instalację HA z przywróconą kopią danych**, bez podłączonych urządzeń wykonawczych. Nie wykonuj pierwszej migracji na jedynym działającym systemie domu.
2. Zachowaj kopię oryginalnego add-onu i wszystkie dane, opcje oraz backup Home Assistant. Zatrzymaj dodatek i zapisujące go procesy przed kopiowaniem bazy; skopiuj też SQLite WAL/SHM, modele, historię i metadane. Zweryfikuj, że potrafisz odtworzyć backup.
3. Zapisz liczbę i ID agentów, tryby Control/Shadow/Paused, modele, generacje Candidate, historię Current/Desired, Correct/Undo i kolejkę treningów. Nie resetuj modeli ani danych.
4. W oddzielnej kopii testowej pozostaw sterowanie fizyczne wyłączone/odizolowane, a istniejące agenty sterujące ustaw w Shadow/Paused **przed testem**, jeżeli nie masz bezpiecznie odciętego HA executora. Nie polegaj wyłącznie na wyłączeniu Universal Collectora jako blokadzie Control.
5. Zapisz wersję, SHA i konfigurację poprzedniego dodatku. **Nie zmieniaj ręcznie slug `adaptive_ai`**, nie odinstalowuj istniejącego dodatku.

## Ręczna instalacja testowa

1. Otwórz udany workflow `Validate HomeMind` dla gałęzi `test/pi-0.14.233`, pobierz `HomeMind-HA-0.14.233-Pi-TEST-<source sha>` z sekcji Artifacts. To NIE jest release GitHub.
2. Sprawdź `sha256sum -c HomeMind-HA-0.14.233-addon.zip.sha256` (lub analogiczny plik repository). Sprawdź `SOURCE_COMMIT.txt` i manifest po rozpakowaniu.
3. Rozpakuj wybrany ZIP we właściwym folderze źródeł lokalnego dodatku HA, zastępując pliki kodu, lecz **nie ruszając trwałego katalogu danych**. Zachowaj osobną kopię oryginalnych źródeł.
4. W HA Supervisor odśwież lokalne dodatki i wybierz **Rebuild / Przebuduj**. Jeśli używasz Raspberry Pi OS z 32-bitowym HA, przerwij: manifest wspiera `aarch64`, nie `armv7`/32-bit.
5. Uruchom dodatek tylko na kopii testowej. Sprawdź wersję 0.14.233, poprawny startup i integralność modeli oraz trybów. Domyślne `universal_observer_enabled`, `universal_action_observer_enabled`, `universal_training_collector_enabled` muszą pozostać **false** (sprawdź rzeczywiste opcje zapisane w Supervisor; wcześniejsze true nie są resetowane przez nowy domyślny manifest).

## Plan pomiaru Raspberry Pi

Zacznij od 10–15 minut bez dodatkowego Collectora: stabilność UI i połączenia HA, CPU/RSS i pamięć dostępna, czas event→intent p95/p99, status workerów, kolejka treningów, Current/Desired, liczba agentów. Zapisz temperaturę/thermal throttling i wykorzystanie dysku/WAL. Następnie przeprowadź obciążenie z aktywnymi Candidate i treningiem w izolowanym HA, z pomiarem p95/p99 i interwałów. Trzymaj źródła i konfigurację stałe.

Włączaj funkcje Universal osobno i wyłącznie w izolowanym środowisku, z zachowaniem oryginalnych budżetów. Zbieraj anonimowe raporty bez tokenów, nazw rzeczywistych urządzeń i danych osobistych. **Nie włączaj fizycznego Control na podstawie samego sukcesu syntetycznego testu.** Po krótkich pomiarach zaplanuj osobny test soak ≥14 400 s oraz migrację rzeczywistych danych na kopii.

Zapisz dla każdego scenariusza: wersja Pi/OS/HA, dokładny `SOURCE_COMMIT`, temperatura, CPU/RSS i MemAvailable, obciążenie HA, p50/p95/p99, oryginalne decyzje, drop/error, Collector queue, StorageBudget/WAL i timestamp start/koniec. Gate D nie jest zamykany automatycznie na podstawie wyników Pi.

## Jeżeli coś się nie uda

Zatrzymaj test, zachowaj osobne logi i spójną kopię danych po awarii. **Nie wykonuj Rebuild modeli, Clear Learning ani resetów.** Sam downgrade źródeł 0.14.233 do poprzednich NIE zapewnia zgodności bazy.

Aby bezpiecznie odtworzyć poprzedni stan, przywróć jednocześnie **poprzednie źródła, cały katalog trwałych danych i dokładne opcje** z jednego sprawdzonego backupu. Zobacz `docs/FINAL_RELEASE_UPGRADE_AND_ROLLBACK.md` i `docs/FINAL_RELEASE_MIGRATION.md`.
