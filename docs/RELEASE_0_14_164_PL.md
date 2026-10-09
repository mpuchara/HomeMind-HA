# HomeMind 0.14.164

Obsługa ręcznej zmiany w HA pobiera teraz konfiguracje agentów wyłącznie dla zmienionej encji. Nie dekoduje raportów benchmarku i wejść wszystkich pozostałych urządzeń. Odczyt pozostaje świeżym zapytaniem SQLite.

## Co pokazał log

adaptive-ai-runtime-debug-20261009-225528.json z 0.14.163: CPU recent 45,07%, RSS 101,85 MiB, event→decision recent p95 196,09 ms (20 próbek). Brak błędów SQLite, restartów workerów i overflow kolejek; checkpoint remaining_frames=0. Wartości z różnych okien nie dowodzą, że cała zmiana CPU wynika z poprzedniego wydania.

W zachowanych 56,34 s wątek adaptive-ai-ha-events wykonał 266 pełnych list_agent_configs. Średnio 11,82 ms przygotowania połączenia, 10,47 ms zapytania/dekodowania i 1,99 ms zamknięcia. Most manual_feedback_lifecycle wyliczał wszystkich agentów po rozpoznaniu explicit_user, chociaż potrzebował tylko agentów danej encji. Również aktualizacja sensora dziedzicząca taki kontekst uruchamiała ten odczyt.

## Zmiana

Store.list_agent_configs_for_target wykonuje parametryzowane SELECT z target_entity i ORDER BY created_at. Indeks idx_agents_target_created powstaje idempotentnie również przy aktualizacji istniejącej bazy. JSON jest dekodowany po zamknięciu zakresu odczytu. Zapytanie obejmuje wszystkie właściwości urządzenia i pozostawia pełną enumerację dla pozostałych wywołań.

Filtr Candidate jest identyczny z filtrem list_agent_configs, włącznie z jawnym zakresem procesu treningowego. Nie używamy cache konfiguracji ani opóźnionego odświeżania: każda ręczna korekta widzi aktualny stan zapisany także przez drugi proces. Zachowano warunki explicit_user/parent_id, enabled, training/qualified, skończonych i różnych wartości oraz wykluczenie własnej komendy. Korekty pozostają uczeniem Candidate, bez zmiany wag Live.

Nowa metryka manual_lifecycle_lookup w telemetry oraz ślad RAM z target_entity i agent_count pozwolą sprawdzić rzeczywisty koszt pozostałych odczytów.

## Sprawdzenie

13 regresji obejmuje porównanie rzeczywistego mostu korekt z poprzednią pełną enumeracją, wszystkie stany cyklu treningowego, niezwiązane sensory, własne echo, wiele właściwości urządzenia, ukrywanie Candidate i przywracanie zakresu po wyjątku, zmiany konfiguracji i członkostwa z niezależnego połączenia oraz migrację indeksu. Pełny zestaw: 1954 testy.

tools/benchmark_target_configs.py porównuje w trzech naprzemiennych powtórzeniach świeży pełny odczyt i zapytanie indeksowane. Uwzględnia otwarcie/zamknięcie połączenia z aktywnym WAL keeperem; każda konfiguracja jest porównywana, a liczby dekodowanych wierszy sprawdzane.

Windows, syntetyczne 9 agentów z raportami benchmarku: mediana powtórzeń dla encji z dwoma agentami 3,477 → 1,286 ms (około 2,7×), dla niezwiązanej encji 3,515 → 0,558 ms (około 6,3×). Dekodowanych wierszy odpowiednio 9→2 i 9→0. Testuje także 64 agentów. Benchmark wykonuje się w CI. To wynik tego komponentu; nie prognoza CPU lub całej latencji HA.

Log nie zawiera próbek treningu/fit, więc nie zmieniamy wag ani zasad uczenia na podstawie tego pomiaru.

## Budowanie w CI

Docker Hub wielokrotnie zwrócił HTTP 429 podczas pobierania bazowego python:3.13-alpine. Runner próbuje najpierw Docker Hub, a po niepowodzeniu pobiera tę linię obrazu z public.ecr.aws/docker/library/python i oznacza ją lokalnie nazwą oczekiwaną przez Dockerfile. Budowanie, smoke test i test pakowanego trenera pozostają obowiązkowe. Domyślne źródło w Dockerfile nie zmienia się.

## Instalacja

HomeMind-Adaptive-AI-0.14.164-addon-root.zip lub repository.zip, z SHA256. Aktualizacja i restart, bez Rebuild.
