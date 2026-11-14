# Adaptive AI 0.15.11 — test Pi

Baza: 0.15.10 (589e0b215a83f664209be5da2cd2a9b8518b014e). Log z 14.11: watchdog zatrzymał przygotowanie po 600 sekundach bez zmiany ostatniego kursora history:weather.dom. Kod 0.15.10 nie raportował odrębnych faz budowy indeksów i quick_check, więc watchdog mógł przerwać poprawnie pracującą bazę po zakończeniu kopiowania. Nie ma jeszcze dowodu, że właśnie ta operacja trwała na fizycznym Pi.

- Finalizacja ma własne nazwy operacji oraz mierzalny licznik wykonanych kroków SQLite; zwykłe kopiowanie nadal wymaga postępu klucza. Limit 600 s pozostaje.
- Indeksy są zatwierdzane osobno; quick_check, spójność prefixu, Correct i atomowa publikacja pozostają.
- Bufor stron SQLite wykorzystuje limit RAM workera, nie limit odzyskiwania uszkodzonych rekordów.
- Naprawiono niezgodną wersję statycznego index.html z 0.15.10 (6 błędów CI).
- Nie skracamy zakresu historii, nie zmieniamy polityk ani danych użytkownika. Przed testem backup dodatku. Brak walidacji na fizycznym Pi.

## Wznowienie po awarii 0.15.10

Przy ponownym Train/Rebuild tego samego agenta można odzyskać zamrożony przedział poprzedniego zadania. Warunki: zachowany deskryptor i checkpoint v6, brak żywego workera i wyniku, zgodne opcje, agent, model, zakres encji, źródło oraz jego fence. Jeżeli któregokolwiek dowodu brakuje, przygotowanie zaczyna się na nowo. Nie rozszerzamy starego kursora o nową datę; późniejsza historia trafia do kolejnego treningu. Watchdog 0.15.11 pozostawia znacznik do jawnej ponownej próby, bez automatycznej pętli.
