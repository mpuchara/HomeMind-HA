# Pomiar na Raspberry Pi 4

Celów nie potwierdzono na urządzeniu. Lokalny [JSON](BENCHMARK_LOCAL.json) opisuje Windows/Python i test z mockiem.

## Powtarzalne sprawdzenie kodu

W katalogu źródeł, na Pythonie 3.11 lub nowszym:

~~~sh
python -m unittest discover -s tests -v
python tools/simulate_anticipation.py
python tools/benchmark.py
~~~

Symulator i benchmark nie wysyłają usług do prawdziwego HA. Benchmark raportuje p95/średnią inferencji binarnej, gęstej polityki 31 poziomów i zdarzenie→intencja Shadow. Pomija transport WebSocket, debounce, realną bazę SQLite i odpowiedź sprzętu. pi4_thresholds_measured=false oznacza brak pełnego pomiaru docelowej instalacji, także gdy uruchomisz sam mikropomiar na Pi.

## Pełna instalacja

1. Zbuduj aplikację na Pi i załaduj rzeczywistą konfigurację, np. około 500 encji i 100 agentów. Zapisz model Pi, RAM, wersję HA, liczbę agentów/encje i wymiary cech.
2. Po 5 minutach rozgrzewki obserwuj 10 minut działania Shadow bez treningu. Zbieraj /api/status przez Ingress lub wewnątrz kontenera na http://127.0.0.1:8099/api/status. Port aplikacji nie musi być wystawiony na hosta.
3. Zapisz telemetry: RSS, cpu_recent, czasy/p95 inferencji i event→intent oraz heavy_job. Kolejki czasów zachowują do 512 ostatnich próbek; zbieraj kolejne snapshoty zamiast uznać ostatnią próbkę za cały eksperyment.
4. Wygeneruj lub obserwuj co najmniej 1000 zmian sensorów. Oddziel czas aplikacji od czasu dostarczenia zdarzenia przez HA i od fizycznego ACK.
5. Uruchom jeden Train, a osobno bootstrap. Próbkuj RSS co sekundę, zapisz szczyt, liczbę rekordów, czas i rows/s. Sprawdź, że drugie ciężkie zadanie nie startuje, anulowanie kończy bootstrap i restart nie uruchamia replay.
6. Dopiero po ocenie Shadow wykonaj próbę Control na wybranym urządzeniu, obserwując intencję, wywołanie i fizyczny stan.

| Metryka | Cel |
| --- | --- |
| RAM bez treningu | poniżej 150 MiB |
| RAM treningu | preferowane poniżej 400 MiB, limit 500 MiB |
| CPU idle | poniżej 5% |
| inferencja p95 | poniżej 10 ms |
| event→intent | preferowane poniżej 25 ms, limit 50 ms |

Domyślny debounce wynosi 25 ms, więc cały tor może przekroczyć cel preferowany. Raportuj konfigurację i rzeczywistą dystrybucję, bez odejmowania opóźnień. Na Windows pomiar RSS oparty o /proc zwraca null; nie oznacza to zużycia 0 MiB.
