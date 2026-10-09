# HomeMind 0.14.166

Import historii Recorder oddaje bazę innym zapisom po każdych najwyżej 128 wierszach. Przerwy budżetu treningu następują po commit i zwolnieniu Store.lock. Połączenie SQLite jest współdzielone tylko przez jeden wątek importu; transakcje pozostają niezależne.

## Co pokazał log

adaptive-ai-runtime-debug-20261010-014039.json z 0.14.165: CPU recent 99,98%, RSS 197,98 MiB, event→decision recent p95 210,76 ms z 21 próbek. Trwa agent_pool i pełny initial_model_build dla nowego agenta. Worker działa bez restartów, zapisy nie zgłaszają błędów SQLite ani overflow. Checkpoint remaining_frames=0; rozmiar fizycznego WAL nie jest liczbą zaległych zapisów.

Poprzednia poprawka działa: 233 lookupy w zachowanym fragmencie mają średnio 2,924 ms, przygotowanie zapytania SQLite 0,012 ms. W poprzednim logu było 13,835 i 10,714 ms. Okna mają inne obciążenie; nie porównujemy bezpośrednio całego CPU ani p95 decyzji.

Import Recorder wykonywał jeden archive_batch dla całej odpowiedzi. Pięć krytycznych zapisów w śladzie trwało 3,2–12,5 s, najdłuższy body 12,375 s. Zapis sensor_quality oczekiwał do 9,319 s. To długie zajęcie pisarza i Store.lock, nawet bez błędu database is locked.

## Zmiana i trwałość

Bufor parsera zawiera najwyżej 128 zachowanych wierszy. Każda partia zatwierdza się osobno; parser oraz checkpointy nie utrzymują transakcji. Store.archive_batch nie zmienia swojego atomowego kontraktu dla innych użytkowników, np. zapisu zdarzeń live.

Zachowujemy kolejność, wszystkie stany kategoryczne, dotychczasowy interwał próbkowania liczb i ostatni stan grupy, również na granicy partii. Reguły aktualizacji atrybutów, użytkownika, source i received_ts są identyczne. Każda partia ma trwałą mutation_revision; późna korekta nadal unieważnia odcinek cache historii.

Anulowanie lub błąd po commit pozostawia poprawny fragment w bazie. Nieudana partia się wycofuje; ponowienie odpowiedzi działa przez istniejący klucz UNIQUE(entity_id,ts). Nie potwierdzamy ukończenia importu przed zapisaniem całości. W diagnostyce history_archive_import raportuje tylko zatwierdzone wiersze, partie i max_batch_rows.

## Sprawdzenie i granice pomiaru

11 regresji: pełna zgodność dużej odpowiedzi, numeric sampling/final row, mieszane i błędne rekordy, UPSERT z live, możliwość zapisu i pełnego PASSIVE checkpointu w przerwie, anulowanie/ponowienie, rollback późniejszej partii, rewizje, stop/empty, diagnostyka częściowego zapisu oraz jedno połączenie zamykane także przy błędzie. Pełny zestaw 1976 testów.

Benchmark porównuje zamrożony parser 0.14.165 z nowym importem dla pełnej i minimalnej historii; każde pole archiwum i ID normalnego importu musi być zgodne. Trzy naprzemienne powtórzenia, Windows, 9600 pełnych wierszy: mediana szczytu archive_batch 97,432 → 7,757 ms; mediana czasu całości 106,966 → 117,671 ms. Pomiar wywołania obejmuje też przygotowanie danych i commit, nie tylko czas blokady. Przerwy budżetu są wyłączone w benchmarku. Benchmark zgodności działa także w CI.

Więcej commit i przerwy mogą wydłużyć ukończenie importu. Limit dotyczy liczby wierszy, nie bezwzględnego czasu na wolnej karcie SD. Celem jest dostępność bazy dla decyzji i innych zapisów. Obecny transport nadal materializuje JSON odpowiedzi HA. Nie zmieniamy modeli, wag, kwalifikacji Control ani treningu v26.

SQLite dopuszcza jednego pisarza w WAL, dlatego wielosekundowa transakcja opóźnia pozostałe zapisy: [dokumentacja SQLite WAL](https://www.sqlite.org/wal.html).

## Instalacja

HomeMind-Adaptive-AI-0.14.166-addon-root.zip lub repository.zip z SHA256. Aktualizacja i restart, bez Rebuild.
