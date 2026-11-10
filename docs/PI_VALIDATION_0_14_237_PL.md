# HomeMind 0.14.237 na Raspberry Pi 5

Wersja diagnostyczna do świeżej instalacji testowej. Rozpakuj `HomeMind-HA-0.14.237-addon.zip` do osobnego katalogu pod `/addons/`, tak aby `config.yaml` leżał w katalogu dodatku. Odśwież sklep, zainstaluj lokalny dodatek Adaptive AI i sprawdź wersję przed uruchomieniem.

## Porównanie kodu

| Wersja lub commit | Żądanie i plan discovery |
| --- | --- |
| 0.8.0 `8b3fdbc3`, ostatnia baza przed współdzielonym stanem domu | GET `/api/history/period/<start>`, encje rozdzielone przecinkami zakodowanymi jako `%2C`, `end_time`, lokalna data ISO 8601 z offsetem i sekundami. Pełna historia bez flag. Skrócona z `minimal_response=`, `no_attributes=`, `significant_changes_only=`. Domyślnie jeden worker. Świeży start zaczynał od pełnej historii do 12 encji przez 6 h, potem pobierał kontekst i starsze dane. |
| 0.9.0 `2a17e888` i 0.9.2 `a178071c` | Wydzielenie klienta z `main.py` do `ha.py` wraz z wprowadzeniem współdzielonego stanu. Dla identycznych danych wejściowych klient generuje identyczny cel żądania jak 0.8.0. |
| `deb5dbc7`, `91c73433` | Pierwszy commit dodaje skan starszej historii przed najnowszym pełnym odczytem. Drugi wydłuża domyślne okno do 14 dni. |
| 0.14.232 przed i po `ff44e24b` | Ten commit jako pierwszy na tej ścieżce wysyła `significant_changes_only=0` dla pełnej historii. HA interpretuje brak flagi jako filtrowanie zmian znaczących, a `0` wyłącza ten filtr. To różnica w zakresie odczytu, zgodna z kodem HA Core. |
| 0.14.236 `bc798628` | Ten sam REST, kodowanie encji i parametry skróconej odpowiedzi. Pełna odpowiedź nadal używa `=0`. Discovery wysyła daty UTC, czasem z mikrosekundami, oraz `skip_initial_state=` po prawej stronie podziału czasowego. Żądania seryjne. Starsza historia obejmuje do 32 encji × 24 h w trybie skróconym lub do 8 × 24 h dla celów zależnych od atrybutów. Najnowsza pełna historia obejmuje do 8 × 3 h. |

Bazę 0.8.0 wskazuje historia Git. Brakuje zarejestrowanej odpowiedzi produkcyjnej HTTP 200 dla konkretnego commita i odpowiedzi dla tego samego żądania z 0.14.236. Porównanie wykrywa zmianę żądania, lecz nie przypisuje jej zgłoszonego HTTP 500. Powiązanie z Core lub Supervisorem wymaga logów tej instalacji.

## Zbieranie danych

1. W Home Assistant otwórz **Ustawienia → Dodatki → Adaptive AI → Dziennik**. Wiersz `[recorder] history_http_error` jest w dzienniku kontenera dodatku. Karta zdarzeń w interfejsie HomeMind pokazuje tylko podsumowanie `manual_discovery_error`. Zapisz ostatni wiersz HTTP 500 z najmniejszego zapytania.
2. `failed_at_utc` wskazuje czas wystąpienia błędu. Szukaj wtedy w **Ustawienia → System → Dzienniki → Home Assistant Core** pełnego tracebacku z `homeassistant/components/history/__init__.py`, `HistoryPeriodView.get` lub `_sorted_significant_states_json`. Zachowaj końcową klasę i komunikat wyjątku, w tym wewnętrzny wyjątek Recorder lub sterownika SQL, jeśli występuje. Przy braku pasującego tracebacku sprawdź log Supervisora w tej samej sekundzie.
3. `api_target` zawiera dokładną zakodowaną ścieżkę REST z parametrami. Przy `via_supervisor: true` Supervisor otrzymuje tę ścieżkę z prefiksem `/core`. `legacy_significance_api_target` to ten sam odczyt z pominiętym `significant_changes_only=0`. Wartość `null` oznacza brak takiego wariantu.
4. Jeśli porównujesz odpowiedzi ręcznie, użyj tego samego adresu, tej samej autoryzacji i tych samych granic czasu. W terminalu z dostępem do HA można wykonać pojedyncze odczyty A/B poniżej. `origin` to adres HA bez końcowego ukośnika i bez `/api`. Porównanie bezpośrednio z Core pomija proxy Supervisora, więc zanotuj tę różnicę, jeśli `via_supervisor` było `true`.

```bash
read -r -p 'Adres HA (np. http://homeassistant.local:8123): ' origin
read -r -p 'api_target: ' current
read -r -p 'legacy_significance_api_target: ' legacy
read -r -s -p 'Token HA: ' token; printf '\n'
for target in "$current" "$legacy"; do
  if [[ "$target" == /api/history/period/* ]]; then
    printf 'header = "Authorization: Bearer %s"\n' "$token" |
      curl --config - --silent --show-error --max-time 20 --output /dev/null \
        --write-out 'HTTP %{http_code} · %{time_total} s · %{size_download} B\n' \
        "${origin}${target}"
  fi
done
unset token
```

Jeśli aktualny wariant daje 500, a kontrolny 200, różnica wiąże się z filtrem znaczących zmian dla tego żądania. Jeśli oba zwracają 500, szukaj wspólnej przyczyny w tracebacku. Przed udostępnieniem logów usuń prywatne identyfikatory i sekrety. HomeMind zapisuje wariant kontrolny w logu bez automatycznego wysyłania dodatkowego żądania.

Kod referencyjny HA: [HistoryPeriodView w Core 2026.1.2](https://github.com/home-assistant/core/blob/2026.1.2/homeassistant/components/history/__init__.py#L49-L155).
