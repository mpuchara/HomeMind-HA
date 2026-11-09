# HomeMind 0.14.234 — próba na Raspberry Pi 5 (WERSJA TESTOWA)

**Status:** Pi-test candidate, nie oficjalne wydanie. Gate D **BLOCKER** (9/13 par spełniło progi, przepustowość zbierania dowodów 2,36–2,61×). Pozostałe bramki wydania pozostają otwarte: Candidate Recovery, pełna integracja HA, migracja rzeczywistej instalacji, retention/rollback, ≥4 h soak, fizyczny Raspberry Pi/HA. Ta paczka nie potwierdza gotowości produkcyjnej ani bezpieczeństwa aktualizacji danych konkretnej instalacji.

## Zmiany związane z Recorder (pierwszy test Raspberry Pi)

Wersja 0.14.234 identyfikuje przyczyny błędów pobierania historii w polach `resource_guard.recorder_last_failure`: `category`, `http_status`, `exception_type`, `retryable`, `backoff`, `action`. Szczegóły nie zawierają URL, tokenów ani identyfikatorów encji. HTTP 400/401/403/404 wstrzymuje *automatyczne* odkrywanie do momentu ręcznego Rescan po rozwiązaniu problemu z HA; błędy sieciowe i przeciążenia zachowują 120-sekundowy backoff. Lokalny limit rozmiaru odpowiedzi (`HA response too large; split the request`) oraz HTTP 413/414 powodują natychmiastowy podział żądania, bez zbędnej pauzy. Jeśli błąd nie znika, odczytaj `resource_guard.recorder_last_failure` i szczegóły zdarzenia `history_background_backoff` lub `history_background_unavailable` (bez sekretów). Nie ma potwierdzenia, że to naprawia nieznaną pierwotną przyczynę na Twojej instalacji.

## Co zawiera paczka

Branch `test/pi-0.14.234` oparty o reporting HEAD PR #369 `4e31467b38957c3379757ef7bb45ea69f4a5f44a`. Punktem wyjścia był ten sam runtime co zachowany `9c245577727c93ede3c59100f11844bf336acb1a` (tree `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`), z aktualizacją numeru wersji w `src/settings.py` i etykiety oraz kluczy cache JS/CSS w `src/static/index.html`. Nie przywracamy wycofanych eksperymentów. `config.yaml`, `Dockerfile`, `BUILD_INFO.json` mają wersję 0.14.234.

GitHub Actions `Validate HomeMind` po pełnym SUCCESS obu Pythonów, image smoke i emulowanego build/smoke ARM64 przygotowuje **artefakt do ręcznej instalacji**, nie GitHub Release:
- `HomeMind-HA-0.14.234-repository.zip`: rozpakuj do katalogu /addons (zawiera `adaptive_ai/`).
- `HomeMind-HA-0.14.234-addon.zip`: rozpakuj do katalogu istniejącego add-onu `/addons/adaptive_ai` (zawiera `config.yaml`, `src/`).
- Obydwa pliki mają `.zip.sha256`, wewnętrzny `SOURCE_COMMIT.txt` i `MANIFEST.sha256`. Wymagane jest potwierdzenie dokładnego SHA uruchomienia CI.

**Nie instaluj obu paczek na siebie.** Wybierz jedną zgodnie z aktualnym układem lokalnego add-onu. Nie rozpakowuj dodatku do katalogu danych `/data`.

## Przed uruchomieniem na Pi

1. Użyj **oddzielnej testowej instalacji HA z przywróconą kopią danych**. Przed pierwszym startem odetnij dostęp do produkcyjnego HA, fizycznych urządzeń i ich bramek. Wyłącz w klonie automatyzacje i integracje wykonawcze. Nie wykonuj pierwszej migracji na jedynym działającym systemie domu.
2. Zachowaj kopię oryginalnego add-onu i wszystkie dane, opcje oraz backup Home Assistant. Zatrzymaj dodatek i zapisujące go procesy przed kopiowaniem bazy; skopiuj też SQLite WAL/SHM, modele, historię i metadane. Zweryfikuj, że potrafisz odtworzyć backup.
3. Zapisz liczbę i ID agentów, tryby Control/Shadow/Paused, modele, generacje Candidate, historię Current/Desired, Correct/Undo i kolejkę treningów. Nie resetuj modeli ani danych.
4. W oddzielnej kopii testowej pozostaw sterowanie fizyczne wyłączone/odizolowane, a istniejące agenty sterujące ustaw w Shadow/Paused **przed testem**, jeżeli nie masz bezpiecznie odciętego HA executora. Nie polegaj wyłącznie na wyłączeniu Universal Collectora jako blokadzie Control.
5. Zapisz wersję, SHA i konfigurację poprzedniego dodatku. **Nie zmieniaj ręcznie slug `adaptive_ai`**, nie odinstalowuj istniejącego dodatku.

## Ręczna instalacja testowa

1. Zaloguj się w GitHubie. Z [PR #370](https://github.com/mpuchara/HomeMind-HA/pull/370) otwórz udane uruchomienie typu push workflow `Validate HomeMind` dla gałęzi `test/pi-0.14.234`, pobierz `HomeMind-HA-0.14.234-Pi-TEST-<source sha>` z sekcji Artifacts. Artefakt jest przechowywany przez 14 dni.
2. Sprawdź `sha256sum -c HomeMind-HA-0.14.234-addon.zip.sha256` (lub analogiczny plik repository). Rozpakuj wybrany ZIP w katalogu tymczasowym, sprawdź `sha256sum -c MANIFEST.sha256` z jego katalogu głównego i porównaj `SOURCE_COMMIT.txt` z pełnym SHA uruchomienia. W Windows możesz porównać wynik `Get-FileHash -Algorithm SHA256 <ścieżka-do-ZIP>` z plikiem `.zip.sha256`.
3. Zatrzymaj dodatek. Zastąp katalog jego źródeł `/addons/adaptive_ai` zweryfikowanym katalogiem z paczki, zachowując oryginał poza `/addons`. W paczce repository użyj podkatalogu `adaptive_ai/`, a w paczce addon użyj plików z głównego poziomu ZIP. Trwały katalog danych pozostaw na miejscu.
4. W HA Supervisor odśwież lokalne dodatki i wybierz **Rebuild / Przebuduj**. Jeśli używasz Raspberry Pi OS z 32-bitowym HA, przerwij: manifest wspiera `aarch64`, nie `armv7`/32-bit.
5. Uruchom dodatek tylko na kopii testowej. Sprawdź wersję 0.14.234, poprawny startup i integralność modeli oraz trybów. Domyślne `universal_observer_enabled`, `universal_action_observer_enabled`, `universal_training_collector_enabled` muszą pozostać **false** (sprawdź rzeczywiste opcje zapisane w Supervisor; wcześniejsze true nie są resetowane przez nowy domyślny manifest).

## Plan pomiaru Raspberry Pi

Na Raspberry Pi 5 z 16 GB RAM i NVMe odnotuj wersje HA/OS, pełne SHA, liczbę agentów i czujników oraz ustawienia użyte w klonie. W każdym etapie utrzymaj izolację od fizycznych urządzeń i zachowaj budżety treningu oraz Collectora.

1. **Start i 15 min pracy bazowej.** Zapisz czas gotowości i czas odświeżania panelu agentów, Current/Desired, historii Correct i widoku Candidate. Porównaj inwentarz modeli, generacji, historii oraz kolejki z kopią sprzed instalacji.
2. **Shadow z powtarzalnym strumieniem zdarzeń.** Użyj encji syntetycznych i zapisz tempo wejścia, liczbę próbek oraz p50/p95/p99 oddzielnie dla zdarzenia → odbiór HA i zdarzenia → intent. Zestaw znaczniki tej samej próbki na wspólnym zegarze. Próbki bez intentu lub ze zgubionym zdarzeniem policz osobno. Zapisz opóźnienia UI z zakładki Network i liczbę błędów/rozłączeń HA.
3. **Candidate i trening pod obciążeniem.** Powtórz ten sam strumień, zwiększając tempo stopniowo. Zapisz kolejkę, ukończone treningi, czas oczekiwania i wykonania, CPU i szczytowy RSS workera, stan generacji Candidate i opóźnienia Shadow. Zanotuj timeouty, odrzucone zdarzenia i ponowienia.
4. **Restart i stabilność.** Wykonaj kontrolowane zatrzymanie i restart, zmierz gotowość, porównaj zachowane modele, Candidate, historię Correct/Undo, opcje i stan kolejki. Osobno przeprowadź test ciągły co najmniej cztery godziny na kopii.

Zbieraj co 5 s czas UTC, CPU i RSS procesu oraz workerów oddzielnie, `MemAvailable`, temperaturę, stan throttlingu i rozmiar SQLite/WAL. Gdzie narzędzia hosta są dostępne, użyj `pidstat -u -r -p <PID-y> 5`, `/proc/meminfo`, `vcgencmd measure_temp` i `vcgencmd get_throttled`. Zapisz także całkowite CPU/RAM dodatku z Supervisor i zajętość NVMe. Do odczytu rozmiarów plików wystarczy `stat`, a kontrolę integralności SQLite wykonuj na spójnej kopii. Brakujące pomiary oznacz w raporcie.

Udostępnij jedynie zanonimizowane zestawienie CSV/JSON z etapem, czasem względnym, licznikami, rozkładami i metrykami zasobów. Użyj syntetycznych aliasów agentów. Przed udostępnieniem usuń tokeny, nagłówki uwierzytelnienia, prywatne adresy, dane zdarzeń i identyfikatory encji. Surowe logi i pliki HAR pozostaw lokalnie. Gate D pozostaje osobną bramką.

## Jeżeli coś się nie uda

Zatrzymaj test, zachowaj osobne logi i spójną kopię danych po awarii. **Nie wykonuj Rebuild modeli, Clear Learning ani resetów.** Sam downgrade źródeł 0.14.234 do poprzednich NIE zapewnia zgodności bazy.

Aby bezpiecznie odtworzyć poprzedni stan, przywróć jednocześnie **poprzednie źródła, cały katalog trwałych danych i dokładne opcje** z jednego sprawdzonego backupu. Odbuduj obraz poprzedniej wersji, uruchom go w izolacji i zweryfikuj gotowość oraz zgodność inwentarza. Przy problemie z rozruchem sprawdź architekturę, log budowania i log dodatku, wolne miejsce, wersję z /api/status i etykietę w UI. Zobacz `docs/FINAL_RELEASE_UPGRADE_AND_ROLLBACK.md` i `docs/FINAL_RELEASE_MIGRATION.md`.
