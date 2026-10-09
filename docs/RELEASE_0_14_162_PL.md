# HomeMind Adaptive AI 0.14.162

## Indeksowane czyszczenie dziennika

FeatureJournal utrzymuje twardy globalny limit zdarzeń. Dotychczas wybór nadmiarowych rekordów używał ORDER BY CASE dla całej tabeli, nawet gdy ponad limit wpadało tylko 128 obserwacji. Nowa implementacja korzysta z indeksu pokrywającego received_time, protected_until i event_key. Wybiera do wymaganego limitu najstarsze niechronione zdarzenia, a następnie, tylko przy konieczności spełnienia twardego limitu, najstarsze chronione. Nie odczytuje payloadu każdej obserwacji, by zbudować pełne sortowanie. Przy równych received_time jawne rowid zachowuje kolejność wstawienia z poprzedniego skanu tabeli. Chronione okna, retencja, granice porównań czasu, per-entity limit i twardy globalny limit pozostają zgodne.

Dodany jest także indeks protected_until dla wygasających feature_windows. Oba indeksy powstają idempotentnie przy starcie na istniejącej bazie. Nie zmieniają wartości zapisanych zdarzeń ani modeli. Całe prune nadal jest jedną transakcją; błąd końcowego DELETE przywraca także wcześniejsze usunięcia wygasłych okien. Nie dzielimy retencji na częściowe commity.

Background state observed pool wcześniej wykonywał COUNT, a następnie odczyt ID, którego wynik zastępował poprzedni. Teraz wykonuje tylko końcowy odczyt ID i sumuje unikalne klucze trwałe oraz RAM. Synchronous fallback zachowuje jeden COUNT. Nie dodajemy cache wyników zapytań; zmiany trwałych danych są nadal widoczne.

## Nowy log

Log adaptive-ai-runtime-debug-20261009-204915.json z 0.14.161: recent CPU 62,389%, RSS 128,035 MiB; recent event → decision p95 229,806 ms. Obserwacja puli p95 24,882 ms wobec 268,003 ms w poprzednim oknie, a pusta kolejka Correct została sprawdzona raz w zachowanym oknie około 24 sekund, wobec 139 odczytów w około 40 sekund poprzedniego logu. To potwierdza mniejszy koszt zmienionych ścieżek; różne obciążenie nie pozwala przypisać wszystkich zmian CPU/latencji tylko wydaniu.

SQLite prune trwał 1667,856 ms, z czego body 1633,462 ms, i nakładał się czasowo na przebieg targetu 2881,248 ms. Prune trzyma Store.lock i transakcję zapisu. Zbieżność czasu nie dowodzi, że jest jedyną przyczyną opóźnienia. Długie przebiegi Shadow obejmują również scoring, uczenie i serializację kandydatów. W logu nie ma błędów bazy, kolejek ani workera. Checkpoint działa w tle; nie zmieniamy jego trwałości ani zasad PASSIVE.

## Walidacja

10 nowych regresji porównuje wszystkie kolumny zachowanych zdarzeń, okien i powiązań z oryginalną implementacją, dla 20 losowych zestawów retencji, ochrony, timestampów i limitów. Osobne przypadki obejmują same chronione zdarzenia, dokładne granice czasu ochrony, równe timestampy, limity encji i brak nadmiaru. Sprawdzamy rollback przy błędzie końcowego usuwania oraz idempotentną migrację bez zmiany modelu lub obserwacji. EXPLAIN potwierdza indeks pokrywający i brak globalnego sortowania. Pool count sprawdza deduplikację trwałych i pending kluczy, świeżość po zmianach bazy oraz jeden odczyt na przebieg. Pełny zestaw: 1921 testów.

Benchmark tools/benchmark_feature_prune.py wykonuje starą i nową implementację w tym samym procesie, z keeperem WAL jak w runtime. Na 50 128 zdarzeniach usuwa 128 i sprawdza identyczne klucze wszystkich zachowanych obserwacji. Windows: prune 43,9–45,6 ms → 7,35–8,32 ms, około 5,5–6×. Zapis batch 128 obserwacji wraz z prune: 59,4–61,0 ms → 22,6–30,5 ms, około 2–2,7×, także przy samych chronionych danych. Drugi pomiar obejmuje koszt aktualizacji dodatkowych indeksów. Budowanie indeksu przy pierwszym starcie jest poza pomiarem steady-state. To pomiar komponentu; wpływ na CPU i opóźnienie całego HA wymaga nowego porównywalnego logu. Benchmark jest też uruchamiany w CI.

## Instalacja

Wydanie zawiera HomeMind-Adaptive-AI-0.14.162-addon-root.zip i repository.zip oraz SHA256. Aktualizacja i restart, bez Rebuild. Cel i wagi treningu, krótkie wizyty jako etykiety, predict → paired score → learn, pełna weryfikacja SHA256 i kwalifikacja kontrolowania urządzeń pozostają zgodne.
