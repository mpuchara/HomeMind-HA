# HomeMind Adaptive AI 0.14.152

Log `adaptive-ai-runtime-debug-20261009-081002.json` potwierdza działanie poprawki 0.14.151: 138 odczytów z cache, jedna odbudowa i brak powtarzalnego odtwarzania kandydatów. Nadal występują długie przerwy: najwolniejsza obserwacja kontekstu 24,17 s, cały przebieg agenta 24,83 s; ostatnie p95 zdarzenie → decyzja 20,04 s i housekeeping 15,86 s. CPU w ostatnim oknie 73%. W śladach samo predict zajmuje średnio 2,12 ms, maksymalnie 6,09 ms. Nie trwał trening ani ciężkie zadanie; połączenie HA było prawidłowe. To agent Shadow, więc nie jest to opóźnienie fizycznego wywołania usługi HA.

W najdłuższej obserwacji po gotowej decyzji występują wielosekundowe przerwy między etapami scoringu oraz przed wymuszonym zapisem modeli. Dotychczasowy ślad nie rozdzielał oczekiwania na blokadę Store od samego SQLite. Analiza kodu wykazała blokadę modelu domu trzymaną podczas checkpointu, zapisy puli i jakości w wątku decyzji, wymuszony flush kandydatów po etykiecie oraz blokadę bufora historii trzymaną przez commit. Poprawka usuwa te konkretne zależności; nie przypisuje całych 24 sekund jednej zmierzonej operacji.

## Zmiany

- Model domu jest odłączany pod krótką blokadą stanu, a zapisywany poza nią. Osobna blokada serializuje checkpointy. Obserwacje przyjęte podczas zapisu pozostają w aktualnym modelu i trafiają do kolejnego checkpointu.
- Pula sensorów i jakość mają najnowszy pełny snapshot dla pary agent/encja. Łączenie zapisów zachowuje wszystkie skumulowane liczniki i ograniczone historyczne listy przykładów; nie łączy samych delt ani nie usuwa krótkich wizyt. Zwykły tick licznika korzysta z już przygotowanego JSON historii.
- Wspólny istniejący writer zapisuje te snapshoty oraz modele kandydatów. Błąd odtwarza oczekującą partię; nowszy snapshot wygrywa. Dwa flush nie mogą zapisać starszego stanu po nowszym. Awaria jednej tabeli nie pomija pozostałych.
- Parowane przyszłe wyniki nadal są oceniane i uczone w tej samej kolejności w RAM. Nowa etykieta budzi writer zamiast czekać na niego w wątku decyzji. Weryfikacja checksum, odrzucanie własnych poleceń, kontrola epok i reguły promocji nie są osłabione.
- Zapis historii Current/Desired zwalnia blokadę bufora przed SQLite. Nowe rekordy powstają podczas commit; po błędzie wraca kolejność chronologiczna, a ewentualne przepełnienie zachowuje najnowsze rekordy i licznik odrzuconych.
- Runtime Debug eksportuje liczniki oczekujących/zapisanych snapshotów i błędów, a `context_rows_persist` oraz `context_shadow_persist` mierzą zapis wraz z oczekiwaniem na Store. Przy zwykłym restarcie występuje jawna bariera zapisu po zatrzymaniu producentów danych.

## Weryfikacja i ograniczenia

16 nowych regresji obejmuje zablokowany Store i rzeczywistego writera SQLite, zachowanie etykiet i krótkich wizyt, odłączone dane, restart, kolejność równoległych flush, rollback całej transakcji, retry z nowszym snapshotem, shutdown, niezależność tabel, nowe obserwacje podczas checkpointu, rejestrowanie Current/Desired podczas commit, przepełnienie po błędzie, diagnostykę bez dostępu do bazy, poprawne zliczanie faktycznie obserwowanych encji oraz oczekiwanie wymuszonego flush historii na zajęty Store. Pełny zestaw: 1773 uruchomionych testów.

Benchmark deweloperski z rzeczywistym kontraktem cech i TemporalHistory używa 96 sensorów po 96 próbek, 256 przykładów klasyfikatora oraz 20 ocen po rozgrzaniu i starzeniu wag. Bez dodatkowej blokady średni czas obserwacji wynosi 55,42 → 43,24 ms, p95 60,05 → 46,05 ms. Przy obcej blokadzie Store na 200 ms w każdym przebiegu: średnia 276,07 → 44,41 ms i p95 309,27 → 48,65 ms. CPU uwzględnia końcowy zapis oczekujących modeli i wierszy; bez blokady 1000 → 906 ms, z blokadą 1328 → 922 ms. W obu wersjach po rozgrzaniu powstaje zero nowych kandydatów. To test konkretnej zależności od wolnego zapisu na komputerze deweloperskim, nie gwarancja takiej redukcji całego opóźnienia na urządzeniu HA.

Nominalny okres zapisu w tle to 5 s, z dodatkowym szybkim pobudzeniem po wyniku i flush przy shutdown. Wolna lub niedostępna baza może wydłużyć ten czas. Nagła utrata zasilania może utracić niezapisane obserwacje kontekstu/Shadow; potwierdzone transakcje zachowują dotychczasowy format. Zmiany schematu, promocje i pozostałe zapisy mają swoje istniejące granice trwałości. Nadal pozostają koszty weryfikacji kandydatów, uczenia i innych etapów; kolejny log pozwoli ocenić rzeczywiste opóźnienia. Ta wersja nie dowodzi przewagi jakości predykcji nad automatyzacją.

## Instalacja

Aktualizacja i restart dodatku wystarczą. Format modeli JSON i rewizja treningu `shared-home-intents-v26` pozostają zgodne; Rebuild nie jest wymagany. Pakiet ręcznej instalacji: `HomeMind-Adaptive-AI-0.14.152-addon-root.zip`. Sumy SHA256 są publikowane obok ZIP.
