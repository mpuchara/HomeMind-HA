# HomeMind 0.14.239 TEST — Recorder HTTP 500

Paczka diagnostyczna na świeżą instalację. Rozpakuj ZIP dodatku do osobnego katalogu pod `/addons/`, z plikiem `config.yaml` w katalogu dodatku. Odśwież sklep i zainstaluj lokalny dodatek w wersji 0.14.239.

Uruchom polecenie z terminala mającego już dostęp do Dockera na hoście HA. Użyj rzeczywistej nazwy kontenera HomeMind z `docker ps`. Zastąp `sensor.przyklad` inną encją, której historię z badanego przedziału potwierdzasz w HA. Nie uruchamiaj równolegle discovery.

```sh
docker exec -it NAZWA_KONTENERA python /app/recorder_diagnostics.py sensor.przyklad --direct-core
```

Opcja `--direct-core` prosi o token HA w ukrytym monicie i używa go tylko dla E na wewnętrznej ścieżce `http://homeassistant:8123/api`. Użyj tokena z uprawnieniami do odczytu badanej encji. Przy braku tokena lub tej ścieżki uruchom bez opcji lub zachowaj wynik błędu transportu dla E. Próby A–D używają tokena Supervisora obecnego w kontenerze. Narzędzie nie ponawia zapytań, nie przekierowuje żądań i nie zapisuje odpowiedzi, tokenów ani danych Recorder. Kod HTTP pochodzi z odpowiedzi, a `records` podaje tylko liczbę zwróconych rekordów. Puste HTTP 200 oznacza, że serwer odpowiedział poprawnie, ale zwrócił zero rekordów.

| Test | Encja | Parametry | Przedział | Ścieżka |
|---|---|---|---|---|
| A | Milight | filtr encji i koniec okna | 05.11.2026, 05:53:43.619576–06:16:13.619577 UTC | Supervisor |
| B | Milight | cztery flagi HomeMind | jak A | Supervisor |
| C | wskazana encja | jak B | jak A | Supervisor |
| D | Milight | jak B | ostatnia godzina UTC | Supervisor |
| E | Milight | jak B | jak A | Core bezpośrednio |

Przekaż pięć wierszy JSON. Jeśli pojawi się HTTP 500, dołącz pełny traceback Home Assistant Core z czasu `request_started_utc` i okolicy, z klasą i komunikatem wyjątku oraz ewentualnym wyjątkiem SQL. Jeśli Core nie loguje błędu, dołącz wpisy Supervisora z tego samego czasu. Dla pierwotnego błędu z 0.14.237 szukaj wpisów z 10.11.2026 około 12:54:37–12:54:42 CET (11:54:37–11:54:42 UTC). Zanonimizuj sekrety i dane prywatne przed udostępnieniem logów.

Jeżeli A daje 200, a B 500, porównanie wskazuje na zestaw flag i wymaga izolacji pojedynczych różnic. Jeżeli B daje 500, a C 200 z rekordami, problem zależy od encji lub jej danych. Jeżeli B daje 500, a D 200, sprawdź różnicę między oknami. Jeżeli B daje 500, a E 200, sprawdź Supervisor i różnice uprawnień. HTTP 401/403 w E wskazuje na problem z dostępem i nie rozstrzyga pochodzenia 500. Jeżeli B i E dają 500, najważniejszym dowodem jest traceback Core. Log 0.14.237 pokazuje już 500 z kompletem flag, w tym `skip_initial_state`. Przyczyna zgłoszenia pozostaje otwarta.
