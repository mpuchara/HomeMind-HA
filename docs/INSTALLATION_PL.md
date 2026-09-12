# Instalacja i aktualizacja Adaptive AI

Dokument dotyczy **Adaptive AI 0.7.12**.

## Wymagania

Adaptive AI jest aplikacją Home Assistant Supervisor i jest przeznaczony przede wszystkim dla **Home Assistant OS**. Home Assistant udostępnia panel Apps właśnie w tej metodzie instalacji.

Wymagane:

- Home Assistant OS,
- architektura `aarch64` lub `amd64`,
- działający Recorder i historia encji,
- dostęp Home Assistant do sterowanych urządzeń,
- wolne miejsce na lokalną bazę historii Adaptive AI.

Nie jest wymagany żaden zewnętrzny klucz API, konto AI ani połączenie z chmurą HomeMind.

## Instalacja z GitHub

Repozytorium:

`https://github.com/mpuchara/HomeMind-HA`

1. W Home Assistant przejdź do **Settings → Apps**.
2. Wybierz **Install app**.
3. Otwórz menu `⋮` w prawym górnym rogu.
4. Wybierz **Repositories**.
5. Dodaj adres:
   `https://github.com/mpuchara/HomeMind-HA`
6. Zamknij listę repozytoriów i odśwież App Store, jeśli karta nie pojawi się od razu.
7. Otwórz **Adaptive AI**.
8. Wybierz **Install**.
9. Po instalacji uruchom aplikację.
10. Opcjonalnie włącz **Show in sidebar**.

Jeżeli repozytorium nie pojawia się w sklepie, sprawdź **Settings → System → Logs → Supervisor**.

## Pierwsze uruchomienie

Pierwsze uruchomienie może być znacznie cięższe niż normalna praca. Aplikacja:

1. łączy się z Home Assistant,
2. pobiera bieżące stany encji,
3. skanuje automatyzacje,
4. importuje historię Recorder,
5. tworzy własne lokalne archiwum,
6. wykrywa aktywnie używane urządzenia sterowalne,
7. tworzy agentów,
8. przeprowadza historyczny trening i benchmark.

Domyślny bootstrap Recorder obejmuje **10 dni** (`history_bootstrap_days: 10`). Lokalna baza może przechowywać dane dłużej, domyślnie do **365 dni**, ale aplikacja nie odzyska historii, którą Recorder wcześniej usunął.

Podczas pierwszej indeksacji wyższe użycie CPU i I/O jest normalne.

## Aktualizacja

Aby zachować bazę danych, modele i historię:

1. **nie odinstalowuj** Adaptive AI,
2. wykonaj backup aplikacji przed większą aktualizacją,
3. przejdź do **Settings → Apps → Install app**,
4. użyj `⋮ → Check for updates`,
5. otwórz kartę Adaptive AI i wybierz **Update**,
6. po aktualizacji uruchom aplikację i sprawdź logi.

Dane robocze są przechowywane w przestrzeni `/data` aplikacji, m.in. w bazie `adaptive_ai.db`. Odinstalowanie aplikacji może spowodować utratę tej historii zależnie od sposobu usunięcia/backupów.

## Co dzieje się po zmianie wersji modelu

Nie każda aktualizacja wymaga ponownego importu całego Recordera. Gdy zmienia się `TRAINING_REVISION`, Adaptive AI może:

- zachować lokalne surowe archiwum,
- ponownie dobrać cechy,
- przebudować polityki,
- wykonać szersze odświeżenie kandydatów z Recorder.

Wersja 0.7.12 celowo wykonuje szerokie sprawdzenie dopuszczonych encji, ponieważ zmieniła się zasada doboru kontekstu.

## Instalacja lokalna dla developmentu

Repozytorium z GitHub jest zalecanym sposobem użytkowania. Do developmentu można użyć katalogu `/addons`, ale taka instalacja jest oddzielną aplikacją i ma oddzielną przestrzeń danych.

Nie uruchamiaj dwóch instancji Adaptive AI w trybie Control dla tych samych urządzeń.
