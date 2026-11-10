# HomeMind 0.14.236 na Raspberry Pi 5

Wersja diagnostyczna do świeżej instalacji testowej. Źródło odpowiedzi HTTP 500 pozostaje nieustalone.

Rozpakuj paczkę `HomeMind-HA-0.14.236-addon.zip` bezpośrednio do osobnego katalogu dodatku pod `/addons/` (np. `/addons/homemind_014236/`). Plik `config.yaml` ma leżeć w tym katalogu. Odśwież sklep dodatków Home Assistant, zainstaluj lokalny dodatek Adaptive AI, sprawdź wersję 0.14.236 i uruchom. Instalacja lokalna buduje obraz na HA. Wariant `repository.zip` zawiera całe repozytorium instalacyjne.

## Zbieranie logów

1. W logu dodatku znajdź wiersz `[recorder] history_http_error`. Każdy wiersz opisuje jedno żądanie zakończone statusem HTTP, w tym identyfikatory encji, czas wykonania, zakodowany rozmiar ścieżki z parametrami (w bajtach, bez nagłówków) i zakres historii w UTC. `response_error_excerpt` zawiera znany komunikat serwera lub `null`. Daty w polach `start_time` i `end_time` opisują historię, której dotyczy zapytanie. Do korelacji użyj czasu **wpisu w logu**.
2. W tej samej sekundzie sprawdź **Ustawienia → System → Dzienniki → Home Assistant Core**. Skopiuj traceback obsługi REST History zawierający `HistoryPeriodView.get` lub `_sorted_significant_states_json` i ostatni wiersz z klasą oraz komunikatem wyjątku (np. wyjątek Recorder lub sterownika SQL, jeśli to on wystąpił). Jeśli brak pasującego błędu w Core, sprawdź także dziennik Supervisora dla tej samej chwili. Przed udostępnieniem usuń sekrety i prywatne identyfikatory.
3. Udany podział zapisuje pobrane fragmenty. Trwały HTTP 500 przy najmniejszym fragmencie lub wyczerpanie limitu zatrzymuje automatyczne skanowanie w bieżącym procesie dodatku. Po odczytaniu logów wybierz **Rescan**, aby ponowić skan.

HomeMind rozdziela HTTP 500 seryjnie, najpierw według encji, następnie według czasu. Podział czasu kończy się przy oknie do 30 minut. Jedno drzewo podziału ma limit 32 odpowiedzi HTTP 500 i ograniczoną głębokość. Fragmenty, których pobranie się nie powiodło, nie oznaczają ukończonego discovery.
