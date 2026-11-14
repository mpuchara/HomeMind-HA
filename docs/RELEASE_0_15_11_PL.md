# Adaptive AI 0.15.11 — test Pi

Baza: 0.15.10 (589e0b215a83f664209be5da2cd2a9b8518b014e). Log z 14.11: watchdog zatrzymał przygotowanie po 600 sekundach bez zmiany ostatniego kursora history:weather.dom. Kod 0.15.10 nie raportował odrębnych faz budowy indeksów i quick_check, więc watchdog mógł przerwać poprawnie pracującą bazę po zakończeniu kopiowania. Nie ma jeszcze dowodu, że właśnie ta operacja trwała na fizycznym Pi.

- Finalizacja ma własne nazwy operacji oraz mierzalny licznik wykonanych kroków SQLite; zwykłe kopiowanie nadal wymaga postępu klucza. Limit 600 s pozostaje.
- Indeksy są zatwierdzane osobno; quick_check, spójność prefixu, Correct i atomowa publikacja pozostają.
- Bufor stron SQLite wykorzystuje limit RAM workera, nie limit odzyskiwania uszkodzonych rekordów.
- Naprawiono niezgodną wersję statycznego index.html z 0.15.10 (6 błędów CI).
- Nie skracamy zakresu historii, nie zmieniamy polityk ani danych użytkownika. Przed testem backup dodatku. Brak walidacji na fizycznym Pi.
