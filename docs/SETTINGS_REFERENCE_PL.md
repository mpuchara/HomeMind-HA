# Ustawienia i parametry

## Ustawienia pojedynczego agenta

| Pole | Znaczenie |
|---|---|
| Minimum nastawy | najniższa wartość, jaką agent może rozważać |
| Maksimum nastawy | najwyższa wartość |
| Tolerancja / deadband | różnica uznawana za nieistotną |
| Wymagana pewność | minimalne Live confidence do sterowania w Control |
| Minimalny odstęp poleceń | cooldown pomiędzy komendami |
| Limit potwierdzenia | ile czekać na echo/stan potwierdzający komendę; `0` = profil |
| Czas stabilizacji | jak długo urządzenie może stabilizować się po komendzie; `0` = profil |
| Pierwszeństwo ręcznej nastawy | okres priorytetu ręcznej zmiany; `0` = profil |
| Encje kontekstu | `*` = automatyczny dobór; można podać konkretne entity_id |

Zmiana minimum, maksimum lub listy kontekstu przebudowuje model agenta.

## Najważniejsze ustawienia globalne 0.7.12

Wartości są w `adaptive_ai/config.yaml`. Domyślne ustawienia są dobrane jako bezpieczny punkt startowy; nie ma potrzeby zmieniać ich przy pierwszym użyciu.

| Parametr | Domyślnie | Znaczenie |
|---|---:|---|
| `realtime_inference_debounce_ms` | 25 ms | opóźnienie grupujące bardzo bliskie zdarzenia realtime |
| `prediction_horizons_seconds` | `1` | reaktywny head polityki |
| `feature_dimensions` | 128 | wymiar reprezentacji |
| `max_context_entities` | 28 | limit zwykłego kontekstu |
| `fast_series_lags_seconds` | `1,3,10` | krótkie opóźnienia dla fast agents |
| `fast_max_context_entities` | 8 | maks. liczba wejść szybkiego światła/switcha |
| `candidate_benchmark_threshold` | 0.78 | próg kwalifikacji; kod wymaga wyniku większego od progu |
| `candidate_benchmark_min_samples` | 12 | minimalna liczba próbek benchmarku |
| `agent_training_chunk_hours` | 48 h | wielkość checkpointowanego fragmentu historii |
| `agent_training_overlap_hours` | 12 h | overlap pomiędzy fragmentami |
| `min_historical_support` | 0.20 | minimalne historyczne wsparcie dla Control |
| `max_context_novelty` | 0.85 | maksymalna novelty dla normalnego Control |
| `confidence_validation_fraction` | 0.20 | część danych do kalibracji/held-out |
| `history_bootstrap_days` | 10 dni | początkowy zakres pobrania Recorder |
| `archive_retention_days` | 365 dni | retencja lokalnego archiwum Adaptive AI |
| `auto_agent_min_changes` | 2 | minimalna aktywność do auto-discovery |
| `max_auto_agents` | 250 | limit automatycznie tworzonych agentów |
| `automation_scan_enabled` | true | skanowanie automatyzacji HA |
| `history_maintenance_minutes` | 30 min | okresowy cykl utrzymania historii |
| `process_nice` | 10 | obniżony priorytet procesu w systemie |

## Context `*` a ręczna lista

### Automatyczny `*`

Zalecany domyślnie. Agent może sprawdzić cały dopuszczony universe i wybrać to, co historycznie najlepiej działa.

### Lista ręczna

Przykład:

```text
binary_sensor.kitchen_presence, sensor.kitchen_illuminance
```

Użyteczna do eksperymentów i diagnostyki, ale ogranicza możliwość znalezienia niespodziewanej zależności, np. telefonu, osoby albo pogody.

Po zmianie listy wykonaj pełne ponowne uczenie danego agenta.

## Czego zwykle nie zmieniać na początku

- `rl_alpha`,
- `feature_dimensions`,
- timingi historyczne,
- validation fraction,
- support/novelty thresholds.

Najpierw oceń jakość `Selected context` i benchmark. Złe wejścia częściej są przyczyną problemu niż sam parametr algorytmu RL.
